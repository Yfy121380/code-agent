# 模型通用工具函数：处理消息归一化、文本视图、参数 JSON、SSE 流和 usage/cache 元数据。

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


def _decode_stream_line(raw_line):
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return str(raw_line)


def _iter_sse_json(response):
    """逐条解析 SSE data JSON。

    OpenAI 和 Anthropic 的流式接口都基于 SSE，但事件名和 data 结构不同。
    这个函数只负责 HTTP/SSE 层：合并多行 data、跳过注释和 [DONE]，
    业务字段由各 provider 适配器继续解析。
    """
    event_name = None
    data_lines = []

    def flush_event():
        if not data_lines:
            return None
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return event_name, data

    while True:
        raw_line = response.readline()
        if not raw_line:
            event = flush_event()
            if event is not None:
                yield event
            return
        line = _decode_stream_line(raw_line).rstrip("\r\n")
        if not line:
            event = flush_event()
            if event is not None:
                yield event
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


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
            return ModelResponse.final(
                output.get("text", ""),
                raw=output,
                commentary_text=output.get("commentary_text", ""),
            )
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
    cache_creation_input_tokens = 0

    # Anthropic reports uncached, cache-creation, and cache-read input separately.
    # Runtime needs their sum to estimate the actual context size for compaction.
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        uncached_input_tokens = int(input_tokens or 0)
        cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        input_tokens = uncached_input_tokens + cached_tokens + cache_creation_input_tokens
    else:
        uncached_input_tokens = None

    total_tokens = usage.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "cache_hit": cached_tokens > 0,
    }
