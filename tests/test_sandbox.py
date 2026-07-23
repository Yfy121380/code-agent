from unittest.mock import patch

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.config.settings import default_settings
from codemate.tools.sandbox import build_shell_sandbox_command


def write_project_settings(tmp_path, sandbox_enabled=True, permissions=None):
    config_dir = tmp_path / ".codemate"
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "mcp": {"servers": {}},
        "sandbox": {"enabled": sandbox_enabled},
        "permissions": permissions or {"read": {"allow": [], "deny": []}, "write": {"allow": [], "deny": []}},
    }
    import json

    (config_dir / "settings.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AllowOnceUI:
    def approval_request(self, name, args, metadata=None):
        return {"allowed": True}

    def tool_start(self, name, args, risk_level=""):
        pass

    def tool_result(self, name, args, result, metadata=None):
        pass


def build_agent(tmp_path, approval_policy="auto", ui=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        ui=ui,
    )


def assert_option(args, *option):
    option = list(option)
    return any(args[index:index + len(option)] == option for index in range(len(args) - len(option) + 1))


def test_default_settings_enable_shell_sandbox():
    assert default_settings()["sandbox"]["enabled"] is True


def test_build_shell_sandbox_command_uses_read_deny_and_write_allow(tmp_path):
    secret_dir = tmp_path / "secret"
    writable_dir = tmp_path / "allowed"
    secret_dir.mkdir()
    writable_dir.mkdir()
    write_project_settings(
        tmp_path,
        permissions={
            "read": {"allow": [], "deny": [str(secret_dir)]},
            "write": {"allow": [str(writable_dir)], "deny": []},
        },
    )
    agent = build_agent(tmp_path)

    args = build_shell_sandbox_command(agent, "echo ok")

    assert args[0].endswith("bwrap")
    assert assert_option(args, "--ro-bind", "/", "/")
    assert "--share-net" in args
    assert assert_option(args, "--tmpfs", "/tmp")
    assert assert_option(args, "--tmpfs", str(secret_dir.resolve()))
    assert assert_option(args, "--bind", str(writable_dir.resolve()), str(writable_dir.resolve()))
    assert args[-3:] == ["/bin/sh", "-lc", "echo ok"]


def test_build_shell_sandbox_command_skips_write_allow_under_read_deny(tmp_path):
    secret_dir = tmp_path / "secret"
    nested_write = secret_dir / "nested"
    nested_write.mkdir(parents=True)
    write_project_settings(
        tmp_path,
        permissions={
            "read": {"allow": [], "deny": [str(secret_dir)]},
            "write": {"allow": [str(nested_write)], "deny": []},
        },
    )
    agent = build_agent(tmp_path)

    args = build_shell_sandbox_command(agent, "echo ok")

    assert assert_option(args, "--tmpfs", str(secret_dir.resolve()))
    assert not assert_option(args, "--bind", str(nested_write.resolve()), str(nested_write.resolve()))


def test_run_shell_uses_bwrap_when_sandbox_enabled(tmp_path):
    write_project_settings(tmp_path, sandbox_enabled=True)
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.run"
    ) as fake_run:
        fake_run.return_value = type("Result", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        result = agent.run_tool("run_shell", {"command": "mkdir logs", "timeout": 20})

    call = fake_run.call_args
    assert call.args[0][0].endswith("bwrap")
    assert call.kwargs["shell"] is False
    assert assert_option(call.args[0], "--bind", str(tmp_path.resolve()), str(tmp_path.resolve()))
    assert "ok" in result


def test_allow_once_write_adds_current_shell_path_to_sandbox_only(tmp_path):
    write_project_settings(tmp_path, sandbox_enabled=True)
    outside_dir = tmp_path.parent.parent / f"{tmp_path.name}-outside-write-dir"
    outside_dir.mkdir(exist_ok=True)
    agent = build_agent(tmp_path, approval_policy="ask", ui=AllowOnceUI())
    before_rules = tuple(agent.permission_rules.write_allow)

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.run"
    ) as fake_run:
        fake_run.return_value = type("Result", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        result = agent.run_tool("run_shell", {"command": f"echo hi > {outside_dir / 'out.txt'}", "timeout": 20})

    call = fake_run.call_args
    assert "ok" in result
    assert assert_option(call.args[0], "--bind", str(outside_dir.resolve()), str(outside_dir.resolve()))
    assert tuple(agent.permission_rules.write_allow) == before_rules


def test_run_shell_reports_bwrap_preflight_error(tmp_path):
    write_project_settings(tmp_path, sandbox_enabled=True)
    agent = build_agent(tmp_path, approval_policy="full")

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value="shell sandbox failed to start: denied"):
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert "exit_code: 126" in result
    assert "shell sandbox failed to start: denied" in result
