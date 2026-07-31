# Anthropic-compatible 适配器：负责 messages 请求格式、工具结果格式和响应解析。

from __future__ import annotations

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .capabilities import model_capability
from .common import _extract_usage_cache_details, _image_block_base64, _iter_sse_json, _json_args, _normalize_messages, _normalize_versioned_base_url
from .schemas import _tool_specs_to_anthropic
from .types import ModelResponse, ModelStreamEvent, ModelToolCall

ANTHROPIC_CACHE_TTLS = {"5m", "1h"}


def _cache_control(ttl):
    """构造 Anthropic cache_control；5 分钟是 API 的默认 TTL。"""
    cache_control = {"type": "ephemeral"}
    if ttl == "1h":
        cache_control["ttl"] = ttl
    return cache_control


def _assistant_content_blocks(message):
    # codemate 默认不启用 thinking，只回放 Anthropic 需要的可见文本和 tool_use。
    # 这样 DeepSeek/Anthropic 兼容接口不会被旧 thinking block 污染。
    content = []
    text = str(message.get("content", "") or "")
    if text:
        content.append({"type": "text", "text": text})
    content.extend(
        {
            "type": "tool_use",
            "id": call.get("id"),
            "name": call.get("name"),
            "input": call.get("args", {}) or {},
        }
        for call in message.get("tool_calls") or []
    )
    return content


def _tool_result_content(message, supports_images=True):
    text = str(message.get("content", "") or "")
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    if supports_images:
        for block in message.get("content_blocks", []) or []:
            if block.get("type") != "image":
                continue
            media_type, data = _image_block_base64(block)
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return blocks[0]["text"]
    return blocks or text


def _to_anthropic_messages(messages, supports_images=True):
    converted = []
    index = 0
    messages = list(messages or [])
    while index < len(messages):
        message = messages[index]
        role = message.get("role", "user")
        if role == "tool":
            # Anthropic 要求一条 assistant 消息里的多个 tool_use，
            # 必须由紧跟其后的一条 user 消息一次性返回所有 tool_result。
            tool_results = []
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_message = messages[index]
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_message.get("tool_call_id"),
                        "content": _tool_result_content(tool_message, supports_images=supports_images),
                    }
                )
                index += 1
            converted.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )
            continue
        elif role == "assistant" and message.get("tool_calls"):
            converted.append(
                {
                    "role": "assistant",
                    "content": _assistant_content_blocks(message),
                }
            )
        else:
            converted.append({"role": role, "content": [{"type": "text", "text": str(message.get("content", ""))}]})
        index += 1
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
    metadata = _extract_usage_cache_details(data)
    if calls:
        return ModelResponse.from_tool_calls(calls, text=text, metadata=metadata, raw=data)
    return ModelResponse.final(text, metadata=metadata, raw=data)


def _anthropic_stream_response_data(blocks, usage):
    # Anthropic 流式内容按 content block index 返回。
    # 结束后重新组装成普通 messages response，复用非流式解析逻辑。
    content = []
    for _index, block in sorted(blocks.items(), key=lambda item: item[0]):
        block_type = block.get("type")
        if block_type == "text":
            content.append({"type": "text", "text": block.get("text", "")})
        elif block_type == "tool_use":
            input_text = block.get("partial_json", "")
            input_value = _json_args(input_text) if input_text else dict(block.get("input") or {})
            content.append(
                {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": input_value,
                }
            )
    return {"content": content, "usage": dict(usage or {})}


def _raise_anthropic_stream_error(event):
    error = event.get("error") if isinstance(event.get("error"), dict) else event
    message = error.get("message") or error.get("type") or "unknown streaming error"
    raise RuntimeError(f"Anthropic-compatible streaming error: {message}")


class AnthropicCompatibleModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        prompt_cache=None,
        prompt_cache_ttl="5m",
    ):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.capabilities = model_capability(model)
        self.supports_streaming = self.capabilities.supports_streaming
        self.supports_images = self.capabilities.supports_images
        self.supports_reasoning = self.capabilities.supports_reasoning
        if prompt_cache_ttl not in ANTHROPIC_CACHE_TTLS:
            raise ValueError("prompt_cache_ttl must be '5m' or '1h'")
        if prompt_cache is None:
            prompt_cache = str(model or "").lower().startswith("claude-")
        self.supports_prompt_cache = bool(prompt_cache)
        self.prompt_cache_ttl = prompt_cache_ttl
        self.supports_tools = True
        self.last_completion_metadata = {}

    def fork(self):
        """为并发子 agent 创建独立客户端实例，避免 last_completion_metadata 互相覆盖。"""
        return type(self)(
            self.model,
            self.base_url,
            self.api_key,
            self.temperature,
            self.timeout,
            prompt_cache=self.supports_prompt_cache,
            prompt_cache_ttl=self.prompt_cache_ttl,
        )

    def _build_payload(
        self,
        messages,
        max_new_tokens,
        tools,
        system,
        structured_output,
        stream,
        cache_enabled,
    ):
        """构造共享的 Anthropic 请求，并在启用时标记稳定前缀和自动缓存。"""
        payload = {
            "model": self.model,
            "messages": _to_anthropic_messages(_normalize_messages(messages), supports_images=self.supports_images),
            "max_tokens": max_new_tokens,
            "stream": stream,
        }
        if cache_enabled:
            cache_control = _cache_control(self.prompt_cache_ttl)
            payload["cache_control"] = cache_control
            if system:
                payload["system"] = [
                    {
                        "type": "text",
                        "text": str(system),
                        "cache_control": cache_control,
                    }
                ]
        elif system:
            payload["system"] = str(system)
        if tools:
            payload["tools"] = _tool_specs_to_anthropic(tools)

        output_config = {}
        if self.supports_reasoning and self.capabilities.anthropic_effort:
            output_config["effort"] = self.capabilities.anthropic_effort
        if structured_output:
            output_config["format"] = {
                "type": "json_schema",
                "schema": dict(structured_output.get("schema") or {}),
            }
        if output_config:
            payload["output_config"] = output_config
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def stream_complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
        """流式调用 Anthropic Messages API，并在结束时返回完整 ModelResponse。

        Anthropic 的文本和工具调用都以 content block 分片返回。这里实时转发
        text_delta 给 UI，但工具参数必须累积到 message_stop 后再解析。
        """
        del prompt_cache_retention
        self.last_completion_metadata = {}
        payload = self._build_payload(
            messages,
            max_new_tokens,
            tools,
            system,
            structured_output,
            stream=True,
            cache_enabled=self.supports_prompt_cache and bool(prompt_cache_key),
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
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
                stream = urllib.request.urlopen(request, timeout=self.timeout)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible streaming request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible streaming backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        blocks = {}
        usage = {}
        stop_reason = None
        try:
            with stream as response:
                for _event_name, event in _iter_sse_json(response):
                    event_type = event.get("type", "")
                    if event_type == "error" or event.get("error"):
                        _raise_anthropic_stream_error(event)
                    if event_type == "message_start":
                        message = event.get("message", {}) or {}
                        usage.update(message.get("usage") or {})
                    elif event_type == "content_block_start":
                        index = event.get("index")
                        if index is None:
                            index = len(blocks)
                        block = event.get("content_block", {}) or {}
                        blocks[index] = {
                            "type": block.get("type"),
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": dict(block.get("input") or {}),
                            "text": str(block.get("text") or ""),
                            "partial_json": "",
                        }
                    elif event_type == "content_block_delta":
                        index = event.get("index")
                        if index is None:
                            index = len(blocks)
                        block = blocks.setdefault(index, {"text": "", "partial_json": ""})
                        delta = event.get("delta", {}) or {}
                        if delta.get("type") == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                block["type"] = block.get("type") or "text"
                                block["text"] = block.get("text", "") + text
                                yield ModelStreamEvent.text_delta(text)
                        elif delta.get("type") == "input_json_delta":
                            block["type"] = block.get("type") or "tool_use"
                            block["partial_json"] = block.get("partial_json", "") + str(delta.get("partial_json") or "")
                    elif event_type == "message_delta":
                        stop_reason = (event.get("delta", {}) or {}).get("stop_reason")
                        usage.update(event.get("usage") or {})
                    elif event_type == "message_stop":
                        break
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "Anthropic-compatible streaming response was interrupted.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}"
            ) from exc

        data = _anthropic_stream_response_data(blocks, usage)
        response = _extract_anthropic_response(data)
        metadata = dict(response.metadata or {})
        if stop_reason:
            metadata["stop_reason"] = stop_reason
        response.metadata = metadata
        self.last_completion_metadata = metadata
        yield ModelStreamEvent.done(response, metadata=metadata)

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
        del prompt_cache_retention
        self.last_completion_metadata = {}
        payload = self._build_payload(
            messages,
            max_new_tokens,
            tools,
            system,
            structured_output,
            stream=False,
            cache_enabled=self.supports_prompt_cache and bool(prompt_cache_key),
        )

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
        response = _extract_anthropic_response(data)
        self.last_completion_metadata = dict(response.metadata or {})
        return response
