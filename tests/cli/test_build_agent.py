"""CLI agent 装配测试。

覆盖模块：cli.build_agent、provider/model 切换、secret 环境变量收集。
重点边界：命令行 secret、默认 secret、项目 .env、环境变量兼容配置、切换 provider/model。
"""

import json
import os
from unittest.mock import patch

import pytest

from codemate import cli as mini_cli
from tests.helpers import isolated_env


def test_cli_skill_evolution_override_is_tri_state():
    parser = mini_cli.build_arg_parser()

    assert parser.parse_args([]).skill_evolution is None
    assert parser.parse_args(["--skill-evolution"]).skill_evolution is True
    assert parser.parse_args(["--no-skill-evolution"]).skill_evolution is False


@pytest.mark.parametrize(
    ("configured_enabled", "cli_options", "expected_enabled"),
    [
        (False, ["--skill-evolution"], True),
        (True, ["--no-skill-evolution"], False),
        (True, ["--skill-evolution", "--benchmark"], False),
    ],
)
def test_cli_skill_evolution_override_precedence(
    tmp_path, configured_enabled, cli_options, expected_enabled
):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            pass

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    settings_dir = tmp_path / ".codemate"
    settings_dir.mkdir(exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"skill_evolution": {"enabled": configured_enabled}}) + "\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, isolated_env(tmp_path), clear=True), patch(
        "codemate.cli.OllamaModelClient", DummyModelClient
    ):
        args = mini_cli.build_arg_parser().parse_args(
            ["--cwd", str(tmp_path), *cli_options]
        )
        agent = mini_cli.build_agent(args)

    assert agent.skill_evolution.enabled is expected_enabled


def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}), clear=True), patch(
        "codemate.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--approval",
                "auto",
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = mini_cli.build_agent(args)
        assert {"GITHUB_PAT", "GH_PAT"} <= set(agent.secret_env_summary()["secret_env_names"])

def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path, {"GH_PAT": "ghp-default-1"}), clear=True), patch(
        "codemate.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert "GH_PAT" in agent.secret_env_summary()["secret_env_names"]

def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CODEMATE_DEEPSEEK_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path), clear=True), patch("codemate.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = mini_cli.build_agent(args)
        assert "CODEMATE_DEEPSEEK_API_KEY" in agent.secret_env_summary()["secret_env_names"]
        assert agent.model_client.kwargs["prompt_cache"] is False


def test_cli_anthropic_provider_enables_prompt_cache_by_default(tmp_path):
    class DummyModelClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    args = mini_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--provider", "anthropic"]
    )
    with patch.dict(os.environ, isolated_env(tmp_path), clear=True), patch(
        "codemate.cli.AnthropicCompatibleModelClient",
        DummyModelClient,
    ):
        client = mini_cli._build_model_client(args)

    assert client.kwargs["prompt_cache"] is True


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        isolated_env(
            tmp_path,
            {
                "MCA_CUSTOM_SECRET": "custom-secret-value",
                "MINI_CODING_AGENT_SECRET_ENV_NAMES": "MCA_CUSTOM_SECRET",
            },
        ),
        clear=True,
    ), patch("codemate.cli.OllamaModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert "MCA_CUSTOM_SECRET" in agent.secret_env_summary()["secret_env_names"]

def test_cli_build_switched_model_client_overrides_provider_and_model(tmp_path):
    class DummyOpenAIClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.model = kwargs["model"]
            self.base_url = kwargs["base_url"]

    args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek", "--model", "deepseek-v4-pro"])

    with patch.dict(os.environ, {}, clear=True), patch("codemate.cli.OpenAICompatibleModelClient", DummyOpenAIClient):
        client = mini_cli._build_switched_model_client(args, "openai", "gpt-5.5")

    assert client.model == "gpt-5.5"
    assert client.kwargs["temperature"] == args.temperature
    assert client.base_url == mini_cli.DEFAULT_OPENAI_BASE_URL

# run_shell只传allow_list，读不到MCA_ALLOWLIST_SECRET
