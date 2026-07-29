"""Markdown stream rendering helpers.

Model providers stream arbitrary text deltas, but Markdown syntax often needs
future text before a block is complete. This module buffers deltas until a
stable block boundary is reached, then renders that block with Rich Markdown.
"""

from __future__ import annotations

from rich.live import Live
from rich.markdown import Markdown


FORCED_FLUSH_PENDING_CHARS = 1200
COMMENTARY_STYLE = "#cbd5e1"


def _fence_marker(line):
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    if stripped.startswith("```"):
        return "`", len(stripped) - len(stripped.lstrip("`"))
    if stripped.startswith("~~~"):
        return "~", len(stripped) - len(stripped.lstrip("~"))
    return None


class MarkdownStreamRenderer:
    """Render streaming Markdown by flushing only stable text blocks.

    The renderer deliberately keeps history concerns out of the UI path. It
    only controls what is shown in the terminal while the canonical assistant
    text remains the complete response recorded by runtime.
    """

    def __init__(self, console, live_factory=Live):
        self.console = console
        self.live_factory = live_factory
        self.live_enabled = bool(getattr(console, "is_terminal", False))
        self.buffer = ""
        self._live = None

    def reset(self):
        if self._live is not None:
            self._live.stop()
            self._live = None
        self.buffer = ""

    def write(self, text, phase=""):
        self.buffer += str(text or "")
        self._flush_ready(phase=phase)
        if self.live_enabled and self.buffer:
            self._update_live(self.buffer, phase=phase)

    def finish(self, phase=""):
        if self.buffer:
            self._freeze_markdown(self.buffer, phase=phase)
            self.buffer = ""
        elif self._live is not None:
            self._live.stop()
            self._live = None

    def _flush_ready(self, phase=""):
        ready = self._ready_prefix()
        if not ready:
            return
        self._freeze_markdown(ready, phase=phase)
        self.buffer = self.buffer[len(ready) :]

    def _ready_prefix(self):
        """Return the buffer prefix that can be rendered without later restyling."""
        scan_end = len(self.buffer) if self.buffer.endswith("\n") else self.buffer.rfind("\n") + 1
        if scan_end <= 0:
            return ""

        in_fence = False
        fence_char = ""
        fence_len = 0
        position = 0
        blank_boundary = 0
        last_safe_line = 0
        for line in self.buffer[:scan_end].splitlines(keepends=True):
            position += len(line)
            marker = _fence_marker(line)
            if marker:
                marker_char, marker_len = marker
                if in_fence and marker_char == fence_char and marker_len >= fence_len:
                    in_fence = False
                    blank_boundary = position
                    last_safe_line = position
                    continue
                if not in_fence:
                    in_fence = True
                    fence_char = marker_char
                    fence_len = marker_len
                    continue
            if not in_fence:
                last_safe_line = position
                if not line.strip():
                    blank_boundary = position

        if blank_boundary:
            return self.buffer[:blank_boundary]
        if len(self.buffer) >= FORCED_FLUSH_PENDING_CHARS and last_safe_line:
            return self.buffer[:last_safe_line]
        return ""

    def _markdown(self, text, phase=""):
        text = str(text or "").strip("\n")
        if not text:
            return None
        style = COMMENTARY_STYLE if phase == "commentary" else "none"
        return Markdown(text, style=style)

    def _update_live(self, text, phase=""):
        """Redraw the unfinished Markdown block as new model deltas arrive."""
        renderable = self._markdown(text, phase=phase)
        if renderable is None:
            return
        if self._live is None:
            self._live = self.live_factory(
                renderable,
                console=self.console,
                refresh_per_second=15,
                transient=True,
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=False)

    def _freeze_markdown(self, text, phase=""):
        """Replace the live preview with one stable Markdown block."""
        renderable = self._markdown(text, phase=phase)
        if renderable is None:
            return
        if self._live is not None:
            self._live.update(renderable, refresh=True)
            self._live.stop()
            self._live = None
        self.console.print(renderable)
