"""Create bounded user feedback without exposing hidden verifier evidence."""

from __future__ import annotations

from .verification import VerificationReport


CATEGORY_ORDER = {
    "functional": 0,
    "regression": 1,
    "safety": 2,
    "instruction": 3,
    "preference": 4,
    "quality": 5,
}


def build_round_feedback(report: VerificationReport, *, max_items=3):
    """Render only reviewed agent_feedback strings, never private evidence."""
    failures = [item for item in report.checks if not item.passed]
    failures.sort(
        key=lambda item: (
            not item.required,
            CATEGORY_ORDER.get(item.category, 99),
            item.priority,
            item.id,
        )
    )
    selected = failures[: max(1, int(max_items))]
    if not selected:
        return "当前检查已经通过，请保持现有正确行为。"
    lines = ["当前结果仍未完全满足要求：", ""]
    lines.extend(f"- {item.agent_feedback}" for item in selected)
    lines.extend(
        ["", "请根据这些现象继续调查和修正，不要修改隐藏或公开测试来绕过问题。"]
    )
    return "\n".join(lines)
