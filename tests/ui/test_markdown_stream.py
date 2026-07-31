"""Markdown streaming renderer tests.

The UI should stream useful output without printing raw half-finished Markdown
syntax. These tests exercise the block buffering rules directly.
"""

from rich.markdown import Markdown
from rich.text import Text

from codemate.ui import TerminalUI
from codemate.ui.markdown_stream import COMMENTARY_STYLE, MarkdownStreamRenderer
from codemate.ui.terminal import ASSISTANT_MARKER_STYLE, ASSISTANT_MARKER_TEXT


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

    renderables = [item["objects"][0] for item in console.printed]
    markdown_objects = [item for item in renderables if isinstance(item, Markdown)]
    markers = [
        item
        for item in renderables
        if isinstance(item, Text) and item.plain == ASSISTANT_MARKER_TEXT
    ]
    assert [item.markup for item in markdown_objects] == ["progress without blank", "final text"]
    assert [item.style for item in markdown_objects] == [COMMENTARY_STYLE, "none"]
    assert len(markers) == 2
    assert all(marker.style == ASSISTANT_MARKER_STYLE for marker in markers)
    assert renderables.index(markers[0]) < renderables.index(markdown_objects[0])
    assert renderables.index(markers[1]) < renderables.index(markdown_objects[1])


def test_terminal_stream_assistant_marker_is_printed_once_for_one_phase():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.stream_start()
    ui.stream_delta("Final", phase="final_answer")
    ui.stream_delta(" answer\n\n", phase="final_answer")
    ui.stream_end(kind="final")
    assert len(console.printed) == 2

    renderables = [item["objects"][0] for item in console.printed]
    markers = [
        item
        for item in renderables
        if isinstance(item, Text) and item.plain == ASSISTANT_MARKER_TEXT
    ]
    markdown_objects = [item for item in renderables if isinstance(item, Markdown)]
    assert len(markers) == 1
    assert [item.markup for item in markdown_objects] == ["Final answer"]


def test_terminal_unphased_stream_uses_common_marker_and_commits_immediately():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.stream_start()
    ui.stream_delta("DeepSeek final paragraph.\n\n")

    renderables = [item["objects"][0] for item in console.printed]
    assert isinstance(renderables[0], Text)
    assert renderables[0].plain == ASSISTANT_MARKER_TEXT
    assert isinstance(renderables[1], Markdown)
    assert renderables[1].markup == "DeepSeek final paragraph."
    assert renderables[1].style == "none"

    ui.stream_end(kind="final")
    assert len(console.printed) == 2


def test_terminal_unphased_tool_text_uses_the_same_common_marker():
    console = RecordingConsole()
    ui = TerminalUI(console=console)

    ui.stream_start()
    ui.stream_delta("I found the entry point and will inspect its callers.\n\n")
    ui.stream_end(kind="tool_calls")

    renderables = [item["objects"][0] for item in console.printed]
    assert len(renderables) == 2
    assert isinstance(renderables[0], Text)
    assert renderables[0].plain == ASSISTANT_MARKER_TEXT
    assert isinstance(renderables[1], Markdown)
    assert renderables[1].style == "none"
    assert renderables[1].markup == "I found the entry point and will inspect its callers."


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


def test_terminal_unphased_stream_uses_normal_live_rendering():
    RecordingLive.instances = []
    console = RecordingConsole()
    console.is_terminal = True
    ui = TerminalUI(console=console)
    ui._stream_renderer = MarkdownStreamRenderer(console, live_factory=RecordingLive)

    ui.stream_start()
    ui.stream_delta("Streaming")
    ui.stream_delta(" without phase")

    live = RecordingLive.instances[0]
    assert live.started is True
    assert [item.markup for item in live.renderables] == [
        "Streaming",
        "Streaming without phase",
    ]
    assert rendered_markup(console) == []
    markers = [
        item["objects"][0]
        for item in console.printed
        if isinstance(item["objects"][0], Text)
    ]
    assert [marker.plain for marker in markers] == [ASSISTANT_MARKER_TEXT]

    ui.stream_end(kind="final")

    assert live.stopped is True
    renderables = [item["objects"][0] for item in console.printed]
    assert isinstance(renderables[0], Text)
    assert renderables[0].plain == ASSISTANT_MARKER_TEXT
    assert isinstance(renderables[1], Markdown)
    assert renderables[1].markup == "Streaming without phase"
