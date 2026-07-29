"""测试 helper。

这个模块只放跨多个测试模块复用的装配逻辑：
- 构造隔离 workspace 和 MiniAgent
- 构造测试 skill
- 模拟可记住本会话 allow 规则的审批 UI
- 为 CLI 测试准备隔离 HOME / CODEMATE_HOME
"""

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext


class RememberingApprovalUI:
    """审批时选择“本会话记住该目录”的测试 UI。"""

    def __init__(self):
        self.calls = []

    def approval_request(self, name, args, metadata=None):
        metadata = dict(metadata or {})
        self.calls.append({"name": name, "args": dict(args or {}), "metadata": metadata})
        return {
            "allowed": True,
            "remember": {
                "access": metadata["approval_access"],
                "path": metadata["suggested_allow_dir"],
            },
        }

    def tool_start(self, name, args, risk_level=""):
        pass

    def tool_result(self, name, args, result, metadata=None):
        pass

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def stream_start(self, phase=""):
        pass

    def stream_delta(self, text, phase=""):
        pass

    def stream_end(self, kind="", metadata=None):
        pass

    def final_answer(self, text):
        pass


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def write_skill(tmp_path, name="backend", description="Backend workflow", body="Follow backend rules."):
    skill_dir = tmp_path / ".codemate" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def isolated_env(tmp_path, extra=None):
    """返回隔离的 HOME / CODEMATE_HOME 环境变量集合。"""

    values = {
        "HOME": str(tmp_path.parent),
        "CODEMATE_HOME": str(tmp_path.parent / f"{tmp_path.name}-home" / ".codemate"),
    }
    values.update(extra or {})
    return values
