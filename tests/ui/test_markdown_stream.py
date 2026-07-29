"""Markdown streaming renderer tests.

The UI should stream useful output without printing raw half-finished Markdown
syntax. These tests exercise the block buffering rules directly.
"""

from rich.markdown import Markdown

from codemate.ui import TerminalUI
from codemate.ui.markdown_stream import COMMENTARY_STYLE, MarkdownStreamRenderer


class RecordingConsole:
    def __init__(self):
        self.printed = []

    def print(self, *objects, **kwargs):
        self.printed.append({"objects": objects, "kwargs": kwargs})


class RecordingLive:
    instances = []

    def __init__(self, renderable, **kwargs):
        self.renderables = [renderable]
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        type(self).instances.append(self)

    def start(self, refresh=False):
        self.started = refresh

    def update(self, renderable, refresh=False):
        self.renderables.append(renderable)

    def stop(self):
        self.stopped = True


def rendered_markup(console):
    return [item["objects"][0].markup for item in console.printed if isinstance(item["objects"][0], Markdown)]


def test_markdown_stream_flushes_complete_paragraph_before_finish():
    console = RecordingConsole()
    renderer = MarkdownStreamRenderer(console)

    renderer.write("**Done** paragraph.\n\nNext")

    assert rendered_markup(console) == ["**Done** paragraph."]
    assert renderer.buffer == "Next"


def test_markdown_stream_keeps_unclosed_fence_until_finish():
    console = RecordingConsole()
    renderer = MarkdownStreamRenderer(console)

    renderer.write("```python\nprint('hi')\n")

    assert rendered_markup(console) == []

    renderer.write("```\n")

    assert rendered_markup(console) == ["```python\nprint('hi')\n```"]
    assert renderer.buffer == ""


def test_markdown_stream_finish_flushes_remaining_text():
    console = RecordingConsole()
    renderer = MarkdownStreamRenderer(console)

    renderer.write("- one\n- two")
    renderer.finish()

    assert rendered_markup(console) == ["- one\n- two"]
    assert renderer.buffer == ""


def test_markdown_stream_dims_commentary_phase():
    console = RecordingConsole()
    renderer = MarkdownStreamRenderer(console)

    renderer.write("progress\n\n", phase="commentary")

    markdown = console.printed[0]["objects"][0]
    assert markdown.style == COMMENTARY_STYLE


def test_terminal_stream_flushes_pending_text_when_phase_changes():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.stream_start()
    ui.stream_delta("progress without blank", phase="commentary")
    ui.stream_delta("final text\n\n", phase="final_answer")
    ui.stream_end(kind="final")

    markdown_objects = [item["objects"][0] for item in console.printed if isinstance(item["objects"][0], Markdown)]
    assert [item.markup for item in markdown_objects] == ["progress without blank", "final text"]
    assert [item.style for item in markdown_objects] == [COMMENTARY_STYLE, "none"]


def test_terminal_live_preview_updates_before_markdown_block_finishes():
    RecordingLive.instances = []
    console = RecordingConsole()
    console.is_terminal = True
    renderer = MarkdownStreamRenderer(console, live_factory=RecordingLive)

    renderer.write("Streaming", phase="final_answer")
    renderer.write(" text", phase="final_answer")

    live = RecordingLive.instances[0]
    assert live.started is True
    assert [item.markup for item in live.renderables] == ["Streaming", "Streaming text"]
    assert rendered_markup(console) == []

    renderer.finish(phase="final_answer")

    assert live.stopped is True
    assert rendered_markup(console) == ["Streaming text"]
