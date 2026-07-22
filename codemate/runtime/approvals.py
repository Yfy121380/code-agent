# 审批交互流程。
# 本文件只处理 runtime 层的“是否询问用户”和“用户选择后的临时规则更新”。
# 路径解析、allow/deny 命中和 shell 风险识别仍由 tools/config 模块负责。
# 这样审批 UI 变化不会污染工具执行主流程。

from ..config import build_permission_rules
from .. import tools as toolkit
from ..workspace import now


class ApprovalMixin:
    def approval_decision(self, name, args, tool):
        # validate_tool 已经完成 allow/ask/deny 判定。
        # 只有 gate 为 ask 的工具调用才会进入这里，因此这里不再重复做策略推导。
        return self.prompt_approval(name, args)

    def prompt_approval(self, name, args):
        if self.read_only:
            return False
        tool = self.tools.get(name, {"risky": True})
        metadata = {
            **self.tool_runtime_metadata(name, tool),
            **self.shell_analysis_metadata(),
        }
        gate = getattr(self, "_last_tool_gate", None)
        if gate is not None and hasattr(gate, "to_metadata"):
            metadata.update(gate.to_metadata())
        decision = self.ui.approval_request(name, args, metadata=metadata)
        if isinstance(decision, dict):
            allowed = bool(decision.get("allowed"))
            remember = decision.get("remember") or {}
            if allowed and remember:
                self.add_temporary_permission(remember.get("access"), remember.get("path"))
            return allowed
        return bool(decision)

    def add_temporary_permission(self, access, directory):
        # 审批中的“本会话允许”写入当前 session。
        # 规则仍走 build_permission_rules 聚合，保证默认/settings/临时规则语义一致。
        access = str(access or "").strip()
        if access not in {"read", "write"}:
            raise ValueError("temporary permission access must be read or write")
        path = toolkit.resolve_tool_path(self, directory, access=access).path
        if not path.exists() or not path.is_dir():
            raise ValueError("temporary permission path must be a directory")
        self.session["temporary_permissions"] = self.temporary_permission_settings
        values = self.temporary_permission_settings["permissions"][access]["allow"]
        text = str(path)
        if text not in values:
            values.append(text)
        self.session["updated_at"] = now()
        self.permission_rules = build_permission_rules(
            self.paths,
            self.settings.user,
            self.settings.project,
            self.temporary_permission_settings,
        )
        self.session_path = self.session_store.save(self.session)
