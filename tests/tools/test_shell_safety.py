"""shell 风险分类与执行门禁测试。

覆盖模块：tools/shell_safety、validators、run_shell。
重点边界：read/risky/dangerous/hard-blocked 分类、glob 和重定向路径、read_only/full/auto 策略差异。
"""

import shlex
import sys
import os

from unittest.mock import patch

from tests.helpers import build_agent


def test_read_shell_command_allows_valid_workspace_paths_in_read_only_policy(tmp_path):
    (tmp_path / "notes.txt").write_text("safe read\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "cat notes.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "safe read" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"
    assert agent._last_tool_result_metadata["risk_level"] == "low"

def test_read_shell_command_allows_globs_when_paths_stay_in_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "cat *.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "alpha" in result
    assert "beta" in result
    assert agent._last_tool_result_metadata["shell_has_glob"] is True

def test_shell_read_outside_workspace_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-shell.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": f"cat ../../{outside.name}", "timeout": 20})

    assert "exit_code: 0" in result
    assert "outside" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"

def test_risky_shell_command_auto_policy_allows_workspace_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "mkdir logs", "timeout": 20})

    assert "exit_code: 0" in result
    assert (tmp_path / "logs").is_dir()
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"

def test_shell_redirection_is_risky_and_auto_policy_allows_workspace_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "echo hi > out.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "hi"
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"
    assert agent._last_tool_result_metadata["shell_has_redirection"] is True

def test_risky_shell_command_rejects_glob_write(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "backup").mkdir()
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "mv *.py backup/", "timeout": 20})

    assert "wildcards are not allowed for risky shell commands" in result
    assert (tmp_path / "a.py").exists()

def test_dangerous_shell_command_auto_policy_still_requires_approval(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("builtins.input", return_value="n"):
        result = agent.run_tool("run_shell", {"command": "rm README.md", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert (tmp_path / "README.md").exists()
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"

def test_python_script_shell_command_is_dangerous(tmp_path):
    (tmp_path / "test.py").write_text("print('hi')\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "python test.py", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"

def test_python_inline_code_is_dangerous_without_path_glob_rejection(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "python -c 'print(route.dependant.body_params[0].type_)'", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"
    assert agent._last_tool_result_metadata["shell_paths"] == []
    assert agent._last_tool_result_metadata["shell_has_glob"] is False

def test_python_py_compile_shell_command_stays_read(tmp_path):
    (tmp_path / "test.py").write_text("print('hi')\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "python -m py_compile test.py", "timeout": 20})

    assert "exit_code: 0" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"

def test_pytest_shell_command_is_risky_not_read(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="ask")

    result = agent.run_tool("run_shell", {"command": "pytest", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"

def test_dangerous_shell_command_full_policy_allows_without_prompt(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="full")

    result = agent.run_tool("run_shell", {"command": "rm README.md", "timeout": 20})

    assert "exit_code: 0" in result
    assert not (tmp_path / "README.md").exists()
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"

def test_hard_blocked_shell_command_full_policy_still_rejects(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="full")

    result = agent.run_tool("run_shell", {"command": "reboot", "timeout": 20})

    assert "shell command is blocked even in full approval mode: reboot" in result
    assert agent._last_tool_result_metadata["shell_blocked"] is True
    assert "hard_blocked_shell_command" in agent._last_tool_result_metadata["shell_reasons"]

def test_dangerous_shell_command_rejects_glob_without_approval(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "rm *.md", "timeout": 20})

    assert "wildcards are not allowed for dangerous shell commands" in result
    assert (tmp_path / "README.md").exists()

def test_dangerous_shell_command_rejects_root_targets_without_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "rm -rf /", "timeout": 20})

    assert "dangerous shell target is blocked: /" in result


# read_only 模式拒绝非读取 shell 命令

def test_read_only_policy_blocks_write_like_shell(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: write operations are blocked in read-only mode"

"""
secret来源
命令行 --secret-env-name
默认根据环境中敏感名称推断
.env 中的 provider key
MINI_CODING_AGENT_SECRET_ENV_NAMES 配置
"""

def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="full")
    script = 'import os; print(os.getenv("MCA_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result
