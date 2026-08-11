"""由文件修改工具驱动的整轮 Diff、Undo 和 Redo 快照。"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .atomic import atomic_write_json


MAX_CHANGE_SNAPSHOT_BYTES = 10 * 1024 * 1024


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot_name(index, side):
    return f"snapshots/{side}-{index:04d}.bin"


def _safe_relative_path(raw):
    path = PurePosixPath(str(raw or ""))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"invalid change path: {raw}")
    return path.as_posix()


def _workspace_relative_path(root, path):
    """只跟踪工作区内文件，避免 Undo/Redo 修改工作区外状态。"""
    try:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    return relative.as_posix()


def _write_snapshot(changes_dir, relative_snapshot, data):
    path = changes_dir / relative_snapshot
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _capture_current(root, relative_path, changes_dir, relative_snapshot):
    """保存一侧文件内容；过大或非普通文件只记录不可逆指纹。"""
    path = root / relative_path
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": "", "mode": 0, "snapshot": ""}
    if path.is_symlink() or not path.is_file():
        metadata = path.lstat()
        return {
            "exists": True,
            "sha256": "",
            "mode": stat.S_IMODE(metadata.st_mode),
            "snapshot": "",
            "error": "only regular files can be restored",
            "fingerprint": f"{metadata.st_size}:{metadata.st_mtime_ns}",
        }
    metadata = path.stat()
    if metadata.st_size > MAX_CHANGE_SNAPSHOT_BYTES:
        return {
            "exists": True,
            "sha256": "",
            "mode": stat.S_IMODE(metadata.st_mode),
            "snapshot": "",
            "error": f"file exceeds {MAX_CHANGE_SNAPSHOT_BYTES} bytes",
            "fingerprint": f"{metadata.st_size}:{metadata.st_mtime_ns}",
        }
    data = path.read_bytes()
    _write_snapshot(changes_dir, relative_snapshot, data)
    return {
        "exists": True,
        "sha256": _sha256(data),
        "mode": stat.S_IMODE(metadata.st_mode),
        "snapshot": relative_snapshot,
    }


def _inspect_current(root, relative_path):
    """读取当前文件指纹，不创建新的可恢复快照。"""
    path = root / relative_path
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "sha256": "", "mode": 0}
    if path.is_symlink() or not path.is_file():
        metadata = path.lstat()
        return {
            "exists": True,
            "sha256": "",
            "mode": stat.S_IMODE(metadata.st_mode),
            "error": "only regular files can be restored",
            "fingerprint": f"{metadata.st_size}:{metadata.st_mtime_ns}",
        }
    metadata = path.stat()
    if metadata.st_size > MAX_CHANGE_SNAPSHOT_BYTES:
        return {
            "exists": True,
            "sha256": "",
            "mode": stat.S_IMODE(metadata.st_mode),
            "error": f"file exceeds {MAX_CHANGE_SNAPSHOT_BYTES} bytes",
            "fingerprint": f"{metadata.st_size}:{metadata.st_mtime_ns}",
        }
    return {
        "exists": True,
        "sha256": _sha256(path.read_bytes()),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _same_snapshot(left, right):
    if left.get("error") or right.get("error"):
        return (
            str(left.get("error") or "") == str(right.get("error") or "")
            and str(left.get("fingerprint") or "")
            == str(right.get("fingerprint") or "")
            and int(left.get("mode") or 0) == int(right.get("mode") or 0)
        )
    return (
        bool(left.get("exists")) == bool(right.get("exists"))
        and str(left.get("sha256") or "") == str(right.get("sha256") or "")
        and int(left.get("mode") or 0) == int(right.get("mode") or 0)
    )


def _change_status(before, after):
    if not before.get("exists") and after.get("exists"):
        return "added"
    if before.get("exists") and not after.get("exists"):
        return "deleted"
    return "modified"


def _snapshot_text(changes_dir, snapshot):
    """读取小型 UTF-8 快照；二进制和不可恢复文件不生成行统计。"""
    if not snapshot.get("exists"):
        return ""
    relative = str(snapshot.get("snapshot") or "")
    if not relative:
        return None
    try:
        data = (changes_dir / relative).read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        return None


def _line_change_stats(changes_dir, before, after):
    before_text = _snapshot_text(changes_dir, before)
    after_text = _snapshot_text(changes_dir, after)
    if before_text is None or after_text is None:
        return {"additions": 0, "deletions": 0}
    additions = 0
    deletions = 0
    for line in difflib.ndiff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
    ):
        if line.startswith("+ "):
            additions += 1
        elif line.startswith("- "):
            deletions += 1
    return {"additions": additions, "deletions": deletions}


class ChangeSetTracker:
    """记录 write/patch 首次修改前状态，并在整轮结束时保存最终状态。"""

    def __init__(self, workspace_root, run_dir, run_id, conversation_id):
        self.root = Path(workspace_root).resolve()
        self.run_dir = Path(run_dir)
        self.changes_dir = self.run_dir / "changes"
        self.run_id = str(run_id)
        self.conversation_id = str(conversation_id)
        self.initial = {}
        self.available = False

    def begin(self):
        # 请求开始时不扫描工作区；目录也延迟到第一次实际修改时创建。
        self.available = True
        return self

    def track_path(self, path):
        """在某路径第一次被修改前保存 before；重复修改不会覆盖基线。"""
        if not self.available:
            return False
        relative_path = _workspace_relative_path(self.root, path)
        if relative_path is None:
            return False
        if relative_path in self.initial:
            return True
        self.changes_dir.mkdir(parents=True, exist_ok=True)
        index = len(self.initial)
        self.initial[relative_path] = _capture_current(
            self.root,
            relative_path,
            self.changes_dir,
            _snapshot_name(index, "before"),
        )
        return True

    def finish(self):
        """只比较登记路径的首次 before 和本轮结束时的最终 after。"""
        if not self.available or not self.initial:
            self._discard_snapshots()
            return None
        files = []
        for index, relative_path in enumerate(sorted(self.initial)):
            before = dict(self.initial[relative_path])
            after = _capture_current(
                self.root,
                relative_path,
                self.changes_dir,
                _snapshot_name(index, "after"),
            )
            if _same_snapshot(before, after):
                continue
            files.append(
                {
                    "path": relative_path,
                    "status": _change_status(before, after),
                    "reversible": not before.get("error") and not after.get("error"),
                    "before": before,
                    "after": after,
                }
            )
        if not files:
            self._discard_snapshots()
            return None
        manifest = {
            "id": self.run_id,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "workspace_root": str(self.root),
            "tracking_mode": "tool",
            "files": files,
        }
        self._save_manifest(manifest)
        return materialize_change_set(self.run_dir, manifest, self.root)

    def _save_manifest(self, manifest):
        atomic_write_json(
            self.changes_dir / "manifest.json",
            manifest,
            sort_keys=True,
        )
        referenced = {
            str(snapshot.get("snapshot") or "")
            for item in manifest.get("files") or []
            for snapshot in (item.get("before") or {}, item.get("after") or {})
            if snapshot.get("snapshot")
        }
        snapshots_dir = self.changes_dir / "snapshots"
        if snapshots_dir.is_dir():
            for path in snapshots_dir.iterdir():
                relative = path.relative_to(self.changes_dir).as_posix()
                if path.is_file() and relative not in referenced:
                    path.unlink()

    def _discard_snapshots(self):
        shutil.rmtree(self.changes_dir, ignore_errors=True)


def _load_manifest(run_dir):
    path = Path(run_dir) / "changes" / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _current_matches(root, file_entry, side):
    expected = dict(file_entry.get(side) or {})
    current = _inspect_current(root, file_entry["path"])
    return _same_snapshot(current, expected)


def _change_set_state(root, manifest):
    files = list(manifest.get("files") or [])
    if manifest.get("error") or any(not item.get("reversible") for item in files):
        return "unavailable"
    if files and all(_current_matches(root, item, "after") for item in files):
        return "applied"
    if files and all(_current_matches(root, item, "before") for item in files):
        return "reverted"
    return "conflict"


def _conflict_paths(root, manifest):
    files = list(manifest.get("files") or [])
    changed = [
        item["path"]
        for item in files
        if not _current_matches(root, item, "before")
        and not _current_matches(root, item, "after")
    ]
    return changed or [item["path"] for item in files]


def materialize_change_set(run_dir, manifest, workspace_root):
    root = Path(workspace_root).resolve()
    changes_dir = Path(run_dir) / "changes"
    files = []
    for item in manifest.get("files") or []:
        before = dict(item.get("before") or {})
        after = dict(item.get("after") or {})
        stats = _line_change_stats(changes_dir, before, after)
        files.append(
            {
                "path": item["path"],
                "status": item["status"],
                "reversible": bool(item.get("reversible")),
                **stats,
                "before_snapshot": (
                    str(changes_dir / before["snapshot"])
                    if before.get("snapshot")
                    else ""
                ),
                "after_snapshot": (
                    str(changes_dir / after["snapshot"])
                    if after.get("snapshot")
                    else ""
                ),
            }
        )
    state = _change_set_state(root, manifest)
    message = str(manifest.get("error") or "")
    if state == "conflict" and not message:
        message = "Files changed after this run: " + ", ".join(
            _conflict_paths(root, manifest)
        )
    return {
        "id": str(manifest.get("id") or ""),
        "run_id": str(manifest.get("run_id") or ""),
        "conversation_id": str(manifest.get("conversation_id") or ""),
        "state": state,
        "message": message,
        "files": files,
    }


def load_change_set(run_dir, workspace_root):
    manifest = _load_manifest(run_dir)
    if manifest is None:
        return None
    return materialize_change_set(run_dir, manifest, workspace_root)


def _atomic_write_bytes(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, int(mode or 0o644))
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _restore_side(root, changes_dir, file_entry, side):
    relative_path = _safe_relative_path(file_entry["path"])
    path = (root / relative_path).resolve()
    path.relative_to(root)
    snapshot = dict(file_entry.get(side) or {})
    if not snapshot.get("exists"):
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError(f"cannot replace directory: {relative_path}")
            path.unlink()
        return
    snapshot_path = changes_dir / str(snapshot.get("snapshot") or "")
    if not snapshot_path.is_file():
        raise RuntimeError(f"missing change snapshot for {relative_path}")
    _atomic_write_bytes(path, snapshot_path.read_bytes(), snapshot.get("mode"))


def apply_change_set(run_dir, workspace_root, action):
    """全部文件预检通过后，原子执行整轮 Undo 或 Redo。"""
    manifest = _load_manifest(run_dir)
    if manifest is None:
        raise ValueError("change set not found")
    root = Path(workspace_root).resolve()
    state = _change_set_state(root, manifest)
    if action == "undo":
        expected_state, source, target = "applied", "after", "before"
    elif action == "redo":
        expected_state, source, target = "reverted", "before", "after"
    else:
        raise ValueError("change action must be undo or redo")
    if state != expected_state:
        detail = ""
        if state == "conflict":
            detail = ": " + ", ".join(_conflict_paths(root, manifest))
        raise ValueError(
            f"cannot {action} change set because workspace files are {state}{detail}"
        )

    files = list(manifest.get("files") or [])
    changes_dir = Path(run_dir) / "changes"
    completed = []
    try:
        for item in files:
            _restore_side(root, changes_dir, item, target)
            completed.append(item)
    except Exception as exc:
        rollback_errors = []
        for item in reversed(completed):
            try:
                _restore_side(root, changes_dir, item, source)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = f"; rollback errors: {rollback_errors}" if rollback_errors else ""
        raise RuntimeError(f"could not {action} change set: {exc}{detail}") from exc
    return materialize_change_set(run_dir, manifest, root)
