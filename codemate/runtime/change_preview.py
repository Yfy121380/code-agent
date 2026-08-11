"""为单次文本修改工具生成有界、可展示的 Diff 片段。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path


MAX_PREVIEW_SOURCE_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_LINES = 24
MAX_PREVIEW_LINE_CHARS = 240


@dataclass(frozen=True)
class TextSnapshot:
    """保存工具执行前的一份小型文本快照，不承担整轮 Undo/Redo。"""

    exists: bool
    text: str | None
    error: str = ""


def capture_text_snapshot(path: Path) -> TextSnapshot:
    """尽力读取文本文件；预览失败不能阻止真正的文件修改。"""
    try:
        if not path.exists():
            return TextSnapshot(exists=False, text="")
        if not path.is_file():
            return TextSnapshot(exists=True, text=None, error="Preview unavailable for non-file paths.")
        size = path.stat().st_size
        if size > MAX_PREVIEW_SOURCE_BYTES:
            return TextSnapshot(
                exists=True,
                text=None,
                error=f"Preview unavailable: file exceeds {MAX_PREVIEW_SOURCE_BYTES} bytes.",
            )
        data = path.read_bytes()
        if b"\0" in data:
            return TextSnapshot(exists=True, text=None, error="Preview unavailable for binary files.")
        return TextSnapshot(exists=True, text=data.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        return TextSnapshot(exists=path.exists(), text=None, error=f"Preview unavailable: {exc}")


def build_change_preview(workspace_root: Path, path: Path, before: TextSnapshot) -> dict:
    """比较工具执行前后内容，返回 UI 使用的紧凑 unified diff。"""
    after = capture_text_snapshot(path)
    display_path = _display_path(workspace_root, path)
    status = _change_status(before, after)
    base = {
        "path": display_path,
        "status": status,
        "additions": 0,
        "deletions": 0,
        "diff": "",
        "truncated": False,
        "message": "",
    }
    if before.text is None or after.text is None:
        base["message"] = before.error or after.error or "Preview unavailable."
        return base
    if before.text == after.text:
        base["message"] = "No textual changes."
        return base

    # 保留行结束符，确保“新增/删除文件末尾换行”也会成为真实变更。
    before_lines = before.text.splitlines(keepends=True)
    after_lines = after.text.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            n=3,
            lineterm="",
        )
    )
    body = diff_lines[2:]
    # 文件头已经从 body 中移除，因此以 +++/--- 开头的源码行也必须计数。
    base["additions"] = sum(1 for line in body if line.startswith("+"))
    base["deletions"] = sum(1 for line in body if line.startswith("-"))
    visible = [_clip_diff_line(line) for line in body[:MAX_PREVIEW_LINES]]
    if len(body) > MAX_PREVIEW_LINES:
        visible.append("... diff preview truncated ...")
        base["truncated"] = True
    base["diff"] = "\n".join(visible)
    return base


def _display_path(workspace_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _change_status(before: TextSnapshot, after: TextSnapshot) -> str:
    if not before.exists and after.exists:
        return "added"
    if before.exists and not after.exists:
        return "deleted"
    if before.text == after.text and before.text is not None:
        return "unchanged"
    return "modified"


def _clip_diff_line(line: str) -> str:
    line = line.rstrip("\r\n")
    if len(line) <= MAX_PREVIEW_LINE_CHARS:
        return line
    return f"{line[:MAX_PREVIEW_LINE_CHARS - 3]}..."
