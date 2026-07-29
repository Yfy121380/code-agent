# 模型通用工具函数：处理消息归一化、文本视图、参数 JSON 和 usage/cache 元数据。

from __future__ import annotations

import json
import base64
from pathlib import Path

from .types import ModelResponse, ModelToolCall


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


def _image_block_base64(block):
    path = Path(str(block.get("path", ""))).expanduser()
    if not path.is_file():
        raise RuntimeError(f"image tool result cache file is missing: {path}")
    media_type = str(block.get("media_type", "") or "").strip()
    if not media_type.startswith("image/"):
        raise RuntimeError("image tool result is missing a valid media_type")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return media_type, data


def _image_block_data_uri(block):
    media_type, data = _image_block_base64(block)
    return media_type, f"data:{media_type};base64,{data}"


def _as_model_response(output):
    if isinstance(output, ModelResponse):
        return output
    if isinstance(output, ModelToolCall):
        return ModelResponse.from_tool_calls([output])
    if isinstance(output, dict):
        if output.get("kind") == "commentary":
            return ModelResponse.commentary(output.get("text", ""), raw=output)
        if output.get("kind") == "tool_calls":
            return ModelResponse.from_tool_calls(output.get("tool_calls", []), text=output.get("text", ""), raw=output)
        if output.get("kind") == "final":
            return ModelResponse.final(output.get("text", ""), raw=output)
        if output.get("name"):
            return ModelResponse.tool_call(output.get("name"), output.get("args", {}), call_id=output.get("id"), raw=output)
    return ModelResponse.final(str(output or ""))
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
