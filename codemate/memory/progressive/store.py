"""Atomic stores for user-level Core memory and project-level Ordinary memory."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ...storage.atomic import atomic_write_json, atomic_write_text
from ...workspace import now


MEMORY_ID_RE = re.compile(r"^M0*([1-9][0-9]*)$")
CORE_KEY_RE = re.compile(
    r"^(identity|preference|safety|privacy)\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$"
)
MAX_MEMORY_TITLE_CHARS = 160
MAX_MEMORY_CONTENT_CHARS = 12_000
MAX_MEMORY_REASON_CHARS = 1_000
MAX_CORE_RENDERED_CHARS = 8_000
ORDINARY_INDEX_VERSION = 1
VISIBILITY_HALF_LIFE_DAYS = 30.0


@contextmanager
def _file_lock(path: Path, *, timeout: float = 5.0, stale_after: float = 1800.0):
    """Acquire a small cross-process lock without adding a platform dependency."""
    deadline = time.monotonic() + timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(now() + "\n")
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"memory store is busy: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _required_text(value, field, limit):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} must contain at most {limit} characters")
    return text


def _normalized_title(value):
    return " ".join(str(value).casefold().split())


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str
    last_accessed_at: str
    access_count: int
    revision: int
    last_update_reason: str

    def metadata(self):
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "revision": self.revision,
            "last_update_reason": self.last_update_reason,
        }

    def render(self):
        metadata = json.dumps(self.metadata(), ensure_ascii=False, sort_keys=True)
        return f"---\n{metadata}\n---\n\n{self.content.strip()}\n"

    @classmethod
    def parse(cls, text):
        parts = str(text).split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("invalid memory record front matter")
        metadata = json.loads(parts[1].strip())
        content = parts[2].strip()
        return cls(
            id=str(metadata["id"]),
            title=str(metadata["title"]),
            content=content,
            created_at=str(metadata["created_at"]),
            updated_at=str(metadata["updated_at"]),
            last_accessed_at=str(metadata["last_accessed_at"]),
            access_count=int(metadata.get("access_count", 0)),
            revision=int(metadata.get("revision", 1)),
            last_update_reason=str(metadata.get("last_update_reason", "")),
        )


class CoreMemoryStore:
    """Store explicit cross-project user facts under stable namespaced keys."""

    def __init__(self, path):
        self.path = Path(path)
        self._thread_lock = threading.RLock()
        self._lock_path = self.path.with_suffix(".lock")
        self.ensure()

    def ensure(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, _file_lock(self._lock_path):
            self._ensure_unlocked()

    def _ensure_unlocked(self):
        if not self.path.exists():
            atomic_write_json(self.path, {"version": 1, "entries": {}}, sort_keys=True)

    def _load_unlocked(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Core memory file: {self.path}") from exc
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            raise ValueError("Core memory entries must be an object")
        return {"version": 1, "entries": dict(entries)}

    def load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, _file_lock(self._lock_path):
            self._ensure_unlocked()
            return self._load_unlocked()

    @staticmethod
    def render_data(data):
        entries = dict((data or {}).get("entries", {}) or {})
        lines = [
            "# Core Memory",
            "",
            "以下为 Core Memory，是需要跨项目长期遵循的用户级记忆。",
        ]
        if not entries:
            return "\n".join([*lines, "", "- none"])
        for namespace in ("identity", "preference", "safety", "privacy"):
            items = [
                (key, str((entries[key] or {}).get("value", "")).strip())
                for key in sorted(entries)
                if key.startswith(namespace + ".")
            ]
            items = [(key, value) for key, value in items if value]
            if not items:
                continue
            lines.extend(["", f"## {namespace} 记忆"])
            lines.extend(f"- {key}: {value}" for key, value in items)
        return "\n".join(lines)

    def render(self):
        return self.render_data(self.load())

    def upsert(self, key, value, reason):
        key = _required_text(key, "key", 160)
        value = _required_text(value, "value", 2_000)
        reason = _required_text(reason, "reason", MAX_MEMORY_REASON_CHARS)
        if not CORE_KEY_RE.fullmatch(key):
            raise ValueError("invalid Core memory key namespace")
        with self._thread_lock, _file_lock(self._lock_path):
            self._ensure_unlocked()
            data = self._load_unlocked()
            entries = data["entries"]
            existing_keys = sorted(entries)
            entries[key] = {"value": value, "updated_at": now(), "reason": reason}
            rendered_chars = len(self.render_data(data))
            if rendered_chars > MAX_CORE_RENDERED_CHARS:
                excess_chars = rendered_chars - MAX_CORE_RENDERED_CHARS
                return {
                    "status": "capacity_exceeded",
                    "key": key,
                    "limit_chars": MAX_CORE_RENDERED_CHARS,
                    "attempted_chars": rendered_chars,
                    "excess_chars": excess_chars,
                    "suggested_max_value_chars": max(1, len(value) - excess_chars),
                    "entry_count": len(entries),
                    "existing_keys": existing_keys,
                }
            atomic_write_json(self.path, data, sort_keys=True)
            return {"status": "updated", "key": key, "entry_count": len(entries)}

    def remove(self, key, reason):
        key = _required_text(key, "key", 160)
        _required_text(reason, "reason", MAX_MEMORY_REASON_CHARS)
        if not CORE_KEY_RE.fullmatch(key):
            raise ValueError("invalid Core memory key namespace")
        with self._thread_lock, _file_lock(self._lock_path):
            self._ensure_unlocked()
            data = self._load_unlocked()
            if key not in data["entries"]:
                raise ValueError(f"Core memory key does not exist: {key}")
            del data["entries"][key]
            atomic_write_json(self.path, data, sort_keys=True)
            return {
                "status": "removed",
                "key": key,
                "entry_count": len(data["entries"]),
            }


class ProjectMemoryStore:
    """Manage equal Ordinary Memory records and a repairable metadata index."""

    def __init__(self, root):
        self.root = Path(root)
        self.records_dir = self.root / "ordinary"
        self.index_path = self.root / "INDEX.json"
        self._lock_path = self.root / ".memory.lock"
        self._thread_lock = threading.RLock()
        self.ensure()

    def ensure(self):
        self.records_dir.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, _file_lock(self._lock_path):
            self._rebuild_index_unlocked()

    def _path(self, memory_id):
        if not MEMORY_ID_RE.fullmatch(str(memory_id)):
            raise ValueError("memory_id must use the M<number> format")
        return self.records_dir / f"{memory_id}.md"

    def _load_path(self, path):
        record = MemoryRecord.parse(path.read_text(encoding="utf-8"))
        if path.stem != record.id:
            raise ValueError(f"memory record id does not match filename: {path}")
        return record

    def _list_records_unlocked(self):
        records = []
        for path in sorted(self.records_dir.glob("M*.md")):
            try:
                records.append(self._load_path(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return records

    def list_records(self):
        with self._thread_lock, _file_lock(self._lock_path):
            return self._list_records_unlocked()

    def _rebuild_index_unlocked(self):
        records = self._list_records_unlocked()
        payload = {
            "version": ORDINARY_INDEX_VERSION,
            "memories": {record.id: record.metadata() for record in records},
        }
        atomic_write_json(self.index_path, payload, sort_keys=True)
        return payload

    def rebuild_index(self):
        with self._thread_lock, _file_lock(self._lock_path):
            return self._rebuild_index_unlocked()

    def _load_index_unlocked(self):
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return self._rebuild_index_unlocked()
        memories = data.get("memories") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("version") != ORDINARY_INDEX_VERSION
            or not isinstance(memories, dict)
        ):
            return self._rebuild_index_unlocked()
        for memory_id, metadata in memories.items():
            if (
                not MEMORY_ID_RE.fullmatch(str(memory_id))
                or not isinstance(metadata, dict)
                or str(metadata.get("id", "")) != str(memory_id)
                or not str(metadata.get("title", "")).strip()
            ):
                return self._rebuild_index_unlocked()
        return data

    @staticmethod
    def visibility_score(metadata, current_time=None):
        """Rank frequently used recent records with a 30-day exponential half-life."""
        current_time = current_time or datetime.now().astimezone()
        current_timestamp = current_time.timestamp()
        activity_timestamp = max(
            _timestamp(metadata.get("last_accessed_at")),
            _timestamp(metadata.get("updated_at")),
        )
        inactive_days = max(0.0, (current_timestamp - activity_timestamp) / 86_400)
        access_count = max(0, int(metadata.get("access_count", 0)))
        return (1.0 + math.log1p(access_count)) * math.pow(
            2.0, -inactive_days / VISIBILITY_HALF_LIFE_DAYS
        )

    def index(self, *, query="", offset=0, limit=50, current_time=None):
        """Return a score-ordered, paginated title view without loading bodies."""
        query = str(query or "").strip().casefold()
        if isinstance(offset, bool) or isinstance(limit, bool):
            raise ValueError("offset and limit must be integers")
        offset = int(offset)
        limit = int(limit)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be >= 0 and limit must be in [1, 100]")
        with self._thread_lock, _file_lock(self._lock_path):
            data = self._load_index_unlocked()
            entries = list(data["memories"].values())
        if query:
            entries = [
                item for item in entries if query in str(item["title"]).casefold()
            ]
        entries.sort(
            key=lambda item: (
                -self.visibility_score(item, current_time=current_time),
                -_timestamp(item.get("updated_at")),
                int(MEMORY_ID_RE.fullmatch(str(item["id"])).group(1)),
            )
        )
        selected = entries[offset : offset + limit]
        return {
            "total": len(entries),
            "offset": offset,
            "limit": limit,
            "items": [{"id": item["id"], "title": item["title"]} for item in selected],
        }

    def prompt_index(self, limit=25):
        result = self.index(limit=limit)
        lines = [
            "# Ordinary Memory",
            "",
            (
                "以下为项目级 Ordinary Memory 索引，仅包含按近期使用情况选出的 "
                f"Top {int(limit)} ID 和标题。需要具体内容时请使用 memory_read；"
                "未展示的记忆可使用 memory_index 搜索。"
            ),
            "",
        ]
        lines.extend(f"- {item['id']} | {item['title']}" for item in result["items"])
        if not result["items"]:
            lines.append("- none")
        return "\n".join(lines), result

    def read(self, memory_id, *, track_access=True):
        path = self._path(memory_id)
        with self._thread_lock, _file_lock(self._lock_path):
            if not path.exists():
                raise ValueError(f"ordinary memory does not exist: {memory_id}")
            record = self._load_path(path)
            if track_access:
                record = replace(
                    record,
                    access_count=record.access_count + 1,
                    last_accessed_at=now(),
                )
                atomic_write_text(path, record.render())
                self._rebuild_index_unlocked()
            return record

    def _next_id_unlocked(self):
        numbers = []
        for path in self.records_dir.glob("M*.md"):
            match = MEMORY_ID_RE.fullmatch(path.stem)
            if match:
                numbers.append(int(match.group(1)))
        return f"M{max(numbers, default=0) + 1:03d}"

    def create(self, title, content, reason):
        """Create one uniquely titled Ordinary Memory and refresh the index."""
        title = _required_text(title, "title", MAX_MEMORY_TITLE_CHARS)
        content = _required_text(content, "content", MAX_MEMORY_CONTENT_CHARS)
        reason = _required_text(reason, "reason", MAX_MEMORY_REASON_CHARS)
        with self._thread_lock, _file_lock(self._lock_path):
            if any(
                _normalized_title(item.title) == _normalized_title(title)
                for item in self._list_records_unlocked()
            ):
                raise ValueError(
                    "a memory with the same normalized title already exists"
                )
            timestamp = now()
            record = MemoryRecord(
                id=self._next_id_unlocked(),
                title=title,
                content=content,
                created_at=timestamp,
                updated_at=timestamp,
                last_accessed_at=timestamp,
                access_count=0,
                revision=1,
                last_update_reason=reason,
            )
            atomic_write_text(self._path(record.id), record.render())
            self._rebuild_index_unlocked()
            return record

    def update(self, memory_id, title, content, reason, expected_revision):
        """Replace a record body under optimistic revision control."""
        title = _required_text(title, "title", MAX_MEMORY_TITLE_CHARS)
        content = _required_text(content, "content", MAX_MEMORY_CONTENT_CHARS)
        reason = _required_text(reason, "reason", MAX_MEMORY_REASON_CHARS)
        with self._thread_lock, _file_lock(self._lock_path):
            path = self._path(memory_id)
            if not path.exists():
                raise ValueError(f"ordinary memory does not exist: {memory_id}")
            current = self._load_path(path)
            if current.revision != int(expected_revision):
                raise ValueError(
                    f"memory revision conflict: expected {expected_revision}, current {current.revision}"
                )
            if any(
                item.id != current.id
                and _normalized_title(item.title) == _normalized_title(title)
                for item in self._list_records_unlocked()
            ):
                raise ValueError(
                    "another memory already uses the same normalized title"
                )
            updated = replace(
                current,
                title=title,
                content=content,
                updated_at=now(),
                revision=current.revision + 1,
                last_update_reason=reason,
            )
            atomic_write_text(path, updated.render())
            self._rebuild_index_unlocked()
            return updated
