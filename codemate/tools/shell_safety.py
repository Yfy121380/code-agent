# Shell 安全分析：识别命令主体、路径、通配符和风险等级，供 run_shell 门禁使用。

from dataclasses import dataclass, field
import re
import shlex

from .constants import (
    SHELL_COPY_MOVE_COMMANDS,
    SHELL_DANGEROUS_SUBJECTS,
    SHELL_DYNAMIC_RE,
    SHELL_GLOB_CHARS,
    SHELL_KIND_ORDER,
    SHELL_PATH_COMMANDS,
    SHELL_READ_SUBJECTS,
    SHELL_REDIRECT_RE,
    SHELL_RISKY_SUBJECTS,
)


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
    # 从单个 shell 片段中抽取可能代表路径的参数。
    # 这里不执行命令，只根据命令主体和常见参数形态做保守识别，
    # 后续统一交给 workspace path 校验确认是否越界。
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


def analyze_shell_command(agent, command):
    # run_shell 的审批前分析入口。
    # 它把原始命令拆成片段，识别命令主体、路径、重定向、动态展开和通配符，
    # 最终产出风险等级 read/risky/dangerous，并处理 shell 专属的硬拒绝。
    # 普通路径边界和审批策略由 validators.py 统一处理，避免安全规则散落多处。
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

    return analysis
