"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import shutil
import shlex
import subprocess
import textwrap
import re
from dataclasses import dataclass, field
from functools import partial

from .workspace import IGNORED_PATH_NAMES

GREP_MODES = {"files_with_matches", "count", "content"}
MAX_GREP_CONTEXT_LINES = 20
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


@dataclass
class ShellCommandAnalysis:
    kind: str = "read"
    subjects: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    has_glob: bool = False
    has_redirection: bool = False
    has_dynamic_expansion: bool = False
    blocked: bool = False
    error: str = ""

    def to_metadata(self):
        return {
            "shell_kind": self.kind,
            "shell_subjects": list(self.subjects),
            "shell_paths": list(self.paths),
            "shell_reasons": list(self.reasons),
            "shell_has_glob": self.has_glob,
            "shell_has_redirection": self.has_redirection,
            "shell_has_dynamic_expansion": self.has_dynamic_expansion,
            "shell_blocked": self.blocked,
        }

BASE_TOOL_SPECS = {
    "list_files": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory path to list.",
                    "default": ".",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "List files in a workspace directory.",
    },
    "read_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to read."},
                "start": {"type": "integer", "description": "1-based starting line number.", "default": 1},
                "end": {"type": "integer", "description": "1-based ending line number, inclusive.", "default": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Read a UTF-8 text file by line range before reasoning about or editing it.",
    },
    "grep": {
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression pattern to search for."},
                "path": {"type": "string", "description": "Workspace-relative file or directory path to search.", "default": "."},
                "mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "count", "content"],
                    "description": (
                        "Output mode. files_with_matches returns only matching file paths; "
                        "count returns per-file match counts plus total_matches; "
                        "content returns matching lines with paths and line numbers."
                    ),
                    "default": "content",
                },
                "after": {"type": "integer", "description": "Context lines after each match in content mode.", "default": 0},
                "before": {"type": "integer", "description": "Context lines before each match in content mode.", "default": 0},
                "context": {"type": "integer", "description": "Symmetric context lines like rg -C; overridden by before/after when set.", "default": 0},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": (
            "Search files with rg-style output. Use files_with_matches to locate relevant files, "
            "count to estimate match/change scale, and content to inspect concrete matching lines. "
            "In content mode, before/after/context control surrounding lines like rg -B/-A/-C; "
            "explicit before/after override context for that side."
        ),
    },
    "run_shell": {
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run in the repository root."},
                "timeout": {"type": "integer", "description": "Timeout in seconds, from 1 to 120.", "default": 20},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": "Run a shell command in the repo root. Read-only commands with workspace-safe paths may run directly; write-like commands are risky; dangerous commands require approval.",
    },
    "write_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to write."},
                "content": {"type": "string", "description": "Complete UTF-8 text content to write."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": "Create a new text file or replace an existing text file.",
    },
    "patch_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to patch."},
                "old_text": {"type": "string", "description": "Exact text block to replace. Must occur exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": "Replace one exact text block in an existing file.",
    },
}

DELEGATE_TOOL_SPEC = {
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Bounded read-only investigation task for the child agent."},
            "max_steps": {"type": "integer", "description": "Maximum child-agent tool steps.", "default": 3},
        },
        "required": ["task"],
        "additionalProperties": False,
    },
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}


def build_tool_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if agent.depth < agent.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
    return tools



def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _raise_shell_error(analysis, message, reason):
    analysis.blocked = True
    analysis.error = message
    if reason not in analysis.reasons:
        analysis.reasons.append(reason)
    return analysis


def _bump_shell_kind(current, candidate):
    return candidate if SHELL_KIND_ORDER[candidate] > SHELL_KIND_ORDER[current] else current


def _shell_tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    return list(lexer)


def _shell_segments(command):
    tokens = _shell_tokens(command)
    segments = []
    current = []
    operators = {";", "|", "&&", "||"}
    for token in tokens:
        if token in operators:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_subject(tokens):
    if not tokens:
        return ""
    base = tokens[0]
    if base == "git" and len(tokens) >= 2:
        return f"git {tokens[1]}"
    if base in {"python", "python3"}:
        if len(tokens) >= 3 and tokens[1] == "-m":
            return f"{base} -m {tokens[2]}"
        return base
    if base == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        if tokens[2] == "pytest":
            return "uv run pytest"
        if tokens[2] in {"python", "python3"} and len(tokens) >= 5 and tokens[3] == "-m" and tokens[4] == "pytest":
            return "uv run python -m pytest"
        if tokens[2] in {"python", "python3"}:
            return "uv run python"
        return f"uv run {tokens[2]}"
    if base == "npm" and len(tokens) >= 2:
        return f"npm {tokens[1]}"
    return base


def _subject_kind(subject):
    if subject in SHELL_READ_SUBJECTS:
        return "read", "read_command"
    if subject in SHELL_RISKY_SUBJECTS:
        return "risky", "risky_command"
    if subject in SHELL_DANGEROUS_SUBJECTS:
        return "dangerous", "dangerous_command"
    return "risky", "unknown_command"


def _strip_redirection_tokens(tokens):
    cleaned = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {">", ">>", "1>", "1>>", "2>", "2>>"}:
            skip_next = True
            continue
        if re.match(r"^\d*>>?.+", token):
            continue
        cleaned.append(token)
    return cleaned


def _redirection_paths(command):
    return [match.group(1).strip() for match in SHELL_REDIRECT_RE.finditer(command) if match.group(1).strip()]


def _non_option_args(args):
    value_options = {
        "-n",
        "--lines",
        "-c",
        "--bytes",
        "-m",
        "--max-count",
        "-e",
        "--regexp",
        "-f",
        "--file",
        "--exclude",
        "--include",
        "--timeout",
    }
    result = []
    skip_next = False
    after_double_dash = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            after_double_dash = True
            continue
        if not after_double_dash and arg in value_options:
            skip_next = True
            continue
        if not after_double_dash and arg.startswith("-"):
            continue
        result.append(arg)
    return result


def _extract_shell_paths_for_segment(tokens):
    if not tokens:
        return []
    tokens = _strip_redirection_tokens(tokens)
    if not tokens:
        return []
    base = tokens[0]
    subject = _command_subject(tokens)
    args = tokens[1:]

    if base in SHELL_PATH_COMMANDS:
        paths = _non_option_args(args)
        if base == "find" and not paths:
            return ["."]
        return paths

    if base in {"rg", "grep"}:
        values = _non_option_args(args)
        if len(values) <= 1:
            return ["."]
        return values[1:]

    if base in SHELL_COPY_MOVE_COMMANDS:
        return _non_option_args(args)

    if subject in {"git diff", "git show", "git add"}:
        if "--" in args:
            return [item for item in args[args.index("--") + 1:] if item and not item.startswith("-")]
        if subject == "git add":
            return _non_option_args(args)
        return []

    if subject in SHELL_DANGEROUS_SUBJECTS:
        return _non_option_args(args)

    if subject in {"python -m py_compile", "python3 -m py_compile"}:
        return _non_option_args(tokens[3:])

    if subject == "uv run python" and len(tokens) >= 4:
        return _non_option_args(tokens[3:])

    if subject == "tee":
        return _non_option_args(args)

    return []


def _has_glob(path):
    return any(char in str(path) for char in SHELL_GLOB_CHARS)


def _is_blocked_dangerous_target(path):
    text = str(path).strip()
    return text in {"/", "~"} or text.startswith("~/") or text.startswith("/*")


def _validate_shell_path(agent, raw_path):
    text = str(raw_path).strip()
    if not text or text == "-":
        return
    agent.path(text)


def analyze_shell_command(agent, command):
    analysis = ShellCommandAnalysis()
    command = str(command or "").strip()
    if not command:
        return _raise_shell_error(analysis, "command must not be empty", "empty_command")

    analysis.has_dynamic_expansion = bool(SHELL_DYNAMIC_RE.search(command))
    if analysis.has_dynamic_expansion:
        analysis.kind = _bump_shell_kind(analysis.kind, "risky")
        analysis.reasons.append("dynamic_shell_expansion")

    redirect_paths = _redirection_paths(command)
    if redirect_paths:
        analysis.has_redirection = True
        analysis.kind = _bump_shell_kind(analysis.kind, "risky")
        analysis.reasons.append("shell_redirection")
        analysis.paths.extend(redirect_paths)

    try:
        segments = _shell_segments(command)
    except ValueError as exc:
        return _raise_shell_error(analysis, f"invalid shell command: {exc}", "shell_parse_error")

    if not segments:
        return _raise_shell_error(analysis, "command must not be empty", "empty_command")

    for tokens in segments:
        subject = _command_subject(tokens)
        if not subject:
            continue
        kind, reason = _subject_kind(subject)
        analysis.kind = _bump_shell_kind(analysis.kind, kind)
        analysis.subjects.append(subject)
        analysis.reasons.append(reason)
        analysis.paths.extend(_extract_shell_paths_for_segment(tokens))

    analysis.subjects = _dedupe(analysis.subjects)
    analysis.paths = _dedupe([path for path in analysis.paths if str(path).strip()])
    analysis.reasons = _dedupe(analysis.reasons)
    analysis.has_glob = any(_has_glob(path) for path in analysis.paths)

    for raw_path in analysis.paths:
        if analysis.kind == "dangerous" and _is_blocked_dangerous_target(raw_path):
            return _raise_shell_error(analysis, f"dangerous shell target is blocked: {raw_path}", "blocked_dangerous_target")
        if analysis.kind in {"risky", "dangerous"} and _has_glob(raw_path):
            return _raise_shell_error(analysis, f"wildcards are not allowed for {analysis.kind} shell commands: {raw_path}", "wildcard_write")
        try:
            _validate_shell_path(agent, raw_path)
        except ValueError as exc:
            return _raise_shell_error(analysis, str(exc), "path_escape")

    return analysis

def validate_tool(agent, name, args):
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

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return


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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def tool_delegate(agent, args):
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from .runtime import CodeMate

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
}
