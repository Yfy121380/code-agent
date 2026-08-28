"""Durable storage and ownership checks for generated Skills.

The active Skill files stay in CodeMate's existing user/project roots. This
store owns only evolution metadata, snapshots, provenance, usage statistics,
and the registry that proves which files the automatic maintainer may update.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..storage.atomic import atomic_append_text, atomic_write_json, atomic_write_text
from ..workspace import now


USAGE_LOG = "usage.jsonl"
ONLINE_PROVENANCE_LOG = "online_provenance.jsonl"
ONLINE_PROVENANCE_INDEX = "online_skill_provenance.json"
SKILL_USAGE_STATS = "skill_usage_stats.json"
MANAGED_SKILLS = "managed_skills.json"
HISTORY_DIR = "history"
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@contextmanager
def _file_lock(path, *, timeout=5.0, stale_after=1800.0):
    """Serialize evolution writes across background workers and processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + float(timeout)
    while True:
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(now() + "\n")
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > float(stale_after):
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"skill evolution store is busy: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _read_json(path, default):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_slug(value):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return text.strip("-.")[:120] or "skill"


def _preview(value, limit=1200):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def parse_skill_document(text):
    """Parse CodeMate's deliberately small YAML-like SKILL.md front matter."""
    raw = str(text or "")
    metadata = {}
    body = raw
    if raw.startswith("---"):
        lines = raw.splitlines()
        end = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end = index
                break
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip("\"'")
        if end is not None:
            body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return metadata, body


def format_skill_document(metadata, body):
    lines = ["---"]
    for key, value in metadata.items():
        normalized = re.sub(r"\s+", " ", str(value or "").strip())
        if normalized:
            lines.append(f"{key}: {normalized}")
    lines.extend(["---", "", str(body or "").rstrip(), ""])
    return "\n".join(lines)


class SkillEvolutionStore:
    """Persist Skill lineage while enforcing automatic-write ownership."""

    def __init__(self, root, project_skills, user_skills, *, prune_min_retrieved=40, prune_max_used=0):
        self.root = Path(root)
        self.project_skills = Path(project_skills)
        self.user_skills = Path(user_skills)
        self.prune_min_retrieved = int(prune_min_retrieved)
        self.prune_max_used = int(prune_max_used)
        self._thread_lock = threading.RLock()
        self._lock_path = self.root / ".lock"
        self.root.mkdir(parents=True, exist_ok=True)

    def _json_path(self, name):
        return self.root / name

    def _append_event(self, filename, row):
        atomic_append_text(
            self._json_path(filename),
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _load_managed(self):
        value = _read_json(self._json_path(MANAGED_SKILLS), {})
        return value if isinstance(value, dict) else {}

    def _save_managed(self, value):
        atomic_write_json(self._json_path(MANAGED_SKILLS), value, sort_keys=True)

    def _skill_root(self, target):
        target = str(target or "project").strip().lower()
        if target not in {"project", "user"}:
            raise ValueError("target must be project or user")
        return self.user_skills if target == "user" else self.project_skills

    def skill_path_for_create(self, name, target="project"):
        name = self.validate_name(name)
        return self._skill_root(target) / name / "SKILL.md"

    @staticmethod
    def validate_name(name):
        name = str(name or "").strip()
        if not SKILL_NAME_RE.fullmatch(name):
            raise ValueError("skill name must be a simple directory name")
        return name

    def managed_path(self, name, *, require_current=True):
        """Return a managed path, detaching it if the user changed the file."""
        name = self.validate_name(name)
        with self._thread_lock, _file_lock(self._lock_path):
            managed = self._load_managed()
            item = managed.get(name)
            if not isinstance(item, dict) or item.get("status", "managed") != "managed":
                return None
            path = Path(str(item.get("path") or ""))
            allowed_paths = {
                (self.project_skills / name / "SKILL.md").resolve(),
                (self.user_skills / name / "SKILL.md").resolve(),
            }
            if path.resolve() not in allowed_paths:
                item["status"] = "invalid_path"
                item["detached_at"] = now()
                self._save_managed(managed)
                return None
            if not path.is_file():
                item["status"] = "missing"
                item["detached_at"] = now()
                self._save_managed(managed)
                return None
            if require_current and _sha256(path) != str(item.get("last_managed_hash") or ""):
                item["status"] = "user_modified"
                item["detached_at"] = now()
                self._save_managed(managed)
                return None
            return path

    def is_managed(self, name, path=None):
        managed_path = self.managed_path(name)
        if managed_path is None:
            return False
        return path is None or managed_path.resolve() == Path(path).resolve()

    def create_skill(
        self,
        *,
        name,
        description,
        instructions,
        when_to_use="",
        target="project",
        context="inline",
        user_invocable=False,
        allowed_tools=None,
        evidence="",
        actor="agent",
        tags=None,
    ):
        """Create and register a new managed Skill atomically at file level."""
        name = self.validate_name(name)
        description = str(description or "").strip()
        instructions = str(instructions or "").strip()
        if not description:
            raise ValueError("description must not be empty")
        if not instructions:
            raise ValueError("instructions must not be empty")
        path = self.skill_path_for_create(name, target)
        with self._thread_lock, _file_lock(self._lock_path):
            if path.exists() or any(root.joinpath(name, "SKILL.md").exists() for root in (self.project_skills, self.user_skills)):
                raise ValueError(f"skill already exists: {name}")
            metadata = {
                "name": name,
                "description": description,
                "version": "0.1.0",
                "created-at": now(),
                "user-invocable": "true" if user_invocable else "false",
                "context": "fork" if str(context).lower() == "fork" else "inline",
            }
            if when_to_use:
                metadata["when-to-use"] = when_to_use
            if tags:
                metadata["tags"] = ",".join(str(item).strip() for item in tags if str(item).strip())
            if allowed_tools:
                values = allowed_tools if isinstance(allowed_tools, list) else str(allowed_tools).split(",")
                metadata["allowed-tools"] = ",".join(str(item).strip() for item in values if str(item).strip())
            body = instructions
            if not body.lstrip().startswith("#"):
                body = "# Skill Instructions\n\n" + body
            if evidence:
                body = body.rstrip() + "\n\n## Creation Evidence\n\n" + str(evidence).strip()
            path.parent.mkdir(parents=True, exist_ok=False)
            try:
                atomic_write_text(path, format_skill_document(metadata, body))
                managed = self._load_managed()
                managed[name] = {
                    "path": str(path.resolve()),
                    "scope": "user" if str(target).lower() == "user" else "project",
                    "revision": 1,
                    "status": "managed",
                    "created_at": now(),
                    "last_managed_hash": _sha256(path),
                }
                self._save_managed(managed)
            except Exception:
                shutil.rmtree(path.parent, ignore_errors=True)
                raise
            event = {
                "event": "create",
                "time": now(),
                "actor": actor,
                "skill": name,
                "file": str(path),
                "target": managed[name]["scope"],
                "version": "0.1.0",
            }
            try:
                self._append_event(USAGE_LOG, event)
            except Exception:
                pass
            return {"ok": True, **event}

    @staticmethod
    def _next_version(value):
        parts = str(value or "0.1.0").split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return "0.1.1"
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"

    def evolve_skill(
        self,
        *,
        name,
        lesson,
        rationale="",
        instructions="",
        description="",
        when_to_use="",
        tags=None,
        target="active",
        actor="agent",
    ):
        """Replace a managed Skill body after writing a version snapshot."""
        name = self.validate_name(name)
        path = self.managed_path(name)
        if path is None:
            raise ValueError(f"skill is not managed by CodeMate or was modified externally: {name}")
        with self._thread_lock, _file_lock(self._lock_path):
            managed = self._load_managed()
            item = managed.get(name, {})
            requested_target = str(target or "active").strip().lower()
            if requested_target not in {"active", "project", "user"}:
                raise ValueError("target must be active, project, or user")
            if requested_target != "active" and requested_target != item.get("scope"):
                raise ValueError(
                    f"managed skill {name} is stored in the {item.get('scope', 'unknown')} scope"
                )
            if _sha256(path) != str(item.get("last_managed_hash") or ""):
                item["status"] = "user_modified"
                item["detached_at"] = now()
                self._save_managed(managed)
                raise ValueError(f"managed skill changed outside the evolution runtime: {name}")
            raw = path.read_text(encoding="utf-8")
            metadata, old_body = parse_skill_document(raw)
            snapshot = {
                "event": "snapshot",
                "time": now(),
                "actor": actor,
                "skill": name,
                "version": metadata.get("version", "0.1.0"),
                "content": raw,
                "lesson": _preview(lesson),
                "rationale": _preview(rationale),
            }
            self._append_event(f"{HISTORY_DIR}/{_safe_slug(name)}.jsonl", snapshot)
            metadata["name"] = name
            metadata["version"] = self._next_version(metadata.get("version"))
            metadata["last-evolved"] = now()
            metadata["evolution-count"] = str(int(metadata.get("evolution-count", "0") or 0) + 1)
            if description:
                metadata["description"] = description
            if when_to_use:
                metadata["when-to-use"] = when_to_use
            if tags:
                old_tags = [part.strip() for part in str(metadata.get("tags", "")).split(",") if part.strip()]
                metadata["tags"] = ",".join(dict.fromkeys([*old_tags, *(str(tag).strip() for tag in tags if str(tag).strip())]))
            body = str(instructions or "").strip()
            if not body:
                body = old_body.rstrip()
                note = f"- {now()[:10]}: {str(lesson or '').strip()}"
                if rationale:
                    note += f" Reason: {str(rationale).strip()}"
                marker = "## Evolution Notes"
                body = body + ("\n" if marker in body else f"\n\n{marker}\n") + "\n" + note
            elif not body.lstrip().startswith("#"):
                body = "# Skill Instructions\n\n" + body
            atomic_write_text(path, format_skill_document(metadata, body))
            try:
                item["revision"] = int(item.get("revision", 1)) + 1
                item["last_managed_hash"] = _sha256(path)
                item["updated_at"] = now()
                managed[name] = item
                self._save_managed(managed)
            except Exception:
                atomic_write_text(path, raw)
                raise
            event = {
                "event": "evolve",
                "time": now(),
                "actor": actor,
                "skill": name,
                "file": str(path),
                "version": metadata["version"],
                "revision": item["revision"],
            }
            try:
                self._append_event(USAGE_LOG, event)
            except Exception:
                pass
            return {"ok": True, **event}

    def record_invocation(self, skill):
        self.record_event(
            {
                "event": "invoke",
                "time": now(),
                "skill": str(skill.get("name") or ""),
                "source": str(skill.get("scope") or ""),
                "context": "inline",
            }
        )

    def record_feedback(self, name, rating, note=""):
        rating = str(rating or "").strip()
        if not rating:
            raise ValueError("rating must not be empty")
        self.record_event(
            {
                "event": "feedback",
                "time": now(),
                "skill": self.validate_name(name),
                "rating": rating,
                "note": _preview(note),
            }
        )

    def record_event(self, row):
        with self._thread_lock, _file_lock(self._lock_path):
            self._append_event(USAGE_LOG, row)

    def record_provenance(
        self,
        *,
        action,
        result=None,
        messages=None,
        window=None,
        loaded_skill_references=None,
        decision=None,
        error="",
    ):
        result = dict(result or {})
        skill = str(result.get("skill") or result.get("candidate", {}).get("name") or "")
        compact_messages = []
        for item in list(messages or [])[-12:]:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            compact_messages.append(
                {"role": item["role"], "content": _preview(item.get("content"), 4000)}
            )
        window = dict(window or {})
        focus = dict(window.get("focus_conversation") or {})
        supporting = [
            str(item.get("id") or "")
            for item in window.get("supporting_conversations") or []
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
        row = {
            "event": "online_ingest",
            "time": now(),
            "action": str(action or "none"),
            "skill": skill,
            "ok": bool(result.get("ok", not error)),
            "result": result,
            "messages": compact_messages,
            "focus_conversation_id": str(focus.get("id") or ""),
            "supporting_conversation_ids": supporting,
            "next_user_feedback": _preview(
                window.get("next_user_feedback"), 4000
            ),
            "source_char_count": int(window.get("source_char_count") or 0),
            "loaded_skill_references": list(loaded_skill_references or []),
            "decision": decision or {},
            "error": _preview(error),
        }
        with self._thread_lock, _file_lock(self._lock_path):
            self._append_event(ONLINE_PROVENANCE_LOG, row)
            if skill:
                index = _read_json(self._json_path(ONLINE_PROVENANCE_INDEX), {})
                item = index.setdefault(skill, {"skill": skill, "source_count": 0, "sources": [], "version_timeline": []})
                item["source_count"] = int(item.get("source_count", 0)) + 1
                item["last_action"] = row["action"]
                item["last_time"] = row["time"]
                item["last_ok"] = row["ok"]
                item["sources"] = [*list(item.get("sources") or []), row][-20:]
                if result.get("version"):
                    item["current_version"] = result["version"]
                    item["version_timeline"] = [
                        *list(item.get("version_timeline") or []),
                        {"time": row["time"], "version": result["version"], "action": row["action"]},
                    ][-50:]
                index[skill] = item
                atomic_write_json(self._json_path(ONLINE_PROVENANCE_INDEX), index, sort_keys=True)

    def record_usage(self, judgments, *, confirm_prune=None):
        """Update usage counters and prune only after an explicit write-policy check."""
        with self._thread_lock, _file_lock(self._lock_path):
            stats = _read_json(self._json_path(SKILL_USAGE_STATS), {})
            pruned = []
            for judgment in judgments:
                name = str(judgment.get("name") or "").strip()
                if not name:
                    continue
                item = stats.setdefault(name, {"retrieved": 0, "relevant": 0, "used": 0})
                item["retrieved"] = int(item.get("retrieved", 0)) + 1
                item["last_retrieved"] = now()
                item["source"] = judgment.get("source", item.get("source", ""))
                item["skill_dir"] = judgment.get("skill_dir", item.get("skill_dir", ""))
                if judgment.get("relevant"):
                    item["relevant"] = int(item.get("relevant", 0)) + 1
                if judgment.get("used"):
                    item["used"] = int(item.get("used", 0)) + 1
                    item["last_used"] = now()
                item["last_reason"] = _preview(judgment.get("reason"), 500)
                item["last_score"] = judgment.get("score", 0)
                if self._prune_if_unused(name, item, confirm_prune=confirm_prune):
                    pruned.append(name)
            atomic_write_json(self._json_path(SKILL_USAGE_STATS), stats, sort_keys=True)
            return {"ok": True, "judgments": len(judgments), "pruned": pruned}

    def _prune_if_unused(self, name, stats, *, confirm_prune):
        if int(stats.get("retrieved", 0)) < self.prune_min_retrieved:
            return False
        if int(stats.get("used", 0)) > self.prune_max_used:
            return False
        managed = self._load_managed()
        item = managed.get(name)
        if not isinstance(item, dict) or item.get("status", "managed") != "managed":
            return False
        path = Path(str(item.get("path") or ""))
        allowed_paths = {
            (self.project_skills / name / "SKILL.md").resolve(),
            (self.user_skills / name / "SKILL.md").resolve(),
        }
        if path.resolve() not in allowed_paths:
            item["status"] = "invalid_path"
            item["detached_at"] = now()
            self._save_managed(managed)
            return False
        if not path.is_file() or _sha256(path) != str(item.get("last_managed_hash") or ""):
            return False
        if confirm_prune is None or not confirm_prune("prune", name, path):
            return False
        destination = self.root / "pruned" / f"{_safe_slug(name)}-{int(time.time())}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path.parent), str(destination))
        try:
            item["status"] = "pruned"
            item["pruned_at"] = now()
            item["pruned_to"] = str(destination)
            managed[name] = item
            self._save_managed(managed)
        except Exception:
            path.parent.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(path.parent))
            raise
        stats["pruned"] = True
        stats["pruned_to"] = str(destination)
        try:
            self._append_event(
                USAGE_LOG,
                {"event": "prune", "time": now(), "skill": name, "to": str(destination)},
            )
        except Exception:
            pass
        return True

    def stats(self):
        stats = _read_json(self._json_path(SKILL_USAGE_STATS), {})
        events = {}
        path = self._json_path(USAGE_LOG)
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = str(row.get("skill") or "")
                if not name:
                    continue
                item = events.setdefault(name, {"created": 0, "invoked": 0, "feedback": 0, "evolved": 0})
                event = str(row.get("event") or "")
                if event in item:
                    item[event] += 1
                elif event == "create":
                    item["created"] += 1
                elif event == "invoke":
                    item["invoked"] += 1
                elif event == "evolve":
                    item["evolved"] += 1
        for name, usage in stats.items():
            events.setdefault(name, {}).update(usage if isinstance(usage, dict) else {})
        return events

    def format_stats(self):
        stats = self.stats()
        if not stats:
            return "No skill evolution events recorded yet."
        lines = ["Skill evolution stats:"]
        for name in sorted(stats):
            item = stats[name]
            fields = (
                "created",
                "invoked",
                "feedback",
                "evolved",
                "retrieved",
                "relevant",
                "used",
            )
            values = ", ".join(f"{field}={item.get(field, 0)}" for field in fields)
            if item.get("pruned"):
                values += ", pruned=true"
            lines.append(f"  {name}: {values}")
        return "\n".join(lines)
