"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
目前包含 task_state、trace 和可逆文件快照。两者分开后，恢复现场和复盘证据
不会混在一起，session 也不需要保存文件内容。
"""

import json
import threading
from pathlib import Path

from .atomic import PersistenceError, atomic_write_json
from .change_sets import ChangeSetTracker, apply_change_set, load_change_set


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._trace_lock = threading.Lock()
        self._change_lock = threading.Lock()

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, task_state.to_dict(), sort_keys=True)
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        try:
            with self._trace_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                # trace 采用 jsonl 追加写入；同一 RunStore 的后台维护事件和
                # 主循环事件必须串行落盘，避免两条 JSON 在写入时交错。
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False))
                    handle.write("\n")
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not append trace {path}: {exc}") from exc
        return path

    def load_task_state(self, task_id):
        return json.loads(self.task_state_path(task_id).read_text(encoding="utf-8"))

    def begin_change_set(self, task_state, workspace_root, conversation_id):
        """Start a reversible workspace snapshot inside the current run."""
        return ChangeSetTracker(
            workspace_root,
            self.run_dir(task_state),
            _run_id(task_state),
            conversation_id,
        ).begin()

    def load_change_set(self, run_id, workspace_root):
        with self._change_lock:
            return load_change_set(self.run_dir(run_id), workspace_root)

    def apply_change_set(self, run_id, workspace_root, action):
        with self._change_lock:
            return apply_change_set(
                self.run_dir(run_id),
                workspace_root,
                action,
            )
