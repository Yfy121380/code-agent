"""Runtime loop 消息记录测试。

覆盖模块：runtime loop、history 写入、重复工具调用保护。
重点边界：多 tool_call 合并为一条 assistant、commentary-only 继续执行、主 agent 无 max_steps 截断。
"""

from PIL import Image

from codemate import ModelResponse
from tests.helpers import build_agent


def test_repeated_tool_call_uses_assistant_tool_calls_and_excludes_current_call(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"path": "README.md", "start": 1, "end": 1}

    first = agent.run_tool("read_file", args)
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "read_file", "args": args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": first, "created_at": "2026-04-09T00:00:00+00:00"})

    second = agent.run_tool("read_file", args, current_tool_call_id="call_2")
    assert "# README.md" in second
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_2", "name": "read_file", "args": args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": second, "created_at": "2026-04-09T00:00:00+00:00"})

    third = agent.run_tool("read_file", args, current_tool_call_id="call_3")

    assert "repeated identical tool call" in third
    notes = agent.session["memory"]["process_notes"]
    assert notes[0]["kind"] == "repeated_call"

def test_runtime_records_one_assistant_message_for_multiple_tool_calls(tmp_path):
    call_id = "toolu_read"
    second_call_id = "toolu_list"
    response = ModelResponse.from_tool_calls(
        [
            {"id": call_id, "name": "read_file", "args": {"path": "README.md", "start": 1, "end": 1}},
            {"id": second_call_id, "name": "list_files", "args": {"path": "."}},
        ],
        text="我先检查 README 和目录结构。",
    )
    agent = build_agent(tmp_path, [response, ModelResponse.final("done")])

    result = agent.ask("inspect README")

    assistant_calls = [item for item in agent.session["history"] if item.get("role") == "assistant" and item.get("tool_calls")]
    assert result == "done"
    assert len(assistant_calls) == 1
    assert assistant_calls[0]["kind"] == "tool_calls"
    assert assistant_calls[0]["content"] == "我先检查 README 和目录结构。"
    assert [call["id"] for call in assistant_calls[0]["tool_calls"]] == [call_id, second_call_id]


def test_runtime_records_image_tool_content_blocks(tmp_path):
    Image.new("RGB", (5, 4), color="red").save(tmp_path / "shot.png")
    response = ModelResponse.tool_call("read_file", {"path": "shot.png"}, call_id="call_image")
    agent = build_agent(tmp_path, [response, ModelResponse.final("done")])
    agent.model_client.supports_images = True

    result = agent.ask("inspect screenshot")

    tool_messages = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert result == "done"
    assert tool_messages[0]["name"] == "read_file"
    assert tool_messages[0]["content_blocks"][0]["type"] == "image"
    assert "base64" not in tool_messages[0]["content"]

def test_runtime_records_commentary_response_and_continues(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.commentary("我先确认当前任务。"), ModelResponse.final("done")])

    result = agent.ask("say hello")

    assert result == "done"
    assert any(
        item.get("role") == "assistant" and item.get("kind") == "commentary" and item.get("content") == "我先确认当前任务。"
        for item in agent.session["history"]
    )

def test_main_agent_does_not_stop_at_max_steps(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": "README.md", "start": 1, "end": 1}, call_id="call_read"),
            ModelResponse.tool_call("list_files", {"path": "."}, call_id="call_list"),
            ModelResponse.final("done"),
        ],
        max_steps=1,
    )

    result = agent.ask("inspect more than one thing")

    assert result == "done"
    assert agent.current_task_state.tool_steps == 2
    assert agent.current_task_state.stop_reason == "final_answer_returned"

def test_repeated_call_process_note_clears_after_any_successful_tool(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"path": "README.md", "start": 1, "end": 1}

    for index in range(2):
        result = agent.run_tool("read_file", args)
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_{index}", "name": "read_file", "args": args}],
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "read_file",
                "content": result,
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )

    rejected = agent.run_tool("read_file", args, current_tool_call_id="call_2")
    assert "repeated identical tool call" in rejected
    assert agent.session["memory"]["process_notes"][0]["kind"] == "repeated_call"

    result = agent.run_tool("list_files", {"path": "."})

    assert "README.md" in result
    assert agent.session["memory"]["process_notes"] == []
