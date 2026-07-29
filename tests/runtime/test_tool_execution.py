"""Runtime 绑定工具方法测试。

覆盖模块：runtime.tool_execution 与 codemate.tools 的委派边界。
重点边界：bound method 不绕过工具模块、delegate 路由到统一工具实现。
"""

from unittest.mock import patch

from tests.helpers import build_agent


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
    assert agent.tool_run_shell.__func__.__module__ == "codemate.runtime.tool_execution"

    with patch("codemate.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"tasks": [{"task": "inspect README.md"}], "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()

# delegate 超过 max_depth 会被拒绝
