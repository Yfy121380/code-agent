# 工具系统对外门面：保持 codemate.tools 的稳定入口，内部实现按职责拆到多个模块。

import subprocess

from .constants import GREP_MODES, MAX_GREP_CONTEXT_LINES, TODO_STATUSES, WEB_TOOL_NAMES
from .handlers import (
    tool_delegate,
    tool_grep,
    tool_list_files,
    tool_patch_file,
    tool_read_file,
    tool_run_shell,
    tool_skill_load,
    tool_skill_unload,
    tool_todo_write,
    tool_web_extract,
    tool_web_research,
    tool_web_search,
    tool_write_file,
)
from .registry import build_tool_registry
from .sandbox import build_shell_sandbox_command, sandbox_enabled, sandbox_preflight_error
from .shell_safety import ShellCommandAnalysis, analyze_shell_command
from .specs import BASE_TOOL_SPECS, DELEGATE_TOOL_SPEC
from .validators import validate_tool
from .mcp import close_mcp_connections, is_mcp_tool_name
from .path_policy import ToolGate, ToolPolicyError, gate_for_access, gate_for_mcp, gate_for_web, resolve_tool_path

__all__ = [
    "BASE_TOOL_SPECS",
    "DELEGATE_TOOL_SPEC",
    "GREP_MODES",
    "MAX_GREP_CONTEXT_LINES",
    "TODO_STATUSES",
    "WEB_TOOL_NAMES",
    "ShellCommandAnalysis",
    "ToolGate",
    "ToolPolicyError",
    "analyze_shell_command",
    "build_shell_sandbox_command",
    "build_tool_registry",
    "close_mcp_connections",
    "gate_for_access",
    "gate_for_mcp",
    "gate_for_web",
    "is_mcp_tool_name",
    "resolve_tool_path",
    "sandbox_enabled",
    "sandbox_preflight_error",
    "validate_tool",
    "tool_delegate",
    "tool_grep",
    "tool_list_files",
    "tool_patch_file",
    "tool_read_file",
    "tool_run_shell",
    "tool_skill_load",
    "tool_skill_unload",
    "tool_todo_write",
    "tool_web_extract",
    "tool_web_research",
    "tool_web_search",
    "tool_write_file",
    "subprocess",
]
