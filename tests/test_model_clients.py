from codemate.models.anthropic import _extract_anthropic_response, _to_anthropic_messages
from codemate.models.openai import _extract_openai_model_response, _to_openai_input


def test_openai_responses_commentary_only_is_not_final():
    response = _extract_openai_model_response(
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "我先检查相关文件。"}],
                }
            ]
        },
        metadata={"input_tokens": 10},
    )

    assert response.kind == "commentary"
    assert response.text == "我先检查相关文件。"
    assert response.metadata["input_tokens"] == 10


def test_openai_responses_commentary_and_tool_call_are_parsed_together():
    response = _extract_openai_model_response(
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "我先读取 README。"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
            ]
        },
        metadata={},
    )

    assert response.kind == "tool_calls"
    assert response.text == "我先读取 README。"
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].args == {"path": "README.md"}


def test_openai_input_keeps_commentary_before_tool_calls():
    messages = _to_openai_input(
        [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "我先读取 README。",
                "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "README.md"}}],
            }
        ]
    )

    assert messages[0]["phase"] == "commentary"
    assert messages[0]["content"][0]["text"] == "我先读取 README。"
    assert messages[1]["type"] == "function_call"
    assert messages[1]["call_id"] == "call_1"


def test_anthropic_thinking_blocks_are_ignored_for_tool_roundtrip():
    response = _extract_anthropic_response(
        {
            "content": [
                {"type": "thinking", "thinking": "Need to inspect the file.", "signature": "sig-123"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )

    messages = _to_anthropic_messages(
        [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": response.text,
                "tool_calls": [response.tool_calls[0].to_dict()],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "# README.md\n   1: demo"},
        ]
    )

    assert response.kind == "tool_calls"
    assert messages[0]["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}}
    ]
    assert messages[1]["content"][0]["type"] == "tool_result"


def test_anthropic_groups_consecutive_tool_results_after_multi_tool_call():
    messages = _to_anthropic_messages(
        [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "我先读取项目结构。",
                "tool_calls": [
                    {"id": "call_1", "name": "list_files", "args": {"path": "."}},
                    {"id": "call_2", "name": "read_file", "args": {"path": "README.md"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "list_files", "content": "codemate\nREADME.md"},
            {"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": "# README"},
        ]
    )

    assert messages[0]["role"] == "assistant"
    assert [block["type"] for block in messages[0]["content"]] == ["text", "tool_use", "tool_use"]
    assert messages[1]["role"] == "user"
    assert [block["tool_use_id"] for block in messages[1]["content"]] == ["call_1", "call_2"]
    assert [block["type"] for block in messages[1]["content"]] == ["tool_result", "tool_result"]
