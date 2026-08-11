"""Integration tests cover new-session and retry state transitions."""

import io
import json
from types import SimpleNamespace

from codemate import ModelResponse
from codemate.bridge.protocol import InteractionBroker, JsonLineWriter
from codemate.bridge.server import BridgeServer, RequestContext
from codemate.bridge.ui import JsonUI
from tests.helpers import build_agent


DISABLED_MAINTENANCE = {
    "relevant_memory": False,
    "long_term_memory": False,
    "memory_candidates": False,
    "memory_dream": False,
    "session_title": False,
}


def build_real_server(tmp_path):
    stream = io.StringIO()
    writer = JsonLineWriter(stream)
    context = RequestContext()
    interactions = InteractionBroker(writer, context.get)
    ui = JsonUI(writer, interactions, context.get)
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.final("first result"),
            ModelResponse.final("second result"),
            ModelResponse.final("retried result"),
        ],
        feature_flags=DISABLED_MAINTENANCE,
        ui=ui,
        stream=False,
    )
    server = BridgeServer(
        agent,
        SimpleNamespace(provider="openai"),
        io.StringIO(),
        writer,
        interactions,
        context,
    )
    return server, stream


def emitted(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_new_task_and_retry_replace_only_the_latest_turn(tmp_path):
    server, stream = build_real_server(tmp_path)

    server._dispatch({"id": "first", "type": "ask", "text": "first request"})
    first_session_id = server.agent.session["id"]
    server._dispatch({"id": "second", "type": "new_ask", "text": "second request"})
    second_session_id = server.agent.session["id"]

    assert second_session_id != first_session_id
    assert [item["content"] for item in server.agent.session["history"]] == [
        "second request",
        "second result",
    ]

    server._dispatch({"id": "retry", "type": "retry", "text": "edited second request"})

    assert server.agent.session["id"] == second_session_id
    assert [item["content"] for item in server.agent.session["history"]] == [
        "edited second request",
        "retried result",
    ]
    retry_events = [
        event for event in emitted(stream) if event.get("request_id") == "retry"
    ]
    assert [event["type"] for event in retry_events] == [
        "checkpoint_restored",
        "run_started",
        "model_status",
        "model_status",
        "final",
        "run_finished",
    ]
    assert retry_events[0]["history"] == []
