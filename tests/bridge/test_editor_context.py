import pytest

from codemate.bridge.server import _render_editor_context


def test_editor_context_renders_bounded_attachments():
    rendered = _render_editor_context(
        [
            {
                "kind": "selection",
                "label": "app.py:2-3",
                "path": "app.py",
                "start_line": 2,
                "end_line": 3,
                "content": "value = 1",
            },
            {
                "kind": "problems",
                "label": "Problems in app.py",
                "path": "app.py",
                "content": "app.py:2:1 [error] broken",
            },
        ]
    )

    assert "repository evidence" in rendered
    assert "[selection] app.py:2-3" in rendered
    assert "[problems] Problems in app.py (app.py)" in rendered


def test_editor_context_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unsupported"):
        _render_editor_context([{"kind": "terminal", "content": "output"}])
