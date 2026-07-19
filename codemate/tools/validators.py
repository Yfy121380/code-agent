# 工具参数校验：在工具真正执行前检查参数、路径边界和审批门禁。

from .constants import GREP_MODES, MAX_GREP_CONTEXT_LINES, TODO_STATUSES
from .mcp import is_mcp_tool_name
from .path_policy import ToolGate, ToolPolicyError, gate_for_access, gate_for_mcp, resolve_tool_path
from .shell_safety import analyze_shell_command


def _normalize_todos(raw_todos):
    # todo_write 的结构化参数清洗入口。
    # 工具执行和参数校验都复用这段逻辑，保证 session 中只保存规范字段。
    if not isinstance(raw_todos, list):
        raise ValueError("todos must be a list")
    todos = []
    in_progress_phases = 0
    for phase_index, item in enumerate(raw_todos):
        if not isinstance(item, dict):
            raise ValueError(f"todo phase at index {phase_index} must be an object")
        phase = str(item.get("phase", "")).strip()
        if not phase:
            raise ValueError(f"todo phase at index {phase_index} phase must not be empty")
        status = str(item.get("status", "")).strip()
        if status not in TODO_STATUSES:
            raise ValueError(f"todo phase at index {phase_index} status must be one of: pending, in_progress, completed")
        if status == "in_progress":
            in_progress_phases += 1
        raw_tasks = item.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ValueError(f"todo phase at index {phase_index} tasks must be a list")

        tasks = []
        in_progress_tasks = 0
        for task_index, task in enumerate(raw_tasks):
            if not isinstance(task, dict):
                raise ValueError(f"todo task at phase {phase_index}, index {task_index} must be an object")
            description = str(task.get("description", "")).strip()
            if not description:
                raise ValueError(f"todo task at phase {phase_index}, index {task_index} description must not be empty")
            task_status = str(task.get("status", "")).strip()
            if task_status not in TODO_STATUSES:
                raise ValueError(
                    f"todo task at phase {phase_index}, index {task_index} status must be one of: pending, in_progress, completed"
                )
            if task_status == "in_progress":
                in_progress_tasks += 1
            if status == "pending" and task_status != "pending":
                raise ValueError("pending phase cannot contain completed or in_progress tasks")
            if status == "completed" and task_status != "completed":
                raise ValueError("completed phase cannot contain pending or in_progress tasks")
            if task_status == "in_progress" and status != "in_progress":
                raise ValueError("phase must be in_progress when one of its tasks is in_progress")
            tasks.append({"description": description, "status": task_status})
        if in_progress_tasks > 1:
            raise ValueError("at most one task may be in_progress within the same phase")
        todos.append({"phase": phase, "status": status, "tasks": tasks})
    if in_progress_phases > 1:
        raise ValueError("at most one phase may be in_progress")
    return todos


def validate_tool(agent, name, args):
    # 所有工具执行前都会经过这里。
    # 这里不执行实际动作，但会完成参数校验、路径硬边界校验和审批门禁判断。
    # deny 在这里直接抛出；返回值只可能是 allow/ask gate。
    args = args or {}

    if is_mcp_tool_name(name):
        return gate_for_mcp(agent)

    if name == "list_files":
        decision = resolve_tool_path(agent, args.get("path", "."), access="read")
        if not decision.path.is_dir():
            raise ValueError("path is not a directory")
        return gate_for_access(agent, "read", [decision])

    if name == "read_file":
        decision = resolve_tool_path(agent, args["path"], access="read")
        if not decision.path.is_file():
            raise ValueError("path is not a file")
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
            if any(reason in analysis.reasons for reason in {"blocked_dangerous_target", "wildcard_write"}):
                raise ToolPolicyError(analysis.error, code="shell_blocked", security_event_type="shell_blocked")
            raise ValueError(analysis.error)
        access = "read" if analysis.kind == "read" else "write"
        path_decisions = [resolve_tool_path(agent, path, access=access) for path in analysis.paths if str(path).strip() != "-"]
        if analysis.kind == "dangerous":
            return gate_for_access(agent, "dangerous", path_decisions)
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
        _normalize_todos(args.get("todos"))
        return ToolGate("allow", "session_update")

    if name == "skill_load":
        skill_name = agent.normalize_skill_name(args.get("name"))
        if skill_name in agent.active_skill_names():
            raise ValueError(f"skill already active: {skill_name}")
        if not agent.skill_file(skill_name).is_file():
            raise ValueError(f"skill not found: {skill_name}")
        return ToolGate("allow", "session_update")

    if name == "skill_unload":
        skill_name = agent.normalize_skill_name(args.get("name"))
        if skill_name not in agent.active_skill_names():
            raise ValueError(f"skill is not active: {skill_name}")
        return ToolGate("allow", "session_update")

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return ToolGate("allow", "delegate_read_only")
