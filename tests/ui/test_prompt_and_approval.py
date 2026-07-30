"""终端输入与审批 UI 测试。

覆盖模块：SlashCommandCompleter、TerminalUI approval menu。
重点边界：斜杠命令补全说明、模板插入、会话级 allow 选项返回结构。
"""

from io import StringIO
from unittest.mock import patch

from prompt_toolkit.document import Document
from rich.console import Console

from codemate import cli as mini_cli
from codemate.ui import TerminalUI


def test_slash_command_completer_shows_descriptions_and_inserts_template():
    completer = mini_cli.SlashCommandCompleter()

    root_items = list(completer.get_completions(Document("/"), None))
    remember_items = list(completer.get_completions(Document("/rem"), None))
    provider_items = list(completer.get_completions(Document("/provider "), None))
    model_items = list(completer.get_completions(Document("/model "), None))
    approval_items = list(completer.get_completions(Document("/approval "), None))
    plan_items = list(completer.get_completions(Document("/plan"), None))
    review_items = list(completer.get_completions(Document("/rev"), None))

    assert any(str(item.display_text) == "/help" and str(item.display_meta_text) for item in root_items)
    assert any(str(item.display_text) == "/approval read_only" for item in approval_items)
    assert any(str(item.display_text) == "/plan exit" for item in plan_items)
    assert any(str(item.display_text) == "/review" for item in review_items)
    assert any(str(item.display_text) == "/review <focus>" for item in review_items)
    assert any(str(item.display_text) == "/provider openai" for item in provider_items)
    assert any(str(item.display_text) == "/provider anthropic" for item in provider_items)
    assert any(str(item.display_text) == "/model gpt-5.5" for item in model_items)
    assert any(str(item.display_text) == "/model claude-opus-4-8" for item in model_items)
    remember = next(item for item in remember_items if str(item.display_text) == "/remember <text>")
    assert remember.text == "/remember "
    assert str(remember.display_meta_text) == "Add a high-confidence memory candidate."

def test_terminal_approval_can_return_session_allow_choice():
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    captured_choices = []

    def fake_menu(choices):
        captured_choices.extend(choices)
        return choices[1][1]

    with patch.object(ui, "approval_menu", fake_menu):
        decision = ui.approval_request(
            "read_file",
            {"path": "/home/user/data/a.txt"},
            metadata={
                "risk_level": "low",
                "approval_access": "read",
                "suggested_allow_dir": "/home/user/data",
            },
        )

    assert decision == {
        "allowed": True,
        "remember": {
            "access": "read",
            "path": "/home/user/data",
        },
    }
    assert [label for label, _decision in captured_choices] == [
        "Allow once",
        "Allow read for /home/user/data this session",
        "Deny",
    ]


def test_terminal_request_user_input_adds_other_and_returns_custom_answer():
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))
    captured = []

    def fake_menu(choices, **_kwargs):
        captured.extend(choices)
        return choices[-1][1]

    with patch.object(ui, "_selection_menu", fake_menu), patch.object(ui.console, "input", return_value="Custom choice"):
        result = ui.request_user_input(
            [
                {
                    "id": "storage",
                    "header": "Storage",
                    "question": "Choose a storage format.",
                    "options": [
                        {
                            "label": "JSON",
                            "description": "Use the current format.",
                            "recommended": True,
                        },
                        {
                            "label": "SQLite",
                            "description": "Add a database.",
                        },
                    ],
                }
            ]
        )

    assert result == {
        "status": "answered",
        "answers": {"storage": {"type": "custom", "value": "Custom choice"}},
    }
    assert captured[-1][0].startswith("Other")
    assert "(Recommended)" in captured[0][0]


def test_terminal_plan_review_collects_revision_feedback():
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))

    def choose_revision(choices, **_kwargs):
        return choices[1][1]

    with patch.object(ui, "_selection_menu", choose_revision), patch.object(
        ui.console,
        "input",
        return_value="Preserve the old format.",
    ):
        result = ui.plan_review("Plan title", "# Plan title\n\n## Tool\n\nUpdate the tool.")

    assert result == {
        "decision": "revision_requested",
        "feedback": "Preserve the old format.",
    }
    assert "Plan title" in output.getvalue()


def test_terminal_plan_review_reprompts_for_empty_revision_feedback():
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))

    def choose_revision(choices, **_kwargs):
        return choices[1][1]

    with patch.object(ui, "_selection_menu", choose_revision), patch.object(
        ui.console,
        "input",
        side_effect=["", "Preserve the old format."],
    ):
        result = ui.plan_review("Plan title", "# Plan title\n\n## Tool\n\nUpdate the tool.")

    assert result == {
        "decision": "revision_requested",
        "feedback": "Preserve the old format.",
    }
    assert "Revision feedback cannot be empty." in output.getvalue()
