"""Selectable long-term-memory backends used by the synchronous agent runtime."""

from __future__ import annotations

from dataclasses import dataclass


MEMORY_BACKENDS = {"legacy", "progressive", "disabled"}


@dataclass(frozen=True)
class MemoryContext:
    core: str = ""
    project: str = ""
    details: dict | None = None


class LegacyMemoryBackend:
    """Adapter around the existing candidate, Dream, and retrieval implementation."""

    name = "legacy"

    def __init__(self, agent):
        self.agent = agent

    def prepare_request(self, user_message, task_state):
        self.agent.retrieve_long_term_memory_for_request(user_message, task_state)

    def context(self):
        return MemoryContext(details={"kind": "legacy"})

    def before_compact(self, task_state):
        return self.agent.maybe_extract_memory_candidates(
            task_state=task_state,
            reason="before_compact",
            background=False,
            force=True,
        )

    def after_completion(self, task_state, *, already_maintained=False):
        if not already_maintained:
            self.agent.maybe_extract_memory_candidates(
                task_state=task_state,
                reason="auto",
                background=True,
            )
        self.agent.schedule_dream_if_needed(task_state)

    def remember(self, text):
        return self.agent.remember_long_term_legacy(text)

    def prompt_rules(self, mode):
        return self.agent.legacy_memory_prompt_rules()

    def close(self):
        return None


class DisabledMemoryBackend:
    """No-op backend used by benchmarks and memory-disabled child runtimes."""

    name = "disabled"

    def __init__(self, agent):
        self.agent = agent

    def prepare_request(self, user_message, task_state):
        return None

    def context(self):
        return MemoryContext(details={"kind": "disabled"})

    def before_compact(self, task_state):
        return {"status": "disabled", "reason": "memory_backend_disabled"}

    def after_completion(self, task_state, *, already_maintained=False):
        return None

    def remember(self, text):
        raise ValueError("long-term memory is disabled")

    def prompt_rules(self, mode):
        return ""

    def close(self):
        return None


def build_memory_backend(agent, name):
    normalized = str(name or "legacy").strip()
    if normalized not in MEMORY_BACKENDS:
        raise ValueError("memory backend must be one of: legacy, progressive, disabled")
    if normalized == "legacy":
        return LegacyMemoryBackend(agent)
    if normalized == "disabled":
        return DisabledMemoryBackend(agent)
    from .progressive import ProgressiveMemoryBackend

    return ProgressiveMemoryBackend(agent)


__all__ = [
    "DisabledMemoryBackend",
    "LegacyMemoryBackend",
    "MEMORY_BACKENDS",
    "MemoryContext",
    "build_memory_backend",
]
