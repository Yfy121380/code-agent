# 工具执行函数：实现 list/read/grep/shell/write/patch/todo/delegate 等工具的实际动作。

import re
import shutil
import subprocess
import textwrap

from ..workspace import IGNORED_PATH_NAMES
from .constants import TODO_STATUSES
from .validators import _normalize_todos


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(agent.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(agent.root)}\n{body}"


def _grep_context_args(args):
    context = int(args.get("context", 0))
    before = int(args["before"]) if "before" in args else context
    after = int(args["after"]) if "after" in args else context
    return before, after


def _grep_files(agent, path):
    if path.is_file():
        return [path]
    return [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(agent.root).parts)
    ]


def _format_grep_count_output(stdout):
    counts = []
    total = 0
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        file_path, separator, raw_count = text.rpartition(":")
        if not separator:
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        counts.append((file_path, count))
        total += count
    if not counts:
        return "total_matches: 0"
    lines = [f"total_matches: {total}"]
    lines.extend(f"{file_path}: {count}" for file_path, count in counts)
    return "\n".join(lines)


def _tool_grep_rg(agent, pattern, path, mode, args):
    command = ["rg", "--smart-case"]
    if mode == "files_with_matches":
        command.append("--files-with-matches")
    elif mode == "count":
        command.extend(["--count-matches", "--with-filename"])
    else:
        before, after = _grep_context_args(args)
        command.extend(["-n", "--with-filename"])
        if before:
            command.extend(["-B", str(before)])
        if after:
            command.extend(["-A", str(after)])
    command.extend(["--", pattern, str(path)])
    result = subprocess.run(
        command,
        cwd=agent.root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return "total_matches: 0" if mode == "count" else "(no matches)"
    if result.returncode > 1:
        return result.stderr.strip() or "error: grep failed"
    if mode == "count":
        return _format_grep_count_output(result.stdout)
    return result.stdout.strip() or "(no matches)"


def _compile_grep_pattern(pattern):
    flags = 0 if any(char.isupper() for char in pattern) else re.IGNORECASE
    return re.compile(pattern, flags)


def _tool_grep_fallback(agent, pattern, path, mode, args):
    try:
        regex = _compile_grep_pattern(pattern)
    except re.error as exc:
        return f"error: invalid regex: {exc}"

    files = _grep_files(agent, path)
    if mode == "files_with_matches":
        matches = []
        for file_path in files:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(regex.search(line) for line in lines):
                matches.append(str(file_path.relative_to(agent.root)))
        return "\n".join(matches) or "(no matches)"

    if mode == "count":
        counts = []
        total = 0
        for file_path in files:
            count = 0
            for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
                count += len(regex.findall(line))
            if count:
                counts.append((str(file_path.relative_to(agent.root)), count))
                total += count
        if not counts:
            return "total_matches: 0"
        lines = [f"total_matches: {total}"]
        lines.extend(f"{file_path}: {count}" for file_path, count in counts)
        return "\n".join(lines)

    before, after = _grep_context_args(args)
    matches = []
    for file_path in files:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        emitted = set()
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            start = max(0, index - before)
            end = min(len(lines), index + after + 1)
            for emit_index in range(start, end):
                if emit_index in emitted:
                    continue
                emitted.add(emit_index)
                separator = ":" if emit_index == index else "-"
                matches.append(f"{file_path.relative_to(agent.root)}{separator}{emit_index + 1}{separator}{lines[emit_index]}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_grep(agent, args):
    # grep 的统一执行入口。
    # 优先使用系统 rg 获得性能和输出一致性；如果环境没有 rg，则退回 Python 实现。
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = agent.path(args.get("path", "."))
    mode = str(args.get("mode", "content"))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        return _tool_grep_rg(agent, pattern, path, mode, args)
    return _tool_grep_fallback(agent, pattern, path, mode, args)


def tool_run_shell(agent, args):
    # shell 命令实际执行入口。
    # 风险识别和审批在 runtime/validators 中已经完成，这里只负责在受控环境中运行命令。
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    result = subprocess.run(
        command,
        cwd=agent.root,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=agent.shell_env(),
    )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    mode = str(args.get("mode", "overwrite"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return f"appended {path.relative_to(agent.root)} ({len(content)} chars)"
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"


def tool_patch_file(agent, args):
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
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(agent.root)}"


def tool_todo_write(agent, args):
    # todo_write 只更新当前 session 的任务列表，不修改工作区文件。
    # 当所有 todo 都完成时直接清空，避免已完成计划长期污染 working memory。
    todos = _normalize_todos(args.get("todos"))
    if not todos:
        agent.session["todos"] = []
        return "todos updated: todo list cleared."
    counts = {status: sum(1 for item in todos if item["status"] == status) for status in TODO_STATUSES}
    if counts["completed"] == len(todos):
        agent.session["todos"] = []
        return f"todos updated: all tasks completed; todo list cleared ({len(todos)} completed)."
    agent.session["todos"] = todos
    return (
        f"todos updated: {len(todos)} items, {counts['in_progress']} in_progress, "
        f"{counts['pending']} pending, {counts['completed']} completed. "
        "Continue working through current_todos."
    )


def tool_delegate(agent, args):
    # delegate 创建一个受限的只读子 agent，用于短程调查。
    # 子 agent 不继承写权限，避免把委派变成绕过主流程审批的执行通道。
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from ..runtime import CodeMate

    child = CodeMate(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        run_store=agent.run_store,
        approval_policy="never",
        max_steps=int(args.get("max_steps", 3)),
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth + 1,
        max_depth=agent.max_depth,
        read_only=True,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
    )
    # 委派的目标是“调查”，不是“放权执行”。
    # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
    child.memory.set_task_summary(task)
    child.session["memory"] = child.memory.to_dict()
    return "delegate_result:\n" + child.ask(task)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep": tool_grep,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "todo_write": tool_todo_write,
}
