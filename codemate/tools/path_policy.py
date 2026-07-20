# 路径安全策略模块：统一处理文件工具和 shell 命令中的路径边界。
#
# 本模块只负责三件事：把模型给出的路径解析成真实路径、识别路径属于
# .codemate 内部目录 / 当前工作区 / home 内的工作区外路径，以及根据当前
# approval policy 产出 allow/ask 或直接拒绝。工具参数本身是否合法仍由
# validators.py 负责，工具的实际读写仍由 handlers.py 负责。

from dataclasses import dataclass, field
from pathlib import Path

from ..memory.long_term import is_memory_path


READ_WRITE_DENY_PREFIXES = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
    ".docker",
    ".config/gh",
    ".password-store",
)
READ_WRITE_DENY_EXACT = (
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
)
WRITE_DENY_EXACT = (
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".zprofile",
    ".zshenv",
    ".gitconfig",
)


@dataclass(frozen=True)
class PathDecision:
    raw: str
    path: Path
    location: str

    @property
    def outside_workspace(self):
        return self.location == "outside_workspace"


@dataclass(frozen=True)
class ToolGate:
    action: str
    reason: str = ""
    paths: tuple[str, ...] = field(default_factory=tuple)
    outside_workspace: bool = False

    def to_metadata(self):
        return {
            "approval_gate": self.action,
            "approval_reason": self.reason,
            "approval_paths": list(self.paths),
            "outside_workspace": self.outside_workspace,
        }


class ToolPolicyError(ValueError):
    def __init__(self, message, code="policy_denied", security_event_type="policy_denied"):
        super().__init__(message)
        self.code = code
        self.security_event_type = security_event_type


def _is_relative_to(path, base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _home_child(home, relative):
    return (home / relative).resolve()


def _sensitive_path_error(home, resolved, access, raw_path):
    for relative in READ_WRITE_DENY_PREFIXES:
        sensitive = _home_child(home, relative)
        if resolved == sensitive or _is_relative_to(resolved, sensitive):
            return f"path is sensitive and cannot be accessed: {raw_path}"
    for relative in READ_WRITE_DENY_EXACT:
        if resolved == _home_child(home, relative):
            return f"path is sensitive and cannot be accessed: {raw_path}"
    if access == "write":
        for relative in WRITE_DENY_EXACT:
            if resolved == _home_child(home, relative):
                return f"write to sensitive config is blocked: {raw_path}"
    return ""


def resolve_tool_path(agent, raw_path, access="read"):
    """解析工具路径并做不可绕过的硬边界校验。

    这里会解析 `../` 和符号链接，最终用真实路径判断边界。当前工作区始终
    是可信根；工作区外路径必须位于当前用户 home 下，且不能命中敏感目录。
    是否需要用户审批不在这里决定，而是交给 `gate_for_access()`。
    """
    root = Path(agent.root).resolve()
    raw_text = str(raw_path)
    path = Path(raw_text).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    home = Path.home().resolve()
    paths = getattr(agent, "paths", None)
    internal_roots = [(root / ".codemate").resolve()]
    if paths is not None:
        internal_roots = [
            paths.project_config_root.resolve(),
            paths.sessions_root.resolve(),
            paths.memory_root.resolve(),
        ]

    in_workspace = _is_relative_to(resolved, root)
    if not in_workspace and not _is_relative_to(resolved, home):
        raise ToolPolicyError(
            f"path is outside the current workspace and outside home: {raw_path}",
            code="path_outside_home",
            security_event_type="path_outside_home",
        )

    if _is_relative_to(resolved, home):
        sensitive_error = _sensitive_path_error(home, resolved, str(access), raw_path)
        if sensitive_error:
            raise ToolPolicyError(
                sensitive_error,
                code="sensitive_path",
                security_event_type="sensitive_path",
            )

    if agent.memory_scope_only and not is_memory_path(root, resolved):
        raise ToolPolicyError(
            f"path outside memory scope: {raw_path}",
            code="path_outside_memory_scope",
            security_event_type="path_outside_memory_scope",
        )

    if any(_is_relative_to(resolved, internal_root) for internal_root in internal_roots):
        location = "internal"
    elif in_workspace:
        location = "workspace"
    else:
        location = "outside_workspace"
    return PathDecision(raw=raw_text, path=resolved, location=location)


def gate_for_access(agent, access, path_decisions=()):
    """把路径分类、工具访问类型和 approval policy 合并成执行门禁。

    返回值只可能是 allow 或 ask；deny 会在这里直接抛出 ToolPolicyError。
    这样 runtime 只需要在 ask 时进入人工审批，allow 可以直接执行。
    """
    decisions = tuple(path_decisions or ())
    paths = tuple(str(item.path) for item in decisions)
    outside = any(item.outside_workspace for item in decisions)
    all_internal = bool(decisions) and all(item.location == "internal" for item in decisions)
    policy = str(agent.approval_policy)

    if access == "read":
        if not outside:
            return ToolGate("allow", "read_in_workspace", paths, False)
        if policy == "never":
            raise ToolPolicyError(
                "reading outside the current workspace requires approval",
                code="outside_workspace_read_denied",
                security_event_type="outside_workspace",
            )
        if policy in {"auto", "full"}:
            return ToolGate("allow", "outside_workspace_read", paths, True)
        return ToolGate("ask", "outside_workspace_read", paths, True)

    if agent.read_only:
        raise ToolPolicyError(
            "write operations are blocked in read-only mode",
            code="read_only_block",
            security_event_type="read_only_block",
        )

    if access == "dangerous":
        if policy == "never":
            raise ToolPolicyError(
                "dangerous shell command requires approval",
                code="dangerous_shell_denied",
                security_event_type="approval_denied",
            )
        if policy == "full":
            return ToolGate("allow", "dangerous_shell_full", paths, outside)
        return ToolGate("ask", "dangerous_shell", paths, outside)

    if access == "write":
        if all_internal:
            return ToolGate("allow", "internal_write", paths, outside)
        if outside:
            if policy == "never":
                raise ToolPolicyError(
                    "writing outside the current workspace requires approval",
                    code="outside_workspace_write_denied",
                    security_event_type="outside_workspace",
                )
            if policy == "full":
                return ToolGate("allow", "outside_workspace_write_full", paths, True)
            return ToolGate("ask", "outside_workspace_write", paths, True)
        if policy == "never":
            raise ToolPolicyError(
                "write operation requires approval",
                code="write_denied",
                security_event_type="approval_denied",
            )
        if policy in {"auto", "full"}:
            return ToolGate("allow", "workspace_write", paths, False)
        return ToolGate("ask", "workspace_write", paths, False)

    raise ValueError(f"unknown access kind: {access}")


def gate_for_mcp(agent):
    """MCP 是外部动态工具，默认必须询问。

    即使当前 approval policy 是 auto，也不能把 MCP 当成内置低风险工具放行；
    只有 full 明确表示全部自动通过。never 和 read_only 都直接拒绝。
    """
    if agent.read_only:
        raise ToolPolicyError(
            "MCP tools are blocked in read-only mode",
            code="mcp_read_only_block",
            security_event_type="read_only_block",
        )
    policy = str(agent.approval_policy)
    if policy == "full":
        return ToolGate("allow", "mcp_full")
    if policy == "never":
        raise ToolPolicyError(
            "MCP tool requires approval",
            code="mcp_approval_required",
            security_event_type="approval_denied",
        )
    return ToolGate("ask", "mcp_default_ask")


def gate_for_web(agent):
    """Web 工具按只读信息获取处理，默认自动放行。

    这些工具不会修改本地文件，但会访问外部网络并把不可信网页内容引入上下文。
    网页注入主要靠提示词和后续工具权限隔离处理，不靠每次搜索人工审批解决。
    """
    return ToolGate("allow", "web_read")
