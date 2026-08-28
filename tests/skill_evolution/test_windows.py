"""Transcript projection and window-boundary tests for Skill evolution."""

import copy

from codemate.skill_evolution.windows import (
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    build_pending_window,
)
from tests.helpers import build_agent


def conversation(conversation_id, user, final, *, extra=None):
    return [
        {
            "role": "user",
            "content": user,
            "conversation_id": conversation_id,
        },
        *(extra or []),
        {
            "role": "assistant",
            "kind": "final",
            "content": final,
            "conversation_id": conversation_id,
        },
    ]


def test_window_selects_latest_five_complete_conversations_in_order():
    transcript = []
    for index in range(7):
        transcript.extend(
            conversation(f"turn-{index}", f"user-{index}", f"final-{index}")
        )
    transcript.append(
        {
            "role": "user",
            "content": "unfinished",
            "conversation_id": "turn-incomplete",
        }
    )

    window = build_pending_window(transcript, "turn-6")

    assert [item["id"] for item in window["supporting_conversations"]] == [
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
    ]
    assert window["focus_conversation"]["id"] == "turn-6"


def test_window_keeps_the_whole_conversation_that_crosses_soft_char_limit():
    transcript = []
    for index in range(4):
        transcript.extend(
            conversation(
                f"turn-{index}",
                f"request-{index}-" + "x" * 19_000,
                f"answer-{index}-" + "y" * 1_000,
            )
        )

    window = build_pending_window(transcript, "turn-3")
    ids = [
        *[item["id"] for item in window["supporting_conversations"]],
        window["focus_conversation"]["id"],
    ]

    assert ids == ["turn-1", "turn-2", "turn-3"]
    assert window["source_char_count"] >= 50_000
    assert (
        "request-1-" in window["supporting_conversations"][0]["messages"][0]["content"]
    )


def test_tool_arguments_and_results_use_uniform_strict_limits():
    conversation_id = "turn-tools"
    transcript = conversation(
        conversation_id,
        "inspect the repository",
        "done",
        extra=[
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "I will inspect both sources.",
                "conversation_id": conversation_id,
                "tool_calls": [
                    {
                        "id": "call-a",
                        "name": "run_shell",
                        "args": {"cmd": "a" * 1_000},
                    },
                    {
                        "id": "call-b",
                        "name": "read_file",
                        "args": {"path": "README.md"},
                    },
                ],
            },
            {
                "role": "tool",
                "name": "run_shell",
                "tool_call_id": "call-a",
                "content": "r" * 2_000,
                "conversation_id": conversation_id,
            },
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call-b",
                "content": "short result",
                "conversation_id": conversation_id,
            },
        ],
    )

    window = build_pending_window(transcript, conversation_id)
    events = window["focus_conversation"]["tool_events"]

    assert len(events) == 2
    assert len(events[0]["arguments"]) == MAX_TOOL_ARGUMENT_CHARS
    assert len(events[0]["result"]) == MAX_TOOL_RESULT_CHARS
    assert events[1]["result"] == "short result"


def test_window_projection_does_not_modify_the_durable_transcript():
    transcript = conversation(
        "turn-original",
        "request",
        "answer",
        extra=[
            {
                "role": "tool",
                "name": "run_shell",
                "tool_call_id": "orphan",
                "content": "z" * 2_000,
                "conversation_id": "turn-original",
            }
        ],
    )
    original = copy.deepcopy(transcript)

    window = build_pending_window(transcript, "turn-original")

    assert transcript == original
    assert len(window["focus_conversation"]["tool_events"][0]["result"]) == 1_200


def test_loaded_skill_references_are_tied_to_the_loading_conversation():
    previous_id = "turn-previous"
    focus_id = "turn-focus"
    transcript = conversation(
        previous_id,
        "previous request",
        "previous answer",
        extra=[
            {
                "role": "assistant",
                "kind": "tool_calls",
                "content": "",
                "conversation_id": previous_id,
                "tool_calls": [
                    {
                        "id": "call-old",
                        "name": "skill_load",
                        "args": {"name": "testing"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "skill_load",
                "tool_call_id": "call-old",
                "content": "loaded",
                "conversation_id": previous_id,
            },
        ],
    )
    transcript.extend(
        conversation(
            focus_id,
            "focus request",
            "focus answer",
            extra=[
                {
                    "role": "assistant",
                    "kind": "tool_calls",
                    "content": "",
                    "conversation_id": focus_id,
                    "tool_calls": [
                        {
                            "id": "call-new",
                            "name": "skill_load",
                            "args": {"name": "reviewing"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "skill_load",
                    "tool_call_id": "call-new",
                    "content": "loaded",
                    "conversation_id": focus_id,
                },
            ],
        )
    )

    window = build_pending_window(
        transcript,
        focus_id,
        available_skills=[
            {"name": "testing", "description": "Test changes", "scope": "project"},
            {"name": "reviewing", "description": "Review changes", "scope": "user"},
        ],
        current_loaded_skills=[
            {
                "name": "reviewing",
                "description": "Review changes carefully",
                "when_to_use": "After implementation",
                "source": "user",
            }
        ],
    )

    assert all(
        event["name"] != "skill_load"
        for conversation in [
            *window["supporting_conversations"],
            window["focus_conversation"],
        ]
        for event in conversation["tool_events"]
    )
    assert window["loaded_skill_references"] == [
        {
            "conversation_id": previous_id,
            "name": "testing",
            "description": "Test changes",
            "when_to_use": "",
            "source": "project",
        },
        {
            "conversation_id": focus_id,
            "name": "reviewing",
            "description": "Review changes carefully",
            "when_to_use": "After implementation",
            "source": "user",
        },
    ]


def test_runtime_builds_pending_from_transcript_after_history_compaction(tmp_path):
    agent = build_agent(tmp_path, [])
    agent._current_conversation_id = "turn-transcript"
    agent.record({"role": "user", "content": "original request"})
    agent.record({"role": "assistant", "kind": "final", "content": "original answer"})
    agent.session["history"] = []

    agent.skill_evolution.after_completion(None, "original request", "original answer")

    pending = agent.session["skill_evolution_pending"]
    assert pending["focus_conversation"]["messages"] == [
        {"role": "user", "kind": "", "content": "original request"},
        {"role": "assistant", "kind": "final", "content": "original answer"},
    ]
