# 记忆通用工具函数：负责路径规范化、freshness、摘要预览和稳定哈希。

import hashlib
import json
from datetime import datetime
import re
from pathlib import Path

from ..workspace import clip


def _ensure_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_workspace_path(raw_path, workspace_root=None):
    path = Path(str(raw_path))
    if workspace_root is None:
        return path

    root = Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def canonicalize_path(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None:
        return Path(str(raw_path)).as_posix()
    if workspace_root is None:
        return Path(str(raw_path)).as_posix()
    root = Path(workspace_root).resolve()
    return resolved.relative_to(root).as_posix()


def file_freshness(raw_path, workspace_root=None):
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


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


def _affected_paths(args, metadata, workspace_root=None):
    paths = [
        canonicalize_path(path, workspace_root)
        for path in _ensure_list((metadata or {}).get("affected_paths", []))
        if str(path).strip()
    ]
    if isinstance(args, dict) and args.get("path"):
        paths.append(canonicalize_path(args["path"], workspace_root))
    return _dedupe_preserve_order([path for path in paths if path])
