# 路径安全策略模块：统一处理文件工具和 shell 命令的访问门禁。
#
# 本模块把模型传入的路径解析为真实绝对路径，并用聚合后的 read/write
# allow/deny 规则判断工具调用是直接放行、需要询问，还是应当拒绝。
# 这里不执行工具动作，只负责在执行前给 runtime 一个清晰的门禁结果。

from dataclasses import dataclass, field
from pathlib import Path

from ..memory.long_term import is_memory_path


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
    access: str = ""
    suggested_allow_dir: str = ""

    def to_metadata(self):
        return {
            "approval_gate": self.action,
            "approval_reason": self.reason,
            "approval_paths": list(self.paths),
            "outside_workspace": self.outside_workspace,
            "approval_access": self.access,
            "suggested_allow_dir": self.suggested_allow_dir,
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


def _matches_rule(path, rules):
    return any(path == rule or _is_relative_to(path, rule) for rule in rules)


def _rules(agent):
    rules = getattr(agent, "permission_rules", None)
    if rules is None:
        raise ToolPolicyError("permission rules are not loaded", code="missing_permission_rules")
    return rules


def _rule_hit(agent, access, decision):
    rules = _rules(agent)
    if access == "read":
        if _matches_rule(decision.path, rules.read_deny):
            return "deny"
        if _matches_rule(decision.path, rules.read_allow):
            return "allow"
        return "ask"
    if _matches_rule(decision.path, rules.write_deny):
        return "deny"
    if _matches_rule(decision.path, rules.write_allow):
        return "allow"
    return "ask"


def _suggest_allow_dir(decisions):
    # 临时 allow 以目录为粒度。
    # 文件路径使用父目录，目录路径使用自身；多个不同目录时不提供快捷记住选项。
    dirs = []
    for decision in decisions:
        path = decision.path
        directory = path if path.exists() and path.is_dir() else path.parent
        dirs.append(directory)
    unique = {str(item) for item in dirs}
    return unique.pop() if len(unique) == 1 else ""


def _gate(action, reason, decisions, access=""):
    decisions = tuple(decisions or ())
    paths = tuple(str(item.path) for item in decisions)
    outside = any(item.outside_workspace for item in decisions)
    suggested = _suggest_allow_dir(decisions) if action == "ask" and access in {"read", "write"} else ""
    return ToolGate(action, reason, paths, outside, access, suggested)


def _deny_for_access(access, decision):
    if access == "read":
        message = f"read denied by permission rules: {decision.raw}"
        code = "read_permission_denied"
    else:
        message = f"write denied by permission rules: {decision.raw}"
        code = "write_permission_denied"
    raise ToolPolicyError(message, code=code, security_event_type="permission_denied")


def resolve_tool_path(agent, raw_path, access="read"):
    """解析工具路径并做不可绕过的硬边界校验。

    这里会解析 `../`、`~` 和符号链接，得到真实绝对路径。路径本身不按
    home 边界直接拒绝；读写权限统一交给聚合后的 allow/deny 规则和审批
    策略判断，避免路径解析层和权限层各管一套规则。
    """
    root = Path(agent.root).resolve()
    raw_text = str(raw_path)
    path = Path(raw_text).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    paths = getattr(agent, "paths", None)
    internal_roots = [(root / ".codemate").resolve()]
    if paths is not None:
        internal_roots = [
            paths.project_config_root.resolve(),
            paths.project_state_root.resolve(),
        ]

    in_workspace = _is_relative_to(resolved, root)
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
    """把路径规则、工具访问类型和 approval policy 合并成执行门禁。

    返回值只可能是 allow 或 ask；deny 会在这里直接抛出 ToolPolicyError。
    这样 runtime 只需要在 ask 时进入人工审批，allow 可以直接执行。
    """
    decisions = tuple(path_decisions or ())
    policy = str(agent.approval_policy)

    if access == "read":
        hits = [_rule_hit(agent, "read", decision) for decision in decisions]
        for decision, hit in zip(decisions, hits):
            if hit == "deny":
                _deny_for_access("read", decision)
        if not decisions or all(hit == "allow" for hit in hits):
            return _gate("allow", "read_allowed_by_rule", decisions, "read")
        if policy in {"auto", "full", "read_only"}:
            return _gate("allow", "read_allowed_by_policy", decisions, "read")
        return _gate("ask", "read_requires_approval", decisions, "read")

    if policy == "read_only":
        raise ToolPolicyError(
            "write operations are blocked in read-only mode",
            code="read_only_block",
            security_event_type="read_only_block",
        )

    if access == "write":
        hits = [_rule_hit(agent, "write", decision) for decision in decisions]
        for decision, hit in zip(decisions, hits):
            if hit == "deny":
                _deny_for_access("write", decision)
        if decisions and all(hit == "allow" for hit in hits):
            return _gate("allow", "write_allowed_by_rule", decisions, "write")
        if policy == "full":
            return _gate("allow", "write_allowed_by_full", decisions, "write")
        if policy == "auto" and all(decision.location in {"workspace", "internal"} for decision in decisions):
            return _gate("allow", "workspace_write_allowed_by_auto", decisions, "write")
        return _gate("ask", "write_requires_approval", decisions, "write")

    if access == "unknown":
        # 未知 shell 命令可能读写文件、访问网络或启动子进程，full 才直接放行。
        if policy == "full":
            return _gate("allow", "unknown_shell_full", decisions, "write")
        return _gate("ask", "unknown_shell", decisions, "write")

    if access == "dangerous":
        if policy == "full":
            return _gate("allow", "dangerous_shell_full", decisions, "write")
        return _gate("ask", "dangerous_shell", decisions, "write")

    raise ValueError(f"unknown access kind: {access}")


def gate_for_mcp(agent):
    """MCP 是外部动态工具，默认必须询问。

    即使当前 approval policy 是 auto，也不能把 MCP 当成内置低风险工具放行；
    只有 full 明确表示全部自动通过。read_only 直接拒绝。
    """
    if str(agent.approval_policy) == "read_only":
        raise ToolPolicyError(
            "MCP tools are blocked in read-only mode",
            code="mcp_read_only_block",
            security_event_type="read_only_block",
        )
    policy = str(agent.approval_policy)
    if policy == "full":
        return ToolGate("allow", "mcp_full")
    return ToolGate("ask", "mcp_default_ask")


def gate_for_web(agent):
    """Web 工具按只读信息获取处理，默认自动放行。

    这些工具不会修改本地文件，但会访问外部网络并把不可信网页内容引入上下文。
    网页注入主要靠提示词和后续工具权限隔离处理，不靠每次搜索人工审批解决。
    """
    return ToolGate("allow", "web_read")
