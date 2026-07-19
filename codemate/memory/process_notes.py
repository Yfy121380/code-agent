# 过程笔记：记录工具异常调用，按成功调用和 TTL 清理，避免重复错误操作。

import hashlib

from ..workspace import clip, now
from .common import (
    _args_digest,
    _args_preview,
    _stable_json,
)
from .constants import PROCESS_NOTE_KIND_BY_ERROR_CODE, PROCESS_NOTE_LIMIT, PROCESS_NOTE_TTL_TURNS


def _process_note_kind(metadata):
    metadata = metadata or {}
    error_code = str(metadata.get("tool_error_code", "")).strip()
    if error_code in PROCESS_NOTE_KIND_BY_ERROR_CODE:
        return PROCESS_NOTE_KIND_BY_ERROR_CODE[error_code]
    status = str(metadata.get("tool_status", "")).strip()
    if status == "error":
        return "error"
    if status == "rejected":
        return "rejected"
    return ""


def _note_key(kind, tool, args_digest):
    payload = {
        "kind": kind,
        "tool": str(tool),
        "args_digest": args_digest,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()[:16]


def _normalize_process_note(note, workspace_root=None):
    if not isinstance(note, dict):
        return None
    kind = str(note.get("kind", "")).strip()
    tool = str(note.get("tool", "")).strip()
    message = clip(str(note.get("message", "")).strip(), 500)
    if not kind or not tool or not message:
        return None

    args_digest = str(note.get("args_digest", "")).strip()
    if not args_digest:
        args_digest = _args_digest(note.get("args_preview", {}))
    note_id = str(note.get("id", "")).strip() or _note_key(kind, tool, args_digest)
    return {
        "id": note_id,
        "kind": kind,
        "tool": tool,
        "tool_error_code": str(note.get("tool_error_code", "")).strip(),
        "args_digest": args_digest,
        "args_preview": note.get("args_preview", {}) if isinstance(note.get("args_preview", {}), dict) else {},
        "message": message,
        "count": max(1, int(note.get("count", 1) or 1)),
        "created_turn": max(0, int(note.get("created_turn", 0) or 0)),
        "updated_turn": max(0, int(note.get("updated_turn", 0) or 0)),
        "created_at": str(note.get("created_at", "")).strip() or now(),
        "updated_at": str(note.get("updated_at", "")).strip() or now(),
    }

def expire_process_notes(state, current_turn, ttl_turns=PROCESS_NOTE_TTL_TURNS, workspace_root=None):
    from .state import normalize_memory_state

    state = normalize_memory_state(state, workspace_root)
    current_turn = max(0, int(current_turn or 0))
    ttl_turns = max(0, int(ttl_turns or 0))
    state["process_notes"] = [
        note
        for note in state["process_notes"]
        if current_turn - int(note.get("updated_turn", 0)) < ttl_turns
    ]
    return state


def record_process_note(state, tool, args, metadata, message, current_turn, workspace_root=None):
    state = expire_process_notes(state, current_turn, workspace_root=workspace_root)
    kind = _process_note_kind(metadata)
    if not kind:
        return state

    digest = _args_digest(args)
    note_id = _note_key(kind, tool, digest)
    timestamp = now()
    incoming = {
        "id": note_id,
        "kind": kind,
        "tool": str(tool),
        "tool_error_code": str((metadata or {}).get("tool_error_code", "")).strip(),
        "args_digest": digest,
        "args_preview": _args_preview(args),
        "message": clip(str(message).strip(), 500),
        "count": 1,
        "created_turn": max(0, int(current_turn or 0)),
        "updated_turn": max(0, int(current_turn or 0)),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    notes = []
    merged = False
    for note in state["process_notes"]:
        if note["id"] == note_id:
            updated = dict(note)
            updated.update(
                {
                    "tool_error_code": incoming["tool_error_code"],
                    "args_preview": incoming["args_preview"],
                    "message": incoming["message"],
                    "count": int(note.get("count", 1)) + 1,
                    "updated_turn": incoming["updated_turn"],
                    "updated_at": incoming["updated_at"],
                }
            )
            notes.append(updated)
            merged = True
        else:
            notes.append(note)
    if not merged:
        notes.append(incoming)
    state["process_notes"] = notes[-PROCESS_NOTE_LIMIT:]
    return state


def resolve_process_notes_after_success(state, tool, args, current_turn, workspace_root=None):
    state = expire_process_notes(state, current_turn, workspace_root=workspace_root)
    tool = str(tool)

    kept = []
    for note in state["process_notes"]:
        kind = note.get("kind")
        if kind == "repeated_call":
            continue
        if kind in {"invalid_arguments", "approval_denied", "rejected", "error"} and note.get("tool") == tool:
            continue
        kept.append(note)
    state["process_notes"] = kept[-PROCESS_NOTE_LIMIT:]
    return state
