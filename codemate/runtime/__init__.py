# 运行时包入口。
# 对外保持 `from codemate.runtime import CodeMate` 的稳定导入方式。
# 具体实现按职责拆分到 agent、loop、tool_execution、approvals 和 dream 模块中。

from .agent import CodeMate, MiniAgent, PromptPrefix

__all__ = ["CodeMate", "MiniAgent", "PromptPrefix"]
