"""上下文预算测试。

覆盖模块：context.token_budget。
重点边界：模型上下文配置、环境变量覆盖、/budget 展示顺序、模型 usage 和工具结果估算合并。
"""

from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate.context.token_budget import compact_trigger_tokens, model_context_tokens


def build_agent(tmp_path, outputs=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags={"memory_dream": False},
    )


def test_context_token_budget_uses_model_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("CODEMATE_CONTEXT_TOKENS", raising=False)
    assert model_context_tokens("gpt-5.4") == 258000
    assert compact_trigger_tokens("gpt-5.4") == 232200

    monkeypatch.setenv("CODEMATE_CONTEXT_TOKENS", "1000")
    assert model_context_tokens("gpt-5.4") == 1000
    assert compact_trigger_tokens("gpt-5.4") == 900


def test_budget_report_shows_section_chars_before_context_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMATE_CONTEXT_TOKENS", "1000")
    agent = build_agent(tmp_path)
    agent.model_client.model = "gpt-5.4"
    agent.update_token_usage_from_model({"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})

    report = agent.budget_report(provider="openai")

    assert report.index("Sections by chars:") < report.index("Context budget:")
    assert report.index("Tool schemas:") < report.index("Context budget:")
    assert "- total:" in report
    assert "- tools count:" in report
    assert "- tool schemas:" in report
    assert "- Model: openai:gpt-5.4" in report
    assert "- Max context: 1000 tokens" in report
    assert "- Compact threshold: 900 tokens (90%)" in report
    assert "- Estimated current context: 150 tokens" in report
    assert "- Usage: 15.0%" in report


def test_model_usage_and_tool_result_estimate_update_budget_state(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.final(
                "done",
                metadata={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
            )
        ],
    )

    agent.ask("say done")
    assert agent.last_token_usage.to_dict() == {
        "input_tokens": 90,
        "output_tokens": 10,
        "tool_result_tokens_added": 0,
        "estimated_total_context_tokens": 100,
    }

    added = agent.add_tool_result_token_estimate("abcdef")
    assert added == 2
    assert agent.last_token_usage.tool_result_tokens_added == 2
    assert agent.last_token_usage.estimated_total_context_tokens == 102
