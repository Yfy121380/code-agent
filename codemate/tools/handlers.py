# 工具执行函数：实现 list/read/grep/shell/write/patch/todo/delegate 等工具的实际动作。

import copy
import re
import shutil
import subprocess
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import memory as memorylib
from ..workspace import IGNORED_PATH_NAMES, now
from ..memory.long_term import is_memory_path
from .constants import BINARY_SNIFF_BYTES, LIST_FILE_LINE_COUNT_MAX_BYTES, TODO_STATUSES
from .images import image_media_type_for_file, path_has_image_extension, prepare_image_read_result, sniff_image_media_type
from .sandbox import build_shell_sandbox_command, sandbox_enabled, sandbox_preflight_error
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
    for root in (agent.paths.user_skills, agent.paths.project_skills):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _allow_internal_tree(agent, path):
    return _allow_memory_tree(agent, path) or _allow_skill_tree(agent, path)


def _file_listing_detail(path):
    """为 list_files 生成轻量文件规模提示，帮助模型决定是否分段读取。"""
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable file"
    try:
        with path.open("rb") as handle:
            sample = handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return "unreadable file"
    if sniff_image_media_type(sample):
        return "image file"
    if b"\x00" in sample:
        return "binary file"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "binary file"
    if size > LIST_FILE_LINE_COUNT_MAX_BYTES:
        return "large file"

    line_count = 0
    saw_bytes = False
    ends_with_newline = False
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                saw_bytes = True
                line_count += chunk.count(b"\n")
                ends_with_newline = chunk.endswith(b"\n")
    except OSError:
        return "unreadable file"
    if saw_bytes and not ends_with_newline:
        line_count += 1
    return f"{line_count} lines"


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    if _path_is_under_ignored_dir(agent, path) and not _allow_internal_tree(agent, path):
        raise ValueError("path is ignored; only codemate memory and skills directories may be listed explicitly")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES or _allow_internal_tree(agent, item)
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        detail = f"  {_file_listing_detail(entry)}" if entry.is_file() else ""
        lines.append(f"{kind} {_display_path(agent, entry)}{detail}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    if image_media_type_for_file(path):
        if not bool(getattr(agent.model_client, "supports_images", False)):
            raise ValueError("current model does not support image input; switch to a model with image support before reading image files")
        return prepare_image_read_result(agent, path, _display_path(agent, path))
    if path_has_image_extension(path):
        raise ValueError("file extension looks like an image, but the file is not a supported or valid image")
    read_all = bool(args.get("read_all", False))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if read_all:
        selected = enumerate(lines, start=1)
    else:
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        selected = enumerate(lines[start - 1:end], start=start)
    body = "\n".join(f"{number:>4}: {line}" for number, line in selected)
    return f"# {_display_path(agent, path)}\n{body}"


def _grep_context_args(args):
    context = int(args.get("context", 0))
    before = int(args["before"]) if "before" in args else context
    after = int(args["after"]) if "after" in args else context
    return before, after


def _grep_files(agent, path):
    if _path_is_under_ignored_dir(agent, path) and not _allow_internal_tree(agent, path):
        raise ValueError("path is ignored; only codemate memory and skills directories may be searched explicitly")
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
        raise ValueError("path is ignored; only codemate memory and skills directories may be searched explicitly")
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
    # sandbox.enabled 为 true 时，非 full 模式会在 bwrap 中执行，作为路径校验之外的第二层防线。
    # full 用于本地测试和完全信任场景，审批和沙箱都不拦截命令。
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    run_args = command
    shell = True
    if sandbox_enabled(agent) and str(getattr(agent, "approval_policy", "")) != "full":
        preflight_error = sandbox_preflight_error()
        if preflight_error:
            return textwrap.dedent(
                f"""\
                exit_code: 126
                stdout:
                (empty)
                stderr:
                {preflight_error}
                """
            ).strip()
        run_args = build_shell_sandbox_command(agent, command)
        shell = False
    result = subprocess.run(
        run_args,
        cwd=agent.root,
        shell=shell,
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
            },
        )
    agent.session_store.save(agent.session)
    return f"skill loaded: {skill['name']} ({skill['root']})"


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
                "reason": reason,
            },
        )
    agent.session_store.save(agent.session)
    return f"skill unloaded: {removed['name']}" + (f" ({reason})" if reason else "")


DELEGATE_ALLOWED_TOOLS = {"list_files", "read_file", "grep", "web_search", "web_extract", "todo_write"}


def _delegate_prompt(task, focus):
    focus_text = focus or "No specific focus was provided. Use the task to choose the smallest useful search scope."
    return textwrap.dedent(
        f"""\
        You are a delegated investigation agent for codemate.

        Your job is to perform a focused read-only investigation and report useful findings back to the main agent. You are not responsible for making edits, running verification for edits, or giving the final user-facing answer.

        The main agent will use your report as supporting evidence. It may inspect target files again before editing. Your goal is to reduce noisy exploration and point the main agent toward the most relevant evidence.

        Investigation task:
        {task}

        Focus:
        {focus_text}

        Guidelines:
        - Stay focused on the investigation task.
        - Prefer direct evidence from files, command output, or web sources over speculation.
        - Use tools only when they help answer the investigation task.
        - Do not modify files.
        - Do not run risky shell commands.
        - Do not investigate unrelated areas.
        - Stop once you have enough evidence to give a useful report.
        - Keep the report concise but specific enough for the main agent to act on.
        - Mention relevant files, functions, commands, URLs, or uncertainties when they matter.
        """
    ).strip()


def _delegate_child_session(agent, index, task):
    # 子 agent 使用独立 session 和 run 目录，避免污染父 history。
    # temporary_permissions 继承父会话，保证已审批的外部读权限在调查中仍然生效。
    timestamp = now()
    return {
        "id": f"delegate-{agent.session['id']}-{index}-{uuid.uuid4().hex[:6]}",
        "created_at": timestamp,
        "updated_at": timestamp,
        "title": "delegate investigation",
        "title_slug": "delegate-investigation",
        "workspace_root": agent.workspace.repo_root,
        "history": [],
        "history_summary": "",
        "memory": memorylib.default_memory_state(),
        "todos": [],
        "active_skills": [],
        "temporary_permissions": copy.deepcopy(agent.session.get("temporary_permissions", {})),
        "delegate_parent_session": agent.session.get("id", ""),
        "delegate_task": task,
    }


def _run_delegate_task(agent, item, index, max_steps):
    from ..runtime import CodeMate

    task = str(item.get("task", "")).strip()
    focus = str(item.get("focus", "")).strip()
    child_depth = agent.depth + 1
    fork_model_client = getattr(agent.model_client, "fork", None)
    model_client = fork_model_client() if callable(fork_model_client) else agent.model_client
    child = CodeMate(
        model_client=model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        session=_delegate_child_session(agent, index, task),
        approval_policy="read_only",
        max_steps=max_steps,
        max_new_tokens=agent.max_new_tokens,
        depth=child_depth,
        max_depth=child_depth,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        feature_flags={
            **agent.feature_flags,
            "memory_dream": False,
            "long_term_memory": False,
            "relevant_memory": False,
            "session_title": False,
        },
        allowed_tools=DELEGATE_ALLOWED_TOOLS,
        timezone_name=getattr(agent, "timezone_name", "Asia/Shanghai"),
    )
    child.memory.set_task_summary(task if not focus else f"{task}\nFocus: {focus}")
    child.session["memory"] = child.memory.to_dict()
    try:
        report = child.ask(_delegate_prompt(task, focus))
        status = "ok"
        error = ""
    except Exception as exc:
        report = ""
        status = "error"
        error = str(exc)
    finally:
        child.close()
    return {
        "index": index,
        "task": task,
        "focus": focus,
        "status": status,
        "report": report,
        "error": error,
        "chars": len(report or error),
        "session_id": child.session.get("id", ""),
        "run_dir": str(getattr(child, "current_run_dir", "") or ""),
    }


def tool_delegate(agent, args):
    # delegate 并发创建若干只读调查子 agent。
    # 它只把最终调查报告交回主 agent，中间工具流水留在各自 child trace 中。
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    tasks = list(args.get("tasks") or [])
    max_steps = int(args.get("max_steps", 20))
    if not tasks:
        raise ValueError("tasks must be a non-empty list")
    if len(tasks) > 3:
        raise ValueError("tasks must contain at most 3 items")
    if max_steps < 1 or max_steps > 40:
        raise ValueError("max_steps must be in [1, 40]")

    results = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {
            executor.submit(_run_delegate_task, agent, item, index, max_steps): index
            for index, item in enumerate(tasks, 1)
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                index = futures[future]
                item = tasks[index - 1]
                results.append(
                    {
                        "index": index,
                        "task": str(item.get("task", "")).strip(),
                        "focus": str(item.get("focus", "")).strip(),
                        "status": "error",
                        "report": "",
                        "error": str(exc),
                        "chars": len(str(exc)),
                        "session_id": "",
                        "run_dir": "",
                    }
                )
    results.sort(key=lambda item: item["index"])
    failed = sum(1 for item in results if item["status"] != "ok")
    agent._last_delegate_metadata = {
        "delegate_status": "ok" if failed == 0 else ("error" if failed == len(results) else "partial_error"),
        "delegate_task_count": len(results),
        "delegate_tasks": [
            {
                "index": item["index"],
                "status": item["status"],
                "chars": item["chars"],
                "session_id": item["session_id"],
                "run_dir": item["run_dir"],
            }
            for item in results
        ],
    }

    lines = ["delegate_result:"]
    for item in results:
        lines.extend(
            [
                "",
                f"Task {item['index']}: {item['task']}",
                f"Focus: {item['focus'] or '(none)'}",
                f"Status: {item['status']}",
            ]
        )
        if item["session_id"]:
            lines.append(f"Child session: {item['session_id']}")
        if item["run_dir"]:
            lines.append(f"Child run: {item['run_dir']}")
        if item["status"] == "ok":
            lines.extend(["Report:", item["report"].strip() or "(empty)"])
        else:
            lines.extend(["Error:", item["error"] or "unknown error"])
    return "\n".join(lines)


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
