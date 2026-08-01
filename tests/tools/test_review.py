"""Independent review tool tests.

These tests cover the serial review child, fixed tool boundary, inherited
approval state, exclusive tool-call rule, phased prompt, and review progress UI.
"""

import json
from types import SimpleNamespace

import pytest

from codemate import ModelResponse, cli
from codemate.models.types import ModelToolCall
from codemate.runtime import agent as agent_module
from codemate.runtime.errors import ModelRequestError
from codemate.runtime.review import (
    MANUAL_REVIEW_REQUEST,
    REVIEW_ALLOWED_TOOLS,
    REVIEW_FINALIZATION_RECOVERY_REQUEST,
    manual_review_request,
)
from codemate.tools import DELEGATE_TOOL_SPEC, REVIEW_TOOL_SPEC, SUBAGENT_MAX_STEPS
from codemate.ui import NullUI
from tests.helpers import build_agent


class RecordingReviewUI:
    def __init__(self, approval_decision=None):
        self.commentary_messages = []
        self.review_events = []
        self.tool_starts = []
        self.tool_results = []
        self.approval_requests = []
        self.approval_decision = approval_decision or {"allowed": False}

    def commentary(self, text):
        self.commentary_messages.append(text)

    def review_start(self):
        self.review_events.append(("start", {}))

    def review_end(self, status="", metadata=None):
        self.review_events.append((status, dict(metadata or {})))

    def tool_start(self, name, args, risk_level=""):
        self.tool_starts.append((name, args, risk_level))

    def tool_result(self, name, args, result, metadata=None):
        self.tool_results.append((name, args, result, dict(metadata or {})))

    def approval_request(self, name, args, metadata=None):
        self.approval_requests.append((name, args, dict(metadata or {})))
        return self.approval_decision

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def stream_start(self, phase=""):
        pass

    def stream_delta(self, text, phase=""):
        pass

    def stream_end(self, kind="", metadata=None):
        pass

    def final_answer(self, text):
        pass


def test_subagent_step_limit_is_runtime_owned():
    assert SUBAGENT_MAX_STEPS == 100
    assert "max_steps" not in DELEGATE_TOOL_SPEC["input_schema"]["properties"]


@pytest.mark.parametrize("approval_policy", ["ask", "auto", "read_only", "full"])
def test_review_child_inherits_parent_runtime_configuration(
    tmp_path, monkeypatch, approval_policy
):
    captured = {}

    class StubReviewChild:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session = kwargs["session"]
            self.current_run_dir = ""
            self.current_task_state = SimpleNamespace(stop_reason="final_answer_returned")

        def ask(self, prompt):
            captured["prompt"] = prompt
            return "No actionable issues found."

        def close(self):
            captured["closed"] = True

    agent = build_agent(tmp_path, [], approval_policy=approval_policy)
    monkeypatch.setattr(agent_module, "CodeMate", StubReviewChild)

    result = agent.run_tool(
        "review",
        {"target": "Check compatibility and adjacent behavior."},
    )

    assert "review_status: ok" in result
    assert captured["approval_policy"] == agent.approval_policy == approval_policy
    assert captured["max_steps"] == SUBAGENT_MAX_STEPS
    assert captured["runtime_mode"] == "review"
    assert captured["stream"] is False
    assert captured["allowed_tools"] == REVIEW_ALLOWED_TOOLS
    assert captured["max_depth"] == captured["depth"]
    assert captured["closed"] is True
    for flag in (
        "long_term_memory",
        "relevant_memory",
        "memory_candidates",
        "memory_dream",
        "session_title",
    ):
        assert captured["feature_flags"][flag] is False


def test_review_target_is_optional_and_general_request_is_explicit(tmp_path):
    assert REVIEW_TOOL_SPEC["input_schema"]["required"] == []

    agent = build_agent(tmp_path, [ModelResponse.final("No findings.")])
    result = agent.run_tool("review", {})

    assert "review_status: ok" in result
    prompt = agent.model_client.prompts[0]
    assert "No specific target was provided" in prompt
    child_sessions = [
        item for item in agent.session_store.root.iterdir() if item.name.startswith("review-")
    ]
    child_session = json.loads(
        (child_sessions[0] / "session.json").read_text(encoding="utf-8")
    )
    assert child_session["review_target"] == ""


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"target": 1}, "target must be a string"),
        ({"task": "legacy"}, "review only accepts the target argument"),
        ({"target": "x" * 20_001}, "target must contain at most 20000 characters"),
    ],
)
def test_review_rejects_invalid_or_legacy_arguments(tmp_path, args, error):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("review", args)

    assert error in result


def test_review_child_tool_allowlist_blocks_direct_file_writes(tmp_path):
    target = tmp_path / "review-must-not-write.txt"
    ui = RecordingReviewUI()
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call(
                "review",
                {"target": "Check the changed ownership boundary."},
            ),
            ModelResponse.commentary("I found the changed ownership boundary and will inspect its caller."),
            ModelResponse.tool_call("list_files", {"path": str(tmp_path)}),
            ModelResponse.tool_call("write_file", {"path": str(target), "content": "nope"}),
            ModelResponse.final("No actionable issues found."),
            ModelResponse.final("Review found no actionable issues."),
        ],
        ui=ui,
    )

    result = agent.ask("Review the current changes.")

    assert result == "Review found no actionable issues."
    assert not target.exists()
    assert ui.commentary_messages == [
        "I found the changed ownership boundary and will inspect its caller."
    ]
    child_starts = [item for item in ui.tool_starts if item[0] != "review"]
    child_results = [item for item in ui.tool_results if item[0] != "review"]
    assert [item[0] for item in child_starts] == ["list_files", "write_file"]
    assert [item[0] for item in child_results] == ["list_files", "write_file"]
    assert child_results[0][3]["tool_status"] == "ok"
    assert child_results[1][3]["tool_status"] == "rejected"
    assert ui.review_events[0][0] == "start"
    assert ui.review_events[-1][0] == "ok"
    tool_results = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert tool_results[0]["name"] == "review"
    assert "No actionable issues found." in tool_results[0]["content"]


def test_review_child_inherits_temporary_read_permissions(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-review-outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "note.txt"
    outside_file.write_text("review evidence\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": str(outside_file), "start": 1, "end": 1}),
            ModelResponse.final("The external evidence is consistent."),
        ],
        approval_policy="ask",
    )
    agent.add_temporary_permission("read", outside_dir)

    result = agent.run_tool(
        "review",
        {"target": "Use the approved external note as supporting evidence."},
    )

    assert "review_status: ok" in result
    assert "The external evidence is consistent." in result
    child_sessions = [item for item in agent.session_store.root.iterdir() if item.name.startswith("review-")]
    assert len(child_sessions) == 1
    child_session = json.loads((child_sessions[0] / "session.json").read_text(encoding="utf-8"))
    assert child_session["temporary_permissions"]["permissions"]["read"]["allow"] == [
        str(outside_dir.resolve())
    ]
    child_results = [item for item in child_session["history"] if item.get("role") == "tool"]
    assert "review evidence" in child_results[0]["content"]
    assert not any(item["id"].startswith("review-") for item in agent.session_store.list_sessions())


def test_review_approval_bubbles_to_parent_and_session_grant_is_shared(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-review-approved"
    outside_dir.mkdir()
    outside_file = outside_dir / "note.txt"
    outside_file.write_text("approved review evidence\n", encoding="utf-8")
    ui = RecordingReviewUI(
        {
            "allowed": True,
            "remember": {"access": "read", "path": str(outside_dir)},
        }
    )
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": str(outside_file), "start": 1, "end": 1}),
            ModelResponse.final("The approved evidence was reviewed."),
        ],
        approval_policy="ask",
        ui=ui,
    )

    result = agent.run_tool("review", {})

    assert "review_status: ok" in result
    assert ui.approval_requests[0][0] == "read_file"
    assert agent.session["temporary_permissions"]["permissions"]["read"]["allow"] == [
        str(outside_dir.resolve())
    ]
    child_sessions = [
        item for item in agent.session_store.root.iterdir() if item.name.startswith("review-")
    ]
    child_session = json.loads(
        (child_sessions[0] / "session.json").read_text(encoding="utf-8")
    )
    assert child_session["temporary_permissions"]["permissions"]["read"]["allow"] == [
        str(outside_dir.resolve())
    ]


def test_review_must_be_the_only_tool_call(tmp_path):
    mixed = ModelResponse.from_tool_calls(
        [
            ModelToolCall.create(
                "review",
                {"target": "Check correctness and adjacent behavior."},
            ),
            ModelToolCall.create("read_file", {"path": "README.md"}),
        ]
    )
    agent = build_agent(tmp_path, [mixed, ModelResponse.final("batch rejected")])

    assert agent.ask("Review changes.") == "batch rejected"
    results = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert len(results) == 2
    assert all("must each be the only tool call" in item["content"] for item in results)
    assert not any(item.name.startswith("review-") for item in agent.session_store.root.iterdir())


def test_review_runtime_has_dedicated_prompt_and_tools(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("No findings.")])

    result = agent.run_tool("review", {"target": "Check adjacent behavior."})

    assert "review_status: ok" in result
    prompt = agent.model_client.prompts[0]
    assert "You are a code reviewer" in prompt
    assert "### Phase 1: Inspect the change scope" in prompt
    assert "### Phase 2: Understand the surrounding behavior" in prompt
    assert "### Phase 3: Investigate and validate concrete risks" in prompt
    assert "### Phase 4: Filter and report findings" in prompt
    assert "### Phase 5:" not in prompt
    assert "Ignore Codemate-generated runtime metadata such as `.codemate/`" in prompt
    assert "Do not report an interpretation as a defect" in prompt
    assert "The optional review target is an investigation focus" in prompt
    assert "Optional review target:\n\nCheck adjacent behavior." in prompt
    tool_names = {item["name"] for item in agent.model_client.tool_specs[0]}
    assert tool_names == {"list_files", "read_file", "grep", "run_shell", "todo_write", "todo_list"}


def test_review_retries_final_report_once_without_tools_after_collected_evidence(
    tmp_path, monkeypatch
):
    captured = {"prompts": [], "visible_tools": []}

    class StubReviewChild:
        def __init__(self, **kwargs):
            self.session = kwargs["session"]
            self.allowed_tools = set(kwargs["allowed_tools"])
            self.tools = {name: {} for name in self.allowed_tools}
            self.current_run_dir = ""
            self.current_task_state = SimpleNamespace(stop_reason="model_error")

        def ask(self, prompt):
            captured["prompts"].append(prompt)
            captured["visible_tools"].append(set(self.tools))
            if len(captured["prompts"]) == 1:
                self.session["history"].append(
                    {"role": "tool", "name": "read_file", "content": "evidence"}
                )
                raise ModelRequestError("backend disconnected")
            self.current_task_state = SimpleNamespace(stop_reason="final_answer_returned")
            return "No actionable issues found."

        def close(self):
            pass

    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(agent_module, "CodeMate", StubReviewChild)

    result = agent.run_tool("review", {"target": "Check the changed behavior."})

    assert "review_status: ok" in result
    assert "No actionable issues found." in result
    assert captured["prompts"] == [
        "Optional review target:\n\nCheck the changed behavior.\n\n"
        "Review all relevant current staged, unstaged, and untracked project\n"
        "changes. Follow the required review phases and return the final review.",
        REVIEW_FINALIZATION_RECOVERY_REQUEST,
    ]
    assert captured["visible_tools"][0] == REVIEW_ALLOWED_TOOLS
    assert captured["visible_tools"][1] == set()
    assert agent._last_review_metadata["review_recovery_attempted"] is True
    assert agent._last_review_metadata["review_recovery_succeeded"] is True


def test_review_does_not_retry_model_error_without_collected_evidence(tmp_path, monkeypatch):
    captured = {"calls": 0}

    class StubReviewChild:
        def __init__(self, **kwargs):
            self.session = kwargs["session"]
            self.current_run_dir = ""
            self.current_task_state = SimpleNamespace(stop_reason="model_error")

        def ask(self, _prompt):
            captured["calls"] += 1
            raise ModelRequestError("backend disconnected")

        def close(self):
            pass

    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(agent_module, "CodeMate", StubReviewChild)

    result = agent.run_tool("review", {})

    assert "review_status: error" in result
    assert "backend disconnected" in result
    assert captured["calls"] == 1
    assert agent._last_review_metadata["review_recovery_attempted"] is False
    assert agent._last_review_metadata["review_recovery_succeeded"] is False


def test_review_does_not_treat_planning_or_rejection_as_collected_evidence(
    tmp_path, monkeypatch
):
    captured = {"calls": 0}

    class StubReviewChild:
        def __init__(self, **kwargs):
            self.session = kwargs["session"]
            self.current_run_dir = ""
            self.current_task_state = SimpleNamespace(stop_reason="model_error")

        def ask(self, _prompt):
            captured["calls"] += 1
            self.session["history"].extend(
                [
                    {"role": "tool", "name": "todo_write", "content": "todos updated"},
                    {
                        "role": "tool",
                        "name": "read_file",
                        "content": "status: rejected\nerror: read denied",
                    },
                ]
            )
            raise ModelRequestError("backend disconnected")

        def close(self):
            pass

    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(agent_module, "CodeMate", StubReviewChild)

    result = agent.run_tool("review", {})

    assert "review_status: error" in result
    assert captured["calls"] == 1
    assert agent._last_review_metadata["review_recovery_attempted"] is False


def test_review_finalization_recovery_is_attempted_only_once(tmp_path, monkeypatch):
    captured = {"calls": 0}

    class StubReviewChild:
        def __init__(self, **kwargs):
            self.session = kwargs["session"]
            self.allowed_tools = set(kwargs["allowed_tools"])
            self.tools = {name: {} for name in self.allowed_tools}
            self.current_run_dir = ""
            self.current_task_state = SimpleNamespace(stop_reason="model_error")

        def ask(self, _prompt):
            captured["calls"] += 1
            if captured["calls"] == 1:
                self.session["history"].append(
                    {"role": "tool", "name": "grep", "content": "evidence"}
                )
            raise ModelRequestError(f"backend disconnected {captured['calls']}")

        def close(self):
            pass

    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(agent_module, "CodeMate", StubReviewChild)

    result = agent.run_tool("review", {})

    assert "review_status: error" in result
    assert "backend disconnected 2" in result
    assert captured["calls"] == 2
    assert agent._last_review_metadata["review_recovery_attempted"] is True
    assert agent._last_review_metadata["review_recovery_succeeded"] is False


def test_review_is_visible_only_in_normal_main_agent(tmp_path):
    agent = build_agent(tmp_path, [])

    assert "review" in agent.active_tool_names()
    assert "Validation and review:" in agent.prefix
    assert "accepts an optional target" in agent.prefix
    assert "independently verify each finding" in agent.prefix

    agent.enter_plan_mode()

    assert "review" not in agent.active_tool_names()
    assert "review tool when an independent examination" not in agent.prefix


def test_manual_review_request_only_adds_nonempty_focus():
    assert manual_review_request() == MANUAL_REVIEW_REQUEST
    assert manual_review_request("  ") == MANUAL_REVIEW_REQUEST

    prompt = manual_review_request("重点检查权限绕过")

    assert prompt.startswith(MANUAL_REVIEW_REQUEST)
    assert prompt.endswith("User-requested review target:\n重点检查权限绕过")


def test_review_slash_command_routes_default_and_focused_requests(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    prompts = []
    inputs = iter(["/review", "/review 重点检查权限绕过", "/exit"])

    class FakePromptSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, _prompt_text):
            return next(inputs)

    monkeypatch.setattr(cli, "PromptSession", FakePromptSession)
    monkeypatch.setattr(agent, "ask", lambda prompt: prompts.append(prompt) or "done")
    args = type("Args", (), {"provider": "openai", "prompt": [], "host": ""})()

    assert cli.run_cli(args, NullUI(), {"agent": agent}) == 0
    assert prompts == [
        MANUAL_REVIEW_REQUEST,
        manual_review_request("重点检查权限绕过"),
    ]


def test_similar_review_prefix_is_not_treated_as_slash_command(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, [])
    prompts = []
    inputs = iter(["/reviewer", "/exit"])

    class FakePromptSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, _prompt_text):
            return next(inputs)

    monkeypatch.setattr(cli, "PromptSession", FakePromptSession)
    monkeypatch.setattr(agent, "ask", lambda prompt: prompts.append(prompt) or "done")
    args = type("Args", (), {"provider": "openai", "prompt": [], "host": ""})()

    assert cli.run_cli(args, NullUI(), {"agent": agent}) == 0
    assert prompts == ["/reviewer"]
