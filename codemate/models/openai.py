# OpenAI-compatible 适配器：负责 Responses/Chat Completions 请求、工具调用解析和 usage 元数据。

from __future__ import annotations

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .common import (
    _extract_usage_cache_details,
    _json_args,
    _normalize_messages,
    _normalize_versioned_base_url,
)
from .schemas import _tool_specs_to_openai, _tool_specs_to_openai_chat
from .types import ModelResponse, ModelToolCall

OPENAI_COMPATIBLE_USER_AGENT = "codemate/0.1"


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


def _openai_structured_response_format(structured_output):
    if not structured_output:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": str(structured_output.get("name") or "structured_output"),
            "schema": dict(structured_output.get("schema") or {}),
            "strict": True,
        },
    }


def _openai_responses_text_format(structured_output):
    response_format = _openai_structured_response_format(structured_output)
    if not response_format:
        return None
    json_schema = response_format["json_schema"]
    return {
        "format": {
            "type": "json_schema",
            "name": json_schema["name"],
            "schema": json_schema["schema"],
            "strict": json_schema["strict"],
        }
    }


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

    def _complete_chat_completions(self, messages, max_new_tokens, tools=None, system=None, structured_output=None):
        payload = {
            "model": self.model,
            "messages": _to_openai_chat_messages(_normalize_messages(messages), system=system),
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = _tool_specs_to_openai_chat(tools)
        response_format = _openai_structured_response_format(structured_output)
        if response_format:
            payload["response_format"] = response_format
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

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": _to_openai_input(_normalize_messages(messages), system=system),
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = _tool_specs_to_openai(tools)
        text_format = _openai_responses_text_format(structured_output)
        if text_format:
            payload["text"] = text_format
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
                        structured_output=structured_output,
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
