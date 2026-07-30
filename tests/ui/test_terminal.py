"""Terminal event styling tests."""

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from codemate.ui.terminal import COMMENTARY_STYLE, FINAL_ANSWER_MARKER_STYLE, TerminalUI


class RecordingConsole:
    def __init__(self):
        self.printed = []

    def print(self, *objects, **kwargs):
        self.printed.append({"objects": objects, "kwargs": kwargs})


def test_tool_call_and_result_use_lightweight_lines_without_panels():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("run_shell", {"command": "pytest -q"}, risk_level="high")
    ui.tool_result(
        "run_shell",
        {"command": "pytest -q"},
        "exit_code: 0\nstdout:\n1 passed\nstderr:\n(empty)",
        {"tool_status": "ok"},
    )

    renderables = [item["objects"][0] for item in console.printed]
    assert all(not isinstance(item, Panel) for item in renderables)
    assert all(isinstance(item, Text) for item in renderables)
    assert renderables[0].plain == "◇ run_shell  [high]"
    assert renderables[1].plain == "      $ pytest -q"
    assert renderables[2].plain == "  ↳ ok · run_shell"


def test_low_risk_label_is_omitted_from_normal_tool_log():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("read_file", {"path": "README.md"}, risk_level="low")

    assert console.printed[0]["objects"][0].plain == "◇ read_file README.md"


def test_commentary_and_final_render_as_unframed_markdown_with_marker():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.commentary("**Progress**")
    ui.final_answer("**Done**")

    commentary = console.printed[0]["objects"][0]
    marker = console.printed[1]["objects"][0]
    final = console.printed[2]["objects"][0]
    assert isinstance(commentary, Markdown)
    assert commentary.style == COMMENTARY_STYLE
    assert isinstance(marker, Text)
    assert marker.plain == "◆ Final answer"
    assert marker.style == FINAL_ANSWER_MARKER_STYLE
    assert isinstance(final, Markdown)
    assert final.style == "none"


def test_submit_plan_tool_start_is_hidden_because_plan_review_renders_panel():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("submit_plan", {"title": "Plan", "plan": "# Plan"})

    assert console.printed == []

    ui.tool_start("read_file", {"path": "README.md"}, risk_level="low")

    assert console.printed[0]["objects"][0].plain == "◇ read_file README.md"
