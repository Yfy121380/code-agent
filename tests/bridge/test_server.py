"""Bridge server tests verify request sequencing without starting a real model."""

import io
import json
from types import SimpleNamespace

from codemate.bridge.protocol import InteractionBroker, JsonLineWriter
from codemate.bridge.server import BridgeServer, RequestContext
from codemate.bridge.ui import JsonUI


class DummySessionStore:
    def __init__(self):
        self.checkpoint = None
        self.transcript = []

    def list_sessions(self):
        return []

    def save_request_checkpoint(self, session, user_request, editor_context=""):
        self.checkpoint = {
            "session": dict(session),
            "user_request": user_request,
            "editor_context": editor_context,
            "transcript_size": len(self.transcript),
        }

    def request_checkpoint_info(self, session_id):
        if self.checkpoint is None:
            return None
        return {"user_request": self.checkpoint["user_request"]}

    def load_request_checkpoint(self, session_id):
        return self.checkpoint

    def load_transcript(self, session_id):
        return list(self.transcript)

    def transcript_size(self, session_id):
        return len(self.transcript)

    def truncate_transcript(self, session_id, size):
        self.transcript = self.transcript[: int(size or 0)]


class DummyAgent:
    def __init__(self, ui):
        self.ui = ui
        self.workspace = SimpleNamespace(repo_root="/tmp/project")
        self.model_client = SimpleNamespace(model="gpt-test")
        self.session_store = DummySessionStore()
        self.session = {
            "id": "session-1",
            "title": "Bridge test",
            "created_at": "created",
            "updated_at": "updated",
            "history": [],
        }
        self.approval_policy = "ask"
        self.last_editor_context = ""

    def ask(self, text, *, source_user_request=None, editor_context=""):
        assert text == "inspect project"
        assert source_user_request is None
        self.last_editor_context = editor_context
        self.ui.commentary("Inspecting files")
        self.ui.final_answer("Done")
        return "Done"

    def save_request_checkpoint(self, user_request, editor_context=""):
        self.session_store.save_request_checkpoint(
            self.session,
            user_request,
            editor_context,
        )

    def latest_change_set(self):
        return None

    def list_change_sets(self):
        return []

    def apply_whole_change_set(self, change_set_id, action):
        return {
            "id": change_set_id,
            "state": "reverted" if action == "undo" else "applied",
            "files": [],
        }

    def is_plan_mode(self):
        return False

    def close(self):
        pass


def build_server():
    stream = io.StringIO()
    writer = JsonLineWriter(stream)
    context = RequestContext()
    interactions = InteractionBroker(writer, context.get)
    ui = JsonUI(writer, interactions, context.get)
    agent = DummyAgent(ui)
    args = SimpleNamespace(provider="openai")
    server = BridgeServer(agent, args, io.StringIO(), writer, interactions, context)
    return server, stream


def messages(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_dispatch_ask_emits_ordered_runtime_events():
    server, stream = build_server()

    server._dispatch({"id": "req-1", "type": "ask", "text": "inspect project"})

    events = messages(stream)
    assert [event["type"] for event in events] == [
        "run_started",
        "commentary",
        "final",
        "run_finished",
    ]
    assert all(event["request_id"] == "req-1" for event in events)
    assert events[-1]["status"] == "completed"
    assert server.agent.session_store.checkpoint["user_request"] == "inspect project"


def test_approval_command_updates_state():
    server, stream = build_server()

    server._dispatch(
        {
            "id": "req-2",
            "type": "command",
            "name": "approval",
            "args": {"mode": "auto"},
        }
    )

    event = messages(stream)[0]
    assert event["type"] == "command_result"
    assert event["value"] == {"approval_policy": "auto"}
    assert event["state"]["approval_policy"] == "auto"


def test_ask_passes_editor_attachments_as_internal_context():
    server, stream = build_server()

    server._dispatch(
        {
            "id": "req-editor",
            "type": "ask",
            "text": "inspect project",
            "attachments": [
                {
                    "kind": "selection",
                    "label": "app.py:1-1",
                    "path": "app.py",
                    "start_line": 1,
                    "end_line": 1,
                    "content": "value = 1",
                }
            ],
        }
    )

    assert "[selection] app.py:1-1" in server.agent.last_editor_context
    checkpoint = server.agent.session_store.checkpoint
    assert checkpoint["editor_context"] == server.agent.last_editor_context


def test_change_undo_command_returns_updated_change_set():
    server, stream = build_server()

    server._dispatch(
        {
            "id": "req-undo",
            "type": "command",
            "name": "change_undo",
            "args": {"change_set_id": "run-1"},
        }
    )

    event = messages(stream)[0]
    assert event["value"]["change_set"]["id"] == "run-1"
    assert event["value"]["change_set"]["state"] == "reverted"


def test_serve_processes_commands_before_a_queued_shutdown():
    server, stream = build_server()
    server.reader = io.StringIO(
        '{"id":"req-3","type":"command","name":"status","args":{}}\n'
        '{"type":"shutdown"}\n'
    )

    server.serve()

    assert [event["type"] for event in messages(stream)] == [
        "ready",
        "command_result",
        "closed",
    ]


def test_retry_restores_history_before_starting_edited_request():
    server, stream = build_server()
    checkpoint_session = dict(server.agent.session)
    checkpoint_session["history"] = [
        {
            "id": "msg-1",
            "conversation_id": "turn-1",
            "role": "user",
            "content": "earlier request",
        }
    ]
    server.agent.session_store.checkpoint = {
        "session": checkpoint_session,
        "user_request": "inspect project",
    }
    server.agent.session["history"] = [
        *checkpoint_session["history"],
        {"role": "assistant", "content": "result to discard"},
    ]

    def replace_agent(*, session=None):
        server.agent.session = session
        return server.agent

    server._replace_agent = replace_agent
    server._dispatch({"id": "retry-1", "type": "retry", "text": "inspect project"})

    events = messages(stream)
    assert [event["type"] for event in events] == [
        "checkpoint_restored",
        "run_started",
        "commentary",
        "final",
        "run_finished",
    ]
    assert events[0]["history"] == [
        {
            "id": "msg-1",
            "role": "user",
            "kind": "",
            "content": "earlier request",
            "created_at": "",
            "conversation_id": "turn-1",
        }
    ]


def test_display_history_keeps_the_first_visible_conversation_intact():
    server, _stream = build_server()
    older = [
        {
            "id": f"old-{index}",
            "conversation_id": f"old-turn-{index}",
            "role": "user",
            "content": "old",
        }
        for index in range(3)
    ]
    grouped = [
        {
            "id": "group-user",
            "conversation_id": "group-turn",
            "role": "user",
            "content": "group request",
        },
        {
            "id": "group-assistant",
            "conversation_id": "group-turn",
            "role": "assistant",
            "kind": "tool_calls",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "read_file", "args": {}}],
        },
        {
            "id": "group-tool",
            "conversation_id": "group-turn",
            "role": "tool",
            "name": "read_file",
            "tool_call_id": "call-1",
            "content": "result",
        },
    ]
    server.agent.session["history"] = [*older, *grouped]

    displayed = server._display_history(limit=2)

    assert [item["id"] for item in displayed] == [
        "group-user",
        "group-assistant",
        "group-tool",
    ]


def test_display_history_restores_tool_change_preview():
    server, _stream = build_server()
    server.agent.session["history"] = [
        {
            "id": "tool-result-1",
            "conversation_id": "turn-1",
            "role": "tool",
            "name": "patch_file",
            "tool_call_id": "call-1",
            "content": "patched app.py",
            "ui_metadata": {
                "change_preview": {
                    "path": "app.py",
                    "additions": 1,
                    "deletions": 1,
                    "diff": "@@ -1 +1 @@\n-old\n+new",
                }
            },
        }
    ]

    displayed = server._display_history()

    assert displayed[0]["metadata"]["change_preview"]["path"] == "app.py"


def test_display_history_hides_large_edit_arguments():
    server, _stream = build_server()
    server.agent.session["history"] = [
        {
            "id": "assistant-edit",
            "conversation_id": "turn-1",
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "patch_file",
                    "args": {
                        "path": "app.py",
                        "old_text": "large old body",
                        "new_text": "large new body",
                    },
                }
            ],
        }
    ]

    displayed = server._display_history()

    assert displayed[0]["tool_calls"][0]["args"] == {"path": "app.py"}


def test_display_history_preserves_long_transcript_after_model_history_is_compacted():
    server, _stream = build_server()
    long_content = "message before compact\n" + ("complete visible answer\n" * 300)
    assert len(long_content) > 4_000
    original = {
        "id": "old-user",
        "conversation_id": "turn-old",
        "role": "assistant",
        "kind": "final",
        "content": long_content,
    }
    server.agent.session_store.transcript = [original]
    server.agent.session["history"] = [
        {
            "id": "compact-context",
            "role": "user",
            "kind": "history_summary_context",
            "content": "summary",
        }
    ]

    assert server._display_history() == [
        {
            "id": "old-user",
            "role": "assistant",
            "kind": "final",
            "content": long_content,
            "created_at": "",
            "conversation_id": "turn-old",
        }
    ]
