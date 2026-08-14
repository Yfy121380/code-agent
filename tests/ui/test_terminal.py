"""Terminal event styling tests."""

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from codemate.ui.summaries import summarize_tool_result
from codemate.ui.terminal import COMMENTARY_STYLE, TerminalUI


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
    assert renderables[0].plain == "  ├─ run_shell  high"
    assert renderables[1].plain == "  │  $ pytest -q"
    assert renderables[2].plain == "  └─ ok  run_shell"


def test_low_risk_label_is_omitted_from_normal_tool_log():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("read_file", {"path": "README.md"}, risk_level="low")

    assert console.printed[0]["objects"][0].plain == "  ├─ read_file README.md"


def test_rejected_shell_result_shows_policy_error_instead_of_empty_streams():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_result(
        "run_shell",
        {"command": "git ls-files --others --exclude-standard"},
        "error: write operations are blocked in read-only mode",
        {"tool_status": "rejected"},
    )

    rendered = [item["objects"][0].plain for item in console.printed]
    assert rendered == [
        "  └─ rejected  run_shell",
        "      error: write operations are blocked i...",
    ]


def test_optional_sandbox_degradation_is_visible_in_shell_result():
    summary = summarize_tool_result(
        "run_shell",
        "sandbox_warning: details\nexit_code: 0\nstdout:\nok\nstderr:\n(empty)",
        {"tool_status": "ok", "sandbox_degraded": True},
    )

    assert "warning: sandbox unavailable; command ran without isolation" in summary
    assert "exit_code: 0" in summary


def test_generic_tool_result_is_limited_to_four_short_lines():
    summary = summarize_tool_result(
        "custom_tool",
        "\n".join(
            [
                "a" * 80,
                "second",
                "third",
                "fourth",
                "fifth",
            ]
        ),
        {"tool_status": "ok"},
    )

    lines = summary.splitlines()
    assert lines[1] == ("a" * 37) + "..."
    assert lines[2:5] == ["second", "third", "fourth"]
    assert lines[5] == "... (1 more lines)"


def test_commentary_and_final_render_as_unframed_markdown_with_marker():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.commentary("**Progress**")
    ui.final_answer("**Done**")

    commentary_marker = console.printed[0]["objects"][0]
    commentary = console.printed[1]["objects"][0]
    final_marker = console.printed[2]["objects"][0]
    final = console.printed[3]["objects"][0]
    assert isinstance(commentary_marker, Text)
    assert commentary_marker.plain == "● CodeMate"
    assert isinstance(commentary, Markdown)
    assert commentary.style == COMMENTARY_STYLE
    assert isinstance(final_marker, Text)
    assert final_marker.plain == commentary_marker.plain
    assert final_marker.style == commentary_marker.style
    assert isinstance(final, Markdown)
    assert final.style == "none"


def test_submit_plan_tool_start_is_hidden_because_plan_review_renders_panel():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("submit_plan", {"title": "Plan", "plan": "# Plan"})

    assert console.printed == []

    ui.tool_start("read_file", {"path": "README.md"}, risk_level="low")

    assert console.printed[0]["objects"][0].plain == "  ├─ read_file README.md"


def test_review_uses_dedicated_progress_status_and_hides_generic_tool_start():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_start("review", {"target": "Check the current changes."})
    ui.review_start()
    ui.review_end(status="ok", metadata={"review_report_chars": 321})
    ui.tool_result(
        "review",
        {"target": "Check the current changes."},
        "review_status: ok\nreview_report:\nNo findings.",
        {"tool_status": "ok", "review_status": "ok", "review_report_chars": 321},
    )

    rendered = "\n".join(str(item["objects"][0]) for item in console.printed)
    assert "review  inspecting current changes" in rendered
    assert "review complete  321 chars" in rendered
    assert "├─ review  Check" not in rendered
    assert len(console.printed) == 2


def test_model_usage_is_rendered_after_non_stream_final_answer():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.model_end(
        kind="final",
        metadata={"input_tokens": 52196, "cached_tokens": 7680, "output_tokens": 105},
    )
    ui.final_answer("Done")

    renderables = [item["objects"][0] for item in console.printed]
    assert isinstance(renderables[-1], Text)
    assert renderables[-1].plain == "    model  52.2k input · 7.7k cached · 105 output"


def test_edit_tool_result_renders_change_counts_without_diff_body():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.tool_result(
        "patch_file",
        {"path": "app.py"},
        "patched app.py",
        {
            "tool_status": "ok",
            "change_preview": {
                "path": "app.py",
                "additions": 1,
                "deletions": 1,
                "diff": "@@ -1 +1 @@\n-old\n+new",
            },
        },
    )

    renderables = [item["objects"][0] for item in console.printed]
    assert any(isinstance(item, Text) and item.plain == "      Δ app.py  +1 -1" for item in renderables)
    rendered = "\n".join(str(item) for item in renderables)
    assert "-old" not in rendered
    assert "+new" not in rendered
