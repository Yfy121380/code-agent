"""终端交互界面模块。

这个包提供 runtime 可以调用的 UI 接口，负责把模型思考、工具调用、
审批请求和最终回答展示给用户。核心逻辑不依赖具体终端样式，
因此测试、批处理和未来其他界面都可以替换不同的 UI 实现。
"""

from .terminal import NullUI, TerminalUI

__all__ = ["NullUI", "TerminalUI"]
