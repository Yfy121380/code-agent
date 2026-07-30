# 上下文管理共享类型与配置。
# 本文件只保留上下文组装和 history compact 需要的稳定常量。
# 旧的“按总字符预算逐层裁剪”逻辑已经移除；其他上下文层依靠自身规则控制大小，
# history 则通过 recent 原文保留和旧消息摘要来控制规模。

from dataclasses import dataclass


SECTION_ORDER = (
    "prefix",
    "skills",
    "runtime_context",
    "relevant_memory",
    "history_summary",
    "history",
    "current_request",
)
CURRENT_REQUEST_SECTION = "current_request"
HISTORY_SUMMARY_SECTION = "history_summary"
LONG_TERM_MEMORY_SOURCES = ("user_profile", "feedback_workflow", "project_context")
RELEVANT_MEMORY_LIMIT = 20
OLD_TOOL_RESULT_CLEARED = "Old tool result content cleared."
MAX_RECENT_OBSERVATION_TOOL_RESULTS = 50
RECENT_HISTORY_MIN_MESSAGES = 20
RECENT_HISTORY_MIN_CHARS = 50_000
MAX_COMPACT_RETRIES = 3
INTERNAL_CONTEXT_MESSAGE_KINDS = frozenset(
    {
        "skill_context",
        "todo_context",
        "plan_context",
        "todo_invalidated_context",
    }
)
HISTORY_SUMMARY_SECTIONS = (
    "Working Directory",
    "User Preferences And Constraints",
    "Current State",
    "Key Decisions",
    "Changed Files",
    "Validation And Issues",
)

@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


@dataclass
class MessageBuild:
    system: str
    messages: list[dict]
    metadata: dict
