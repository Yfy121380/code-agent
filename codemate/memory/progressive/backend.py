"""Runtime orchestration for progressive Core and Ordinary Memory."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from collections import OrderedDict
from datetime import datetime

from ...storage import STOP_REASON_FINAL_ANSWER_RETURNED
from ...ui import NullUI
from ...workspace import clip, now
from ..backend import MemoryContext
from .prompts import (
    AGENT_MEMORY_PROMPT,
    CONSOLIDATION_REQUEST,
    CONSOLIDATION_SYSTEM_PROMPT,
    PLAN_MEMORY_PROMPT,
)
from .store import CoreMemoryStore, ProjectMemoryStore


CONSOLIDATION_INTERVAL_USER_TURNS = 20
CONSOLIDATION_MIN_CHARS = 50_000
VISIBLE_ORDINARY_MEMORIES = 25


def _conversation_is_complete(messages):
    return any(
        item.get("role") == "assistant"
        and (
            str(item.get("kind", "")) == "final"
            or (
                not item.get("tool_calls") and str(item.get("kind", "")) != "commentary"
            )
            or str(item.get("content", "")).startswith("Stopped after ")
        )
        for item in messages
    )


def _message_chars(item):
    return len(str(item.get("content", ""))) + len(
        json.dumps(item.get("tool_calls", []), ensure_ascii=False, sort_keys=True)
    )


class ProgressiveMemoryBackend:
    """Expose a bounded title index and consolidate durable project topics."""

    name = "progressive"

    def __init__(self, agent):
        self.agent = agent
        self.core_store = CoreMemoryStore(agent.paths.progressive_core_memory)
        self.project_store = ProjectMemoryStore(agent.paths.progressive_memory_root)
        self._context = MemoryContext()
        self._consolidation_lock = threading.RLock()
        self._running = False
        self._read_ids = set()

    def prepare_request(self, user_message, task_state):
        """Freeze Core and the top-scoring title index for one user request."""
        del user_message, task_state
        core = ""
        if self.agent.runtime_mode not in {
            "memory_consolidation",
            "review",
            "delegate",
        }:
            core = self.core_store.render()
        project, index_result = self.project_store.prompt_index(
            VISIBLE_ORDINARY_MEMORIES
        )
        self._context = MemoryContext(
            core=core,
            project=project,
            details={
                "kind": "progressive",
                "ordinary_count": index_result["total"],
                "visible_count": len(index_result["items"]),
                "core_chars": len(core),
            },
        )

    def context(self):
        if not self._context.project:
            self.prepare_request("", None)
        return self._context

    def prompt_rules(self, mode):
        if self.agent.runtime_mode == "memory_consolidation":
            return CONSOLIDATION_SYSTEM_PROMPT
        return PLAN_MEMORY_PROMPT if str(mode) == "plan" else AGENT_MEMORY_PROMPT

    def pending(self):
        """Collect complete transcript conversations after the stable checkpoint."""
        messages = self.agent.session_store.load_transcript(self.agent.session["id"])
        checkpoint = str(
            self.agent.session.get("memory_consolidation_checkpoint", "") or ""
        )
        grouped = OrderedDict()
        for message in messages:
            conversation_id = str(message.get("conversation_id", "") or "")
            if conversation_id:
                grouped.setdefault(conversation_id, []).append(message)
        ids = list(grouped)
        if checkpoint and checkpoint in grouped:
            ids = ids[ids.index(checkpoint) + 1 :]
        complete = [
            {"id": conversation_id, "messages": grouped[conversation_id]}
            for conversation_id in ids
            if _conversation_is_complete(grouped[conversation_id])
        ]
        flat = [item for conversation in complete for item in conversation["messages"]]
        user_turns = sum(1 for item in flat if item.get("role") == "user")
        chars = sum(_message_chars(item) for item in flat)
        return {
            "conversations": complete,
            "user_turns": user_turns,
            "chars": chars,
            "due": (
                user_turns >= CONSOLIDATION_INTERVAL_USER_TURNS
                or chars >= CONSOLIDATION_MIN_CHARS
            ),
        }

    def before_compact(self, task_state):
        return self.maybe_consolidate(
            task_state, reason="before_compact", background=False, force=True
        )

    def after_completion(self, task_state, *, already_maintained=False):
        if self.agent.runtime_mode != "agent" or already_maintained:
            return None
        return self.maybe_consolidate(task_state, reason="auto", background=True)

    def maybe_consolidate(
        self, task_state=None, *, reason="auto", background=True, force=False
    ):
        """Run or schedule one bounded consolidation over the pending snapshot."""
        if self.agent.runtime_mode != "agent":
            return {"status": "skipped", "reason": "runtime_mode"}
        pending = self.pending()
        if not pending["conversations"]:
            return {"status": "skipped", "reason": "no_complete_conversations"}
        if not force and not pending["due"]:
            return {
                "status": "skipped",
                "reason": "not_due",
                "user_turns": pending["user_turns"],
                "chars": pending["chars"],
            }
        if not background:
            return self._consolidate_once(task_state, reason, pending["conversations"])
        with self._consolidation_lock:
            if self._running:
                return {"status": "skipped", "reason": "already_running"}
            self._running = True

        def run():
            try:
                self._consolidate_once(task_state, reason, pending["conversations"])
            finally:
                with self._consolidation_lock:
                    self._running = False
                with self.agent._session_lock:
                    self.agent._background_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run, daemon=True, name="codemate-memory-consolidation"
        )
        with self.agent._session_lock:
            self.agent._background_threads.add(thread)
        thread.start()
        self._trace(
            task_state,
            "memory_consolidation_scheduled",
            {"reason": reason, "conversation_count": len(pending["conversations"])},
        )
        return {"status": "scheduled", "reason": reason}

    def _consolidate_once(self, task_state, reason, conversations):
        with self._consolidation_lock:
            messages = [
                copy.deepcopy(item)
                for conversation in conversations
                for item in conversation["messages"]
            ]
            result = self._run_consolidation_child(messages, reason)
            if result["status"] != "ok":
                self._trace(task_state, "memory_consolidation_failed", result)
                return result
            checkpoint = conversations[-1]["id"]
            with self.agent._session_lock:
                if self.agent._closed:
                    return {"status": "skipped", "reason": "runtime_closed"}
                self.agent.session["memory_consolidation_checkpoint"] = checkpoint
                self.agent.session_path = self.agent.session_store.save(
                    self.agent.session
                )
            result["checkpoint"] = checkpoint
            self._trace(task_state, "memory_consolidation_finished", result)
            return result

    def _run_consolidation_child(self, history, reason):
        """Run a memory-only child agent without task, recall, or Dream features."""
        child_session = {
            "id": "memory-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": str(self.agent.root),
            "history": history,
            "history_summary": "",
            "workflow_mode": "agent",
            "plan": None,
            "memory_backend": "progressive",
            "memory_consolidation_checkpoint": "",
            "read_files": {},
            "todos": [],
            "invoked_skills": [],
            "change_sets": [],
        }
        fork = getattr(self.agent.model_client, "fork", None)
        child = self.agent.__class__(
            model_client=fork() if callable(fork) else self.agent.model_client,
            workspace=self.agent.workspace,
            session_store=self.agent.session_store,
            session=child_session,
            approval_policy="auto",
            max_steps=100,
            max_new_tokens=self.agent.max_new_tokens,
            depth=0,
            max_depth=0,
            shell_env_allowlist=self.agent.shell_env_allowlist,
            secret_env_names=self.agent.secret_env_names,
            feature_flags={**self.agent.feature_flags, "session_title": False},
            allowed_tools={
                "memory_index",
                "memory_read",
                "memory_create",
                "memory_update",
            },
            memory_backend="progressive",
            runtime_mode="memory_consolidation",
            timezone_name=self.agent.timezone_name,
            ui=NullUI(),
        )
        try:
            child.ask(CONSOLIDATION_REQUEST)
            stop_reason = str(
                getattr(child.current_task_state, "stop_reason", "") or ""
            )
            if stop_reason != STOP_REASON_FINAL_ANSWER_RETURNED:
                return {
                    "status": "error",
                    "reason": reason,
                    "stop_reason": stop_reason or "unknown",
                }
            return {
                "status": "ok",
                "reason": reason,
                "conversation_messages": len(history),
            }
        except Exception as exc:
            return {"status": "error", "reason": reason, "error": str(exc)}
        finally:
            child.close()

    def remember(self, text):
        text = str(text or "").strip()
        if not text:
            raise ValueError("memory text must not be empty")
        synthetic = [
            {
                "role": "user",
                "content": (
                    "The user explicitly asked to remember this project information:\n"
                    f"{text}"
                ),
                "conversation_id": "manual_memory",
            },
            {
                "role": "assistant",
                "kind": "final",
                "content": "Acknowledged.",
                "conversation_id": "manual_memory",
            },
        ]
        return self._run_consolidation_child(synthetic, "manual")

    def index(self, *, query="", offset=0, limit=50):
        return self.project_store.index(query=query, offset=offset, limit=limit)

    def read(self, memory_id):
        track_access = self.agent.runtime_mode not in {
            "memory_consolidation",
            "review",
            "delegate",
        }
        record = self.project_store.read(memory_id, track_access=track_access)
        if self.agent.runtime_mode == "memory_consolidation":
            self._read_ids.add(record.id)
        return record

    def create(self, title, content, reason):
        if self.agent.runtime_mode != "memory_consolidation":
            raise ValueError(
                "memory_create is only available during project-memory consolidation"
            )
        return self.project_store.create(title, content, reason)

    def update(self, memory_id, title, content, reason, expected_revision):
        if self.agent.runtime_mode != "memory_consolidation":
            raise ValueError(
                "memory_update is only available during project-memory consolidation"
            )
        if str(memory_id) not in self._read_ids:
            raise ValueError(
                "memory_update requires memory_read for the same ID during this consolidation"
            )
        return self.project_store.update(
            memory_id, title, content, reason, expected_revision
        )

    def update_core(self, key, value, reason, statement):
        self._validate_current_statement(statement)
        return self.core_store.upsert(key, value, reason)

    def remove_core(self, key, reason, statement):
        self._validate_current_statement(statement)
        return self.core_store.remove(key, reason)

    def _validate_current_statement(self, statement):
        statement = str(statement or "").strip()
        current = str(getattr(self.agent, "_current_source_user_request", "") or "")
        if not statement or statement not in current:
            raise ValueError(
                "explicit_user_statement must be an exact substring of the current user request"
            )

    def _trace(self, task_state, event, payload):
        if task_state is None:
            return
        try:
            safe_payload = {
                key: clip(value, 1000) if isinstance(value, str) else value
                for key, value in payload.items()
            }
            self.agent.emit_trace(task_state, event, safe_payload)
        except Exception:
            pass

    def close(self):
        return None
