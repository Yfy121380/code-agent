"""history compact 测试。

覆盖模块：runtime.compaction。
重点边界：summary 更新、recent history 保留、失败三次回滚、tool_call/tool_result 配对、预算超限自动触发。
"""

from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate.runtime.compaction import SUMMARY_WRAPPER_PREFIX


SUMMARY = """## Working Directory
- `/tmp/project`: demo project.

## User Preferences And Constraints
- Use Chinese.

## Current State
- History compaction is being implemented.

## Key Decisions
- Keep recent history verbatim and summarize older history.

## Changed Files
- `codemate/runtime/compaction.py`: added compact flow.

## Validation And Issues
- None.
"""


def build_agent(tmp_path, outputs=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags={"memory_dream": False, "memory_candidates": False},
    )


def add_plain_history(agent, count):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        agent.record({"role": role, "content": f"message-{index}", "created_at": f"2026-07-21T00:{index:02d}:00+00:00"})


def test_history_compaction_updates_summary_and_keeps_recent_messages(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    add_plain_history(agent, 26)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert agent.session["history_summary"].startswith("## Working Directory")
    assert len(agent.session["history"]) == 20
    assert agent.session["history"][0]["content"] == "message-6"
    assert "message-0" in agent.model_client.prompts[0]
    assert "message-25" not in agent.model_client.prompts[0]
    assert agent.model_client.prompts[0].count("Please summarize the conversation history above") == 1


def test_existing_history_summary_is_wrapped_for_next_compaction(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    agent.session["history_summary"] = SUMMARY
    add_plain_history(agent, 24)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert SUMMARY_WRAPPER_PREFIX in agent.model_client.prompts[0]
    assert "## User Preferences And Constraints" in agent.model_client.prompts[0]


def test_history_compaction_failure_retries_three_times_and_preserves_history(tmp_path):
    agent = build_agent(tmp_path, ["invalid summary", "", ModelResponse.tool_call("read_file", {"path": "README.md"})])
    add_plain_history(agent, 24)
    original_history = list(agent.session["history"])

    result = agent.compact_history(reason="manual")

    assert result["status"] == "error"
    assert result["attempts"] == 3
    assert agent.session["history"] == original_history
    assert agent.session["history_summary"] == ""
    assert len(agent.model_client.prompts) == 3


def test_history_compaction_keeps_tool_call_result_pairs_in_recent_history(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    add_plain_history(agent, 8)
    call_id = "call_read"
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": "read_file", "args": {"path": "README.md"}}],
            "created_at": "2026-07-21T00:20:00+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": "# README.md\ndemo",
            "created_at": "2026-07-21T00:21:00+00:00",
        }
    )
    add_plain_history(agent, 18)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    tool_results = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert tool_results and tool_results[0]["tool_call_id"] == call_id
    assistant_calls = [item for item in agent.session["history"] if item.get("role") == "assistant" and item.get("tool_calls")]
    assert assistant_calls and assistant_calls[0]["tool_calls"][0]["id"] == call_id


def test_ask_auto_compacts_before_model_request_when_budget_is_high(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMATE_CONTEXT_TOKENS", "100")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.final(SUMMARY),
            ModelResponse.final("done", metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        ],
    )
    agent.feature_flags["long_term_memory"] = False
    add_plain_history(agent, 26)
    agent.update_token_usage_from_model({"input_tokens": 95, "output_tokens": 1, "total_tokens": 96})

    result = agent.ask("continue")

    assert result == "done"
    assert len(agent.model_client.prompts) == 2
    assert agent.model_client.tool_specs[0] == []
    assert agent.model_client.tool_specs[1]
    assert agent.session["history_summary"].startswith("## Working Directory")


def test_compaction_restores_missing_skill_and_todo_context(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    agent.session["invoked_skills"] = [
        {
            "name": "backend",
            "root": str(tmp_path / ".codemate" / "skills" / "backend"),
            "content": "Follow the backend workflow.",
        }
    ]
    agent.session["todos"] = [
        {
            "phase": "Implement compact restoration",
            "status": "in_progress",
            "tasks": [{"description": "Add tests", "status": "in_progress"}],
        }
    ]
    add_plain_history(agent, 26)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert [item["kind"] for item in agent.session["history"][:2]] == ["skill_context", "todo_context"]
    assert "Follow the backend workflow." in agent.session["history"][0]["content"]
    assert "Implement compact restoration" in agent.session["history"][1]["content"]


def test_compaction_does_not_restore_todo_when_recent_write_has_current_plan(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    todos = [{"phase": "Run focused tests", "status": "in_progress", "tasks": []}]
    agent.session["todos"] = todos
    add_plain_history(agent, 8)
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_todo", "name": "todo_write", "args": {"todos": todos}}],
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": "call_todo",
            "name": "todo_write",
            "content": "todos updated: 1 phases. Continue working through the active todo plan.",
        }
    )
    add_plain_history(agent, 18)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert not any(item.get("kind") == "todo_context" for item in agent.session["history"])
    assert any(
        call.get("name") == "todo_write"
        for item in agent.session["history"]
        for call in item.get("tool_calls", [])
    )


def test_compaction_does_not_restore_todo_when_recent_list_has_current_plan(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    todos = [{"phase": "Review context state", "status": "in_progress", "tasks": []}]
    agent.session["todos"] = todos
    add_plain_history(agent, 8)
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_todo_list", "name": "todo_list", "args": {}}],
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": "call_todo_list",
            "name": "todo_list",
            "content": "Active todo plan:\n1. [in_progress] Review context state",
        }
    )
    add_plain_history(agent, 18)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert not any(item.get("kind") == "todo_context" for item in agent.session["history"])


def test_compaction_does_not_restore_skill_when_recent_load_is_retained(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY)])
    agent.session["invoked_skills"] = [
        {"name": "backend", "root": str(tmp_path / "skill"), "content": "Follow the backend workflow."}
    ]
    add_plain_history(agent, 8)
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_skill", "name": "skill_load", "args": {"name": "backend"}}],
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": "call_skill",
            "name": "skill_load",
            "content": (
                f"Skill loaded: backend\nRoot: {tmp_path / 'skill'}\n\n"
                "Instructions:\nFollow the backend workflow."
            ),
        }
    )
    add_plain_history(agent, 18)

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert not any(item.get("kind") == "skill_context" for item in agent.session["history"])


def test_compaction_replaces_old_restoration_messages_instead_of_accumulating(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final(SUMMARY), ModelResponse.final(SUMMARY)])
    agent.session["invoked_skills"] = [
        {"name": "backend", "root": str(tmp_path / "skill"), "content": "Keep this instruction."}
    ]
    add_plain_history(agent, 26)
    assert agent.compact_history(reason="manual")["status"] == "ok"
    add_plain_history(agent, 22)

    assert agent.compact_history(reason="manual")["status"] == "ok"

    restored = [item for item in agent.session["history"] if item.get("kind") == "skill_context"]
    assert len(restored) == 1
