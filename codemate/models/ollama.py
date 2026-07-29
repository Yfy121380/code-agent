# Ollama 适配器：负责本地 Ollama chat 请求、工具 schema 转换和工具调用解析。

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .common import _json_args, _normalize_messages
from .schemas import _tool_specs_to_ollama
from .types import ModelResponse, ModelToolCall


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

class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_streaming = False
        self.supports_prompt_cache = False
        self.supports_tools = True
        self.last_completion_metadata = {}

    def fork(self):
        """为并发子 agent 创建独立客户端实例，避免 last_completion_metadata 互相覆盖。"""
        return type(self)(self.model, self.host, self.temperature, self.top_p, self.timeout)

    def complete(self, messages, max_new_tokens, tools=None, system=None, **kwargs):
        structured_output = kwargs.pop("structured_output", None)
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
        if structured_output:
            payload["format"] = dict(structured_output.get("schema") or {})
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
        metadata = {
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
            "total_tokens": (
                (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
                if data.get("prompt_eval_count") is not None or data.get("eval_count") is not None
                else None
            ),
            "cached_tokens": 0,
            "cache_hit": False,
        }
        self.last_completion_metadata = metadata
        message = data.get("message", {}) or {}
        calls = []
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {}) or {}
            name = function.get("name") or call.get("name")
            if not name:
                continue
            calls.append(ModelToolCall.create(name, args=_json_args(function.get("arguments", {})), call_id=call.get("id")))
        if calls:
            return ModelResponse.from_tool_calls(calls, text=message.get("content", ""), metadata=metadata, raw=data)
        return ModelResponse.final(message.get("content", ""), metadata=metadata, raw=data)
