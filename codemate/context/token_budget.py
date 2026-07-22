# 上下文 token 预算工具。
# 本文件只负责模型上下文窗口、压缩阈值、usage 状态和预算报告格式。
# 这里不做 history 压缩，也不参与 prompt 渲染，避免预算判断和上下文构造互相耦合。
# runtime 在每次模型请求前读取这里的判断结果，在模型/工具返回后更新 usage。

from __future__ import annotations

import math
import os
from dataclasses import dataclass


DEFAULT_CONTEXT_WINDOW_TOKENS = {
    "gpt-5.4": 128_000,
    "gpt-5.5": 128_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-8": 200_000,
    "deepseek-v4-pro": 128_000,
}
DEFAULT_COMPACT_TRIGGER_RATIO = 0.90
CONTEXT_TOKENS_ENV = "CODEMATE_CONTEXT_TOKENS"
COMPACT_RATIO_ENV = "CODEMATE_COMPACT_TRIGGER_RATIO"


@dataclass
class TokenUsageState:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_result_tokens_added: int = 0
    estimated_total_context_tokens: int = 0

    def to_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_result_tokens_added": self.tool_result_tokens_added,
            "estimated_total_context_tokens": self.estimated_total_context_tokens,
        }


def _positive_int_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _ratio_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if 0 < parsed <= 1 else None


def model_context_tokens(model):
    """返回当前模型的最大上下文 token 数。

    默认表应维护为真实模型规格；`CODEMATE_CONTEXT_TOKENS` 只作为调试覆盖，
    方便把上下文窗口临时调小来测试预算判断。
    """
    override = _positive_int_env(CONTEXT_TOKENS_ENV)
    if override is not None:
        return override
    return int(DEFAULT_CONTEXT_WINDOW_TOKENS.get(str(model or ""), 128_000))


def compact_trigger_ratio():
    return _ratio_env(COMPACT_RATIO_ENV) or DEFAULT_COMPACT_TRIGGER_RATIO


def compact_trigger_tokens(model):
    return int(model_context_tokens(model) * compact_trigger_ratio())


def rough_token_estimate(text):
    # 中文和混合文本下 len/4 偏乐观，这里用 len/3 做工具结果增量估算。
    return max(0, int(math.ceil(len(str(text or "")) / 3)))


def usage_from_metadata(metadata):
    metadata = dict(metadata or {})
    input_tokens = _safe_int(metadata.get("input_tokens"))
    output_tokens = _safe_int(metadata.get("output_tokens"))
    total_tokens = _safe_int(metadata.get("total_tokens"))
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return TokenUsageState(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_result_tokens_added=0,
        estimated_total_context_tokens=total_tokens,
    )


def _safe_int(value):
    try:
        if value is None:
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def section_char_report(prompt_metadata):
    sections = dict((prompt_metadata or {}).get("sections", {}) or {})
    values = {
        "prefix": _section_chars(sections, "prefix"),
        "available skills": _section_chars(sections, "skills"),
        "working memory": _section_chars(sections, "memory"),
        "relevant memory": _section_chars(sections, "relevant_memory"),
        "history summary": _section_chars(sections, "history_summary"),
        "recent history": _section_chars(sections, "history"),
    }
    values["total"] = sum(values.values())
    return values


def _section_chars(sections, name):
    section = sections.get(name) or {}
    return _safe_int(section.get("rendered_chars"))


def budget_status(model, usage_state):
    usage_state = usage_state if isinstance(usage_state, TokenUsageState) else TokenUsageState()
    max_tokens = model_context_tokens(model)
    threshold = int(max_tokens * compact_trigger_ratio())
    estimated = int(usage_state.estimated_total_context_tokens)
    return {
        "model": str(model or ""),
        "max_context_tokens": max_tokens,
        "compact_threshold_tokens": threshold,
        "compact_threshold_ratio": compact_trigger_ratio(),
        "estimated_current_context_tokens": estimated,
        "usage_ratio": (estimated / max_tokens) if max_tokens > 0 else 0.0,
        "compact_needed": estimated >= threshold if estimated > 0 else False,
    }


def format_budget_report(provider, model, prompt_metadata, usage_state, tool_schema_count=0, tool_schema_chars=0):
    sections = section_char_report(prompt_metadata)
    status = budget_status(model, usage_state)
    usage_percent = status["usage_ratio"] * 100
    ratio_percent = status["compact_threshold_ratio"] * 100
    lines = ["Sections by chars:"]
    for name, chars in sections.items():
        lines.append(f"- {name}: {chars} chars")
    lines.extend(
        [
            "",
            "Tool schemas:",
            f"- tools count: {tool_schema_count}",
            f"- tool schemas: {tool_schema_chars} chars",
            "",
            "Context budget:",
            f"- Model: {provider}:{model}" if provider else f"- Model: {model}",
            f"- Max context: {status['max_context_tokens']} tokens",
            f"- Compact threshold: {status['compact_threshold_tokens']} tokens ({ratio_percent:.0f}%)",
            f"- Estimated current context: {status['estimated_current_context_tokens']} tokens",
            f"- Usage: {usage_percent:.1f}%",
        ]
    )
    return "\n".join(lines)
