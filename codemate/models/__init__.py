# 模型系统对外门面：保持 codemate.models 的稳定入口，内部按 provider 拆分实现。

from .anthropic import AnthropicCompatibleModelClient
from .capabilities import ModelCapability, model_capability
from .fake import FakeModelClient
from .ollama import OllamaModelClient
from .openai import OpenAICompatibleModelClient
from .types import ModelResponse, ModelStreamEvent, ModelToolCall

__all__ = [
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
    "ModelCapability",
    "ModelResponse",
    "ModelStreamEvent",
    "ModelToolCall",
    "OllamaModelClient",
    "OpenAICompatibleModelClient",
    "model_capability",
]
