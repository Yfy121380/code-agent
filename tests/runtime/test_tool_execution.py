"""Runtime 绑定工具方法测试。

覆盖模块：runtime.tool_execution 与 codemate.tools 的委派边界。
重点边界：bound method 不绕过工具模块、delegate 路由到统一工具实现。
"""

from unittest.mock import patch

from tests.helpers import build_agent


def test_bound_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.subprocess.Popen") as fake_popen:
        fake_popen.return_value.returncode = 0
        fake_popen.return_value.communicate.return_value = ("toolkit-shell\n", "")
        shell_result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_popen.assert_called_once()
    assert agent.tool_run_shell.__func__.__module__ == "codemate.runtime.tool_execution"

    with patch("codemate.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"tasks": [{"task": "inspect README.md"}]})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()

    with patch("codemate.tools.tool_review", return_value="toolkit-review") as fake_review:
        review_result = agent.tool_review({"task": "Review the current changes."})

    assert review_result == "toolkit-review"
    fake_review.assert_called_once()

# delegate 超过 max_depth 会被拒绝
