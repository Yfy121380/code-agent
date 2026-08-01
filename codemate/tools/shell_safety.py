# Shell 安全分析：识别命令主体、路径、通配符和风险等级，供 run_shell 门禁使用。

from dataclasses import dataclass, field
import re
import shlex

from .constants import (
    SHELL_COPY_MOVE_COMMANDS,
    SHELL_DANGEROUS_PATH_SUBJECTS,
    SHELL_DANGEROUS_SUBJECTS,
    SHELL_DYNAMIC_RE,
    SHELL_GLOB_CHARS,
    SHELL_HARD_BLOCKED_SUBJECTS,
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
    approval_subject: str = ""

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
            "shell_approval_subject": self.approval_subject,
        }


@dataclass(frozen=True)
class _HeredocSpec:
    delimiter: str
    strip_tabs: bool
    quoted: bool


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


def _read_heredoc_word(line, start, *, strip_tabs):
    """Parse one heredoc delimiter word and apply shell quote removal."""
    index = start
    delimiter = []
    quoted = False
    metacharacters = ";&|<>"

    while index < len(line):
        char = line[index]
        if char in "\r\n" or char.isspace() or char in metacharacters:
            break
        if char == "'":
            quoted = True
            end = line.find("'", index + 1)
            if end < 0:
                raise ValueError("No closing quotation")
            delimiter.append(line[index + 1:end])
            index = end + 1
            continue
        if char == '"':
            quoted = True
            index += 1
            while index < len(line) and line[index] != '"':
                if line[index] == "\\" and index + 1 < len(line):
                    index += 1
                delimiter.append(line[index])
                index += 1
            if index >= len(line):
                raise ValueError("No closing quotation")
            index += 1
            continue
        if char == "\\":
            quoted = True
            index += 1
            if index >= len(line) or line[index] in "\r\n":
                raise ValueError("missing here-document delimiter")
            delimiter.append(line[index])
            index += 1
            continue
        delimiter.append(char)
        index += 1

    value = "".join(delimiter)
    if not value:
        raise ValueError("missing here-document delimiter")
    return _HeredocSpec(value, strip_tabs, quoted), index


def _heredoc_specs_on_line(line, initial_quote="", initial_arithmetic_depth=0):
    """Find heredocs outside shell quotes, comments, and arithmetic expressions."""
    declarations = []
    index = 0
    quote = initial_quote
    arithmetic_depth = initial_arithmetic_depth
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
            index += 1
            continue
        if arithmetic_depth:
            if line.startswith("$((", index):
                arithmetic_depth += 1
                index += 3
            elif line.startswith("((", index):
                arithmetic_depth += 1
                index += 2
            elif line.startswith("))", index):
                arithmetic_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        if line.startswith("$((", index):
            arithmetic_depth = 1
            index += 3
            continue
        if line.startswith("((", index) and (
            index == 0 or line[index - 1].isspace() or line[index - 1] in ";&|"
        ):
            arithmetic_depth = 1
            index += 2
            continue
        if line.startswith("<<<", index):
            index += 3
            continue
        if not line.startswith("<<", index):
            index += 1
            continue

        operator_start = index
        fd_start = operator_start
        while fd_start > 0 and line[fd_start - 1].isdigit():
            fd_start -= 1
        if fd_start < operator_start and (
            fd_start == 0 or line[fd_start - 1].isspace() or line[fd_start - 1] in ";&|<>"
        ):
            operator_start = fd_start
        index += 2
        strip_tabs = index < len(line) and line[index] == "-"
        if strip_tabs:
            index += 1
        while index < len(line) and line[index] in " \t":
            index += 1
        spec, index = _read_heredoc_word(line, index, strip_tabs=strip_tabs)
        declarations.append((spec, operator_start, index))
    return declarations, quote, arithmetic_depth


def _prepare_heredocs(command):
    """Remove heredoc payloads from shell syntax while retaining expansions.

    Quoted heredocs are literal input and therefore do not participate in shell
    risk analysis. Unquoted payloads may perform command substitution, so their
    text is returned separately for dynamic-expansion detection.
    """
    lines = command.splitlines(keepends=True)
    shell_lines = []
    expandable_bodies = []
    index = 0
    shell_quote = ""
    shell_arithmetic_depth = 0

    while index < len(lines):
        line = lines[index]
        declarations, shell_quote, shell_arithmetic_depth = _heredoc_specs_on_line(
            line,
            shell_quote,
            shell_arithmetic_depth,
        )
        sanitized_parts = []
        cursor = 0
        for _spec, start, end in declarations:
            sanitized_parts.append(line[cursor:start])
            cursor = end
        sanitized_parts.append(line[cursor:])
        sanitized_line = "".join(sanitized_parts)
        if declarations:
            # A heredoc command ends after its delimiter line. Preserve that
            # boundary for the segment analyzer when later commands follow.
            sanitized_line = sanitized_line.rstrip("\r\n") + ";\n"
        shell_lines.append(sanitized_line)
        index += 1

        for spec, _start, _end in declarations:
            body = []
            found = False
            while index < len(lines):
                candidate = lines[index].rstrip("\r\n")
                comparable = candidate.lstrip("\t") if spec.strip_tabs else candidate
                if comparable == spec.delimiter:
                    index += 1
                    found = True
                    break
                body.append(lines[index])
                index += 1
            if not found:
                raise ValueError(
                    f"unterminated here-document: expected delimiter {spec.delimiter!r}"
                )
            if not spec.quoted:
                expandable_bodies.extend(body)

    return "".join(shell_lines), "".join(expandable_bodies)


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
    return "unknown", "unknown_command"


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

    if subject in SHELL_DANGEROUS_PATH_SUBJECTS:
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

    try:
        shell_command, expandable_heredoc_text = _prepare_heredocs(command)
    except ValueError as exc:
        return _raise_shell_error(analysis, f"invalid shell command: {exc}", "shell_parse_error")

    dynamic_text = f"{shell_command}\n{expandable_heredoc_text}"
    analysis.has_dynamic_expansion = bool(SHELL_DYNAMIC_RE.search(dynamic_text))
    if analysis.has_dynamic_expansion:
        analysis.kind = _bump_shell_kind(analysis.kind, "risky")
        analysis.reasons.append("dynamic_shell_expansion")

    redirect_paths = _redirection_paths(shell_command)
    if redirect_paths:
        analysis.has_redirection = True
        analysis.kind = _bump_shell_kind(analysis.kind, "risky")
        analysis.reasons.append("shell_redirection")
        analysis.paths.extend(redirect_paths)

    try:
        segments = _shell_segments(shell_command)
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
        if subject in SHELL_HARD_BLOCKED_SUBJECTS:
            return _raise_shell_error(
                analysis,
                f"shell command is blocked even in full approval mode: {subject}",
                "hard_blocked_shell_command",
            )
        analysis.paths.extend(_extract_shell_paths_for_segment(tokens))

    analysis.subjects = _dedupe(analysis.subjects)
    analysis.paths = _dedupe([path for path in analysis.paths if str(path).strip()])
    analysis.reasons = _dedupe(analysis.reasons)
    analysis.has_glob = any(_has_glob(path) for path in analysis.paths)

    # Session-scoped command approval is deliberately narrow: only one
    # non-read subject may be remembered. Compound write-like commands must
    # continue to use one-off approval instead of granting several families.
    non_read_subjects = [
        subject
        for subject in analysis.subjects
        if _subject_kind(subject)[0] != "read"
    ]
    if len(non_read_subjects) == 1 and len(non_read_subjects[0]) <= 128:
        analysis.approval_subject = non_read_subjects[0]

    for raw_path in analysis.paths:
        if analysis.kind == "dangerous" and _is_blocked_dangerous_target(raw_path):
            return _raise_shell_error(analysis, f"dangerous shell target is blocked: {raw_path}", "blocked_dangerous_target")
        if analysis.kind in {"risky", "dangerous"} and _has_glob(raw_path):
            return _raise_shell_error(analysis, f"wildcards are not allowed for {analysis.kind} shell commands: {raw_path}", "wildcard_write")

    return analysis
