# 长期记忆通用工具函数。

import hashlib
import json
from datetime import datetime
import re
from ..workspace import clip


def _tokenize(text):
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", str(text))}


def _parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


def _stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _args_digest(args):
    return hashlib.sha256(_stable_json(args or {}).encode("utf-8")).hexdigest()[:16]


def _preview_value(value, limit=160):
    if isinstance(value, dict):
        return {str(key): _preview_value(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_preview_value(item, limit=limit) for item in value[:8]]
    if isinstance(value, tuple):
        return [_preview_value(item, limit=limit) for item in value[:8]]
    text = str(value)
    return clip(text, limit)


def _args_preview(args):
    if not isinstance(args, dict):
        return {}
    preview = {}
    for key in sorted(args):
        limit = 80 if key in {"content", "new_text", "old_text"} else 160
        preview[str(key)] = _preview_value(args[key], limit=limit)
    return preview
