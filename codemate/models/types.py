# 模型响应类型：定义 provider 适配层统一返回给 runtime 的结构化结果。

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class ModelToolCall:
    id: str
    name: str
    args: dict = field(default_factory=dict)

    @classmethod
    def create(cls, name, args=None, call_id=None):
        return cls(
            id=str(call_id or f"call_{uuid.uuid4().hex[:12]}"),
            name=str(name),
            args=dict(args or {}),
        )

    def to_dict(self):
        return {"id": self.id, "name": self.name, "args": dict(self.args or {})}


@dataclass
class ModelResponse:
    kind: str
    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    raw: dict | None = None

    @classmethod
    def final(cls, text, metadata=None, raw=None):
        return cls(kind="final", text=str(text or ""), metadata=dict(metadata or {}), raw=raw)

    @classmethod
    def tool_call(cls, name, args=None, call_id=None, text="", metadata=None, raw=None):
        return cls.from_tool_calls(
            [ModelToolCall.create(name, args=args, call_id=call_id)],
            text=text,
            metadata=metadata,
            raw=raw,
        )

    @classmethod
    def from_tool_calls(cls, calls, text="", metadata=None, raw=None):
        normalized = []
        for call in calls or []:
            if isinstance(call, ModelToolCall):
                normalized.append(call)
                continue
            if isinstance(call, dict):
                normalized.append(
                    ModelToolCall.create(
                        call.get("name", ""),
                        args=call.get("args", call.get("input", {})) or {},
                        call_id=call.get("id") or call.get("call_id"),
                    )
                )
        return cls(kind="tool_calls", text=str(text or ""), tool_calls=normalized, metadata=dict(metadata or {}), raw=raw)
