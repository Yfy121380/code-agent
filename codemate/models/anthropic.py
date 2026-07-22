# Anthropic-compatible 适配器：负责 messages 请求格式、工具结果格式和响应解析。

from __future__ import annotations

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .common import _extract_usage_cache_details, _normalize_messages, _normalize_versioned_base_url
from .schemas import _tool_specs_to_anthropic
from .types import ModelResponse, ModelToolCall


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

    def complete(self, messages, max_new_tokens, tools=None, system=None, prompt_cache_key=None, prompt_cache_retention=None, structured_output=None):
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
        if structured_output:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": dict(structured_output.get("schema") or {}),
                }
            }
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
        response = _extract_anthropic_response(data)
        self.last_completion_metadata = dict(response.metadata or {})
        return response
