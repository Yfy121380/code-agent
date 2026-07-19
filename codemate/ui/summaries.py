"""工具调用摘要模块。

这里把结构化工具参数和工具结果转换成人能快速阅读的短文本。
它只关心展示信息的取舍，不执行工具、不做安全判断，也不修改 session。
终端审批和工具执行过程展示都会复用这些摘要，避免完整 JSON 参数糊屏。
"""

import json


MAX_PREVIEW_LINES = 18
MAX_RESULT_LINES = 40
MAX_LINE_CHARS = 160
COMPACT_RESULT_TOOLS = {"list_files", "read_file", "grep", "web_search", "web_extract", "web_research"}


def _clip_line(line, limit=MAX_LINE_CHARS):
    line = str(line)
    if len(line) <= limit:
        return line
    return line[: limit - 3] + "..."


def preview_text(text, max_lines=MAX_PREVIEW_LINES):
    """生成带行号的短预览，主要用于 write_file 和 patch_file 的内容展示。"""
    text = str(text or "")
    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    rendered = []
    for index, line in enumerate(lines[:max_lines], 1):
        rendered.append(f"{index:>4}  {_clip_line(line)}")
    if len(lines) > max_lines:
        rendered.append(f"      ... ({len(lines) - max_lines} more lines)")
    if not rendered:
        rendered.append("      (empty)")
    return "\n".join(rendered)


def compact_json(value, limit=600):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_tool_call(name, args):
    """按工具类型提取最关键参数，避免在终端直接展示完整 tool_call JSON。"""
    args = dict(args or {})
    if name == "todo_write":
        todos = args.get("todos") or []
        if not todos:
            return "todo_write\n  clear todo list"
        lines = ["todo_write"]
        for index, item in enumerate(todos, 1):
            status = str(item.get("status", "")).strip() or "unknown"
            phase = str(item.get("phase", "")).strip()
            lines.append(f"  {index}. [{status}] {_clip_line(phase, 100)}")
            for task in item.get("tasks") or []:
                task_status = str(task.get("status", "")).strip() or "unknown"
                description = str(task.get("description", "")).strip()
                lines.append(f"     - [{task_status}] {_clip_line(description, 100)}")
        return "\n".join(lines)
    if name == "write_file":
        content = str(args.get("content", ""))
        path = args.get("path", "")
        mode = str(args.get("mode", "overwrite"))
        lines = [f"write_file {path}", f"  mode: {mode}", f"  size: {len(content)} chars", "  preview:", preview_text(content)]
        return "\n".join(lines)
    if name == "patch_file":
        path = args.get("path", "")
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        lines = [
            f"patch_file {path}",
            f"  old_text: {len(old_text)} chars",
            preview_text(old_text, max_lines=8),
            f"  new_text: {len(new_text)} chars",
            preview_text(new_text, max_lines=8),
        ]
        return "\n".join(lines)
    if name == "run_shell":
        return f"run_shell\n  $ {args.get('command', '')}"
    if name == "read_file":
        bits = [f"read_file {args.get('path', '')}"]
        if "start" in args or "end" in args:
            bits.append(f"  lines: {args.get('start', '')}-{args.get('end', '')}")
        return "\n".join(bits)
    if name == "grep":
        return "\n".join(
            [
                f"grep {args.get('pattern', '')!r}",
                f"  path: {args.get('path', '.')}",
                f"  mode: {args.get('mode', 'content')}",
            ]
        )
    if name == "web_search":
        return "\n".join(
            [
                f"web_search {_clip_line(args.get('query', ''), 140)!r}",
                f"  max_results: {args.get('max_results', 5)}",
                f"  depth: {args.get('search_depth', 'basic')}",
            ]
        )
    if name == "web_extract":
        urls = args.get("urls") or []
        lines = ["web_extract"]
        for url in urls[:5]:
            lines.append(f"  - {_clip_line(url, 140)}")
        if len(urls) > 5:
            lines.append(f"  ... ({len(urls) - 5} more URLs)")
        return "\n".join(lines)
    if name == "web_research":
        return "\n".join(
            [
                f"web_research {_clip_line(args.get('input', ''), 140)!r}",
                f"  model: {args.get('model', 'auto')}",
                f"  output_length: {args.get('output_length', 'standard')}",
            ]
        )
    if name == "list_files":
        return f"list_files {args.get('path', '.')}"
    if name == "delegate":
        return f"delegate\n  task: {_clip_line(args.get('task', ''), 140)}"
    return f"{name}\n  args: {compact_json(args)}"


def summarize_read_tool_result(name, result, metadata=None):
    """读类工具在终端只展示规模信息，避免把读取内容刷满屏幕。"""
    metadata = dict(metadata or {})
    result = str(result or "")
    status = metadata.get("tool_status", "ok")
    if not result:
        return f"{status}, empty result"
    if result == "(empty)":
        return f"{status}, empty result"
    lines = result.splitlines()
    char_count = len(result)
    if name == "list_files":
        dir_count = sum(1 for line in lines if line.startswith("[D]"))
        file_count = sum(1 for line in lines if line.startswith("[F]"))
        if dir_count or file_count:
            return f"{status}, {dir_count} dirs, {file_count} files"
    if name == "grep":
        return f"{status}, {len(lines)} lines, {char_count} chars"
    if name == "read_file":
        return f"{status}, {len(lines)} lines, {char_count} chars"
    if name == "web_search":
        result_count = sum(1 for line in lines if line.lstrip().split(". ", 1)[0].isdigit() and "Title:" in line)
        return f"{status}, {result_count} results, {len(lines)} lines, {char_count} chars"
    if name == "web_extract":
        source_count = sum(1 for line in lines if line.lstrip().split(". ", 1)[0].isdigit() and ("URL:" in line or "Content:" in line))
        failed_count = sum(1 for line in lines if line.lstrip().startswith("- "))
        bits = [f"{status}, {source_count} sources"]
        if failed_count:
            bits.append(f"{failed_count} failed")
        bits.append(f"{len(lines)} lines")
        bits.append(f"{char_count} chars")
        return ", ".join(bits)
    if name == "web_research":
        return f"{status}, report, {len(lines)} lines, {char_count} chars"
    return f"{status}, {len(lines)} lines, {char_count} chars"


def summarize_tool_result(name, result, metadata=None):
    """把工具结果压成终端可读摘要，完整结果仍保留在 history 和 trace 中。"""
    metadata = dict(metadata or {})
    result = str(result or "")
    lines = []
    status = metadata.get("tool_status")
    if status:
        lines.append(f"status: {status}")
    if name == "todo_write":
        lines.append(result)
        return "\n".join(lines)
    result_lines = result.splitlines()
    for line in result_lines[:MAX_RESULT_LINES]:
        lines.append(_clip_line(line))
    if len(result_lines) > MAX_RESULT_LINES:
        lines.append(f"... ({len(result_lines) - MAX_RESULT_LINES} more lines)")
    if not lines:
        lines.append("(empty)")
    return "\n".join(lines)
