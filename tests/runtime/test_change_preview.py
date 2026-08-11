from pathlib import Path

from codemate.runtime.change_preview import build_change_preview, capture_text_snapshot


def test_change_preview_reports_bounded_hunk_and_line_counts(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    before = capture_text_snapshot(path)

    path.write_text("one\nchanged\nthree\nfour\n", encoding="utf-8")
    preview = build_change_preview(tmp_path, path, before)

    assert preview["path"] == "app.py"
    assert preview["status"] == "modified"
    assert preview["additions"] == 2
    assert preview["deletions"] == 1
    assert "@@" in preview["diff"]
    assert "+changed" in preview["diff"]
    assert "-two" in preview["diff"]


def test_change_preview_handles_new_and_unchanged_files(tmp_path):
    path = tmp_path / "new.py"
    missing = capture_text_snapshot(path)
    path.write_text("value = 1\n", encoding="utf-8")

    added = build_change_preview(tmp_path, path, missing)
    unchanged = build_change_preview(tmp_path, path, capture_text_snapshot(path))

    assert added["status"] == "added"
    assert added["additions"] == 1
    assert unchanged["status"] == "unchanged"
    assert unchanged["message"] == "No textual changes."


def test_change_preview_does_not_read_large_sources(tmp_path):
    path = Path(tmp_path) / "large.txt"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    snapshot = capture_text_snapshot(path)
    preview = build_change_preview(tmp_path, path, snapshot)

    assert snapshot.text is None
    assert "exceeds" in preview["message"]


def test_change_preview_detects_removed_final_newline(tmp_path):
    path = tmp_path / "newline.txt"
    path.write_text("value\n", encoding="utf-8")
    before = capture_text_snapshot(path)
    path.write_text("value", encoding="utf-8")

    preview = build_change_preview(tmp_path, path, before)

    assert preview["status"] == "modified"
    assert preview["additions"] == 1
    assert preview["deletions"] == 1


def test_change_preview_counts_source_lines_that_start_like_diff_headers(tmp_path):
    path = tmp_path / "operators.txt"
    path.write_text("old\n", encoding="utf-8")
    before = capture_text_snapshot(path)
    path.write_text("++value\n---value\n", encoding="utf-8")

    preview = build_change_preview(tmp_path, path, before)

    assert preview["additions"] == 2
    assert preview["deletions"] == 1
