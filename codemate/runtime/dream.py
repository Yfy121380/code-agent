# Dream 运行流程。
# 本文件负责长期记忆整理的触发、后台线程启动和子 agent 执行。
# 它不直接处理 memory 文件格式，只协调 long_term/dream 模块和 runtime 状态。
# 这样主 agent 文件不会被后台整理任务的细节淹没。

import threading
import uuid
import hashlib
import json
from datetime import datetime

from .. import memory as memorylib
from ..memory import dream as dreamlib
from ..memory import long_term as longterm
from ..storage import STOP_REASON_FINAL_ANSWER_RETURNED
from ..ui import NullUI
from ..workspace import now


class DreamMixin:
    def retrieve_long_term_memory_for_request(self, user_message, task_state):
        """为当前 ask 执行一次长期记忆模型召回。

        召回只发生在用户请求开始时，后续工具循环复用 `relevant_long_term_memory`，
        避免每次重新组 prompt 都额外调用模型。失败不会中断主任务，只会降级为空召回。
        """
        self.relevant_long_term_memory = []
        self.long_term_memory_status = "disabled"
        if not (self.feature_enabled("memory") and self.feature_enabled("relevant_memory") and self.feature_enabled("long_term_memory")):
            return
        if self.runtime_mode != "agent":
            self.long_term_memory_status = "skipped_runtime_mode"
            return

        memory_files = memorylib.read_long_term_memory(self.root)
        cache_payload = json.dumps(
            {"user_message": str(user_message), "memory_files": memory_files},
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
        if self._long_term_memory_cache_key == cache_key:
            return

        self.emit_trace(task_state, "memory_retrieval_started", {"memory_hash": cache_key})
        try:
            result = memorylib.retrieve_long_term_memory(self.model_client, self.root, user_message)
        except Exception as exc:
            self.relevant_long_term_memory = []
            self.long_term_memory_status = "failed"
            metadata = {"error": str(exc), "memory_hash": cache_key}
            self.emit_trace(task_state, "memory_retrieval_failed", metadata)
            self._long_term_memory_cache_key = cache_key
            return

        selected = list(result.get("selected", []) or [])
        self.relevant_long_term_memory = selected
        self.long_term_memory_status = str(result.get("status", "ok"))
        metadata = {
            "status": self.long_term_memory_status,
            "selected_count": len(selected),
            "selected_sources": [str(item.get("source", "")) for item in selected],
            "duration_ms": int(result.get("duration_ms", 0) or 0),
            "memory_hash": cache_key,
        }
        self.emit_trace(task_state, "memory_retrieval_finished", metadata)
        self._long_term_memory_cache_key = cache_key

    def schedule_dream_if_needed(self, task_state):
        # 自动 dream 只做轻量触发判断；真正整理放到后台线程，避免阻塞当前回答。
        if not self.feature_enabled("memory_dream") or self.runtime_mode != "agent":
            return
        session_count = self.session_store.count()
        due, reason = dreamlib.should_run_dream(self.root, session_count)
        if not due:
            return
        self.start_dream_background(reason=reason)
        self.emit_trace(task_state, "dream_scheduled", {"reason": reason, "session_count": session_count})

    def start_dream_background(self, reason="manual"):
        thread = threading.Thread(target=self.run_dream_once, kwargs={"reason": reason, "foreground": False}, daemon=True)
        thread.start()
        return thread

    def run_dream_once(self, reason="manual", foreground=True):
        # Dream 用独立子 agent 执行，权限限制在记忆整理需要的文件工具内。
        # 只有子 agent 正常 final 退出时才推进 daily log cursor，避免失败后跳过日志。
        with longterm.dream_lock(self.root) as acquired:
            if not acquired:
                return "dream skipped: another dream process is already running"
            try:
                state = longterm.load_dream_state(self.root)
                cursor_text = dreamlib.render_daily_log_cursor(state)
                dream_session = {
                    "id": "dream-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
                    "created_at": now(),
                    "workspace_root": str(self.root),
                    "history": [],
                    "memory": memorylib.default_memory_state(),
                    "todos": [],
                    "runtime_mode": "dream",
                    "reason": str(reason),
                }
                child = self.__class__(
                    model_client=self.model_client,
                    workspace=self.workspace,
                    session_store=self.session_store,
                    session=dream_session,
                    approval_policy="auto",
                    max_steps=12,
                    max_new_tokens=self.max_new_tokens,
                    depth=0,
                    max_depth=0,
                    read_only=False,
                    shell_env_allowlist=self.shell_env_allowlist,
                    secret_env_names=self.secret_env_names,
                    feature_flags={
                        **self.feature_flags,
                        "long_term_memory": False,
                        "relevant_memory": False,
                        "memory_dream": False,
                    },
                    allowed_tools={"list_files", "read_file", "grep", "write_file", "patch_file", "todo_write"},
                    memory_scope_only=True,
                    runtime_mode="dream",
                    timezone_name=self.timezone_name,
                    ui=self.ui if foreground else NullUI(),
                )
                child.ask(dreamlib.dream_prompt(cursor_text))
                if getattr(child.current_task_state, "stop_reason", "") != STOP_REASON_FINAL_ANSWER_RETURNED:
                    reason_text = getattr(child.current_task_state, "stop_reason", "") or "unknown"
                    dreamlib.mark_dream_failed(self.root, status=reason_text)
                    return f"dream failed: child agent stopped with {reason_text}"
                updated = dreamlib.mark_dream_complete(self.root, self.session_store.count(), status="ok")
                cursor = updated.get("last_processed_daily_log") or {}
                file_name = cursor.get("file") or ""
                line = cursor.get("line", 0) or 0
                return f"dream completed: processed through {file_name or 'no daily logs'} line {line}"
            except Exception as exc:
                dreamlib.mark_dream_failed(self.root, status="error")
                return f"dream failed: {exc}"
