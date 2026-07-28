from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate import cli
from codemate.ui.banner import build_welcome
import unicodedata


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in str(text))


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    model = FakeModelClient(outputs)
    model.supports_session_title = True
    return MiniAgent(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags={"long_term_memory": False, "memory_dream": False},
    )


def test_session_title_generated_after_first_final_answer(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.final("完成。"),
            ModelResponse.final("实现 history compact。"),
        ],
    )

    result = agent.ask("帮我实现 history compact")

    assert result == "完成。"
    assert agent.session["title"] == "实现 history compact"
    assert agent.session["title_slug"] == "实现-history-compact"
    assert agent.model_client.tool_specs[-1] == []
    assert agent.session_store.load(agent.session["id"])["title"] == "实现 history compact"


def test_session_title_is_not_regenerated_when_title_exists(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("完成。")])
    agent.rename_session("已有标题")

    result = agent.ask("继续")

    assert result == "完成。"
    assert agent.session["title"] == "已有标题"
    assert len(agent.model_client.prompts) == 1


def test_resume_without_value_enters_session_selection_mode():
    args = cli.build_arg_parser().parse_args(["--resume"])

    assert args.resume == cli.RESUME_SELECT


def test_benchmark_flag_disables_cross_session_memory_and_title(tmp_path, monkeypatch):
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--benchmark"])
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_build_model_client", lambda _args: FakeModelClient([]))

    agent = cli.build_agent(args)

    assert args.benchmark is True
    for name in (
        "long_term_memory",
        "relevant_memory",
        "memory_candidates",
        "memory_dream",
        "session_title",
    ):
        assert agent.feature_enabled(name) is False
    assert agent.feature_enabled("memory") is True
    agent.close()


def test_session_title_normalization_removes_markers_and_limits_chinese(tmp_path):
    agent = build_agent(tmp_path, [])

    title = agent.rename_session("你好我在这儿可以帮你写代码<CPA_DONE>")

    assert title == "你好我在这儿可以帮你"
    assert agent.session["title"] == "你好我在这儿可以帮你"


def test_welcome_banner_clips_long_session_title(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["title"] = "你好！我在这儿。可以帮你写代码、写脚本、排查问题"

    banner = build_welcome(agent, model="openai:gpt-5.4", host="")

    widths = {display_width(line) for line in banner.splitlines()}
    assert len(widths) == 1
    assert "..." in banner
