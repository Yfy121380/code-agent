# 上下文管理包入口。
# 本文件导出上下文构建所需的公共类型和 ContextManager。
# 运行时通过稳定的 context 包接口构造 prompt 和 messages。
# 包内模块分别负责预算裁剪、历史处理和共享数据结构。

from .manager import ContextManager
from .types import MessageBuild, SectionRender

__all__ = ["ContextManager", "MessageBuild", "SectionRender"]
