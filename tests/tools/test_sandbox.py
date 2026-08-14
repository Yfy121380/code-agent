"""shell 沙箱测试。

覆盖模块：tools.sandbox、run_shell 沙箱接入。
重点边界：三态配置、bwrap 命令构造、read deny 覆盖、write allow 绑定、preflight 降级、full 跳过沙箱。
"""

import signal
import subprocess
from unittest.mock import Mock, patch

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.config.settings import default_settings
from codemate.tools.sandbox import build_shell_sandbox_command


def write_project_settings(tmp_path, sandbox_mode="required", permissions=None):
    config_dir = tmp_path / ".codemate"
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "mcp": {"servers": {}},
        "sandbox": {"mode": sandbox_mode},
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


class RememberShellUI(AllowOnceUI):
    def approval_request(self, name, args, metadata=None):
        return {
            "allowed": True,
            "remember": {"shell_subject": metadata["suggested_shell_subject"]},
        }


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


def test_default_settings_require_shell_sandbox():
    assert default_settings()["sandbox"]["mode"] == "required"


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


def test_run_shell_uses_bwrap_when_sandbox_required(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="required")
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.Popen"
    ) as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": "mkdir logs", "timeout": 20})

    call = fake_popen.call_args
    assert call.args[0][0].endswith("bwrap")
    assert call.kwargs["shell"] is False
    assert assert_option(call.args[0], "--bind", str(tmp_path.resolve()), str(tmp_path.resolve()))
    assert "ok" in result


def test_allow_once_write_adds_current_shell_path_to_sandbox_only(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="required")
    outside_dir = tmp_path.parent.parent / f"{tmp_path.name}-outside-write-dir"
    outside_dir.mkdir(exist_ok=True)
    agent = build_agent(tmp_path, approval_policy="ask", ui=AllowOnceUI())
    before_rules = tuple(agent.permission_rules.write_allow)

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.Popen"
    ) as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": f"echo hi > {outside_dir / 'out.txt'}", "timeout": 20})

    call = fake_popen.call_args
    assert "ok" in result
    assert assert_option(call.args[0], "--bind", str(outside_dir.resolve()), str(outside_dir.resolve()))
    assert tuple(agent.permission_rules.write_allow) == before_rules


def test_temporary_shell_subject_does_not_add_unapproved_sandbox_write_mount(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="required")
    outside_dir = tmp_path.parent.parent / f"{tmp_path.name}-outside-python-dir"
    outside_dir.mkdir(exist_ok=True)
    agent = build_agent(tmp_path, approval_policy="ask", ui=RememberShellUI())
    write_command = f"python -c \"open('{outside_dir / 'out.txt'}', 'w').write('x')\""

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.Popen"
    ) as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        first = agent.run_tool("run_shell", {"command": "python -c 'print(1)'", "timeout": 20})
        second = agent.run_tool("run_shell", {"command": write_command, "timeout": 20})

    second_args = fake_popen.call_args_list[1].args[0]
    assert "ok" in first
    assert "ok" in second
    assert agent.temporary_permission_settings["shell"]["allow_subjects"] == ["python"]
    assert not assert_option(second_args, "--bind", str(outside_dir.resolve()), str(outside_dir.resolve()))


def test_required_sandbox_reports_bwrap_preflight_error(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="required")
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value="shell sandbox failed to start: denied"):
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert "exit_code: 126" in result
    assert "shell sandbox failed to start: denied" in result
    assert agent._last_tool_result_metadata["sandbox_mode"] == "required"
    assert agent._last_tool_result_metadata["sandbox_status"] == "unavailable"
    assert agent._last_tool_result_metadata["sandbox_degraded"] is False


def test_optional_sandbox_explicitly_degrades_when_preflight_fails(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="optional")
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch(
        "codemate.tools.handlers.sandbox_preflight_error",
        return_value="shell sandbox failed to start: denied",
    ), patch("codemate.tools.handlers.subprocess.Popen") as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    call = fake_popen.call_args
    assert call.args[0] == "echo hi"
    assert call.kwargs["shell"] is True
    assert "sandbox_warning:" in result
    assert "ok" in result
    assert agent._last_tool_result_metadata["sandbox_mode"] == "optional"
    assert agent._last_tool_result_metadata["sandbox_status"] == "degraded"
    assert agent._last_tool_result_metadata["sandbox_degraded"] is True


def test_optional_sandbox_uses_bwrap_when_preflight_succeeds(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="optional")
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.handlers.sandbox_preflight_error", return_value=""), patch(
        "codemate.tools.handlers.subprocess.Popen"
    ) as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    call = fake_popen.call_args
    assert call.args[0][0].endswith("bwrap")
    assert call.kwargs["shell"] is False
    assert "sandbox_warning:" not in result


def test_disabled_sandbox_runs_without_preflight(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="disabled")
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.handlers.sandbox_preflight_error") as fake_preflight, patch(
        "codemate.tools.handlers.subprocess.Popen"
    ) as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    call = fake_popen.call_args
    assert call.args[0] == "echo hi"
    assert call.kwargs["shell"] is True
    fake_preflight.assert_not_called()
    assert "sandbox_warning:" not in result


def test_full_approval_skips_shell_sandbox(tmp_path):
    write_project_settings(tmp_path, sandbox_mode="required")
    agent = build_agent(tmp_path, approval_policy="full")

    with patch("codemate.tools.handlers.sandbox_preflight_error") as fake_preflight, patch(
        "codemate.tools.handlers.build_shell_sandbox_command"
    ) as fake_sandbox_command, patch("codemate.tools.handlers.subprocess.Popen") as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("ok\n", "")
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    call = fake_popen.call_args
    assert "ok" in result
    assert call.args[0] == "echo hi"
    assert call.kwargs["shell"] is True
    fake_preflight.assert_not_called()
    fake_sandbox_command.assert_not_called()


def test_run_shell_timeout_terminates_process_group(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")
    process = Mock()
    process.pid = 1234
    process.returncode = -15
    process.poll.return_value = None
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("sleep", 1, output="partial\n", stderr=""),
        ("partial\n", ""),
    ]

    with patch("codemate.tools.handlers.subprocess.Popen", return_value=process), patch(
        "codemate.tools.handlers.os.killpg"
    ) as killpg:
        result = agent.run_tool("run_shell", {"command": "sleep 5", "timeout": 1})

    killpg.assert_called_once_with(1234, 15)
    assert "exit_code: 124" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "tool_timeout"
    assert agent._last_tool_result_metadata["tool_timeout"] is True


def test_run_shell_unexpected_failure_terminates_process_group(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")
    process = Mock()
    process.pid = 1234
    process.poll.return_value = None
    process.communicate.side_effect = RuntimeError("pipe failed")

    with patch("codemate.tools.handlers.subprocess.Popen", return_value=process), patch(
        "codemate.tools.handlers.os.killpg"
    ) as killpg:
        result = agent.run_tool("run_shell", {"command": "sleep 5", "timeout": 1})

    killpg.assert_called_once_with(1234, signal.SIGKILL)
    assert "pipe failed" in result
