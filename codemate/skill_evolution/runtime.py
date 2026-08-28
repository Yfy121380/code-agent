"""Lifecycle adapter between CodeMate's synchronous loop and Skill evolution."""

from __future__ import annotations

import copy
import threading
from pathlib import Path

from ..models.types import ModelResponse
from ..tools.path_policy import gate_for_access, resolve_tool_path
from ..workspace import now
from .evaluation import evaluate_online_skills, format_online_skill_eval
from .online import MAX_MANAGER_SKILLS, online_ingest
from .prompts import SKILL_EVOLUTION_PROMPT_RULES
from .store import SkillEvolutionStore


class SkillEvolutionRuntime:
    """Keep online evolution state out of the main Agent and Agent Loop classes."""

    def __init__(self, agent):
        self.agent = agent
        settings = dict(getattr(agent.settings, "skill_evolution", {}) or {})
        self.enabled = bool(
            agent.feature_enabled("skill_evolution")
            and agent.depth == 0
            and agent.runtime_mode == "agent"
            and settings.get("enabled", True)
        )
        self.target = str(settings.get("target", "project") or "project")
        self.current_loaded_skills = []
        self._ready_window = None
        self.store = SkillEvolutionStore(
            agent.paths.skill_evolution_root,
            agent.paths.project_skills,
            agent.paths.user_skills,
            prune_min_retrieved=int(settings.get("prune_min_retrieved", 40)),
            prune_max_used=int(settings.get("prune_max_used", 0)),
        )

    def prompt_rules(self, mode="agent"):
        """Expose maintenance guidance only where its tools are model-visible."""
        return SKILL_EVOLUTION_PROMPT_RULES if self.enabled and mode == "agent" else ""

    def prepare_request(self, user_message):
        """Attach next-user feedback and reset per-request Skill invocation state."""
        self.current_loaded_skills = []
        self._ready_window = None
        if not self.enabled or self.agent.is_plan_mode():
            return
        with self.agent._session_lock:
            pending = self.agent.session.get("skill_evolution_pending")
            self.agent.session["skill_evolution_pending"] = None
            self.agent.session["updated_at"] = now()
        if isinstance(pending, dict):
            messages = list(pending.get("messages") or [])
            feedback = str(user_message or "").strip()
            if feedback:
                messages.append({"role": "user", "content": feedback})
            pending = dict(pending)
            pending["messages"] = messages[-10:]
            pending["next_user_feedback"] = feedback
            self._ready_window = pending

    def after_completion(self, task_state, user_message, assistant_text):
        """Save the current window and schedule candidate extraction when ready."""
        if not self.enabled or self.agent.is_plan_mode():
            return
        pending = {
            "messages": self._recent_dialog_messages(max_messages=8),
            "latest_user": str(user_message or ""),
            "latest_assistant": str(assistant_text or ""),
            "loaded_skill_references": copy.deepcopy(self.current_loaded_skills),
            "session_id": str(self.agent.session.get("id") or ""),
        }
        with self.agent._session_lock:
            if self.agent._closed:
                return
            self.agent.session["skill_evolution_pending"] = pending
            self.agent.session["updated_at"] = now()
            self.agent.session_path = self.agent.session_store.save(self.agent.session)
        # Usage judging and usage-based pruning are intentionally dormant. They
        # measure visible adoption rather than Skill quality and would spend an
        # additional model request after every response with retrieved Skills.
        if self._ready_window:
            window = copy.deepcopy(self._ready_window)
            self._schedule(
                "skill_evolution",
                lambda: self._run_online(window, interactive=False),
                task_state,
            )

    def _recent_dialog_messages(self, *, max_messages):
        messages = []
        for item in self.agent.session.get("history", []):
            role = str(item.get("role") or "")
            kind = str(item.get("kind") or "")
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or kind.endswith("_context") or not content:
                continue
            messages.append({"role": role, "content": content})
        return messages[-max(2, int(max_messages)) :]

    def _schedule(self, operation, callback, task_state):
        def worker():
            try:
                callback()
            except Exception as exc:
                try:
                    self.agent.emit_trace(
                        task_state,
                        "maintenance_failed",
                        {"operation": operation, "error": str(exc)[:4000]},
                    )
                except Exception:
                    pass
            finally:
                with self.agent._session_lock:
                    self.agent._background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=worker, daemon=True, name=f"codemate-{operation}")
        with self.agent._session_lock:
            self.agent._background_threads.add(thread)
        thread.start()

    def _side_query(self, system, user, max_tokens):
        fork = getattr(self.agent.model_client, "fork", None)
        client = fork() if callable(fork) else self.agent.model_client
        response = client.complete(
            [{"role": "user", "content": str(user)}],
            int(max_tokens),
            tools=None,
            system=str(system),
        )
        if not isinstance(response, ModelResponse):
            return str(response or "")
        return str(response.text or response.commentary_text or "")

    def _background_write_allowed(self, _action, _name, path):
        try:
            decision = resolve_tool_path(self.agent, path, access="write")
            gate = gate_for_access(self.agent, "write", [decision])
            if gate.action == "allow":
                return True
            # The user Skill root is a narrow CodeMate-owned location outside
            # the workspace. In auto mode the evolution service may maintain
            # its own registered files there, while ordinary write tools keep
            # their existing outside-workspace approval behavior.
            return (
                self.agent.approval_policy == "auto"
                and Path(path).resolve().is_relative_to(
                    self.agent.paths.user_skills.resolve()
                )
            )
        except Exception:
            return False

    def _interactive_write_allowed(self, action, name, path):
        try:
            decision = resolve_tool_path(self.agent, path, access="write")
            gate = gate_for_access(self.agent, "write", [decision])
        except Exception:
            return False
        if gate.action == "allow" and self.agent.approval_policy != "ask":
            return True
        return bool(
            self.agent.ui.approval_request(
                f"skill_{action}",
                {"name": name, "path": str(path)},
                metadata={"risk_level": "high", "read_only": False, **gate.to_metadata()},
            )
        )

    def _run_online(self, window, *, interactive):
        if not self.enabled:
            return {"ok": False, "action": "disabled"}
        return online_ingest(
            self.agent,
            self.store,
            list(window.get("messages") or []),
            self._side_query,
            loaded_skill_references=list(window.get("loaded_skill_references") or []),
            hint=str(window.get("hint") or ""),
            target=self.target,
            confirm_write=(
                self._interactive_write_allowed
                if interactive
                else self._background_write_allowed
            ),
        )

    def extract_now(self, hint=""):
        with self.agent._session_lock:
            pending = copy.deepcopy(self.agent.session.get("skill_evolution_pending"))
        if not isinstance(pending, dict):
            return {"ok": False, "error": "no pending online skill extraction window"}
        pending["hint"] = str(hint or "")
        result = self._run_online(pending, interactive=True)
        with self.agent._session_lock:
            self.agent.session["skill_evolution_pending"] = None
            self.agent.session["updated_at"] = now()
            self.agent.session_path = self.agent.session_store.save(self.agent.session)
        return result

    def record_invocation(self, skill):
        """Record a Skill actually loaded during the current user request."""
        if self.enabled:
            name = str(skill.get("name") or "").strip()
            if name:
                reference = {
                    "name": name,
                    "description": str(skill.get("description") or ""),
                    "when_to_use": str(skill.get("when_to_use") or ""),
                    "source": str(skill.get("scope") or ""),
                }
                self.current_loaded_skills = [
                    item
                    for item in self.current_loaded_skills
                    if str(item.get("name") or "") != name
                ]
                self.current_loaded_skills.append(reference)
                self.current_loaded_skills = self.current_loaded_skills[
                    -MAX_MANAGER_SKILLS:
                ]
            try:
                self.store.record_invocation(skill)
            except Exception:
                # Usage telemetry must never make a successfully loaded Skill
                # unavailable to the main task.
                pass

    def record_feedback(self, name, rating, note=""):
        self.store.record_feedback(name, rating, note)

    def format_stats(self):
        return self.store.format_stats()

    def evaluate(self):
        return evaluate_online_skills(self.agent, self.store, self._side_query)

    def format_evaluation(self):
        return format_online_skill_eval(self.evaluate())

    def create_skill(self, **kwargs):
        return self.store.create_skill(**kwargs)

    def evolve_skill(self, **kwargs):
        return self.store.evolve_skill(**kwargs)
