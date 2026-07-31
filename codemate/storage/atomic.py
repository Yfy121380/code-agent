"""Crash-resistant helpers for replacing persisted text and JSON files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


class PersistenceError(RuntimeError):
    """A durable runtime artifact could not be read or written safely."""


def _target_mode(path):
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o644


def _fsync_directory(path):
    """Best-effort directory sync after replace so the rename reaches disk."""
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _replace_from_writer(path, writer):
    """Write a sibling temporary file and atomically replace the destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, _target_mode(path))
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path


def atomic_write_text(path, content):
    """Replace a UTF-8 text file without exposing a partially written target."""
    text = str(content)
    try:
        return _replace_from_writer(path, lambda handle: handle.write(text))
    except OSError as exc:
        raise PersistenceError(f"could not atomically write {Path(path)}: {exc}") from exc


def atomic_append_text(path, content):
    """Append text through an atomic whole-file replacement."""
    path = Path(path)
    text = str(content)

    def write(handle):
        if path.exists():
            with path.open("r", encoding="utf-8") as source:
                shutil.copyfileobj(source, handle)
        handle.write(text)

    try:
        return _replace_from_writer(path, write)
    except OSError as exc:
        raise PersistenceError(f"could not atomically append {path}: {exc}") from exc


def atomic_write_json(path, payload, *, sort_keys=False):
    """Serialize JSON into a temporary file and atomically replace the target."""
    def write(handle):
        json.dump(payload, handle, indent=2, sort_keys=sort_keys, ensure_ascii=False)
        handle.write("\n")

    try:
        return _replace_from_writer(path, write)
    except (OSError, TypeError, ValueError) as exc:
        raise PersistenceError(f"could not atomically write JSON {Path(path)}: {exc}") from exc
