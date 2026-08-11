"""Integration tests cover new-session and retry state transitions."""

import io
import json
from types import SimpleNamespace

from codemate import ModelResponse
from codemate.bridge.annotations import response_content_hash
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


def test_annotation_only_request_keeps_model_prompt_out_of_display_history(tmp_path):
    server, stream = build_real_server(tmp_path)
    server._dispatch({"id": "first", "type": "ask", "text": "first request"})
    source = next(
        item
        for item in server.agent.session_store.load_transcript(
            server.agent.session["id"]
        )
        if item.get("role") == "assistant" and item.get("kind") == "final"
    )
    annotation = {
        "id": "annotation-1",
        "source_message_id": source["id"],
        "source_content_hash": response_content_hash(source["content"]),
        "selected_text": "first result",
        "surrounding_text": "first result",
        "comment": "Explain this result.",
    }

    server._dispatch(
        {
            "id": "annotated",
            "type": "ask",
            "text": "",
            "response_annotations": [annotation],
        }
    )

    model_user_message = server.agent.session["history"][-2]
    assert "Annotation 1:" in model_user_message["content"]
    assert model_user_message["display_content"] == ""
    assert model_user_message["response_annotations"][0]["comment"] == (
        "Explain this result."
    )
    displayed_user = [
        item
        for item in server._display_history()
        if item.get("conversation_id") == model_user_message["conversation_id"]
        and item.get("role") == "user"
    ][0]
    assert displayed_user["content"] == ""
    assert displayed_user["response_annotations"][0]["selected_text"] == (
        "first result"
    )
    finished = [
        event
        for event in emitted(stream)
        if event.get("request_id") == "annotated"
        and event.get("type") == "run_finished"
    ][0]
    assert any(
        item.get("kind") == "final" and item.get("content_hash")
        for item in finished["messages"]
    )
