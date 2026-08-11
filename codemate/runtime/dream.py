# 长期记忆维护运行流程。
# 本文件负责长期记忆召回、候选记忆提取、dream 触发和后台子 agent 执行。
# 具体 memory 文件格式放在 memory 包中，这里只协调 runtime 状态、trace 和线程。
# 这样主 agent loop 不需要关心候选 JSONL、dream lock、召回失败降级等细节。

import copy
import threading
import uuid
import hashlib
import json
from datetime import datetime

from .. import memory as memorylib
from ..memory.constants import (
    MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES,
)
from ..memory import dream as dreamlib
from ..memory import long_term as longterm
from ..storage import STOP_REASON_FINAL_ANSWER_RETURNED
from ..ui import NullUI
from ..workspace import now


class DreamMixin:
    def retrieve_long_term_memory_for_request(self, user_message, task_state):
        """为当前 ask 准备一次相关长期记忆。

        长期记忆只在用户请求开始时处理，后续工具循环复用 `relevant_long_term_memory`，
        记忆为空会跳过，小规模记忆直接使用，较大规模记忆才通过模型筛选。
        避免每次重新组 prompt 都额外调用模型。失败不会中断主任务，只会降级为空召回。
        """
        self.relevant_long_term_memory = []
        self.long_term_memory_status = "disabled"
        if not (self.feature_enabled("relevant_memory") and self.feature_enabled("long_term_memory")):
            return
        if self.runtime_mode != "agent":
            self.long_term_memory_status = "skipped_runtime_mode"
            return

        cache_key = ""
        try:
            memory_files = memorylib.read_long_term_memory(self.root)
            recent_messages = self.context_manager.history_renderer.recent_messages_for_retrieval(
                max_messages=10,
                tool_result_chars=300,
            )
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
            self.emit_trace(
                task_state,
                "memory_retrieval_started",
                {"memory_hash": cache_key, "recent_messages": len(recent_messages)},
            )
            result = memorylib.retrieve_long_term_memory(
                self.model_client,
                self.root,
                user_message,
                recent_messages=recent_messages,
                memory_files=memory_files,
            )
        except Exception as exc:
            self.relevant_long_term_memory = []
            self.long_term_memory_status = "failed"
            metadata = {"error": str(exc), "memory_hash": cache_key}
            try:
                self.emit_trace(task_state, "memory_retrieval_failed", metadata)
            except Exception:
                pass
            if cache_key:
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
        if not (self.feature_enabled("long_term_memory") and self.feature_enabled("memory_candidates")):
            return {"status": "disabled", "reason": "feature_disabled"}
        if self.runtime_mode != "agent":
            return {"status": "skipped", "reason": "runtime_mode"}

        with self._session_lock:
            state = memorylib.candidate_extract_status(self.session)
        if not force and not state["due"]:
            return {
                "status": "skipped",
                "reason": "not_due",
                "user_turns_since_last_extract": state["user_turns_since_last_extract"],
                "chars_since_last_extract": state["chars_since_last_extract"],
            }

        if background:
            with self._session_lock:
                if self._memory_candidate_extract_running:
                    return {"status": "skipped", "reason": "already_running"}
                self._memory_candidate_extract_running = True

            def run_background():
                try:
                    self.extract_memory_candidates_once(
                        task_state=task_state,
                        reason=reason,
                    )
                finally:
                    with self._session_lock:
                        self._memory_candidate_extract_running = False
                        self._background_threads.discard(threading.current_thread())

            thread = threading.Thread(
                target=run_background,
                daemon=True,
            )
            with self._session_lock:
                self._background_threads.add(thread)
            try:
                thread.start()
            except Exception:
                with self._session_lock:
                    self._background_threads.discard(thread)
                    self._memory_candidate_extract_running = False
                raise
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

        return self.extract_memory_candidates_once(
            task_state=task_state,
            reason=reason,
        )

    def extract_memory_candidates_once(self, task_state=None, reason="auto"):
        """Extract and persist the next complete conversation batch exactly once."""
        with self._memory_candidate_extract_lock:
            with self._session_lock:
                info = memorylib.conversations_since_checkpoint(self.session)
                conversations = copy.deepcopy(info["conversations"])
            if not conversations:
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
            candidates = None
            for attempts in range(1, MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES + 1):
                try:
                    candidates = memorylib.extract_candidate_memories(
                        client,
                        conversations,
                        max_new_tokens=min(self.max_new_tokens, 1200),
                    )
                    break
                except Exception as exc:
                    last_error = str(exc)

            if candidates is None:
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

            try:
                with self._session_lock:
                    if self._closed:
                        return {
                            "status": "skipped",
                            "reason": "runtime_closed",
                            "attempts": attempts,
                        }
                    write_result = memorylib.append_candidate_memories(
                        self.root,
                        candidates,
                    )
                    checkpoint = memorylib.mark_candidate_extracted(
                        self.session,
                        conversations,
                    )
                    self.session_path = self.session_store.save(self.session)
            except Exception as exc:
                result = {
                    "status": "error",
                    "reason": reason,
                    "attempts": attempts,
                    "error": str(exc),
                    "checkpoint_missing": bool(info.get("checkpoint_missing")),
                }
                if task_state is not None:
                    self.emit_trace(task_state, "memory_candidate_extract_failed", result)
                return result

            result = {
                "status": "ok",
                "reason": reason,
                "attempts": attempts,
                "candidate_count": len(candidates),
                "candidate_file": write_result["path"],
                "checkpoint_missing": bool(info.get("checkpoint_missing")),
                "last_extracted_conversation_id": checkpoint,
            }
            if task_state is not None:
                self.emit_trace(task_state, "memory_candidate_extract", result)
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
        def run_background():
            try:
                self.run_dream_once(reason=reason, foreground=False)
            finally:
                with self._session_lock:
                    self._background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=run_background, daemon=True)
        with self._session_lock:
            self._background_threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._session_lock:
                self._background_threads.discard(thread)
            raise
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
                    "history_summary": "",
                    "read_files": {},
                    "todos": [],
                    "invoked_skills": [],
                    "runtime_mode": "dream",
                    "reason": str(reason),
                }
                fork_model_client = getattr(self.model_client, "fork", None)
                model_client = fork_model_client() if callable(fork_model_client) else self.model_client
                child = self.__class__(
                    model_client=model_client,
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
                    allowed_tools={
                        "list_files",
                        "read_file",
                        "grep",
                        "write_file",
                        "patch_file",
                        "todo_write",
                        "todo_list",
                    },
                    memory_scope_only=True,
                    runtime_mode="dream",
                    timezone_name=self.timezone_name,
                    ui=self.ui if foreground else NullUI(),
                )
                try:
                    child.ask(dreamlib.dream_prompt(candidate_batch))
                finally:
                    child.close()
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
