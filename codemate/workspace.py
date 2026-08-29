"""工作区快照工具。

这个模块收集工作目录和 Git 信息，供终端状态、trace 和运行时诊断使用。
模型 prefix 只使用稳定的路径信息，避免文件修改导致 prompt 缓存失效。
"""

import subprocess
import textwrap
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_TOOL_OUTPUT = 20000
MAX_HISTORY = 48000
IGNORED_PATH_NAMES = {
    ".git",
    ".codemate",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}
TRACE_TIMEZONE = "Asia/Shanghai"


def now():
    # trace、history、memory 都复用这个时间入口。
    # 使用中国时区能让本地调试时 trace.jsonl 与终端时间一致。
    try:
        timezone_info = ZoneInfo(TRACE_TIMEZONE)
    except Exception:
        timezone_info = timezone(timedelta(hours=8))
    return datetime.now(timezone_info).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        cwd = Path(cwd).resolve()
        # Agent 工作区由启动目录确定，不再向上继承父级 Git 仓库。
        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else cwd
        )

        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(
                lambda branch: (
                    branch[len("origin/") :] if branch.startswith("origin/") else branch
                )
            )(
                git(
                    ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                    "origin/main",
                )
                or "origin/main"
            ),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[
                line for line in git(["log", "--oneline", "-5"]).splitlines() if line
            ],
        )

    def text(self):
        """返回适合放入模型 prefix 的稳定工作区信息。"""
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            """
        ).strip()

    def fingerprint(self):
        # 这个指纹用来判断仓库状态是否发生了足够大的变化，
        # 从而决定是否需要重建缓存中的 prompt prefix。
        payload = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status": self.status,
            "recent_commits": list(self.recent_commits),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
