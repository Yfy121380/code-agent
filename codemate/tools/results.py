"""工具返回结构。

大多数工具只返回文本；图片读取这类多模态结果需要额外携带结构化 block。
runtime 仍把短文本作为普通 tool result 保存，把 block 交给模型适配层按需转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolRunOutput:
    """工具执行结果的内部统一表示。"""

    content: str
    content_blocks: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def normalize_tool_output(value):
    """把旧的字符串工具结果和新的结构化工具结果统一成 ToolRunOutput。"""

    if isinstance(value, ToolRunOutput):
        return value
    return ToolRunOutput(content=str(value))
