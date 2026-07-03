# 工作记忆状态：负责 memory state 规范化、渲染和长期记忆召回视图。

from pathlib import Path

from ..workspace import clip, now
from .common import _dedupe_preserve_order, _ensure_list, canonicalize_path, file_freshness
from .constants import FILE_SUMMARY_LIMIT, PROCESS_NOTE_LIMIT, WORKING_FILE_LIMIT
from .durable import DurableMemoryStore
from .process_notes import _normalize_process_note


def default_memory_state():
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "file_summaries": {},
        "process_notes": [],
    }

def normalize_memory_state(state, workspace_root=None):
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    working = state.get("working")
    if not isinstance(working, dict):
        working = {}
    task_summary = clip(str(working.get("task_summary", "")).strip(), 300)
    recent_files = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in _ensure_list(working.get("recent_files", []))
            if str(path).strip()
        ]
    )[-WORKING_FILE_LIMIT:]

    file_summaries = state.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        path = canonicalize_path(path, workspace_root)
        if isinstance(summary, dict):
            text = clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or now()
            freshness = summary.get("freshness")
            freshness = None if freshness in (None, "") else str(freshness).strip() or None
        else:
            text = clip(str(summary).strip(), 500)
            created_at = now()
            freshness = None
        if not path or not text:
            continue
        normalized_file_summaries[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }

    process_notes = []
    for note in _ensure_list(state.get("process_notes", [])):
        normalized = _normalize_process_note(note, workspace_root)
        if normalized is not None:
            process_notes.append(normalized)

    return {
        "working": {
            "task_summary": task_summary,
            "recent_files": recent_files,
        },
        "file_summaries": normalized_file_summaries,
        "process_notes": process_notes[-PROCESS_NOTE_LIMIT:],
    }

def retrieval_candidates(state, query, limit=3, workspace_root=None):
    normalize_memory_state(state, workspace_root)
    if workspace_root is None:
        return []
    durable_store = DurableMemoryStore(Path(workspace_root) / ".codemate" / "memory")
    return durable_store.retrieval_candidates(query, limit=limit)


def retrieval_view(state, query, limit=3, workspace_root=None):
    candidates = retrieval_candidates(state, query, limit=limit, workspace_root=workspace_root)
    lines = ["Relevant memory:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for note in candidates:
        lines.append(f"- {note['text']}")
    return "\n".join(lines)


def render_memory_text(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    lines = [
        "Working memory:",
        f"- task: {state['working']['task_summary'] or '-'}",
        f"- recent_files: {', '.join(state['working']['recent_files']) or '-'}",
    ]

    summaries = []
    for path in state["working"]["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("summary", "") and summary.get("freshness") == current_freshness:
            summaries.append(f"- {path}: {summary['summary']}")
    if summaries:
        lines.append("- file_summaries:")
        lines.extend(f"  {line}" for line in summaries)
    else:
        lines.append("- file_summaries: -")

    if state["process_notes"]:
        lines.append("- process_notes:")
        for note in state["process_notes"]:
            paths = ", ".join(note.get("affected_paths", [])) or "workspace"
            lines.append(f"  - {note['tool']} {note['kind']} on {paths}, count={note['count']}")
            lines.append(f"    {note['message']}")
    else:
        lines.append("- process_notes: -")

    durable_topics = []
    if workspace_root is not None:
        durable_topics = DurableMemoryStore(Path(workspace_root) / ".codemate" / "memory").topic_slugs()
    # lines.append(f"- durable_topics: {', '.join(durable_topics) or '-'}")
    return "\n".join(lines)


def is_effectively_empty(state, workspace_root=None):
    state = normalize_memory_state(state, workspace_root)
    return (
        not str(state["working"]["task_summary"]).strip()
        and not state["working"]["recent_files"]
        and not state["process_notes"]
        and not state["file_summaries"]
    )
