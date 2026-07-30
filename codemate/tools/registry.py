# 工具注册表：把工具 schema、风险标记和执行函数绑定成 runtime 可调用的工具集合。

from functools import partial

from .handlers import _TOOL_RUNNERS, tool_delegate, tool_review
from .mcp import build_mcp_tool_registry
from .specs import BASE_TOOL_SPECS, DELEGATE_TOOL_SPEC, PLAN_TOOL_SPECS, REVIEW_TOOL_SPEC


def build_tool_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in {**BASE_TOOL_SPECS, **PLAN_TOOL_SPECS}.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if agent.depth < agent.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
    # Review is a main-agent boundary: review/delegate children cannot recurse
    # into another independent reviewer.
    if agent.depth == 0 and getattr(agent, "runtime_mode", "agent") == "agent":
        tools["review"] = {**REVIEW_TOOL_SPEC, "run": partial(tool_review, agent)}
    allowed_tools = getattr(agent, "allowed_tools", None)
    if allowed_tools is None or any(str(name).startswith("mcp__") for name in allowed_tools):
        tools.update(build_mcp_tool_registry(agent))
    return tools
