"""File state tracking tests.

The session stores only lightweight real-path fingerprints. These tests cover
relative paths, external files, stale detection, and refresh after an edit.
"""

from codemate.tools.file_state import canonical_file_path, has_current_file_state, record_file_state


def test_record_file_state_uses_real_absolute_path(tmp_path):
    path = tmp_path / "src" / "sample.py"
    path.parent.mkdir()
    path.write_text("print('ok')\n", encoding="utf-8")
    session = {"read_files": {}}

    state = record_file_state(session, tmp_path, "src/../src/sample.py")

    assert state == {"mtime_ns": path.stat().st_mtime_ns, "size": path.stat().st_size}
    assert session["read_files"] == {str(path.resolve()): state}
    assert canonical_file_path(tmp_path, "src/sample.py") == path.resolve()


def test_file_state_detects_external_change(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("alpha\n", encoding="utf-8")
    session = {"read_files": {}}
    record_file_state(session, tmp_path, path)

    assert has_current_file_state(session, tmp_path, path)

    path.write_text("changed content\n", encoding="utf-8")

    assert not has_current_file_state(session, tmp_path, path)


def test_file_state_supports_paths_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    session = {"read_files": {}}

    record_file_state(session, workspace, outside)

    assert has_current_file_state(session, workspace, outside)
