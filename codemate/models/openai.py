# OpenAI-compatible 适配器：负责 Responses/Chat Completions 请求、工具调用解析和 usage 元数据。

from __future__ import annotations

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .common import (
    _extract_usage_cache_details,
    _image_block_data_uri,
    _iter_sse_json,
    _json_args,
    _normalize_messages,
    _normalize_versioned_base_url,
)
from .capabilities import model_capability
from .schemas import _tool_specs_to_openai, _tool_specs_to_openai_chat
from .types import ModelResponse, ModelStreamEvent, ModelToolCall

OPENAI_COMPATIBLE_USER_AGENT = "codemate/0.1"
OPENAI_RETRY_DELAYS = (1.0, 3.0, 7.0)


def _connection_error_detail(exc):
    """Return the useful underlying reason hidden by urllib wrappers."""
    return str(getattr(exc, "reason", exc))


def _text_from_openai_content(content):
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        if item.get("type") in {"function_call", "tool_call"}:
            continue
        text = _text_from_openai_content(item.get("content", []))
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


def _extract_openai_phase_texts(data):
    # Responses API 可以把用户可见的中间进度和最终回答区分为不同 phase。
    commentary = []
    final = []
    fallback = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") in {"function_call", "tool_call"}:
            continue
        text = _text_from_openai_content(item.get("content", []))
        if not text:
            continue
        phase = str(item.get("phase", "") or "")
        if phase == "commentary":
            commentary.append(text)
        elif phase == "final_answer":
            final.append(text)
        else:
            fallback.append(text)
    if data.get("output_text") and not final and not commentary:
        fallback.append(str(data["output_text"]))
    return {
        "commentary": "\n".join(commentary).strip(),
        "final": "\n".join(final).strip(),
        "fallback": "\n".join(fallback).strip(),
    }


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


def _extract_openai_model_response(data, metadata):
    texts = _extract_openai_phase_texts(data)
    calls = _extract_openai_tool_calls(data)
    if calls:
        return ModelResponse.from_tool_calls(
            calls,
            text=texts["commentary"] or texts["fallback"] or _extract_openai_text(data),
            metadata=metadata,
            raw=data,
        )
    if texts["final"]:
        return ModelResponse.final(
            texts["final"],
            metadata=metadata,
            raw=data,
            commentary_text=texts["commentary"],
        )
    if texts["commentary"]:
        return ModelResponse.commentary(texts["commentary"], metadata=metadata, raw=data)
    return ModelResponse.final(texts["fallback"] or _extract_openai_text(data), metadata=metadata, raw=data)


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

def _openai_response_content(text, content_blocks=None, supports_images=True):
    content = []
    if text:
        content.append({"type": "input_text", "text": str(text)})
    if supports_images:
        for block in content_blocks or []:
            if block.get("type") != "image":
                continue
            _media_type, data_uri = _image_block_data_uri(block)
            content.append({"type": "input_image", "image_url": data_uri, "detail": "auto"})
    return content or [{"type": "input_text", "text": ""}]


def _openai_tool_output(message, supports_images=True):
    text = str(message.get("content", ""))
    image_content = _openai_response_content(text, message.get("content_blocks", []), supports_images=supports_images)
    if len(image_content) == 1 and image_content[0]["type"] == "input_text":
        return image_content[0]["text"]
    return image_content


def _openai_chat_image_content(text, content_blocks=None, supports_images=True):
    content = []
    if text:
        content.append({"type": "text", "text": str(text)})
    if supports_images:
        for block in content_blocks or []:
            if block.get("type") != "image":
                continue
            _media_type, data_uri = _image_block_data_uri(block)
            content.append({"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}})
    return content


def _to_openai_input(messages, system=None, supports_images=True):
    items = []
    if system:
        items.append({"role": "system", "content": [{"type": "input_text", "text": str(system)}]})
    for message in messages or []:
        role = message.get("role", "user")
        if role in {"user", "system"}:
            items.append(
                {
                    "role": role,
                    "content": _openai_response_content(
                        str(message.get("content", "")),
                        message.get("content_blocks", []),
                        supports_images=supports_images,
                    ),
                }
            )
        elif role == "assistant" and message.get("tool_calls"):
            content = str(message.get("content", "") or "")
            if content:
                items.append(
                    {
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
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
            kind = str(message.get("kind", "") or "")
            item = {"role": "assistant", "content": [{"type": "output_text", "text": str(message.get("content", ""))}]}
            if kind == "commentary":
                item["phase"] = "commentary"
            elif kind == "final":
                item["phase"] = "final_answer"
            items.append(item)
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": _openai_tool_output(message, supports_images=supports_images),
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


def _to_openai_chat_messages(messages, system=None, supports_images=True):
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
            image_content = _openai_chat_image_content(
                f"Image output for tool {message.get('name', '')} ({message.get('tool_call_id', '')}).",
                message.get("content_blocks", []),
                supports_images=supports_images,
            )
            if len(image_content) > 1:
                converted.append({"role": "user", "content": image_content})
    return converted


def _openai_stream_fallback_data(text_parts, tool_calls):
    # 正常 Responses 流会在 response.completed 中给出完整 response。
    # 这个兜底只处理连接提前结束但已经收集到可用文本或工具参数的情况。
    output = []
    text = "".join(text_parts).strip()
    if text:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for key, call in tool_calls.items():
        if not call.get("name"):
            continue
        output.append(
            {
                "type": "function_call",
                "id": call.get("id") or key,
                "call_id": call.get("call_id") or call.get("id") or key,
                "name": call.get("name"),
                "arguments": call.get("arguments", "") or "{}",
            }
        )
    return {"output": output, "output_text": text}


def _raise_openai_stream_error(event):
    error = event.get("error") if isinstance(event.get("error"), dict) else event
    message = error.get("message") or error.get("code") or "unknown streaming error"
    raise RuntimeError(f"OpenAI-compatible streaming error: {message}")


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.capabilities = model_capability(model)
        self.supports_streaming = self.capabilities.supports_streaming
        self.supports_images = self.capabilities.supports_images
        self.supports_reasoning = self.capabilities.supports_reasoning
        # Responses-compatible providers may proxy OpenAI prompt caching under custom URLs.
        # Always send the cache hint instead of guessing support from the hostname.
        self.supports_prompt_cache = True
        self.supports_tools = True
        self.last_completion_metadata = {}

    def fork(self):
        """为并发子 agent 创建独立客户端实例，避免 last_completion_metadata 互相覆盖。"""
        return type(self)(self.model, self.base_url, self.api_key, self.temperature, self.timeout)

    def _complete_chat_completions(self, messages, max_new_tokens, tools=None, system=None, structured_output=None):
        payload = {
            "model": self.model,
            "messages": _to_openai_chat_messages(_normalize_messages(messages), system=system, supports_images=self.supports_images),
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
                f"Model: {self.model}\n"
                f"Cause: {_connection_error_detail(exc)}"
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

    def _stream_chat_completions(self, messages, max_new_tokens, tools=None, system=None, structured_output=None):
        # Chat Completions 流式事件只提供 delta，需要按 tool call index 累加参数。
        payload = {
            "model": self.model,
            "messages": _to_openai_chat_messages(_normalize_messages(messages), system=system, supports_images=self.supports_images),
            "max_tokens": max_new_tokens,
            "stream": True,
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
            "Accept": "text/event-stream",
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
            stream = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI-compatible chat streaming fallback failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "Could not reach the OpenAI-compatible chat streaming fallback.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}\n"
                f"Cause: {_connection_error_detail(exc)}"
            ) from exc

        text_parts = []
        tool_calls = {}
        usage = {}
        try:
            with stream as response:
                for _event_name, event in _iter_sse_json(response):
                    if event.get("error"):
                        _raise_openai_stream_error(event)
                    usage.update(event.get("usage") or {})
                    for choice in event.get("choices", []) or []:
                        delta = choice.get("delta", {}) or {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            text_parts.append(text)
                            yield ModelStreamEvent.text_delta(text)
                        for call_delta in delta.get("tool_calls", []) or []:
                            index = str(call_delta.get("index", len(tool_calls)))
                            call = tool_calls.setdefault(index, {"arguments": ""})
                            if call_delta.get("id"):
                                call["id"] = call_delta["id"]
                            function = call_delta.get("function", {}) or {}
                            if function.get("name"):
                                call["name"] = function["name"]
                            if function.get("arguments"):
                                call["arguments"] = call.get("arguments", "") + function["arguments"]
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "OpenAI-compatible chat streaming fallback was interrupted.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}\n"
                f"Cause: {_connection_error_detail(exc)}"
            ) from exc

        metadata = {
            "prompt_cache_supported": False,
            "prompt_cache_key": None,
            "prompt_cache_retention": None,
            **_extract_usage_cache_details({"usage": usage}),
            "openai_compat_fallback": "chat_completions",
        }
        calls = [
            ModelToolCall.create(call.get("name", ""), args=_json_args(call.get("arguments", "{}")), call_id=call.get("id") or index)
            for index, call in sorted(tool_calls.items())
            if call.get("name")
        ]
        response = (
            ModelResponse.from_tool_calls(calls, text="".join(text_parts), metadata=metadata)
            if calls
            else ModelResponse.final("".join(text_parts), metadata=metadata)
        )
        self.last_completion_metadata = metadata
        yield ModelStreamEvent.done(response, metadata=metadata)

    def stream_complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
        """流式调用 Responses API，并在结束时产出完整 ModelResponse。

        runtime 只用 text_delta 做临时展示；真正的 tool_calls/final 仍等
        done 事件后按完整 response 处理，避免半截 JSON 参数进入工具审批链。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": _to_openai_input(_normalize_messages(messages), system=system, supports_images=self.supports_images),
            "max_output_tokens": max_new_tokens,
            "stream": True,
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
        if self.supports_reasoning and self.capabilities.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.capabilities.openai_reasoning_effort}

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
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
        attempts = len(OPENAI_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                stream = urllib.request.urlopen(request, timeout=self.timeout)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(OPENAI_RETRY_DELAYS[attempt])
                    continue
                if tools and exc.code >= 500:
                    yield from self._stream_chat_completions(
                        messages,
                        max_new_tokens,
                        tools=tools,
                        system=system,
                        structured_output=structured_output,
                    )
                    return
                raise RuntimeError(f"OpenAI-compatible streaming request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(OPENAI_RETRY_DELAYS[attempt])
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible streaming backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}\n"
                    f"Cause: {_connection_error_detail(exc)}"
                ) from exc

        text_parts = []
        tool_calls = {}
        item_phases = {}
        completed_response = None
        try:
            with stream as response:
                for _event_name, event in _iter_sse_json(response):
                    event_type = event.get("type", "")
                    if event_type == "error" or event.get("error"):
                        _raise_openai_stream_error(event)
                    if event_type == "response.output_item.added":
                        item = event.get("item", {}) or {}
                        item_id = item.get("id") or event.get("item_id")
                        if item_id and item.get("phase"):
                            item_phases[item_id] = item.get("phase")
                    elif event_type in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            text_parts.append(delta)
                            phase = event.get("phase") or item_phases.get(event.get("item_id"))
                            yield ModelStreamEvent.text_delta(delta, phase=phase)
                    elif event_type == "response.function_call_arguments.delta":
                        key = str(event.get("item_id") or event.get("output_index") or len(tool_calls))
                        call = tool_calls.setdefault(key, {"arguments": ""})
                        call["arguments"] = call.get("arguments", "") + str(event.get("delta") or "")
                    elif event_type == "response.function_call_arguments.done":
                        key = str(event.get("item_id") or event.get("output_index") or len(tool_calls))
                        call = tool_calls.setdefault(key, {"arguments": ""})
                        call["id"] = event.get("item_id") or key
                        call["call_id"] = event.get("call_id") or event.get("item_id") or key
                        call["name"] = event.get("name")
                        call["arguments"] = event.get("arguments", call.get("arguments", "") or "{}")
                    elif event_type == "response.completed":
                        completed_response = event.get("response") or {}
                        break
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "OpenAI-compatible streaming response was interrupted.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}\n"
                f"Cause: {_connection_error_detail(exc)}"
            ) from exc

        data = completed_response or _openai_stream_fallback_data(text_parts, tool_calls)
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible streaming error: {data['error']}")
        metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        self.last_completion_metadata = metadata
        yield ModelStreamEvent.done(_extract_openai_model_response(data, metadata), metadata=metadata)

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": _to_openai_input(_normalize_messages(messages), system=system, supports_images=self.supports_images),
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
        if self.supports_reasoning and self.capabilities.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.capabilities.openai_reasoning_effort}

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
        attempts = len(OPENAI_RETRY_DELAYS) + 1
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
                    time.sleep(OPENAI_RETRY_DELAYS[attempt])
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
                    time.sleep(OPENAI_RETRY_DELAYS[attempt])
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}\n"
                    f"Cause: {_connection_error_detail(exc)}"
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
        if text and not data.get("output") and not data.get("choices") and not data.get("output_text"):
            data = {**data, "output_text": text}
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        self.last_completion_metadata = metadata
        return _extract_openai_model_response(data, metadata)
