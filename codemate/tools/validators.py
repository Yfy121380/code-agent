# 工具参数校验：在工具真正执行前检查参数、路径边界和审批门禁。

import ipaddress
from datetime import datetime
from urllib.parse import urlparse

from .constants import (
    GREP_MODES,
    MAX_GREP_CONTEXT_LINES,
    WEB_EXTRACT_DEPTHS,
    WEB_EXTRACT_FORMATS,
    WEB_RESEARCH_MODELS,
    WEB_RESEARCH_OUTPUT_LENGTHS,
    WEB_SEARCH_DEPTHS,
    WEB_SEARCH_TOPICS,
    WEB_TIME_RANGES,
    WEB_TOOL_NAMES,
)
from .mcp import is_mcp_tool_name
from .path_policy import ToolGate, ToolPolicyError, gate_for_access, gate_for_mcp, gate_for_web, resolve_tool_path
from .shell_safety import analyze_shell_command
from .images import path_has_image_extension
from .todos import normalize_todos


def _validate_string_array(args, name, limit=20):
    value = args.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    if len(value) > limit:
        raise ValueError(f"{name} must contain at most {limit} items")
    cleaned = []
    for index, item in enumerate(value):
        text = str(item).strip()
        if not text:
            raise ValueError(f"{name}[{index}] must not be empty")
        cleaned.append(text)
    return cleaned


def _validate_date(value, name):
    if not value:
        return
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{name} must be in YYYY-MM-DD format") from exc


def _validate_public_http_url(value):
    parsed = urlparse(str(value))
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("web_extract URLs must use http or https")
    if not parsed.hostname:
        raise ValueError("web_extract URL must include a hostname")
    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise ValueError("web_extract refuses localhost URLs")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved:
        raise ValueError("web_extract refuses private, loopback, link-local, multicast, or reserved IP URLs")


def _validate_web_search(args):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query must not be empty")
    if len(query) > 1000:
        raise ValueError("query must be <= 1000 characters")
    max_results = int(args.get("max_results", 5))
    if max_results < 1 or max_results > 20:
        raise ValueError("max_results must be in [1, 20]")
    search_depth = str(args.get("search_depth", "basic"))
    if search_depth not in WEB_SEARCH_DEPTHS:
        raise ValueError("search_depth must be one of: basic, advanced, fast, ultra-fast")
    topic = str(args.get("topic", "general"))
    if topic not in WEB_SEARCH_TOPICS:
        raise ValueError("topic must be one of: general, news, finance")
    time_range = args.get("time_range")
    if time_range and str(time_range) not in WEB_TIME_RANGES:
        raise ValueError("time_range must be one of: day, week, month, year")
    _validate_date(args.get("start_date"), "start_date")
    _validate_date(args.get("end_date"), "end_date")
    if time_range and (args.get("start_date") or args.get("end_date")):
        raise ValueError("time_range cannot be combined with start_date or end_date")
    _validate_string_array(args, "include_domains")
    _validate_string_array(args, "exclude_domains")


def _validate_web_extract(args):
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("urls must be a non-empty list")
    if len(urls) > 20:
        raise ValueError("urls must contain at most 20 items")
    for item in urls:
        _validate_public_http_url(item)
    extract_depth = str(args.get("extract_depth", "basic"))
    if extract_depth not in WEB_EXTRACT_DEPTHS:
        raise ValueError("extract_depth must be one of: basic, advanced")
    output_format = str(args.get("format", "markdown"))
    if output_format not in WEB_EXTRACT_FORMATS:
        raise ValueError("format must be one of: markdown, text")
    query = str(args.get("query", "")).strip()
    if len(query) > 1000:
        raise ValueError("query must be <= 1000 characters")
    chunks = int(args.get("chunks_per_source", 3))
    if chunks < 1 or chunks > 5:
        raise ValueError("chunks_per_source must be in [1, 5]")
    timeout = int(args.get("timeout", 30))
    if timeout < 5 or timeout > 120:
        raise ValueError("timeout must be in [5, 120]")


def _validate_web_research(args):
    research_input = str(args.get("input", "")).strip()
    if not research_input:
        raise ValueError("input must not be empty")
    if len(research_input) > 4000:
        raise ValueError("input must be <= 4000 characters")
    model = str(args.get("model", "auto"))
    if model not in WEB_RESEARCH_MODELS:
        raise ValueError("model must be one of: mini, pro, auto")
    output_length = str(args.get("output_length", "standard"))
    if output_length not in WEB_RESEARCH_OUTPUT_LENGTHS:
        raise ValueError("output_length must be one of: short, standard, long")
    _validate_string_array(args, "include_domains")
    _validate_string_array(args, "exclude_domains")


def validate_tool(agent, name, args):
    # 所有工具执行前都会经过这里。
    # 这里不执行实际动作，但会完成参数校验、路径硬边界校验和审批门禁判断。
    # deny 在这里直接抛出；返回值只可能是 allow/ask gate。
    args = args or {}

    if is_mcp_tool_name(name):
        return gate_for_mcp(agent)

    if name in WEB_TOOL_NAMES:
        if name == "web_search":
            _validate_web_search(args)
        elif name == "web_extract":
            _validate_web_extract(args)
        else:
            _validate_web_research(args)
        return gate_for_web(agent)

    if name == "list_files":
        decision = resolve_tool_path(agent, args.get("path", "."), access="read")
        if not decision.path.is_dir():
            raise ValueError("path is not a directory")
        return gate_for_access(agent, "read", [decision])

    if name == "read_file":
        decision = resolve_tool_path(agent, args["path"], access="read")
        if not decision.path.is_file():
            raise ValueError("path is not a file")
        if not path_has_image_extension(decision.path) and not bool(args.get("read_all", False)):
            start = int(args.get("start", 1))
            end = int(args.get("end", 200))
            if start < 1 or end < start:
                raise ValueError("invalid line range")
        return gate_for_access(agent, "read", [decision])

    if name == "grep":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        decision = resolve_tool_path(agent, args.get("path", "."), access="read")
        if not decision.path.exists():
            raise ValueError("path does not exist")
        if not (decision.path.is_file() or decision.path.is_dir()):
            raise ValueError("path must be a file or directory")
        mode = str(args.get("mode", "content"))
        if mode not in GREP_MODES:
            raise ValueError("mode must be one of: files_with_matches, count, content")
        before = int(args.get("before", 0))
        after = int(args.get("after", 0))
        context = int(args.get("context", 0))
        if before < 0 or after < 0 or context < 0:
            raise ValueError("before, after, and context must be non-negative")
        if before > MAX_GREP_CONTEXT_LINES or after > MAX_GREP_CONTEXT_LINES or context > MAX_GREP_CONTEXT_LINES:
            raise ValueError(f"before, after, and context must be <= {MAX_GREP_CONTEXT_LINES}")
        if mode != "content" and (before or after or context):
            raise ValueError("before/after/context are only valid when mode='content'")
        return gate_for_access(agent, "read", [decision])

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        analysis = analyze_shell_command(agent, command)
        agent._last_shell_analysis = analysis
        if analysis.blocked:
            if any(
                reason in analysis.reasons
                for reason in {"blocked_dangerous_target", "wildcard_write", "hard_blocked_shell_command"}
            ):
                raise ToolPolicyError(analysis.error, code="shell_blocked", security_event_type="shell_blocked")
            raise ValueError(analysis.error)
        access = "read" if analysis.kind == "read" else "write"
        path_decisions = [resolve_tool_path(agent, path, access=access) for path in analysis.paths if str(path).strip() != "-"]
        if analysis.kind == "dangerous":
            return gate_for_access(agent, "dangerous", path_decisions)
        if analysis.kind == "unknown":
            return gate_for_access(agent, "unknown", path_decisions)
        return gate_for_access(agent, access, path_decisions)

    if name == "write_file":
        decision = resolve_tool_path(agent, args["path"], access="write")
        if decision.path.exists() and decision.path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        mode = str(args.get("mode", "overwrite"))
        if mode not in {"overwrite", "append"}:
            raise ValueError("mode must be one of: overwrite, append")
        return gate_for_access(agent, "write", [decision])

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        decision = resolve_tool_path(agent, args["path"], access="write")
        if not decision.path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = decision.path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return gate_for_access(agent, "write", [decision])

    if name == "todo_write":
        normalize_todos(args.get("todos"))
        return ToolGate("allow", "session_update")

    if name == "todo_list":
        if args:
            raise ValueError("todo_list does not accept arguments")
        return ToolGate("allow", "session_read")

    if name == "skill_load":
        skill_name = agent.normalize_skill_name(args.get("name"))
        if not agent.skill_file(skill_name).is_file():
            raise ValueError(f"skill not found: {skill_name}")
        return ToolGate("allow", "session_update")

    if name == "skill_unload":
        skill_name = agent.normalize_skill_name(args.get("name"))
        if skill_name not in agent.invoked_skill_names():
            raise ValueError(f"skill was not invoked: {skill_name}")
        return ToolGate("allow", "session_update")

    if name == "delegate":
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list")
        if len(tasks) > 3:
            raise ValueError("tasks must contain at most 3 items")
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                raise ValueError(f"tasks[{index}] must be an object")
            task = str(item.get("task", "")).strip()
            if not task:
                raise ValueError(f"tasks[{index}].task must not be empty")
            if "focus" in item and not isinstance(item.get("focus"), str):
                raise ValueError(f"tasks[{index}].focus must be a string")
        max_steps = int(args.get("max_steps", 20))
        if max_steps < 1 or max_steps > 40:
            raise ValueError("max_steps must be in [1, 40]")
        return ToolGate("allow", "delegate_read_only")
