"""todo_write 工具测试。

覆盖模块：todo_write schema/状态机/session 更新。
重点边界：状态枚举、单 in_progress 限制、空内容、read_only 允许、完成后清空。
"""

from tests.helpers import build_agent


def test_todo_write_updates_session_without_workspace_change(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Inspect current implementation",
                    "status": "completed",
                    "tasks": [{"description": "Read tool files", "status": "completed"}],
                },
                {
                    "phase": "Add todo_write tool",
                    "status": "in_progress",
                    "tasks": [{"description": "Update handler", "status": "in_progress"}],
                },
                {"phase": "Add tests", "status": "pending", "tasks": []},
            ]
        },
    )

    assert "todos updated: 3 phases, 2 tasks, 1 phase in_progress, 1 task in_progress" in result
    assert agent.session["todos"] == [
        {
            "phase": "Inspect current implementation",
            "status": "completed",
            "tasks": [{"description": "Read tool files", "status": "completed"}],
        },
        {
            "phase": "Add todo_write tool",
            "status": "in_progress",
            "tasks": [{"description": "Update handler", "status": "in_progress"}],
        },
        {"phase": "Add tests", "status": "pending", "tasks": []},
    ]
    assert agent._last_tool_result_metadata["read_only"] is True
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"

def test_todo_write_rejects_invalid_status_and_multiple_in_progress(tmp_path):
    agent = build_agent(tmp_path, [])

    invalid_status = agent.run_tool("todo_write", {"todos": [{"phase": "Do work", "status": "blocked", "tasks": []}]})
    multiple_active = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {"phase": "Do first thing", "status": "in_progress", "tasks": []},
                {"phase": "Do second thing", "status": "in_progress", "tasks": []},
            ]
        },
    )
    multiple_active_tasks = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Do work",
                    "status": "in_progress",
                    "tasks": [
                        {"description": "Do first thing", "status": "in_progress"},
                        {"description": "Do second thing", "status": "in_progress"},
                    ],
                }
            ]
        },
    )

    assert "status must be one of" in invalid_status
    assert "at most one phase may be in_progress" in multiple_active
    assert "at most one task may be in_progress within the same phase" in multiple_active_tasks
    assert agent.session["todos"] == []

def test_todo_write_rejects_empty_content(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("todo_write", {"todos": [{"phase": "  ", "status": "pending", "tasks": []}]})

    assert "phase must not be empty" in result
    assert agent.session["todos"] == []

def test_read_only_policy_allows_todo_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("todo_write", {"todos": [{"phase": "Inspect files", "status": "pending", "tasks": []}]})

    assert "todos updated: 1 phases" in result
    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    assert agent.session["todos"] == [{"phase": "Inspect files", "status": "pending", "tasks": []}]

def test_todo_write_rejects_inconsistent_phase_and_task_status(tmp_path):
    agent = build_agent(tmp_path, [])

    pending_with_done_task = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Implement changes",
                    "status": "pending",
                    "tasks": [{"description": "Fix bug", "status": "completed"}],
                }
            ]
        },
    )
    completed_with_pending_task = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Implement changes",
                    "status": "completed",
                    "tasks": [{"description": "Fix bug", "status": "pending"}],
                }
            ]
        },
    )

    assert "pending phase cannot contain completed or in_progress tasks" in pending_with_done_task
    assert "completed phase cannot contain pending or in_progress tasks" in completed_with_pending_task
    assert agent.session["todos"] == []

def test_todo_write_empty_or_all_completed_clears_session(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["todos"] = [{"phase": "Old task", "status": "pending", "tasks": []}]

    cleared = agent.run_tool("todo_write", {"todos": []})
    agent.session["todos"] = [{"phase": "Old task", "status": "pending", "tasks": []}]
    completed = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Inspect implementation",
                    "status": "completed",
                    "tasks": [{"description": "Read files", "status": "completed"}],
                },
                {"phase": "Run tests", "status": "completed", "tasks": []},
            ]
        },
    )

    assert cleared == "todos updated: todo list cleared."
    assert "all phases completed; todo list cleared" in completed
    assert agent.session["todos"] == []
