"""Runtime loop 消息记录测试。

覆盖模块：runtime loop、history 写入、重复工具调用保护。
重点边界：多 tool_call 合并为一条 assistant、commentary-only 继续执行、主 agent 无 max_steps 截断。
"""

from PIL import Image

import pytest

from codemate import ModelResponse, ModelStreamEvent
from codemate.models import ModelStreamIncompleteError
from codemate.context.history import INTERRUPTED_TOOL_RESULT, repair_incomplete_tool_results
from codemate.models.anthropic import _to_anthropic_messages
from codemate.runtime.errors import ModelRequestError
from codemate.storage import PersistenceError
from tests.helpers import build_agent


class RecordingUI:
    def __init__(self):
        self.streamed = []
        self.commentary_messages = []
        self.final_messages = []
        self.tool_results = []
        self.stream_end_kinds = []

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def stream_start(self, phase=""):
        pass

    def stream_delta(self, text, phase=""):
        self.streamed.append({"text": text, "phase": phase})

    def stream_end(self, kind="", metadata=None):
        self.stream_end_kinds.append(kind)

    def commentary(self, text):
        self.commentary_messages.append(text)

    def final_answer(self, text):
        self.final_messages.append(text)

    def tool_start(self, name, args, risk_level=""):
        pass

    def tool_result(self, name, args, result, metadata=None):
        self.tool_results.append({"name": name, "result": result})


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
    assert "memory" not in agent.session

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


def test_tool_result_is_persisted_before_ui_rendering(tmp_path):
    ui = RecordingUI()
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call(
                "read_file",
                {"path": "README.md", "start": 1, "end": 1},
                call_id="call_read",
            )
        ],
        ui=ui,
    )

    def fail_tool_result(*_args, **_kwargs):
        raise RuntimeError("terminal rendering failed")

    ui.tool_result = fail_tool_result

    with pytest.raises(RuntimeError, match="terminal rendering failed"):
        agent.ask("inspect README")

    tool_messages = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_read"


def test_repair_incomplete_tool_batch_preserves_provider_pairing():
    session = {
        "history": [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "conversation_id": "turn_1",
                "tool_calls": [
                    {"id": "call_1", "name": "read_file", "args": {"path": "a.py"}},
                    {"id": "call_2", "name": "write_file", "args": {"path": "b.py", "content": "x"}},
                ],
            },
            {
                "role": "tool",
                "conversation_id": "turn_1",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "ok",
            },
            {"role": "user", "conversation_id": "turn_2", "content": "continue"},
        ]
    }

    repaired = repair_incomplete_tool_results(session)

    assert repaired == 1
    assert session["history"][2]["tool_call_id"] == "call_2"
    assert session["history"][2]["content"] == INTERRUPTED_TOOL_RESULT
    assert session["history"][2]["outcome_unknown"] is True
    converted = _to_anthropic_messages(session["history"])
    assert converted[1]["role"] == "user"
    assert [block["tool_use_id"] for block in converted[1]["content"]] == ["call_1", "call_2"]


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


def test_runtime_streams_text_but_records_complete_final_message(tmp_path):
    ui = RecordingUI()
    agent = build_agent(tmp_path, [ModelResponse.final("done")], ui=ui)

    result = agent.ask("say hello")

    assert result == "done"
    assert [item["text"] for item in ui.streamed] == ["done"]
    assert ui.final_messages == []
    assistant_messages = [item for item in agent.session["history"] if item.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["kind"] == "final"
    assert assistant_messages[0]["content"] == "done"


def test_model_failure_marks_run_failed(tmp_path):
    agent = build_agent(tmp_path, [], stream=False)

    def fail_complete(*_args, **_kwargs):
        raise RuntimeError("backend unavailable")

    agent.model_client.complete = fail_complete

    with pytest.raises(ModelRequestError, match="backend unavailable"):
        agent.ask("inspect")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "model_error"


def test_persistence_failure_marks_run_failed(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [ModelResponse.final("done")], stream=False)

    def fail_save(_session):
        raise PersistenceError("disk full")

    monkeypatch.setattr(agent.session_store, "save", fail_save)

    with pytest.raises(RuntimeError, match="Agent persistence failed"):
        agent.ask("inspect")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "persistence_error"


def test_post_completion_maintenance_failure_does_not_hide_final(tmp_path, monkeypatch):
    ui = RecordingUI()
    agent = build_agent(tmp_path, [ModelResponse.final("done")], ui=ui, stream=False)

    def fail_maintenance(*_args, **_kwargs):
        raise RuntimeError("maintenance failed")

    monkeypatch.setattr(agent, "maybe_generate_session_title", fail_maintenance)
    monkeypatch.setattr(agent, "maybe_extract_memory_candidates", fail_maintenance)
    monkeypatch.setattr(agent, "schedule_dream_if_needed", fail_maintenance)

    result = agent.ask("finish")

    assert result == "done"
    assert ui.final_messages == ["done"]
    assert agent.current_task_state.status == "completed"


def test_run_finished_trace_failure_does_not_hide_completed_final(tmp_path, monkeypatch):
    ui = RecordingUI()
    agent = build_agent(tmp_path, [ModelResponse.final("done")], ui=ui, stream=False)
    original_emit_trace = agent.emit_trace

    def fail_terminal_trace(task_state, event_type, payload):
        if event_type == "run_finished":
            raise PersistenceError("trace disk full")
        return original_emit_trace(task_state, event_type, payload)

    monkeypatch.setattr(agent, "emit_trace", fail_terminal_trace)

    result = agent.ask("finish")

    assert result == "done"
    assert ui.final_messages == ["done"]
    assert agent.current_task_state.status == "completed"


def test_runtime_non_stream_final_preserves_and_displays_leading_commentary(tmp_path):
    ui = RecordingUI()
    response = ModelResponse.final(
        "done",
        commentary_text="I verified the affected behavior.",
    )
    agent = build_agent(tmp_path, [response], ui=ui, stream=False)

    result = agent.ask("finish the task")

    assert result == "done"
    assert ui.commentary_messages == ["I verified the affected behavior."]
    assert ui.final_messages == ["done"]
    assistant_messages = [
        item for item in agent.session["history"] if item.get("role") == "assistant"
    ]
    assert [item["kind"] for item in assistant_messages] == ["commentary", "final"]


def test_runtime_does_not_treat_streamed_commentary_as_streamed_final(tmp_path):
    class CommentaryThenFinalClient:
        model = "gpt-5.5"
        supports_streaming = True
        supports_images = False
        supports_prompt_cache = False
        supports_tools = True
        supports_session_title = False
        last_completion_metadata = {}

        def stream_complete(self, messages, max_new_tokens, **kwargs):
            del messages, max_new_tokens, kwargs
            response = ModelResponse.final(
                "Final body.",
                commentary_text="Progress update.",
            )
            yield ModelStreamEvent.text_delta(
                "Progress update.",
                phase="commentary",
            )
            yield ModelStreamEvent.done(response)

    ui = RecordingUI()
    agent = build_agent(tmp_path, [], ui=ui)
    agent.model_client = CommentaryThenFinalClient()

    result = agent.ask("finish the task")

    assert result == "Final body."
    assert ui.streamed == [{"text": "Progress update.", "phase": "commentary"}]
    assert ui.commentary_messages == []
    assert ui.final_messages == ["Final body."]


def test_incomplete_stream_marks_run_failed_without_recording_partial_text(tmp_path):
    class IncompleteClient:
        model = "test"
        supports_streaming = True
        supports_images = False
        supports_prompt_cache = False
        supports_tools = True
        last_completion_metadata = {}

        def stream_complete(self, *_args, **_kwargs):
            yield ModelStreamEvent.text_delta("partial")

    ui = RecordingUI()
    agent = build_agent(tmp_path, [], ui=ui)
    agent.model_client = IncompleteClient()

    with pytest.raises(ModelStreamIncompleteError, match="stream_incomplete"):
        agent.ask("finish")

    assert ui.streamed == [{"text": "partial", "phase": ""}]
    assert ui.stream_end_kinds == ["error"]
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "stream_incomplete"
    assert not any(
        item.get("role") == "assistant" and item.get("content") == "partial"
        for item in agent.session["history"]
    )


def test_keyboard_interrupt_marks_run_stopped_and_closes_stream_ui(tmp_path):
    class InterruptedClient:
        model = "test"
        supports_streaming = True
        supports_images = False
        supports_prompt_cache = False
        supports_tools = True
        last_completion_metadata = {}

        def stream_complete(self, *_args, **_kwargs):
            yield ModelStreamEvent.text_delta("partial")
            raise KeyboardInterrupt

    ui = RecordingUI()
    agent = build_agent(tmp_path, [], ui=ui)
    agent.model_client = InterruptedClient()

    with pytest.raises(KeyboardInterrupt):
        agent.ask("finish")

    assert ui.stream_end_kinds == ["error"]
    assert agent.current_task_state.status == "stopped"
    assert agent.current_task_state.stop_reason == "user_interrupted"


def test_runtime_streams_tool_commentary_before_executing_complete_tool_call(tmp_path):
    ui = RecordingUI()
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call(
                "read_file",
                {"path": "README.md", "start": 1, "end": 1},
                call_id="call_read",
                text="我先读取 README。",
            ),
            ModelResponse.final("done"),
        ],
        ui=ui,
    )

    result = agent.ask("inspect README")

    assert result == "done"
    assert ui.streamed[0]["text"] == "我先读取 README。"
    assert ui.commentary_messages == []
    assert ui.tool_results[0]["name"] == "read_file"
    tool_call_messages = [item for item in agent.session["history"] if item.get("tool_calls")]
    assert tool_call_messages[0]["tool_calls"][0]["args"] == {"path": "README.md", "start": 1, "end": 1}

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

def test_successful_tool_still_runs_after_repeated_call_rejection(tmp_path):
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

    result = agent.run_tool("list_files", {"path": "."})

    assert "README.md" in result
