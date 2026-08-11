"""JSONL bridge tests cover framing, correlation, and UI event translation."""

import io
import json
import threading

import pytest

from codemate.bridge.protocol import (
    InteractionBroker,
    JsonLineWriter,
    ProtocolError,
    parse_message,
)
from codemate.bridge.ui import JsonUI


def emitted_messages(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_writer_emits_one_compact_object_per_line():
    stream = io.StringIO()
    writer = JsonLineWriter(stream)

    writer.emit("ready", text="你好")
    writer.emit("closed")

    assert emitted_messages(stream) == [
        {"type": "ready", "text": "你好"},
        {"type": "closed"},
    ]


@pytest.mark.parametrize("line", ["[]", "{}", '{"type": 1}', "not-json"])
def test_parse_message_rejects_invalid_protocol_objects(line):
    with pytest.raises(ProtocolError):
        parse_message(line)


def test_interaction_broker_matches_response_by_id():
    written = threading.Event()

    class SignalingStream(io.StringIO):
        def write(self, value):
            result = super().write(value)
            written.set()
            return result

    stream = SignalingStream()
    writer = JsonLineWriter(stream)
    broker = InteractionBroker(writer, lambda: "req-1")
    result = {}

    thread = threading.Thread(
        target=lambda: result.update(
            value=broker.request("approval_request", {"name": "run_shell"})
        )
    )
    thread.start()
    assert written.wait(timeout=1)
    request = emitted_messages(stream)[0]

    assert broker.deliver(
        {
            "type": "interaction_response",
            "interaction_id": request["interaction_id"],
            "value": {"allowed": True},
        }
    )
    thread.join(timeout=1)

    assert result["value"] == {"allowed": True}
    assert request["request_id"] == "req-1"


class RecordingInteractions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, event_type, payload):
        self.calls.append((event_type, payload))
        return self.response


def test_json_ui_builds_rememberable_approval_options():
    stream = io.StringIO()
    interactions = RecordingInteractions({"allowed": True})
    ui = JsonUI(JsonLineWriter(stream), interactions, lambda: "req-2")

    decision = ui.approval_request(
        "run_shell",
        {"command": "pytest -q"},
        {
            "approval_access": "write",
            "suggested_allow_dir": "/tmp/project",
            "suggested_shell_subject": "pytest",
        },
    )

    assert decision == {"allowed": True}
    event_type, payload = interactions.calls[0]
    assert event_type == "approval_request"
    assert payload["options"] == [
        {"label": "Allow once", "value": {"allowed": True}},
        {
            "label": "Allow all `pytest` commands this session",
            "value": {"allowed": True, "remember": {"shell_subject": "pytest"}},
        },
        {
            "label": "Allow write for /tmp/project this session",
            "value": {
                "allowed": True,
                "remember": {"access": "write", "path": "/tmp/project"},
            },
        },
        {
            "label": "Allow `pytest` and write for /tmp/project this session",
            "value": {
                "allowed": True,
                "remember": {
                    "shell_subject": "pytest",
                    "access": "write",
                    "path": "/tmp/project",
                },
            },
        },
        {"label": "Deny", "value": {"allowed": False}},
    ]


def test_json_ui_hides_edit_bodies_from_approval_events():
    stream = io.StringIO()
    interactions = RecordingInteractions({"allowed": True})
    ui = JsonUI(JsonLineWriter(stream), interactions, lambda: "req-edit")

    ui.approval_request(
        "patch_file",
        {"path": "app.py", "old_text": "old", "new_text": "new"},
    )

    _event_type, payload = interactions.calls[0]
    assert payload["args"] == {"path": "app.py"}


def test_json_ui_stream_events_share_a_stream_id():
    stream = io.StringIO()
    ui = JsonUI(JsonLineWriter(stream), RecordingInteractions(None), lambda: "req-3")

    ui.stream_start()
    ui.stream_delta("hello", phase="commentary")
    ui.stream_end(kind="tool_calls")

    events = emitted_messages(stream)
    assert [event["type"] for event in events] == [
        "stream_start",
        "text_delta",
        "stream_end",
    ]
    assert {event["stream_id"] for event in events} == {events[0]["stream_id"]}
    assert all(event["request_id"] == "req-3" for event in events)
