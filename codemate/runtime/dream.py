# 长期记忆维护运行流程。
# 本文件负责长期记忆召回、候选记忆提取、dream 触发和后台子 agent 执行。
# 具体 memory 文件格式放在 memory 包中，这里只协调 runtime 状态、trace 和线程。
# 这样主 agent loop 不需要关心候选 JSONL、dream lock、召回失败降级等细节。

import threading
import uuid
import hashlib
import json
from datetime import datetime

from .. import memory as memorylib
from ..memory.constants import (
    MEMORY_CANDIDATE_EXTRACT_INTERVAL_TURNS,
    MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES,
    MEMORY_CANDIDATE_EXTRACT_MIN_CHARS,
)
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
        recent_messages = self.context_manager.history_renderer.recent_messages_for_retrieval(max_messages=10, tool_result_chars=300)
        cache_payload = json.dumps(
            {
                "user_message": str(user_message),
                "memory_files": memory_files,
                "recent_messages": recent_messages,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
        if self._long_term_memory_cache_key == cache_key:
            return

        self.emit_trace(task_state, "memory_retrieval_started", {"memory_hash": cache_key, "recent_messages": len(recent_messages)})
        try:
            result = memorylib.retrieve_long_term_memory(self.model_client, self.root, user_message, recent_messages=recent_messages)
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
            "selected_created_at": [str(item.get("created_at", "")) for item in selected],
            "selected_reasons": [str(item.get("reason", "")) for item in selected],
            "duration_ms": int(result.get("duration_ms", 0) or 0),
            "memory_hash": cache_key,
        }
        self.emit_trace(task_state, "memory_retrieval_finished", metadata)
        self._long_term_memory_cache_key = cache_key

    def maybe_extract_memory_candidates(self, task_state=None, reason="auto", background=True, force=False):
        """按会话增量触发候选记忆提取。

        候选提取以完整 conversation 为单位。普通维护可以后台执行；history compact
        前会同步执行一次，避免旧消息被压缩后还没进入候选池。
        """
        if not (
            self.feature_enabled("memory")
            and self.feature_enabled("long_term_memory")
            and self.feature_enabled("memory_candidates")
        ):
            return {"status": "disabled", "reason": "feature_disabled"}
        if self.runtime_mode != "agent":
            return {"status": "skipped", "reason": "runtime_mode"}

        state = memorylib.update_candidate_extract_counters(self.session)
        due = (
            state["user_turns_since_last_extract"] >= MEMORY_CANDIDATE_EXTRACT_INTERVAL_TURNS
            or state["chars_since_last_extract"] >= MEMORY_CANDIDATE_EXTRACT_MIN_CHARS
        )
        if not force and not due:
            self.session_path = self.session_store.save(self.session)
            return {
                "status": "skipped",
                "reason": "not_due",
                "user_turns_since_last_extract": state["user_turns_since_last_extract"],
                "chars_since_last_extract": state["chars_since_last_extract"],
            }

        if background:
            if self._memory_candidate_extract_running:
                return {"status": "skipped", "reason": "already_running"}
            self._memory_candidate_extract_running = True

            def run_background():
                try:
                    self.extract_memory_candidates_once(task_state=task_state, reason=reason)
                finally:
                    self._memory_candidate_extract_running = False

            thread = threading.Thread(
                target=run_background,
                daemon=True,
            )
            thread.start()
            if task_state is not None:
                self.emit_trace(
                    task_state,
                    "memory_candidate_extract_scheduled",
                    {
                        "reason": reason,
                        "user_turns_since_last_extract": state["user_turns_since_last_extract"],
                        "chars_since_last_extract": state["chars_since_last_extract"],
                    },
                )
            return {"status": "scheduled", "reason": reason}

        return self.extract_memory_candidates_once(task_state=task_state, reason=reason)

    def extract_memory_candidates_once(self, task_state=None, reason="auto"):
        info = memorylib.conversations_since_checkpoint(self.session)
        conversations = list(info["conversations"])
        if not conversations:
            memorylib.update_candidate_extract_counters(self.session)
            self.session_path = self.session_store.save(self.session)
            result = {
                "status": "skipped",
                "reason": "no_complete_conversations",
                "checkpoint_missing": bool(info.get("checkpoint_missing")),
            }
            if task_state is not None:
                self.emit_trace(task_state, "memory_candidate_extract", result)
            return result

        client = self.model_client.fork() if hasattr(self.model_client, "fork") else self.model_client
        attempts = 0
        last_error = ""
        for attempts in range(1, MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES + 1):
            try:
                candidates = memorylib.extract_candidate_memories(
                    client,
                    conversations,
                    max_new_tokens=min(self.max_new_tokens, 1200),
                )
                write_result = memorylib.append_candidate_memories(self.root, candidates)
                state = memorylib.mark_candidate_extracted(self.session, conversations)
                self.session_path = self.session_store.save(self.session)
                result = {
                    "status": "ok",
                    "reason": reason,
                    "attempts": attempts,
                    "candidate_count": len(candidates),
                    "candidate_file": write_result["path"],
                    "checkpoint_missing": bool(info.get("checkpoint_missing")),
                    "last_extracted_conversation_id": state["last_extracted_conversation_id"],
                }
                if task_state is not None:
                    self.emit_trace(task_state, "memory_candidate_extract", result)
                return result
            except Exception as exc:
                last_error = str(exc)

        result = {
            "status": "error",
            "reason": reason,
            "attempts": attempts,
            "error": last_error or "candidate_memory_extract_failed",
            "checkpoint_missing": bool(info.get("checkpoint_missing")),
        }
        if task_state is not None:
            self.emit_trace(task_state, "memory_candidate_extract_failed", result)
        return result

    def schedule_dream_if_needed(self, task_state):
        # 自动 dream 只做轻量触发判断；真正整理放到后台线程，避免阻塞当前回答。
        if not self.feature_enabled("memory_dream") or self.runtime_mode != "agent":
            return
        due, reason = dreamlib.should_run_dream(self.root)
        if not due:
            return
        self.start_dream_background(reason=reason)
        self.emit_trace(task_state, "dream_scheduled", {"reason": reason})

    def start_dream_background(self, reason="manual"):
        thread = threading.Thread(target=self.run_dream_once, kwargs={"reason": reason, "foreground": False}, daemon=True)
        thread.start()
        return thread

    def run_dream_once(self, reason="manual", foreground=True):
        # Dream 用独立子 agent 执行，权限限制在记忆整理需要的文件工具内。
        # 只有子 agent 正常 final 退出时才推进 candidate cursor，避免失败后跳过候选记忆。
        with longterm.dream_lock(self.root) as acquired:
            if not acquired:
                return "dream skipped: another dream process is already running"
            try:
                candidates = dreamlib.unprocessed_candidates(self.root)
                if not candidates:
                    return "dream skipped: no unprocessed candidate memories"
                candidate_batch = dreamlib.render_candidate_batch(candidates)
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
                    max_steps=50,
                    max_new_tokens=self.max_new_tokens,
                    depth=0,
                    max_depth=0,
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
                child.ask(dreamlib.dream_prompt(candidate_batch))
                if getattr(child.current_task_state, "stop_reason", "") != STOP_REASON_FINAL_ANSWER_RETURNED:
                    reason_text = getattr(child.current_task_state, "stop_reason", "") or "unknown"
                    dreamlib.mark_dream_failed(self.root, status=reason_text)
                    return f"dream failed: child agent stopped with {reason_text}"
                updated = dreamlib.mark_dream_complete(self.root, candidates, status="ok")
                cursor = updated.get("last_processed_candidate") or {}
                file_name = cursor.get("file") or ""
                line = cursor.get("line", 0) or 0
                return f"dream completed: processed through {file_name or 'no candidates'} line {line}"
            except Exception as exc:
                dreamlib.mark_dream_failed(self.root, status="error")
                return f"dream failed: {exc}"
