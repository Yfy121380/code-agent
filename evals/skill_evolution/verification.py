"""Run hidden verifiers and convert their private evidence into scored results."""

from __future__ import annotations

import runpy
from dataclasses import dataclass
from pathlib import Path

from .schema import StageSpec


HARD_COMPLETION_CATEGORIES = frozenset(
    {"functional", "regression", "instruction", "safety"}
)


@dataclass(frozen=True)
class CheckResult:
    id: str
    category: str
    required: bool
    weight: int
    passed: bool
    evidence: str
    agent_feedback: str
    priority: int

    def to_dict(self):
        return {
            "id": self.id,
            "category": self.category,
            "required": self.required,
            "weight": self.weight,
            "passed": self.passed,
            "evidence": self.evidence,
            "agent_feedback": self.agent_feedback,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class VerificationReport:
    checks: tuple[CheckResult, ...]
    category_scores: dict[str, float]
    total_score: float
    completed: bool

    def to_dict(self):
        return {
            "checks": [item.to_dict() for item in self.checks],
            "category_scores": dict(self.category_scores),
            "total_score": self.total_score,
            "completed": self.completed,
        }


def _score(results):
    categories = {}
    for item in results:
        earned, possible = categories.setdefault(item.category, [0, 0])
        categories[item.category] = [
            earned + (item.weight if item.passed else 0),
            possible + item.weight,
        ]
    category_scores = {
        category: round(100.0 * earned / possible, 2) if possible else 0.0
        for category, (earned, possible) in categories.items()
    }
    total_possible = sum(item.weight for item in results)
    total_earned = sum(item.weight for item in results if item.passed)
    return category_scores, (
        round(100.0 * total_earned / total_possible, 2) if total_possible else 0.0
    )


def _load_verify_function(path):
    namespace = runpy.run_path(str(Path(path).resolve()))
    verify = namespace.get("verify")
    if not callable(verify):
        raise ValueError(f"hidden verifier must define verify(): {path}")
    return verify


def run_hidden_verifier(stage: StageSpec, workspace, final_answer, trace_events=None):
    """Execute evaluator code outside the copied Agent workspace."""
    verify = _load_verify_function(stage.verifier)
    raw = verify(
        Path(workspace).resolve(),
        str(final_answer or ""),
        list(trace_events or []),
    )
    if not isinstance(raw, dict):
        raise ValueError(f"verifier returned a non-object: {stage.verifier}")

    results = []
    for spec in stage.checks:
        value = raw.get(spec.id, {})
        if isinstance(value, bool):
            passed, evidence = value, ""
        elif isinstance(value, dict):
            passed = bool(value.get("passed", False))
            evidence = str(value.get("evidence") or "")[:4000]
        else:
            passed, evidence = False, "verifier omitted this check"
        results.append(
            CheckResult(
                id=spec.id,
                category=spec.category,
                required=spec.required,
                weight=spec.weight,
                passed=passed,
                evidence=evidence,
                agent_feedback=spec.agent_feedback,
                priority=spec.priority,
            )
        )

    category_scores, total_score = _score(results)
    completed = all(
        item.passed
        for item in results
        if item.required and item.category in HARD_COMPLETION_CATEGORIES
    )
    return VerificationReport(
        checks=tuple(results),
        category_scores=category_scores,
        total_score=total_score,
        completed=completed,
    )
