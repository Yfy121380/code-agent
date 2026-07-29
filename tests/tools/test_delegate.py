"""delegate 子 agent 测试。

覆盖模块：delegate 工具、子 agent 权限、并发调查和临时权限继承。
重点边界：depth 限制、read_only 子 agent、多个调查任务、workspace 外临时读权限继承。
"""

import json

from codemate import ModelResponse
from tests.helpers import build_agent


def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"tasks": [{"task": "inspect README.md"}], "max_steps": 2})
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
            ModelResponse.tool_call("delegate", {"tasks": [{"task": "write a file"}], "max_steps": 2}),
            ModelResponse.tool_call("write_file", {"path": "child-was-not-allowed.txt", "content": "nope"}),
            ModelResponse.final("child done"),
            ModelResponse.final("parent done"),
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]

def test_delegate_runs_multiple_read_only_investigations(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("first report"), ModelResponse.final("second report")], approval_policy="auto")

    result = agent.run_tool(
        "delegate",
        {
            "tasks": [
                {"task": "inspect README", "focus": "README.md"},
                {"task": "inspect tests", "focus": "tests"},
            ],
            "max_steps": 2,
        },
    )

    assert "Task 1: inspect README" in result
    assert "Task 2: inspect tests" in result
    assert "Status: ok" in result
    assert "first report" in result
    assert "second report" in result
    assert agent._last_tool_result_metadata["delegate_task_count"] == 2
    assert [item["status"] for item in agent._last_tool_result_metadata["delegate_tasks"]] == ["ok", "ok"]

def test_delegate_child_inherits_temporary_read_permissions(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-delegate-outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "note.txt"
    outside_file.write_text("delegated evidence\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": str(outside_file), "start": 1, "end": 1}),
            ModelResponse.final("read delegated evidence"),
        ],
        approval_policy="ask",
    )
    agent.add_temporary_permission("read", outside_dir)

    result = agent.run_tool("delegate", {"tasks": [{"task": "read outside note", "focus": str(outside_file)}], "max_steps": 3})

    assert "Status: ok" in result
    assert "read delegated evidence" in result
    child_sessions = [item for item in agent.session_store.root.iterdir() if item.name.startswith("delegate-")]
    assert child_sessions
    child_session = json.loads((child_sessions[0] / "session.json").read_text(encoding="utf-8"))
    assert child_session["temporary_permissions"]["permissions"]["read"]["allow"] == [str(outside_dir.resolve())]
    child_tool_results = [item for item in child_session["history"] if item.get("role") == "tool"]
    assert child_tool_results
    assert "delegated evidence" in child_tool_results[0]["content"]

# 构造包含 secret 值的 payload，然后写 trace。
