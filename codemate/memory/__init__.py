# 记忆系统对外门面：保持 codemate.memory 的稳定入口，内部按工作记忆/过程笔记/长期记忆拆分。

from .common import canonicalize_path, file_freshness, resolve_workspace_path
from .facade import LayeredMemory
from .file_memory import (
    has_fresh_file_summary,
    invalidate_file_summary,
    invalidate_stale_file_summaries,
    remember_file,
    set_file_summary,
    set_task_summary,
    summarize_read_result,
)
from .process_notes import expire_process_notes, record_process_note, resolve_process_notes_after_success
from .long_term import daily_log_path, ensure_long_term_memory, memory_root, read_long_term_memory
from .retrieval import retrieve_long_term_memory
from .state import default_memory_state, is_effectively_empty, normalize_memory_state, render_memory_text

__all__ = [
    "LayeredMemory",
    "canonicalize_path",
    "default_memory_state",
    "daily_log_path",
    "ensure_long_term_memory",
    "expire_process_notes",
    "file_freshness",
    "has_fresh_file_summary",
    "invalidate_file_summary",
    "invalidate_stale_file_summaries",
    "is_effectively_empty",
    "normalize_memory_state",
    "record_process_note",
    "remember_file",
    "memory_root",
    "read_long_term_memory",
    "render_memory_text",
    "resolve_process_notes_after_success",
    "resolve_workspace_path",
    "retrieve_long_term_memory",
    "set_file_summary",
    "set_task_summary",
    "summarize_read_result",
]
