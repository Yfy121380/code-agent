"""模型后端适配层。

runtime 只关心一件事：给模型一组 messages 和可用 tools，拿回结构化决策。
不同 provider 在 HTTP 接口、工具 schema、响应结构、usage 字段上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

OPENAI_COMPATIBLE_USER_AGENT = "codemate/0.1"


@dataclass
class ModelToolCall:
    id: str
    name: str
    args: dict = field(default_factory=dict)

    @classmethod
    def create(cls, name, args=None, call_id=None):
        return cls(
            id=str(call_id or f"call_{uuid.uuid4().hex[:12]}"),
            name=str(name),
            args=dict(args or {}),
        )

    def to_dict(self):
        return {"id": self.id, "name": self.name, "args": dict(self.args or {})}


@dataclass
class ModelResponse:
    kind: str
    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    raw: dict | None = None

    @classmethod
    def final(cls, text, metadata=None, raw=None):
        return cls(kind="final", text=str(text or ""), metadata=dict(metadata or {}), raw=raw)

    @classmethod
    def tool_call(cls, name, args=None, call_id=None, text="", metadata=None, raw=None):
        return cls.from_tool_calls(
            [ModelToolCall.create(name, args=args, call_id=call_id)],
            text=text,
            metadata=metadata,
            raw=raw,
        )

    @classmethod
    def from_tool_calls(cls, calls, text="", metadata=None, raw=None):
        normalized = []
        for call in calls or []:
            if isinstance(call, ModelToolCall):
                normalized.append(call)
                continue
            if isinstance(call, dict):
                normalized.append(
                    ModelToolCall.create(
                        call.get("name", ""),
                        args=call.get("args", call.get("input", {})) or {},
                        call_id=call.get("id") or call.get("call_id"),
                    )
                )
        return cls(kind="tool_calls", text=str(text or ""), tool_calls=normalized, metadata=dict(metadata or {}), raw=raw)


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _normalize_messages(messages, system=None):
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    normalized = []
    if system:
        normalized.append({"role": "system", "content": str(system)})
    for message in messages or []:
        item = dict(message or {})
        role = str(item.get("role", "user"))
        normalized.append({**item, "role": role})
    return normalized


def _messages_to_text(messages, system=None):
    lines = []
    if system:
        lines.append(str(system))
    for message in messages or []:
        role = str(message.get("role", ""))
        if role == "assistant" and message.get("tool_calls"):
            lines.append(f"[assistant tool_calls] {json.dumps(message.get('tool_calls'), ensure_ascii=False, sort_keys=True)}")
            continue
        if role == "tool":
            lines.append(f"[tool:{message.get('name', '')}] {message.get('content', '')}")
            continue
        lines.append(f"[{role}] {message.get('content', '')}")
    return "\n".join(lines).strip()


def _as_model_response(output):
    if isinstance(output, ModelResponse):
        return output
    if isinstance(output, ModelToolCall):
        return ModelResponse.from_tool_calls([output])
    if isinstance(output, dict):
        if output.get("kind") == "tool_calls":
            return ModelResponse.from_tool_calls(output.get("tool_calls", []), text=output.get("text", ""), raw=output)
        if output.get("kind") == "final":
            return ModelResponse.final(output.get("text", ""), raw=output)
        if output.get("name"):
            return ModelResponse.tool_call(output.get("name"), output.get("args", {}), call_id=output.get("id"), raw=output)
    return ModelResponse.final(str(output or ""))


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.supports_tools = True
        self.last_completion_metadata = {}

    def complete(self, messages, max_new_tokens, tools=None, system=None, **kwargs):
        del max_new_tokens, tools, kwargs
        self.prompts.append(_messages_to_text(_normalize_messages(messages), system=system))
        self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        response = _as_model_response(self.outputs.pop(0))
        self.last_completion_metadata = dict(response.metadata or {})
        return response


def _tool_specs_to_openai(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _tool_specs_to_openai_chat(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def _tool_specs_to_anthropic(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "input_schema": spec.get("input_schema", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _tool_specs_to_ollama(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def _json_args(value):
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        if item.get("type") in {"function_call", "tool_call"}:
            continue
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_tool_calls(data):
    calls = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"function_call", "tool_call"}:
            continue
        name = item.get("name") or item.get("function", {}).get("name")
        if not name:
            continue
        arguments = item.get("arguments")
        if arguments is None and isinstance(item.get("function"), dict):
            arguments = item["function"].get("arguments")
        calls.append(
            ModelToolCall.create(
                name,
                args=_json_args(arguments),
                call_id=item.get("call_id") or item.get("id"),
            )
        )

    for choice in data.get("choices", []) or []:
        message = choice.get("message", {}) or {}
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) or {}
            name = function.get("name") or call.get("name")
            if not name:
                continue
            calls.append(
                ModelToolCall.create(
                    name,
                    args=_json_args(function.get("arguments", call.get("arguments"))),
                    call_id=call.get("id"),
                )
            )
    return calls


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                return _extract_openai_text(response), response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    if deltas:
        return "".join(deltas), last_response or {}
    return "", {}


def _extract_usage_cache_details(data):
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _to_openai_input(messages, system=None):
    items = []
    if system:
        items.append({"role": "system", "content": [{"type": "input_text", "text": str(system)}]})
    for message in messages or []:
        role = message.get("role", "user")
        if role in {"user", "system"}:
            items.append({"role": role, "content": [{"type": "input_text", "text": str(message.get("content", ""))}]})
        elif role == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": call.get("name"),
                        "arguments": json.dumps(call.get("args", {}) or {}, ensure_ascii=False),
                    }
                )
        elif role == "assistant":
            items.append({"role": "assistant", "content": [{"type": "output_text", "text": str(message.get("content", ""))}]})
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": str(message.get("content", "")),
                }
            )
    return items


def _to_openai_chat_messages(messages, system=None):
    converted = []
    if system:
        converted.append({"role": "system", "content": str(system)})
    for message in messages or []:
        role = message.get("role", "user")
        if role in {"user", "system"}:
            converted.append({"role": role, "content": str(message.get("content", ""))})
        elif role == "assistant" and message.get("tool_calls"):
            converted.append(
                {
                    "role": "assistant",
                    "content": str(message.get("content", "")) or None,
                    "tool_calls": [
                        {
                            "id": call.get("id"),
                            "type": "function",
                            "function": {
                                "name": call.get("name"),
                                "arguments": json.dumps(call.get("args", {}) or {}, ensure_ascii=False),
                            },
                        }
                        for call in message.get("tool_calls") or []
                    ],
                }
            )
        elif role == "assistant":
            converted.append({"role": "assistant", "content": str(message.get("content", ""))})
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": str(message.get("content", "")),
                }
            )
    return converted


def _to_anthropic_messages(messages):
    converted = []
    for message in messages or []:
        role = message.get("role", "user")
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": str(message.get("content", "")),
                        }
                    ],
                }
            )
        elif role == "assistant" and message.get("tool_calls"):
            converted.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": call.get("name"),
                            "input": call.get("args", {}) or {},
                        }
                        for call in message.get("tool_calls") or []
                    ],
                }
            )
        else:
            converted.append({"role": role, "content": [{"type": "text", "text": str(message.get("content", ""))}]})
    return converted


def _to_ollama_messages(messages, system=None):
    converted = []
    if system:
        converted.append({"role": "system", "content": str(system)})
    for message in messages or []:
        role = message.get("role", "user")
        if role == "assistant" and message.get("tool_calls"):
            converted.append(
                {
                    "role": "assistant",
                    "content": str(message.get("content", "")),
                    "tool_calls": [
                        {
                            "function": {
                                "name": call.get("name"),
                                "arguments": call.get("args", {}) or {},
                            }
                        }
                        for call in message.get("tool_calls") or []
                    ],
                }
            )
        elif role == "tool":
            converted.append({"role": "tool", "content": str(message.get("content", ""))})
        else:
            converted.append({"role": role, "content": str(message.get("content", ""))})
    return converted


def _extract_anthropic_response(data):
    text_parts = []
    calls = []
    for item in data.get("content", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif item.get("type") == "tool_use":
            calls.append(
                ModelToolCall.create(
                    item.get("name", ""),
                    args=item.get("input", {}) or {},
                    call_id=item.get("id"),
                )
            )
    text = "".join(text_parts)
    if calls:
        return ModelResponse.from_tool_calls(calls, text=text, raw=data)
    return ModelResponse.final(text, raw=data)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_tools = True
        self.last_completion_metadata = {}

    def complete(self, messages, max_new_tokens, tools=None, system=None, **kwargs):
        del kwargs
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": _to_ollama_messages(_normalize_messages(messages), system=system),
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        if tools:
            payload["tools"] = _tool_specs_to_ollama(tools)
        request = urllib.request.Request(
            self.host + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        message = data.get("message", {}) or {}
        calls = []
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) or {}
            name = function.get("name") or call.get("name")
            if not name:
                continue
            calls.append(ModelToolCall.create(name, args=_json_args(function.get("arguments", {})), call_id=call.get("id")))
        if calls:
            return ModelResponse.from_tool_calls(calls, text=message.get("content", ""), raw=data)
        return ModelResponse.final(message.get("content", ""), raw=data)


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.supports_tools = True
        self.last_completion_metadata = {}

    def _complete_chat_completions(self, messages, max_new_tokens, tools=None, system=None):
        payload = {
            "model": self.model,
            "messages": _to_openai_chat_messages(_normalize_messages(messages), system=system),
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = _tool_specs_to_openai_chat(tools)
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI-compatible chat fallback failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "Could not reach the OpenAI-compatible chat fallback.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible chat fallback error: {data['error']}")
        metadata = {
            "prompt_cache_supported": False,
            "prompt_cache_key": None,
            "prompt_cache_retention": None,
            **_extract_usage_cache_details(data),
            "openai_compat_fallback": "chat_completions",
        }
        self.last_completion_metadata = metadata
        calls = _extract_openai_tool_calls(data)
        text = _extract_openai_text(data)
        if calls:
            return ModelResponse.from_tool_calls(calls, text=text, metadata=metadata, raw=data)
        return ModelResponse.final(text, metadata=metadata, raw=data)

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": _to_openai_input(_normalize_messages(messages), system=system),
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = _tool_specs_to_openai(tools)
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if tools and exc.code >= 500:
                    return self._complete_chat_completions(
                        messages,
                        max_new_tokens,
                        tools=tools,
                        system=system,
                    )
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, data = _extract_openai_response_from_sse(body_text)
        else:
            try:
                data = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
                ) from exc
            text = _extract_openai_text(data)
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        self.last_completion_metadata = metadata
        calls = _extract_openai_tool_calls(data)
        if calls:
            return ModelResponse.from_tool_calls(calls, text=text, metadata=metadata, raw=data)
        return ModelResponse.final(text, metadata=metadata, raw=data)


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_tools = True
        self.last_completion_metadata = {}

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None):
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": _to_anthropic_messages(_normalize_messages(messages)),
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if system:
            payload["system"] = str(system)
        if tools:
            payload["tools"] = _tool_specs_to_anthropic(tools)
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        return _extract_anthropic_response(data)
