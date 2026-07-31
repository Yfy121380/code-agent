# 上下文管理包入口。
# 本文件导出上下文构建所需的公共类型和 ContextManager。
# 运行时通过稳定的 context 包接口构造 prompt 和 messages。
# 包内模块分别负责分层组装、历史处理、token 预算观察和共享数据结构。

from .manager import ContextManager
from .history import repair_incomplete_tool_results
from .token_budget import TokenUsageState, budget_status, format_budget_report
from .types import MessageBuild, SectionRender

__all__ = [
    "ContextManager",
    "MessageBuild",
    "SectionRender",
    "TokenUsageState",
    "budget_status",
    "format_budget_report",
    "repair_incomplete_tool_results",
]
