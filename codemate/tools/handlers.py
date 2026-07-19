# 工具执行函数：实现 list/read/grep/shell/write/patch/todo/delegate 等工具的实际动作。

import re
import shutil
import subprocess
import textwrap

from ..workspace import IGNORED_PATH_NAMES
from ..memory.long_term import is_memory_path
from .constants import TODO_STATUSES
from .validators import _normalize_todos
from .web import tool_web_extract, tool_web_research, tool_web_search


def _path_is_under_ignored_dir(agent, path):
    try:
        parts = path.relative_to(agent.root).parts
    except ValueError:
        return False
    return any(part in IGNORED_PATH_NAMES for part in parts)


def _display_path(agent, path):
    try:
        return str(path.relative_to(agent.root))
    except ValueError:
        try:
            return "~/" + str(path.relative_to(path.home()))
        except ValueError:
            return str(path)


def _allow_memory_tree(agent, path):
    return is_memory_path(agent.root, path)


def _allow_skill_tree(agent, path):
    try:
        path.relative_to(agent.skills_root())
        return True
    except ValueError:
        return False


def _allow_internal_tree(agent, path):
    return _allow_memory_tree(agent, path) or _allow_skill_tree(agent, path)


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    if _path_is_under_ignored_dir(agent, path) and not _allow_internal_tree(agent, path):
        raise ValueError("path is ignored; only .codemate/memory and .codemate/skills may be listed explicitly")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES or _allow_internal_tree(agent, item)
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {_display_path(agent, entry)}")
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
    return f"# {_display_path(agent, path)}\n{body}"


def _grep_context_args(args):
    context = int(args.get("context", 0))
    before = int(args["before"]) if "before" in args else context
    after = int(args["after"]) if "after" in args else context
    return before, after


def _grep_files(agent, path):
    if _path_is_under_ignored_dir(agent, path) and not _allow_internal_tree(agent, path):
        raise ValueError("path is ignored; only .codemate/memory and .codemate/skills may be searched explicitly")
    if path.is_file():
        return [path]
    allow_internal = _allow_internal_tree(agent, path)
    return [
        item for item in path.rglob("*")
        if item.is_file()
        and (allow_internal or not _path_is_under_ignored_dir(agent, item))
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
    if _path_is_under_ignored_dir(agent, path) and not _allow_internal_tree(agent, path):
        raise ValueError("path is ignored; only .codemate/memory and .codemate/skills may be searched explicitly")
    command = ["rg", "--smart-case"]
    if _allow_internal_tree(agent, path):
        command.append("--hidden")
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
                matches.append(_display_path(agent, file_path))
        return "\n".join(matches) or "(no matches)"

    if mode == "count":
        counts = []
        total = 0
        for file_path in files:
            count = 0
            for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
                count += len(regex.findall(line))
            if count:
                counts.append((_display_path(agent, file_path), count))
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
                matches.append(f"{_display_path(agent, file_path)}{separator}{emit_index + 1}{separator}{lines[emit_index]}")
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
        return f"appended {_display_path(agent, path)} ({len(content)} chars)"
    path.write_text(content, encoding="utf-8")
    return f"wrote {_display_path(agent, path)} ({len(content)} chars)"


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
    return f"patched {_display_path(agent, path)}"


def tool_todo_write(agent, args):
    # todo_write 只更新当前 session 的任务列表，不修改工作区文件。
    # 当所有 phase 都完成时直接清空，避免已完成计划长期污染 working memory。
    todos = _normalize_todos(args.get("todos"))
    if not todos:
        agent.session["todos"] = []
        return "todos updated: todo list cleared."
    phase_counts = {status: sum(1 for phase in todos if phase["status"] == status) for status in TODO_STATUSES}
    task_count = sum(len(phase["tasks"]) for phase in todos)
    task_counts = {
        status: sum(1 for phase in todos for task in phase["tasks"] if task["status"] == status)
        for status in TODO_STATUSES
    }
    if phase_counts["completed"] == len(todos):
        agent.session["todos"] = []
        return f"todos updated: all phases completed; todo list cleared ({len(todos)} phases, {task_count} tasks)."
    agent.session["todos"] = todos
    return (
        f"todos updated: {len(todos)} phases, {task_count} tasks, "
        f"{phase_counts['in_progress']} phase in_progress, {task_counts['in_progress']} task in_progress, "
        f"{phase_counts['pending']} phases pending, {phase_counts['completed']} phases completed. "
        "Continue working through current_todos."
    )


def tool_skill_load(agent, args):
    skill = agent.load_skill(args.get("name"))
    if getattr(agent, "current_task_state", None) is not None:
        agent.emit_trace(
            agent.current_task_state,
            "skill_loaded",
            {
                "skill": skill["name"],
                "root": skill["root"],
                "source": skill["source"],
            },
        )
    agent.session_store.save(agent.session)
    return f"skill loaded: {skill['name']} ({skill['source']})"


def tool_skill_unload(agent, args):
    removed = agent.unload_skill(args.get("name"), reason=args.get("reason", ""))
    reason = str(removed.get("reason", "")).strip()
    if getattr(agent, "current_task_state", None) is not None:
        agent.emit_trace(
            agent.current_task_state,
            "skill_unloaded",
            {
                "skill": removed["name"],
                "root": removed.get("root", ""),
                "source": removed.get("source", ""),
                "reason": reason,
            },
        )
    agent.session_store.save(agent.session)
    return f"skill unloaded: {removed['name']}" + (f" ({reason})" if reason else "")


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
        feature_flags={
            **agent.feature_flags,
            "memory_dream": False,
            "long_term_memory": False,
            "relevant_memory": False,
        },
        timezone_name=getattr(agent, "timezone_name", "Asia/Shanghai"),
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
    "web_search": tool_web_search,
    "web_extract": tool_web_extract,
    "web_research": tool_web_research,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "todo_write": tool_todo_write,
    "skill_load": tool_skill_load,
    "skill_unload": tool_skill_unload,
}
