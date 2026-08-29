"""工作区基础工具测试。

覆盖模块：workspace.now。
重点边界：运行时间使用 Asia/Shanghai 时区，保证 trace/session 时间可读。
"""

import subprocess
from datetime import datetime

from codemate.workspace import WorkspaceContext, now


def test_now_uses_china_timezone():
    timestamp = now()
    parsed = datetime.fromisoformat(timestamp)

    assert parsed.utcoffset().total_seconds() == 8 * 60 * 60
    assert timestamp.endswith("+08:00") or "+08:00" in timestamp


def test_workspace_prompt_text_only_contains_stable_paths(tmp_path):
    workspace = WorkspaceContext(
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
        branch="feature/cache",
        default_branch="main",
        status="M codemate/runtime/agent.py",
        recent_commits=["abc123 change prompt"],
    )

    text = workspace.text()

    assert f"- cwd: {tmp_path}" in text
    assert f"- repo_root: {tmp_path}" in text
    assert "feature/cache" not in text
    assert "default_branch" not in text
    assert "status:" not in text
    assert "abc123" not in text


def test_workspace_build_does_not_inherit_parent_git_root(tmp_path):
    parent = tmp_path / "repository"
    child = parent / "nested" / "task"
    child.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(parent)], check=True)

    workspace = WorkspaceContext.build(child)

    assert workspace.cwd == str(child.resolve())
    assert workspace.repo_root == str(child.resolve())
