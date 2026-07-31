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
        tool = self.tools.get(name, {"risky": True})
        metadata = {
            **self.tool_runtime_metadata(name, tool),
            **self.shell_analysis_metadata(),
        }
        gate = getattr(self, "_last_tool_gate", None)
        if gate is not None and hasattr(gate, "to_metadata"):
            metadata.update(gate.to_metadata())
        shell_subject = self._suggest_temporary_shell_subject()
        if shell_subject:
            metadata["suggested_shell_subject"] = shell_subject
        decision = self.ui.approval_request(name, args, metadata=metadata)
        if isinstance(decision, dict):
            allowed = bool(decision.get("allowed"))
            remember = decision.get("remember") or {}
            if allowed and remember:
                self.add_temporary_approval(remember)
            return allowed
        return bool(decision)

    def _suggest_temporary_shell_subject(self):
        """Return the single command family that may be remembered safely."""
        analysis = getattr(self, "_last_shell_analysis", None)
        subject = str(getattr(analysis, "approval_subject", "") or "").strip()
        if not subject or toolkit.temporary_shell_subject_allowed(self, subject):
            return ""
        return subject

    def add_temporary_approval(self, remember):
        """Persist path and shell grants from one approval decision atomically."""
        remember = dict(remember or {})
        access = str(remember.get("access", "") or "").strip()
        directory = remember.get("path")
        subject = str(remember.get("shell_subject", "") or "").strip()

        path = None
        if access or directory is not None:
            if access not in {"read", "write"}:
                raise ValueError("temporary permission access must be read or write")
            path = toolkit.resolve_tool_path(self, directory, access=access).path
            if not path.exists() or not path.is_dir():
                raise ValueError("temporary permission path must be a directory")
        if subject and (len(subject) > 128 or "\n" in subject or "\r" in subject):
            raise ValueError("invalid temporary shell subject")
        if path is None and not subject:
            raise ValueError("temporary approval must include a path or shell subject")

        with self._session_lock:
            path_changed = False
            if path is not None:
                values = self.temporary_permission_settings["permissions"][access]["allow"]
                text = str(path)
                if text not in values:
                    values.append(text)
                    path_changed = True
            if subject:
                shell = self.temporary_permission_settings.setdefault("shell", {})
                subjects = shell.setdefault("allow_subjects", [])
                if subject not in subjects:
                    subjects.append(subject)

            self.session["temporary_permissions"] = self.temporary_permission_settings
            self.session["updated_at"] = now()
            if path_changed:
                self.permission_rules = build_permission_rules(
                    self.paths,
                    self.settings.user,
                    self.settings.project,
                    self.temporary_permission_settings,
                )
            self.session_path = self.session_store.save(self.session)

    def add_temporary_permission(self, access, directory):
        # 审批中的“本会话允许”写入当前 session。
        # 规则仍走 build_permission_rules 聚合，保证默认/settings/临时规则语义一致。
        self.add_temporary_approval({"access": access, "path": directory})

    def add_temporary_shell_permission(self, subject):
        self.add_temporary_approval({"shell_subject": subject})
