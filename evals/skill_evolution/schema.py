"""Strict task schema for reproducible Skill-evolution evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


CHECK_CATEGORIES = frozenset(
    {"functional", "regression", "instruction", "preference", "safety", "quality"}
)
TASK_CATEGORIES = frozenset(
    {"code_modification", "code_research", "project_build", "web_research"}
)


def _required_text(value, field):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


@dataclass(frozen=True)
class CheckSpec:
    """One hidden check and its separately reviewed user-safe feedback."""

    id: str
    category: str
    required: bool
    weight: int
    agent_feedback: str
    priority: int = 50

    @classmethod
    def from_dict(cls, value):
        value = dict(value or {})
        category = _required_text(value.get("category"), "check.category")
        if category not in CHECK_CATEGORIES:
            raise ValueError(f"unsupported check category: {category}")
        weight = int(value.get("weight", 0))
        priority = int(value.get("priority", 50))
        if weight <= 0:
            raise ValueError("check.weight must be positive")
        if priority < 0:
            raise ValueError("check.priority must not be negative")
        return cls(
            id=_required_text(value.get("id"), "check.id"),
            category=category,
            required=bool(value.get("required", False)),
            weight=weight,
            agent_feedback=_required_text(
                value.get("agent_feedback"), "check.agent_feedback"
            ),
            priority=priority,
        )


@dataclass(frozen=True)
class StageSpec:
    """Public workspace/request plus an evaluator-only verifier contract."""

    name: str
    workspace: Path
    verifier: Path
    request: str
    checks: tuple[CheckSpec, ...]
    max_work_rounds: int = 1
    pass_feedback: str = ""
    fail_feedback: str = ""

    @classmethod
    def from_dict(cls, root, name, value):
        value = dict(value or {})
        checks = tuple(CheckSpec.from_dict(item) for item in value.get("checks", []))
        if not checks:
            raise ValueError(f"{name}.checks must not be empty")
        check_ids = [item.id for item in checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError(f"{name}.checks contains duplicate ids")
        rounds = int(value.get("max_work_rounds", 1))
        if rounds < 1 or rounds > 4:
            raise ValueError(f"{name}.max_work_rounds must be between 1 and 4")
        workspace = (
            Path(root) / _required_text(value.get("workspace"), f"{name}.workspace")
        ).resolve()
        verifier = (
            Path(root) / _required_text(value.get("verifier"), f"{name}.verifier")
        ).resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace does not exist: {workspace}")
        if not verifier.is_file():
            raise ValueError(f"verifier does not exist: {verifier}")
        pass_feedback = str(value.get("pass_feedback") or "").strip()
        fail_feedback = str(value.get("fail_feedback") or "").strip()
        if name == "induction":
            pass_feedback = _required_text(pass_feedback, f"{name}.pass_feedback")
            fail_feedback = _required_text(fail_feedback, f"{name}.fail_feedback")
        return cls(
            name=name,
            workspace=workspace,
            verifier=verifier,
            request=_required_text(value.get("request"), f"{name}.request"),
            checks=checks,
            max_work_rounds=rounds,
            pass_feedback=pass_feedback,
            fail_feedback=fail_feedback,
        )


@dataclass(frozen=True)
class TaskPair:
    id: str
    category: str
    skill_target: str
    learned_preferences: tuple[str, ...]
    root: Path
    induction: StageSpec
    transfer: StageSpec

    @classmethod
    def from_dict(cls, root, value):
        value = dict(value or {})
        category = _required_text(value.get("category"), "category")
        if category not in TASK_CATEGORIES:
            raise ValueError(f"unsupported task category: {category}")
        return cls(
            id=_required_text(value.get("id"), "id"),
            category=category,
            skill_target=_required_text(value.get("skill_target"), "skill_target"),
            learned_preferences=tuple(
                _required_text(item, "learned_preferences[]")
                for item in value.get("learned_preferences", [])
            ),
            root=Path(root).resolve(),
            induction=StageSpec.from_dict(root, "induction", value.get("induction")),
            transfer=StageSpec.from_dict(root, "transfer", value.get("transfer")),
        )


def load_task_pair(path):
    """Load one task pair without executing any evaluator-controlled code."""
    path = Path(path).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid task JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"task definition must be an object: {path}")
    return TaskPair.from_dict(path.parent, value)
