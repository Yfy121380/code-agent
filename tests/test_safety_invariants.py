import os
import shlex
import sys
from unittest.mock import patch

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate import cli as mini_cli
from codemate.task_state import TaskState


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

# 路径逃逸拒绝
def test_workspace_escape_is_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "../outside.txt"})

    assert "path escapes workspace" in result

# 符号链接逃逸拒绝
def test_symlink_path_traversal_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "path escapes workspace" in result

def test_grep_path_escape_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "abc", "path": "../outside"})

    assert "path escapes workspace" in result


def test_search_tool_name_is_not_registered(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("search", {"pattern": "demo", "path": "."})

    assert result == "error: unknown tool 'search'"


def test_grep_files_with_matches_returns_only_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("nothing\nneedle too\n", encoding="utf-8")
    (tmp_path / "src" / "gamma.py").write_text("nothing\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "needle", "path": "src", "mode": "files_with_matches"})

    assert "src/alpha.py" in result
    assert "src/beta.py" in result
    assert "src/gamma.py" not in result
    assert "needle here" not in result


def test_grep_count_returns_per_file_counts_and_total(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("needle needle\nneedle\n", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("needle\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "needle", "path": "src", "mode": "count"})

    assert "total_matches: 4" in result
    assert "src/alpha.py: 3" in result
    assert "src/beta.py: 1" in result


def test_grep_content_context_supports_before_after_priority_over_context(tmp_path):
    (tmp_path / "sample.txt").write_text(
        "one\ntwo\nneedle\nfour\nfive\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "grep",
        {"pattern": "needle", "path": "sample.txt", "mode": "content", "context": 2, "before": 1, "after": 0},
    )

    assert "sample.txt-2-two" in result
    assert "sample.txt:3:needle" in result
    assert "sample.txt-1-one" not in result
    assert "sample.txt-4-four" not in result


def test_grep_rejects_context_for_non_content_modes(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "demo", "path": ".", "mode": "count", "context": 1})

    assert "before/after/context are only valid when mode='content'" in result

def test_patch_file_requires_fresh_read_first(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"


def test_grep_does_not_satisfy_edit_read_requirement(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    grep_result = agent.run_tool("grep", {"pattern": "alpha", "path": "target.txt"})
    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "target.txt:1:alpha" in grep_result
    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"


def test_patch_file_allows_freshly_read_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    read_result = agent.run_tool("read_file", {"path": "target.txt", "start": 1, "end": 10})
    agent.record({"role": "tool", "name": "read_file", "args": {"path": "target.txt", "start": 1, "end": 10}, "content": read_result, "created_at": "2026-04-09T00:00:00+00:00"})
    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert result == "patched target.txt"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "beta\n"


def test_patch_file_rejects_stale_read_after_external_change(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    read_result = agent.run_tool("read_file", {"path": "target.txt", "start": 1, "end": 10})
    agent.record({"role": "tool", "name": "read_file", "args": {"path": "target.txt", "start": 1, "end": 10}, "content": read_result, "created_at": "2026-04-09T00:00:00+00:00"})
    (tmp_path / "target.txt").write_text("alpha changed\n", encoding="utf-8")

    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha changed\n"


def test_write_file_allows_new_file_without_prior_read(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "created.txt", "content": "new\n"})

    assert result == "wrote created.txt (4 chars)"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "new\n"


def test_write_file_requires_fresh_read_for_existing_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"

# 危险工具审批拒绝
def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: approval denied for run_shell"

"""
secret来源
命令行 --secret-env-name
默认根据环境中敏感名称推断
.env 中的 provider key
MINI_CODING_AGENT_SECRET_ENV_NAMES 配置
"""
def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
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

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
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

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CODEMATE_DEEPSEEK_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True), patch("codemate.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = mini_cli.build_agent(args)
        assert "CODEMATE_DEEPSEEK_API_KEY" in agent.secret_env_summary()["secret_env_names"]


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        {
            "MCA_CUSTOM_SECRET": "custom-secret-value",
            "MINI_CODING_AGENT_SECRET_ENV_NAMES": "MCA_CUSTOM_SECRET",
        },
        clear=True,
    ), patch("codemate.cli.OllamaModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert "MCA_CUSTOM_SECRET" in agent.secret_env_summary()["secret_env_names"]

# run_shell只传allow_list，读不到MCA_ALLOWLIST_SECRET
def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="auto")
    script = 'import os; print(os.getenv("MCA_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_bound_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.subprocess.run") as fake_run:
        fake_run.return_value = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "toolkit-shell\n", "stderr": ""},
        )()
        shell_result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_run.assert_called_once()
    assert agent.tool_run_shell.__func__.__module__ == "codemate.runtime"

    with patch("codemate.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"task": "inspect README.md", "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()

# delegate 超过 max_depth 会被拒绝
def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"task": "inspect README.md", "max_steps": 2})
    except ValueError as exc:
        assert "delegate depth exceeded" in str(exc)
    else:
        raise AssertionError("delegate depth validation did not fail")

# delegate 创建的 child agent 是 read_only
def test_delegate_child_is_read_only(tmp_path):
    target = tmp_path / "child-was-not-allowed.txt"
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"write a file","max_steps":2}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>child done</final>",
            "<final>parent done</final>",
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]

# 构造包含 secret 值的 payload，然后写 trace 和 report。
def test_configured_secret_env_names_are_redacted_in_trace_and_report(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}, clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)
        agent.run_store.write_report(
            state,
            agent.redact_artifact({"task_state": state.to_dict(), "payload": payload}),
        )

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert github_pat not in report_text
    assert gh_pat not in report_text
    assert trace_text.count("<redacted>") >= 4
    assert report_text.count("<redacted>") >= 4
