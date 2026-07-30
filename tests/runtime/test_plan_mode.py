"""Plan Mode workflow tests.

These tests cover the state and permission boundary, plan-only tools, approval
transitions, same-loop implementation, session recovery, and compact recovery.
"""

import json
from types import SimpleNamespace

from codemate import MiniAgent, ModelResponse
from codemate import cli
from codemate.context.types import INTERNAL_CONTEXT_MESSAGE_KINDS
from codemate.memory.candidates import conversations_since_checkpoint
from codemate.runtime.planning import AGENT_MODE, PLAN_MODE
from codemate.ui import NullUI
from tests.helpers import build_agent


PLAN = """# Add status command

## CLI

Add a read-only status command and preserve existing command behavior.

## Validation

Cover the new command and existing help output.
"""


class PlanTestUI(NullUI):
    def __init__(self, decision="approved", feedback="", answers=None):
        self.decision = decision
        self.feedback = feedback
        self.answers = answers or {}
        self.reviewed = []
        self.questions = []

    def plan_review(self, title, plan):
        self.reviewed.append((title, plan))
        return {"decision": self.decision, "feedback": self.feedback}

    def request_user_input(self, questions):
        self.questions.append(questions)
        return {"status": "answered", "answers": self.answers}


def submit_response(call_id="call_plan"):
    return ModelResponse.tool_call(
        "submit_plan",
        {"title": "Add status command", "plan": PLAN},
        call_id=call_id,
    )


def test_plan_mode_switches_policy_prefix_and_visible_tools(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    normal_names = {item["name"] for item in agent.model_tools()}

    assert "write_file" in normal_names
    assert "submit_plan" not in normal_names
    assert agent.enter_plan_mode() is True

    plan_names = {item["name"] for item in agent.model_tools()}
    assert agent.session["workflow_mode"] == PLAN_MODE
    assert agent.session["plan"]["status"] == "drafting"
    assert agent.session["plan"]["previous_approval_policy"] == "ask"
    assert agent.approval_policy == "read_only"
    assert {"request_user_input", "submit_plan", "todo_write", "delegate"} <= plan_names
    assert "write_file" not in plan_names
    assert "patch_file" not in plan_names
    assert "skill_unload" not in plan_names
    assert not any(name.startswith("mcp__") for name in plan_names)
    assert "## Planning workflow" in agent.prefix
    assert "After editing code" not in agent.prefix
    assert "must each be the only tool call" in agent.prefix
    plan_specs = {item["name"]: item for item in agent.model_tools()}
    assert "tests, builds, formatters" in plan_specs["run_shell"]["description"]
    assert "It is not the implementation plan" in plan_specs["todo_write"]["description"]

    assert agent.exit_plan_mode() is True
    assert agent.session["workflow_mode"] == AGENT_MODE
    assert agent.session["plan"] is None
    assert agent.approval_policy == "ask"
    normal_specs = {item["name"]: item for item in agent.model_tools()}
    assert normal_specs["todo_write"]["description"] != plan_specs["todo_write"]["description"]


def test_plan_mode_read_only_boundary_rejects_non_read_shell(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    agent.enter_plan_mode()

    read_result = agent.run_tool("run_shell", {"command": "pwd"})
    modify_result = agent.run_tool("run_shell", {"command": "touch created.txt"})
    unknown_result = agent.run_tool("run_shell", {"command": "python -c 'print(1)'"})
    hidden_result = agent.run_tool("write_file", {"path": "hidden.txt", "content": "no"})

    assert str(tmp_path) in read_result
    assert "blocked in read-only" in modify_result
    assert "blocked in read-only" in unknown_result
    assert "not available in Plan Mode" in hidden_result
    assert not (tmp_path / "created.txt").exists()
    assert not (tmp_path / "hidden.txt").exists()


def test_request_user_input_validates_and_returns_answers(tmp_path):
    ui = PlanTestUI(answers={"storage": {"type": "option", "value": "JSON"}})
    agent = build_agent(tmp_path, [], ui=ui)
    agent.enter_plan_mode()
    args = {
        "questions": [
            {
                "id": "storage",
                "header": "Storage",
                "question": "Which format should be persisted?",
                "options": [
                    {"label": "JSON", "description": "Use the existing format.", "recommended": True},
                    {"label": "SQLite", "description": "Add a database dependency."},
                ],
            }
        ]
    }

    result = json.loads(agent.run_tool("request_user_input", args))

    assert result["status"] == "answered"
    assert result["answers"]["storage"]["value"] == "JSON"
    assert ui.questions == [args["questions"]]

    args["questions"][0]["options"].reverse()
    invalid = agent.run_tool("request_user_input", args)
    assert "recommended option must be first" in invalid


def test_plan_tools_reject_wrong_runtime_types(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.enter_plan_mode()

    invalid_plan = agent.run_tool("submit_plan", {"title": 123, "plan": PLAN})
    invalid_recommendation = agent.run_tool(
        "request_user_input",
        {
            "questions": [
                {
                    "id": "storage",
                    "header": "Storage",
                    "question": "Which format?",
                    "options": [
                        {
                            "label": "JSON",
                            "description": "Use JSON.",
                            "recommended": "false",
                        },
                        {"label": "SQLite", "description": "Use SQLite."},
                    ],
                }
            ]
        },
    )

    assert "title must be a string" in invalid_plan
    assert "recommended must be a boolean" in invalid_recommendation


def test_interactive_plan_tool_must_be_the_only_call_in_response(tmp_path):
    ui = PlanTestUI(decision="approved")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.from_tool_calls(
                [
                    {
                        "id": "call_plan",
                        "name": "submit_plan",
                        "args": {"title": "Add status command", "plan": PLAN},
                    },
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "args": {"path": "escaped.txt", "content": "must not run\n"},
                    },
                ]
            ),
            ModelResponse.final("The mixed tool response was rejected."),
        ],
        approval_policy="auto",
        ui=ui,
    )
    agent.enter_plan_mode()

    result = agent.ask("Prepare a plan.")

    assert result == "The mixed tool response was rejected."
    assert agent.is_plan_mode()
    assert agent.session["plan"]["status"] == "drafting"
    assert not (tmp_path / "escaped.txt").exists()
    assert ui.reviewed == []
    tool_results = [
        item
        for item in agent.session["history"]
        if item.get("role") == "tool" and item.get("tool_call_id") in {"call_plan", "call_write"}
    ]
    assert len(tool_results) == 2
    assert all("must each be the only tool call" in item["content"] for item in tool_results)


def test_plan_mode_can_return_an_explanation_without_submitting(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("This can be answered without a plan.")])
    agent.enter_plan_mode()

    result = agent.ask("Explain the current planning boundary.")

    assert result == "This can be answered without a plan."
    assert agent.is_plan_mode()
    assert agent.session["plan"]["status"] == "drafting"


def test_submit_plan_approval_implements_in_same_agent_loop(tmp_path):
    ui = PlanTestUI(decision="approved")
    agent = build_agent(
        tmp_path,
        [
            submit_response(),
            ModelResponse.tool_call(
                "write_file",
                {"path": "implemented.txt", "content": "implemented\n"},
                call_id="call_write",
            ),
            ModelResponse.final("Implemented and verified."),
        ],
        approval_policy="auto",
        ui=ui,
    )
    agent.enter_plan_mode()

    result = agent.ask("Plan and implement the status command.")

    assert result == "Implemented and verified."
    assert (tmp_path / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert agent.session["workflow_mode"] == AGENT_MODE
    assert agent.session["plan"]["status"] == "approved"
    assert agent.approval_policy == "auto"
    assert ui.reviewed == [("Add status command", PLAN.strip())]
    assert "submit_plan" in {item["name"] for item in agent.model_client.tool_specs[0]}
    assert "write_file" in {item["name"] for item in agent.model_client.tool_specs[1]}


def test_approved_plan_is_retained_across_user_turns(tmp_path):
    ui = PlanTestUI(decision="approved")
    agent = build_agent(
        tmp_path,
        [
            submit_response(),
            ModelResponse.final("The plan is approved; implementation will continue later."),
            ModelResponse.final("Handled the newer request without discarding the approved plan."),
        ],
        approval_policy="auto",
        ui=ui,
    )
    agent.enter_plan_mode()

    agent.ask("Prepare the implementation plan.")
    assert agent.session["plan"]["status"] == "approved"

    agent.ask("Answer this newer question first.")

    assert agent.session["plan"]["status"] == "approved"
    assert "latest user request always takes precedence" in agent.prefix


def test_submit_plan_revision_stays_in_plan_mode(tmp_path):
    ui = PlanTestUI(decision="revision_requested", feedback="Keep the existing CLI output unchanged.")
    agent = build_agent(
        tmp_path,
        [submit_response(), ModelResponse.final("I will revise the plan.")],
        approval_policy="ask",
        ui=ui,
    )
    agent.enter_plan_mode()

    agent.ask("Prepare a plan.")

    assert agent.is_plan_mode()
    assert agent.approval_policy == "read_only"
    assert agent.session["plan"]["status"] == "drafting"
    assert agent.session["plan"]["revision_feedback"] == "Keep the existing CLI output unchanged."
    tool_results = [item for item in agent.session["history"] if item.get("name") == "submit_plan"]
    assert "Keep the existing CLI output unchanged." in tool_results[-1]["content"]


def test_submit_plan_cancel_restores_policy_and_clears_plan(tmp_path):
    ui = PlanTestUI(decision="cancelled")
    agent = build_agent(
        tmp_path,
        [submit_response(), ModelResponse.final("Planning was cancelled.")],
        approval_policy="auto",
        ui=ui,
    )
    agent.enter_plan_mode()
    agent.session["todos"] = [{"phase": "Investigate CLI", "status": "pending", "tasks": []}]

    agent.ask("Prepare a plan.")

    assert not agent.is_plan_mode()
    assert agent.approval_policy == "auto"
    assert agent.session["plan"] is None
    assert agent.session["todos"] == []
    submit_call_index = next(
        index
        for index, item in enumerate(agent.session["history"])
        if item.get("role") == "assistant"
        and any(call.get("name") == "submit_plan" for call in item.get("tool_calls", []))
    )
    assert agent.session["history"][submit_call_index + 1]["name"] == "submit_plan"
    assert agent.session["history"][submit_call_index + 2]["kind"] == "todo_invalidated_context"


def test_exiting_plan_mode_clears_active_todos_and_records_invalidation(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.enter_plan_mode()
    agent.session["todos"] = [{"phase": "Inspect permissions", "status": "pending", "tasks": []}]

    assert agent.exit_plan_mode() is True

    assert agent.session["todos"] == []
    invalidation = agent.session["history"][-1]
    assert invalidation["kind"] == "todo_invalidated_context"
    assert "Do not continue those Todo items" in invalidation["content"]


def test_plan_exit_can_clear_an_approved_plan(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    agent.approve_plan()

    assert agent.exit_plan_mode() is True
    assert agent.session["plan"] is None
    assert agent.approval_policy == "auto"


def test_clearing_retained_plan_does_not_restore_an_obsolete_policy(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    agent.approve_plan()
    agent.approval_policy = "ask"

    assert agent.exit_plan_mode() is True
    assert agent.session["plan"] is None
    assert agent.approval_policy == "ask"


def test_starting_a_new_plan_replaces_a_retained_approved_plan(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    agent.approve_plan()

    assert agent.enter_plan_mode() is True
    assert agent.session["plan"]["status"] == "drafting"
    assert agent.session["plan"]["content"] == ""
    assert agent.session["plan"]["previous_approval_policy"] == "auto"


def test_session_resume_resets_pending_approval_to_drafting(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    session_id = agent.session["id"]
    agent.close()

    resumed = MiniAgent.from_session(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=session_id,
        approval_policy="full",
    )

    assert resumed.is_plan_mode()
    assert resumed.approval_policy == "read_only"
    assert resumed.session["plan"]["status"] == "drafting"
    assert resumed.session["plan"]["previous_approval_policy"] == "ask"


def test_session_resume_keeps_saved_policy_after_plan_was_approved(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    agent.approve_plan()
    session_id = agent.session["id"]

    resumed = MiniAgent.from_session(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=session_id,
        approval_policy="ask",
    )

    assert not resumed.is_plan_mode()
    assert resumed.session["plan"]["status"] == "approved"
    assert resumed.approval_policy == "auto"


def test_compact_restores_plan_only_when_not_retained(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.enter_plan_mode()
    agent.session["plan"].update({"title": "Add status command", "content": PLAN})

    restored = agent._restore_compact_context([])
    assert len(restored) == 1
    assert restored[0]["kind"] == "plan_context"
    assert PLAN.strip() in restored[0]["content"]

    retained = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_plan",
                    "name": "submit_plan",
                    "args": {"title": "Add status command", "plan": PLAN},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_plan",
            "name": "submit_plan",
            "content": '{"status": "revision_requested"}',
        },
    ]
    assert not any(item.get("kind") == "plan_context" for item in agent._restore_compact_context(retained))


def test_compact_labels_approved_plan_as_retained_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.enter_plan_mode()
    agent.begin_plan_submission("Add status command", PLAN)
    agent.approve_plan()

    restored = agent._restore_compact_context([])

    assert restored[0]["kind"] == "plan_context"
    assert "previously approved" in restored[0]["content"].lower()
    assert "possible continuation" in restored[0]["content"].lower()


def test_plan_runtime_context_is_excluded_from_memory_candidates(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "user",
            "kind": "plan_context",
            "content": "Synthetic retained plan.",
        }
    )

    extracted = conversations_since_checkpoint(agent.session, include_incomplete=True)

    assert "plan_context" in INTERNAL_CONTEXT_MESSAGE_KINDS
    assert extracted["conversations"] == []


def test_plan_slash_commands_enter_run_block_approval_change_and_exit(tmp_path, monkeypatch, capsys):
    agent = build_agent(tmp_path, [ModelResponse.final("Plan Mode explanation.")], approval_policy="auto")
    inputs = iter(["/plan", "inspect the CLI", "/approval full", "/plan exit", "/exit"])

    class FakePromptSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, prompt_text):
            expected = "\ncodemate[plan]> " if agent.is_plan_mode() else "\ncodemate> "
            assert prompt_text == expected
            return next(inputs)

    monkeypatch.setattr(cli, "PromptSession", FakePromptSession)
    args = SimpleNamespace(provider="openai", prompt=[], host="")

    assert cli.run_cli(args, NullUI(), {"agent": agent}) == 0
    assert not agent.is_plan_mode()
    assert agent.approval_policy == "auto"
    assert "approval policy cannot be changed in Plan Mode" in capsys.readouterr().out


def test_plan_inline_task_command_is_not_supported(tmp_path, monkeypatch, capsys):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    inputs = iter(["/plan inspect the CLI", "/exit"])

    class FakePromptSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, prompt_text):
            assert prompt_text == "\ncodemate> "
            return next(inputs)

    monkeypatch.setattr(cli, "PromptSession", FakePromptSession)
    args = SimpleNamespace(provider="openai", prompt=[], host="")

    assert cli.run_cli(args, NullUI(), {"agent": agent}) == 0
    assert not agent.is_plan_mode()
    assert "usage: /plan or /plan exit" in capsys.readouterr().out
