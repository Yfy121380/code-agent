# 工具参数校验：在工具真正执行前检查参数、路径边界和 shell 风险分析结果。

from .constants import GREP_MODES, MAX_GREP_CONTEXT_LINES, TODO_STATUSES
from .shell_safety import analyze_shell_command


def _normalize_todos(raw_todos):
    # todo_write 的结构化参数清洗入口。
    # 工具执行和参数校验都复用这段逻辑，保证 session 中只保存规范字段。
    if not isinstance(raw_todos, list):
        raise ValueError("todos must be a list")
    todos = []
    in_progress_count = 0
    for index, item in enumerate(raw_todos):
        if not isinstance(item, dict):
            raise ValueError(f"todo at index {index} must be an object")
        content = str(item.get("content", "")).strip()
        if not content:
            raise ValueError(f"todo at index {index} content must not be empty")
        status = str(item.get("status", "")).strip()
        if status not in TODO_STATUSES:
            raise ValueError(f"todo at index {index} status must be one of: pending, in_progress, completed")
        if status == "in_progress":
            in_progress_count += 1
        todos.append({"content": content, "status": status})
    if in_progress_count > 1:
        raise ValueError("at most one todo may be in_progress")
    return todos


def validate_tool(agent, name, args):
    # 所有工具执行前都会经过这里。
    # 这里只做边界和参数合法性检查，不执行实际动作；真正执行交给 handlers。
    args = args or {}

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "grep":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        path = agent.path(args.get("path", "."))
        if not path.exists():
            raise ValueError("path does not exist")
        if not (path.is_file() or path.is_dir()):
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
        return

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
            raise ValueError(analysis.error)
        return

    if name == "write_file":
        path = agent.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        mode = str(args.get("mode", "overwrite"))
        if mode not in {"overwrite", "append"}:
            raise ValueError("mode must be one of: overwrite, append")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "todo_write":
        _normalize_todos(args.get("todos"))
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return
