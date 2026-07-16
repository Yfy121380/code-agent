# 上下文管理共享类型与配置。
# 本文件集中定义上下文预算、分层顺序、裁剪占位文本等常量。
# 同时提供承载分层渲染结果和 messages 构建结果的数据结构。
# 这些结构被上下文主流程和历史处理逻辑共同使用。

from dataclasses import dataclass


DEFAULT_TOTAL_BUDGET = 128000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 10000,
    "skills": 6000,
    "memory": 16000,
    "relevant_memory": 14000,
    "history": 82000,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 5000,
    "skills": 1000,
    "memory": 3000,
    "relevant_memory": 3000,
    "history": 20000,
}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "skills", "memory", "prefix")
SECTION_ORDER = ("prefix", "skills", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3
OMITTED_TOOL_RESULT = "[tool result omitted due to context budget]"
OLD_TOOL_RESULT_CLEARED = "Old tool result content cleared."
MAX_RECENT_OBSERVATION_TOOL_RESULTS = 20

def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."

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
