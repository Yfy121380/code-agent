# 记忆门面：LayeredMemory 向 runtime 暴露统一 API。
# 工作记忆、文件摘要和过程笔记仍然保存在 session 中。
# 长期记忆按项目隔离存放在用户级 codemate 状态目录，由 dedicated 模块管理。

from pathlib import Path

from .common import canonicalize_path
from .file_memory import (
    has_fresh_file_summary,
    invalidate_file_summary,
    invalidate_stale_file_summaries,
    remember_file,
    set_file_summary,
    set_task_summary,
)
from .long_term import ensure_long_term_memory, read_long_term_memory
from .process_notes import expire_process_notes, record_process_note, resolve_process_notes_after_success
from .state import normalize_memory_state, render_memory_text


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)
        if workspace_root is not None:
            ensure_long_term_memory(Path(workspace_root))

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

    def render_memory_text(self):
        return render_memory_text(self.state, self.workspace_root)

    def read_long_term_memory(self):
        if self.workspace_root is None:
            return {}
        return read_long_term_memory(self.workspace_root)
