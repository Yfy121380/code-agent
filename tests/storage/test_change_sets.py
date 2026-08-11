import subprocess

import pytest

import codemate.storage.change_sets as change_sets
from codemate.storage.change_sets import ChangeSetTracker, apply_change_set


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def repository(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def track(root, run_dir, paths, mutate):
    tracker = ChangeSetTracker(root, run_dir, "run-1", "turn-1").begin()
    for path in paths:
        assert tracker.track_path(root / path)
    mutate()
    return tracker.finish()


def plain_workspace(tmp_path, files):
    root = tmp_path / "workspace"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_undo_and_redo_restore_preexisting_dirty_baseline(tmp_path):
    root = repository(tmp_path, {"app.py": "committed\n"})
    path = root / "app.py"
    path.write_text("user change\n", encoding="utf-8")
    run_dir = tmp_path / "run"

    summary = track(
        root,
        run_dir,
        ["app.py"],
        lambda: path.write_text("agent change\n", encoding="utf-8"),
    )

    assert summary["state"] == "applied"
    assert summary["files"][0]["status"] == "modified"
    assert summary["files"][0]["additions"] == 1
    assert summary["files"][0]["deletions"] == 1
    assert apply_change_set(run_dir, root, "undo")["state"] == "reverted"
    assert path.read_text(encoding="utf-8") == "user change\n"
    assert apply_change_set(run_dir, root, "redo")["state"] == "applied"
    assert path.read_text(encoding="utf-8") == "agent change\n"


def test_whole_undo_rejects_all_files_when_one_changed_later(tmp_path):
    root = repository(tmp_path, {"a.py": "a0\n", "b.py": "b0\n"})
    run_dir = tmp_path / "run"

    def mutate():
        (root / "a.py").write_text("a1\n", encoding="utf-8")
        (root / "b.py").write_text("b1\n", encoding="utf-8")

    track(root, run_dir, ["a.py", "b.py"], mutate)
    (root / "b.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"conflict: b\.py"):
        apply_change_set(run_dir, root, "undo")
    assert (root / "a.py").read_text(encoding="utf-8") == "a1\n"
    assert (root / "b.py").read_text(encoding="utf-8") == "external\n"


def test_added_and_deleted_files_restore_as_one_change_set(tmp_path):
    root = repository(tmp_path, {"deleted.txt": "old\n"})
    run_dir = tmp_path / "run"

    def mutate():
        (root / "deleted.txt").unlink()
        (root / "added.txt").write_text("new\n", encoding="utf-8")

    summary = track(root, run_dir, ["deleted.txt", "added.txt"], mutate)
    assert {item["status"] for item in summary["files"]} == {"added", "deleted"}

    apply_change_set(run_dir, root, "undo")
    assert (root / "deleted.txt").read_text(encoding="utf-8") == "old\n"
    assert not (root / "added.txt").exists()

    apply_change_set(run_dir, root, "redo")
    assert not (root / "deleted.txt").exists()
    assert (root / "added.txt").read_text(encoding="utf-8") == "new\n"


def test_unchanged_run_has_no_change_set(tmp_path):
    root = repository(tmp_path, {"app.py": "same\n"})
    tracker = ChangeSetTracker(root, tmp_path / "run", "run-1", "turn-1").begin()

    assert tracker.finish() is None


def test_workspace_nested_inside_repository_uses_workspace_relative_paths(tmp_path):
    root = repository(tmp_path, {"package/app.py": "before\n", "outside.py": "same\n"})
    workspace = root / "package"
    run_dir = tmp_path / "run"
    path = workspace / "app.py"

    summary = track(
        workspace,
        run_dir,
        ["app.py"],
        lambda: path.write_text("after\n", encoding="utf-8"),
    )

    assert [item["path"] for item in summary["files"]] == ["app.py"]
    apply_change_set(run_dir, workspace, "undo")
    assert path.read_text(encoding="utf-8") == "before\n"


def test_non_git_workspace_tracks_modified_added_and_deleted_files(tmp_path):
    root = plain_workspace(
        tmp_path,
        {
            "modified.py": "before\n",
            "deleted.txt": "old\n",
            "unchanged.md": "same\n",
        },
    )
    run_dir = tmp_path / "run"

    def mutate():
        (root / "modified.py").write_text("after\n", encoding="utf-8")
        (root / "deleted.txt").unlink()
        (root / "nested").mkdir()
        (root / "nested" / "added.txt").write_text("new\n", encoding="utf-8")

    summary = track(
        root,
        run_dir,
        ["modified.py", "deleted.txt", "nested/added.txt"],
        mutate,
    )

    assert summary["state"] == "applied"
    assert {
        item["path"]: item["status"] for item in summary["files"]
    } == {
        "deleted.txt": "deleted",
        "modified.py": "modified",
        "nested/added.txt": "added",
    }

    assert apply_change_set(run_dir, root, "undo")["state"] == "reverted"
    assert (root / "modified.py").read_text(encoding="utf-8") == "before\n"
    assert (root / "deleted.txt").read_text(encoding="utf-8") == "old\n"
    assert not (root / "nested" / "added.txt").exists()

    assert apply_change_set(run_dir, root, "redo")["state"] == "applied"
    assert (root / "modified.py").read_text(encoding="utf-8") == "after\n"
    assert not (root / "deleted.txt").exists()
    assert (root / "nested" / "added.txt").read_text(encoding="utf-8") == "new\n"
    assert len(list((run_dir / "changes" / "snapshots").iterdir())) == 4


def test_non_git_workspace_rejects_undo_after_later_edit(tmp_path):
    root = plain_workspace(tmp_path, {"app.py": "before\n"})
    run_dir = tmp_path / "run"
    path = root / "app.py"

    track(
        root,
        run_dir,
        ["app.py"],
        lambda: path.write_text("agent\n", encoding="utf-8"),
    )
    path.write_text("user\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"conflict: app\.py"):
        apply_change_set(run_dir, root, "undo")
    assert path.read_text(encoding="utf-8") == "user\n"


def test_non_git_unchanged_workspace_has_no_change_set(tmp_path):
    root = plain_workspace(tmp_path, {"app.py": "same\n"})
    tracker = ChangeSetTracker(root, tmp_path / "run", "run-1", "turn-1").begin()

    assert tracker.available is True
    assert tracker.finish() is None
    assert not (tmp_path / "run" / "changes").exists()


def test_unregistered_shell_only_changes_are_not_tracked(tmp_path):
    root = plain_workspace(
        tmp_path,
        {
            "app.py": "same\n",
            "node_modules/package/index.js": "before\n",
            ".venv/lib/module.py": "before\n",
            "__pycache__/app.pyc": "before\n",
        },
    )
    run_dir = tmp_path / "run"

    def mutate():
        (root / "node_modules/package/index.js").write_text("after\n", encoding="utf-8")
        (root / ".venv/lib/module.py").write_text("after\n", encoding="utf-8")
        (root / "__pycache__/app.pyc").write_text("after\n", encoding="utf-8")

    tracker = ChangeSetTracker(root, run_dir, "run-1", "turn-1").begin()
    mutate()

    assert tracker.finish() is None


def test_repository_without_commits_uses_tool_tracking(tmp_path):
    root = plain_workspace(tmp_path, {"app.py": "before\n"})
    git(root, "init", "-q")
    run_dir = tmp_path / "run"
    tracker = ChangeSetTracker(root, run_dir, "run-1", "turn-1").begin()

    assert tracker.track_path(root / "app.py")
    (root / "app.py").write_text("after\n", encoding="utf-8")
    summary = tracker.finish()

    assert summary["state"] == "applied"
    assert summary["files"][0]["path"] == "app.py"


def test_large_tracked_file_is_reported_as_non_reversible(
    tmp_path,
    monkeypatch,
):
    root = plain_workspace(tmp_path, {"app.py": "before\n"})
    monkeypatch.setattr(change_sets, "MAX_CHANGE_SNAPSHOT_BYTES", 1)
    tracker = ChangeSetTracker(
        root,
        tmp_path / "run",
        "run-1",
        "turn-1",
    ).begin()

    assert tracker.track_path(root / "app.py")
    (root / "app.py").write_text("after content\n", encoding="utf-8")

    summary = tracker.finish()

    assert summary["state"] == "unavailable"
    assert summary["files"][0]["reversible"] is False


def test_final_line_stats_include_trailing_newline_changes(tmp_path):
    root = plain_workspace(tmp_path, {"app.py": "value\n"})
    path = root / "app.py"

    summary = track(
        root,
        tmp_path / "run",
        ["app.py"],
        lambda: path.write_text("value", encoding="utf-8"),
    )

    assert summary["files"][0]["additions"] == 1
    assert summary["files"][0]["deletions"] == 1
