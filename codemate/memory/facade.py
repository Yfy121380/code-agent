# 记忆门面：LayeredMemory 向 runtime 暴露统一 API，内部委托给各记忆子模块。

from pathlib import Path

from .common import canonicalize_path
from .durable import DurableMemoryStore
from .file_memory import (
    has_fresh_file_summary,
    invalidate_file_summary,
    invalidate_stale_file_summaries,
    remember_file,
    set_file_summary,
    set_task_summary,
)
from .process_notes import expire_process_notes, record_process_note, resolve_process_notes_after_success
from .state import normalize_memory_state, render_memory_text, retrieval_candidates, retrieval_view


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)
        self.durable_store = DurableMemoryStore(Path(workspace_root) / ".codemate" / "memory") if workspace_root is not None else None

    def to_dict(self):
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        return canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.state = set_task_summary(self.state, summary, self.workspace_root)
        return self

    def remember_file(self, path):
        self.state = remember_file(self.state, path, self.workspace_root)
        return self

    def set_file_summary(self, path, summary):
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def has_fresh_file_summary(self, path):
        return has_fresh_file_summary(self.state, path, self.workspace_root)

    def invalidate_file_summary(self, path):
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def invalidate_stale_file_summaries(self):
        self.state, invalidated = invalidate_stale_file_summaries(self.state, self.workspace_root)
        return invalidated

    def expire_process_notes(self, current_turn):
        self.state = expire_process_notes(self.state, current_turn, workspace_root=self.workspace_root)
        return self

    def record_process_note(self, tool, args, metadata, message, current_turn):
        self.state = record_process_note(
            self.state,
            tool,
            args,
            metadata,
            message,
            current_turn,
            workspace_root=self.workspace_root,
        )
        return self

    def resolve_process_notes_after_success(self, tool, args, current_turn):
        self.state = resolve_process_notes_after_success(
            self.state,
            tool,
            args,
            current_turn,
            workspace_root=self.workspace_root,
        )
        return self

    def retrieval_candidates(self, query, limit=3):
        return retrieval_candidates(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def retrieval_view(self, query, limit=3):
        return retrieval_view(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)

    def promote_durable(self, promotions):
        if self.durable_store is None:
            return [], []
        self.state = normalize_memory_state(self.state, self.workspace_root)
        promoted, superseded = self.durable_store.promote(promotions)
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return promoted, superseded
