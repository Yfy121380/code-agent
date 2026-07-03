# 工具系统常量：集中保存工具参数限制、todo 状态和 shell 风险分类表。

import re

GREP_MODES = {"files_with_matches", "count", "content"}
MAX_GREP_CONTEXT_LINES = 50
TODO_STATUSES = {"pending", "in_progress", "completed"}
SHELL_KIND_ORDER = {"read": 0, "risky": 1, "dangerous": 2}
SHELL_GLOB_CHARS = ("*", "?", "[", "]")
SHELL_READ_SUBJECTS = {
    "pwd",
    "ls",
    "cat",
    "head",
    "tail",
    "nl",
    "wc",
    "rg",
    "grep",
    "find",
    "git status",
    "git diff",
    "git log",
    "git show",
    "python -m py_compile",
    "python3 -m py_compile",
    "pytest",
    "uv run pytest",
    "uv run python -m pytest",
}
SHELL_RISKY_SUBJECTS = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "echo",
    "tee",
    "git add",
    "git commit",
}
SHELL_DANGEROUS_SUBJECTS = {
    "rm",
    "chmod",
    "chown",
    "kill",
    "pkill",
    "reboot",
    "shutdown",
    "sudo",
    "su",
    "dd",
    "mkfs",
    "mount",
    "umount",
    "git reset",
    "git clean",
    "git push",
}
SHELL_PATH_COMMANDS = {"ls", "cat", "head", "tail", "nl", "wc", "mkdir", "touch", "find"}
SHELL_COPY_MOVE_COMMANDS = {"cp", "mv"}
SHELL_REDIRECT_RE = re.compile(r"(?:^|\s)(?:\d*)>>?\s*([^&\s;|]+)")
SHELL_DYNAMIC_RE = re.compile(r"(`[^`]*`|\$\(|\b(?:bash|sh)\s+-c\b)")
