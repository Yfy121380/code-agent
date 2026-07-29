# 模型能力表：集中维护上下文窗口、视觉输入和推理参数支持情况。
# provider 适配层只读取这里的能力，不在请求代码里散落模型名判断。

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    context_tokens: int
    supports_streaming: bool = False
    supports_images: bool = False
    supports_reasoning: bool = False
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None


PROVIDER_MODELS = {
    "openai": ("gpt-5.4", "gpt-5.5"),
    "anthropic": ("claude-sonnet-4-6", "claude-opus-4-8"),
    "deepseek": ("deepseek-v4-pro",),
}

DEFAULT_PROVIDER_MODELS = {
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
    "deepseek": "deepseek-v4-pro",
}

DEFAULT_CONTEXT_TOKENS = 258_000
DEFAULT_MODEL_CAPABILITY = ModelCapability(context_tokens=DEFAULT_CONTEXT_TOKENS)

MODEL_CAPABILITIES = {
    "gpt-5.4": ModelCapability(
        context_tokens=258_000,
        supports_streaming=True,
        supports_images=True,
        supports_reasoning=True,
        openai_reasoning_effort="high",
    ),
    "gpt-5.5": ModelCapability(
        context_tokens=258_000,
        supports_streaming=True,
        supports_images=True,
        supports_reasoning=True,
        openai_reasoning_effort="high",
    ),
    "claude-sonnet-4-6": ModelCapability(
        context_tokens=500_000,
        supports_streaming=True,
        supports_images=True,
        supports_reasoning=True,
        anthropic_effort="high",
    ),
    "claude-opus-4-8": ModelCapability(
        context_tokens=500_000,
        supports_streaming=True,
        supports_images=True,
        supports_reasoning=True,
        anthropic_effort="high",
    ),
    "deepseek-v4-pro": ModelCapability(
        context_tokens=258_000,
        supports_streaming=True,
        supports_images=False,
        supports_reasoning=False,
    ),
}


def model_capability(model):
    return MODEL_CAPABILITIES.get(str(model or ""), DEFAULT_MODEL_CAPABILITY)


def model_context_tokens(model):
    return int(model_capability(model).context_tokens)


def models_for_provider(provider):
    return list(PROVIDER_MODELS.get(str(provider or ""), ()))


def default_model_for_provider(provider, fallback=""):
    return DEFAULT_PROVIDER_MODELS.get(str(provider or ""), fallback)
