# 工具系统常量：集中保存工具参数限制、todo 状态和 shell 风险分类表。

import re

GREP_MODES = {"files_with_matches", "count", "content"}
MAX_GREP_CONTEXT_LINES = 50
MAX_READ_ALL_LINES = 1000
TODO_STATUSES = {"pending", "in_progress", "completed"}
WEB_TOOL_NAMES = {"web_search", "web_extract", "web_research"}
WEB_SEARCH_DEPTHS = {"basic", "advanced", "fast", "ultra-fast"}
WEB_SEARCH_TOPICS = {"general", "news", "finance"}
WEB_TIME_RANGES = {"day", "week", "month", "year"}
WEB_EXTRACT_DEPTHS = {"basic", "advanced"}
WEB_EXTRACT_FORMATS = {"markdown", "text"}
WEB_RESEARCH_MODELS = {"mini", "pro", "auto"}
WEB_RESEARCH_OUTPUT_LENGTHS = {"short", "standard", "long"}
SHELL_KIND_ORDER = {"read": 0, "risky": 1, "unknown": 2, "dangerous": 3}
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
    "pytest",
    "uv run pytest",
    "uv run python -m pytest",
}
SHELL_DANGEROUS_SUBJECTS = {
    "python",
    "python3",
    "uv run python",
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
SHELL_HARD_BLOCKED_SUBJECTS = {
    "reboot",
    "shutdown",
    "sudo",
    "su",
    "kill",
    "pkill",
    "dd",
    "mkfs",
    "mount",
    "umount",
}
SHELL_DANGEROUS_PATH_SUBJECTS = {
    "rm",
    "chmod",
    "chown",
    "dd",
    "mkfs",
    "mount",
    "umount",
    "git reset",
    "git clean",
}
SHELL_PATH_COMMANDS = {"ls", "cat", "head", "tail", "nl", "wc", "mkdir", "touch", "find"}
SHELL_COPY_MOVE_COMMANDS = {"cp", "mv"}
SHELL_REDIRECT_RE = re.compile(r"(?:^|\s)(?:\d*)>>?\s*([^&\s;|]+)")
SHELL_DYNAMIC_RE = re.compile(r"(`[^`]*`|\$\(|\b(?:bash|sh)\s+-c\b)")
