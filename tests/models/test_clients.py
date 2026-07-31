"""模型客户端格式转换测试。

覆盖模块：models.openai、models.anthropic、内部 ModelResponse。
重点边界：commentary-only、commentary+tool_call、OpenAI 输入转换、Anthropic thinking 忽略、多 tool_result 合并。
"""

import json
import urllib.error
import urllib.request

from PIL import Image
import pytest

from codemate.models import AnthropicCompatibleModelClient, ModelStreamIncompleteError, OpenAICompatibleModelClient
from codemate.models.anthropic import _extract_anthropic_response, _to_anthropic_messages
from codemate.models.capabilities import model_capability
from codemate.models.openai import (
    OPENAI_RETRY_DELAYS,
    _extract_openai_model_response,
    _to_openai_input,
)


class FakeHTTPResponse:
    def __init__(self, data, content_type="application/json"):
        self._body = data.encode("utf-8") if isinstance(data, str) else json.dumps(data).encode("utf-8")
        self._lines = iter(self._body.splitlines(keepends=True))
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body

    def readline(self):
        return next(self._lines, b"")


class InterruptedHTTPResponse(FakeHTTPResponse):
    def __init__(self, data, *, fail_after_lines, content_type="text/event-stream"):
        super().__init__(data, content_type=content_type)
        self._fail_after_lines = fail_after_lines
        self._read_count = 0

    def readline(self):
        if self._read_count >= self._fail_after_lines:
            raise urllib.error.URLError("connection reset")
        self._read_count += 1
        return super().readline()


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


def test_openai_responses_final_preserves_commentary_from_same_response():
    response = _extract_openai_model_response(
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "验证已经完成。"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "修改已通过测试。"}],
                },
            ]
        },
        metadata={},
    )

    assert response.kind == "final"
    assert response.commentary_text == "验证已经完成。"
    assert response.text == "修改已通过测试。"


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


def test_openai_function_call_output_can_include_image_blocks(tmp_path):
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (2, 2), color="red").save(image_path)

    messages = _to_openai_input(
        [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "shot.png"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "Image file: shot.png",
                "content_blocks": [{"type": "image", "path": str(image_path), "media_type": "image/png"}],
            },
        ]
    )

    output = messages[1]["output"]
    assert messages[1]["type"] == "function_call_output"
    assert [item["type"] for item in output] == ["input_text", "input_image"]
    assert output[1]["image_url"].startswith("data:image/png;base64,")


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


def test_anthropic_tool_result_can_include_image_blocks(tmp_path):
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (2, 2), color="red").save(image_path)

    messages = _to_anthropic_messages(
        [
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "shot.png"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "Image file: shot.png",
                "content_blocks": [{"type": "image", "path": str(image_path), "media_type": "image/png"}],
            },
        ]
    )

    tool_result_content = messages[1]["content"][0]["content"]
    assert [item["type"] for item in tool_result_content] == ["text", "image"]
    assert tool_result_content[1]["source"]["media_type"] == "image/png"
    assert tool_result_content[1]["source"]["data"]


def test_common_model_capabilities_are_configured():
    assert model_capability("gpt-5.4").supports_images is True
    assert model_capability("gpt-5.5").openai_reasoning_effort == "high"
    assert model_capability("claude-sonnet-4-6").supports_images is True
    assert model_capability("claude-opus-4-8").anthropic_effort == "high"
    assert model_capability("deepseek-v4-pro").supports_images is False
    assert model_capability("deepseek-v4-pro").supports_reasoning is False


def test_openai_reasoning_effort_is_added_for_reasoning_models(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"output_text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    response = client.complete("hello", 100)

    assert response.text == "ok"
    assert client.supports_images is True
    assert captured["payload"]["reasoning"] == {"effort": "high"}


def test_openai_custom_endpoint_receives_prompt_cache_key(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"output_text": "ok", "usage": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    client.complete("hello", 100, prompt_cache_key="stable-prefix")

    assert client.supports_prompt_cache is True
    assert captured["payload"]["prompt_cache_key"] == "stable-prefix"
    assert "prompt_cache_retention" not in captured["payload"]


def test_openai_responses_retries_transient_connection_errors(monkeypatch):
    attempts = []
    delays = []

    def fake_urlopen(request, timeout):
        del request, timeout
        attempts.append(1)
        if len(attempts) <= len(OPENAI_RETRY_DELAYS):
            raise urllib.error.URLError("temporary disconnect")
        return FakeHTTPResponse({"output_text": "ok", "usage": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("codemate.models.openai.time.sleep", delays.append)
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    response = client.complete("hello", 100)

    assert response.text == "ok"
    assert len(attempts) == len(OPENAI_RETRY_DELAYS) + 1
    assert delays == list(OPENAI_RETRY_DELAYS)


def test_openai_connection_error_includes_underlying_reason(monkeypatch):
    def fake_urlopen(request, timeout):
        del request, timeout
        raise urllib.error.URLError("connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("codemate.models.openai.time.sleep", lambda _delay: None)
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    with pytest.raises(RuntimeError, match="Cause: connection reset by peer"):
        client.complete("hello", 100)


def test_anthropic_effort_is_added_for_claude_but_not_deepseek(monkeypatch):
    payloads = []

    def fake_urlopen(request, timeout):
        del timeout
        payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeHTTPResponse({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    claude = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)
    deepseek = AnthropicCompatibleModelClient("deepseek-v4-pro", "https://example.test/v1", "", None, 30)
    claude.complete("hello", 8192)
    deepseek.complete("hello", 8192)

    assert claude.supports_images is True
    assert deepseek.supports_images is False
    assert claude.supports_prompt_cache is True
    assert deepseek.supports_prompt_cache is False
    assert payloads[0]["output_config"] == {"effort": "high"}
    assert "output_config" not in payloads[1]
    assert "cache_control" not in payloads[0]
    assert "cache_control" not in payloads[1]
    assert "thinking" not in payloads[0]
    assert "thinking" not in payloads[1]


def test_anthropic_prompt_cache_marks_system_and_automatic_breakpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 900,
                    "output_tokens": 5,
                },
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = AnthropicCompatibleModelClient(
        "claude-sonnet-4-6",
        "https://example.test/v1",
        "",
        None,
        30,
    )

    response = client.complete(
        "hello",
        8192,
        system="stable system prompt",
        prompt_cache_key="stable-prefix",
    )

    assert captured["payload"]["cache_control"] == {"type": "ephemeral"}
    assert captured["payload"]["system"] == [
        {
            "type": "text",
            "text": "stable system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert response.metadata["input_tokens"] == 1020
    assert response.metadata["uncached_input_tokens"] == 20
    assert response.metadata["cache_creation_input_tokens"] == 100
    assert response.metadata["cached_tokens"] == 900
    assert response.metadata["total_tokens"] == 1025
    assert response.metadata["cache_hit"] is True


def test_anthropic_prompt_cache_configuration_is_preserved_by_fork():
    client = AnthropicCompatibleModelClient(
        "claude-sonnet-4-6",
        "https://example.test/v1",
        "",
        None,
        30,
        prompt_cache=True,
        prompt_cache_ttl="1h",
    )

    child = client.fork()

    assert child.supports_prompt_cache is True
    assert child.prompt_cache_ttl == "1h"


def test_anthropic_effort_and_structured_output_share_output_config(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)

    client.complete(
        "hello",
        8192,
        structured_output={
            "name": "demo",
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    )

    output_config = captured["payload"]["output_config"]
    assert output_config["effort"] == "high"
    assert output_config["format"]["type"] == "json_schema"
    assert output_config["format"]["schema"]["type"] == "object"


def test_openai_responses_streaming_yields_text_delta_and_done_response(monkeypatch):
    captured = {}
    body = "\n\n".join(
        [
            'data: {"type":"response.output_item.added","item":{"id":"msg_1","phase":"commentary"}}',
            'data: {"type":"response.output_text.delta","item_id":"msg_1","delta":"我先读取。"}',
            'data: {"type":"response.function_call_arguments.done","item_id":"fc_1","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}',
            'data: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","phase":"commentary","content":[{"type":"output_text","text":"我先读取。"}]},{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}],"usage":{"input_tokens":2,"output_tokens":3}}}',
        ]
    ) + "\n\n"

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(body, content_type="text/event-stream")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    events = list(client.stream_complete("hello", 100, tools=[{"name": "read_file", "description": "", "input_schema": {"type": "object"}}]))

    assert captured["payload"]["stream"] is True
    assert events[0].kind == "text_delta"
    assert events[0].text == "我先读取。"
    assert events[0].phase == "commentary"
    assert events[-1].kind == "done"
    assert events[-1].response.kind == "tool_calls"
    assert events[-1].response.text == "我先读取。"
    assert events[-1].response.tool_calls[0].name == "read_file"
    assert events[-1].response.tool_calls[0].args == {"path": "README.md"}
    assert client.last_completion_metadata["input_tokens"] == 2


def test_openai_stream_without_response_completed_is_rejected(monkeypatch):
    body = (
        'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        'data: {"type":"response.output_text.done","text":"partial"}\n\n'
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeHTTPResponse(body, content_type="text/event-stream"),
    )
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    with pytest.raises(ModelStreamIncompleteError, match="response.completed"):
        list(client.stream_complete("hello", 100))


def test_openai_interrupted_stream_is_reported_as_incomplete(monkeypatch):
    body = 'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: InterruptedHTTPResponse(body, fail_after_lines=2),
    )
    client = OpenAICompatibleModelClient("gpt-5.4", "https://example.test/v1", "", None, 30)

    with pytest.raises(ModelStreamIncompleteError, match="stream_incomplete"):
        list(client.stream_complete("hello", 100))


def test_anthropic_streaming_yields_text_delta_and_done_response(monkeypatch):
    captured = {}
    body = "\n\n".join(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":4,"cache_creation_input_tokens":10,"cache_read_input_tokens":100,"output_tokens":1}}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"我先读取。"}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file","input":{}}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"README.md\\"}"}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":8}}',
            'event: message_stop\ndata: {"type":"message_stop"}',
        ]
    ) + "\n\n"

    def fake_urlopen(request, timeout):
        del timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(body, content_type="text/event-stream")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)

    events = list(
        client.stream_complete(
            "hello",
            100,
            tools=[{"name": "read_file", "description": "", "input_schema": {"type": "object"}}],
            system="stable system prompt",
            prompt_cache_key="stable-prefix",
        )
    )

    assert captured["payload"]["stream"] is True
    assert captured["payload"]["cache_control"] == {"type": "ephemeral"}
    assert events[0].kind == "text_delta"
    assert events[0].text == "我先读取。"
    assert events[-1].kind == "done"
    assert events[-1].response.kind == "tool_calls"
    assert events[-1].response.text == "我先读取。"
    assert events[-1].response.tool_calls[0].id == "toolu_1"
    assert events[-1].response.tool_calls[0].args == {"path": "README.md"}
    assert client.last_completion_metadata["input_tokens"] == 114
    assert client.last_completion_metadata["cached_tokens"] == 100
    assert client.last_completion_metadata["cache_creation_input_tokens"] == 10
    assert client.last_completion_metadata["output_tokens"] == 8
    assert client.last_completion_metadata["stop_reason"] == "tool_use"


def test_anthropic_stream_without_message_stop_is_rejected(monkeypatch):
    body = "\n\n".join(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}',
        ]
    ) + "\n\n"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeHTTPResponse(body, content_type="text/event-stream"),
    )
    client = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)

    with pytest.raises(ModelStreamIncompleteError, match="message_stop"):
        list(client.stream_complete("hello", 100))


def test_anthropic_interrupted_stream_is_reported_as_incomplete(monkeypatch):
    body = "\n\n".join(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        ]
    ) + "\n\n"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: InterruptedHTTPResponse(body, fail_after_lines=4),
    )
    client = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)

    with pytest.raises(ModelStreamIncompleteError, match="stream_incomplete"):
        list(client.stream_complete("hello", 100))


def test_anthropic_stream_with_incomplete_tool_input_is_rejected(monkeypatch):
    body = "\n\n".join(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file","input":{}}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            'event: message_stop\ndata: {"type":"message_stop"}',
        ]
    ) + "\n\n"
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeHTTPResponse(body, content_type="text/event-stream"),
    )
    client = AnthropicCompatibleModelClient("claude-sonnet-4-6", "https://example.test/v1", "", None, 30)

    with pytest.raises(ModelStreamIncompleteError, match="incomplete tool input"):
        list(client.stream_complete("hello", 100))


def test_anthropic_streaming_final_text_is_unphased_until_done(monkeypatch):
    body = "\n\n".join(
        [
            'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":4,"output_tokens":1}}}',
            'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Final response."}}',
            'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
            'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":3}}',
            'event: message_stop\ndata: {"type":"message_stop"}',
        ]
    ) + "\n\n"

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeHTTPResponse(body, content_type="text/event-stream"),
    )
    client = AnthropicCompatibleModelClient(
        "deepseek-v4-pro",
        "https://example.test/v1",
        "",
        None,
        30,
    )

    events = list(client.stream_complete("hello", 100))

    assert events[0].kind == "text_delta"
    assert events[0].phase is None
    assert events[-1].kind == "done"
    assert events[-1].response.kind == "final"
    assert events[-1].response.text == "Final response."
    assert client.last_completion_metadata["stop_reason"] == "end_turn"
