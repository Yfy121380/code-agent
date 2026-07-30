"""Session-scoped file version tracking for safe edits.

The runtime records the version of files observed through ``read_file``. Editing
an existing file is allowed only while that version still matches the file on
disk, which prevents stale or unread content from being overwritten.
"""

from pathlib import Path


def canonical_file_path(workspace_root, raw_path):
    """Return a stable real absolute path without applying access policy."""
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = Path(workspace_root) / path
    return path.resolve(strict=False)


def file_state(path):
    """Return the lightweight file fingerprint persisted in the session."""
    path = Path(path)
    if not path.is_file():
        return None
    stat = path.stat()
    return {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def record_file_state(session, workspace_root, raw_path):
    """Record the current version after a successful read or runtime edit."""
    path = canonical_file_path(workspace_root, raw_path)
    state = file_state(path)
    read_files = session.setdefault("read_files", {})
    if state is None:
        read_files.pop(str(path), None)
        return None
    read_files[str(path)] = state
    return state


def has_current_file_state(session, workspace_root, raw_path):
    """Return whether the file still matches the last version seen by the agent."""
    path = canonical_file_path(workspace_root, raw_path)
    expected = session.setdefault("read_files", {}).get(str(path))
    return expected is not None and expected == file_state(path)
