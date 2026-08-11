# 会话存储：负责把每个 session 的状态和运行目录组织在同一个本地文件夹中。
# 每个 session 拥有独立目录，目录下保存 session.json 和该会话产生的 runs。
# 这样排查历史时可以直接从一次会话进入对应的运行工件，而不是在全局 runs 中查找。

import copy
import json
from pathlib import Path
import re
import threading
import warnings
from datetime import datetime

from .atomic import PersistenceError, atomic_write_json, atomic_write_text


def _title_slug(text):
    value = str(text or "").strip().lower()
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", value)
    value = value.strip("-")
    return value[:80]


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def session_dir(self, session_id):
        return self.root / str(session_id)

    def path(self, session_id):
        return self.session_dir(session_id) / "session.json"

    def backup_path(self, session_id):
        return self.session_dir(session_id) / "session.json.bak"

    def request_checkpoint_path(self, session_id):
        return self.session_dir(session_id) / "request_checkpoint.json"

    def transcript_path(self, session_id):
        return self.session_dir(session_id) / "transcript.jsonl"

    def runs_dir(self, session_id):
        return self.session_dir(session_id) / "runs"

    def media_dir(self, session_id):
        # 多模态工具结果缓存放在 session 目录内，只在请求模型时再读取为 base64。
        # 这样 session.json 不会保存大块图片数据，恢复会话时引用仍然稳定。
        return self.session_dir(session_id) / "media"

    def save(self, session):
        path = self.path(session["id"])
        backup_path = self.backup_path(session["id"])
        try:
            with self._write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_file():
                    previous_text = path.read_text(encoding="utf-8")
                    try:
                        json.loads(previous_text)
                    except json.JSONDecodeError:
                        pass
                    else:
                        atomic_write_text(backup_path, previous_text)
                atomic_write_json(path, session)
        except PersistenceError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not save session {session.get('id', '')!r}: {exc}") from exc
        return path

    def load(self, session_id):
        path = self.path(session_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as primary_error:
            backup_path = self.backup_path(session_id)
            try:
                recovered = json.loads(backup_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise primary_error
            warnings.warn(
                f"Recovered session {session_id!r} from {backup_path.name} because session.json was unreadable.",
                RuntimeWarning,
                stacklevel=2,
            )
            return recovered

    def save_request_checkpoint(self, session, user_request, editor_context=""):
        """Persist the state immediately before one user request starts.

        The checkpoint lives beside ``session.json`` instead of inside it so a
        snapshot can never recursively contain an older snapshot. Only the most
        recent request checkpoint is retained for each session.
        """
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            raise PersistenceError("cannot checkpoint a session without an id")
        payload = {
            "version": 1,
            "user_request": str(user_request or ""),
            "editor_context": str(editor_context or ""),
            "transcript_size": self.transcript_size(session_id),
            "session": copy.deepcopy(session),
        }
        path = self.request_checkpoint_path(session_id)
        try:
            with self._write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, payload)
        except PersistenceError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(
                f"could not save request checkpoint for {session_id!r}: {exc}"
            ) from exc
        return path

    def load_request_checkpoint(self, session_id):
        """Load and validate the latest pre-request checkpoint for a session."""
        path = self.request_checkpoint_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                f"could not load request checkpoint for {session_id!r}: {exc}"
            ) from exc
        snapshot = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(snapshot, dict) or str(snapshot.get("id") or "") != str(
            session_id
        ):
            raise PersistenceError(f"invalid request checkpoint for {session_id!r}")
        return payload

    def request_checkpoint_info(self, session_id):
        """Return lightweight retry metadata without exposing the snapshot."""
        payload = self.load_request_checkpoint(session_id)
        if payload is None:
            return None
        return {"user_request": str(payload.get("user_request") or "")}

    def clear_request_checkpoint(self, session_id):
        """Remove retry state when the conversation is explicitly reset."""
        try:
            with self._write_lock:
                self.request_checkpoint_path(session_id).unlink(missing_ok=True)
        except OSError as exc:
            raise PersistenceError(
                f"could not clear request checkpoint for {session_id!r}: {exc}"
            ) from exc

    def append_transcript(self, session_id, item):
        """Append one UI-visible history record without coupling it to compact."""
        path = self.transcript_path(session_id)
        try:
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
            return path
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(
                f"could not append transcript for {session_id!r}: {exc}"
            ) from exc

    def load_transcript(self, session_id):
        """Load the append-only UI transcript, tolerating a partial final line."""
        path = self.transcript_path(session_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise PersistenceError(
                f"could not load transcript for {session_id!r}: {exc}"
            ) from exc
        records = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def transcript_size(self, session_id):
        """Return a stable byte checkpoint used to roll back the latest request."""
        try:
            return self.transcript_path(session_id).stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise PersistenceError(
                f"could not inspect transcript for {session_id!r}: {exc}"
            ) from exc

    def truncate_transcript(self, session_id, size):
        """Restore the transcript to a previously persisted byte boundary."""
        target = max(0, int(size or 0))
        path = self.transcript_path(session_id)
        try:
            with self._write_lock:
                if not path.exists():
                    if target == 0:
                        return
                    raise PersistenceError(
                        f"transcript for {session_id!r} is missing"
                    )
                with path.open("r+b") as handle:
                    if target > handle.seek(0, 2):
                        raise PersistenceError(
                            f"transcript checkpoint for {session_id!r} is invalid"
                        )
                    handle.truncate(target)
        except PersistenceError:
            raise
        except OSError as exc:
            raise PersistenceError(
                f"could not truncate transcript for {session_id!r}: {exc}"
            ) from exc

    def clear_transcript(self, session_id):
        try:
            with self._write_lock:
                self.transcript_path(session_id).unlink(missing_ok=True)
        except OSError as exc:
            raise PersistenceError(
                f"could not clear transcript for {session_id!r}: {exc}"
            ) from exc

    def latest(self):
        sessions = self.list_sessions()
        return sessions[0]["id"] if sessions else None

    def count(self):
        return len(self.list_sessions())

    def list_sessions(self):
        # 返回当前项目下的人类可读 session 列表，按最近更新时间倒序。
        # dream/delegate/review session 属于后台或子任务运行工件，
        # 不参与用户会话恢复，也不计入项目的人类会话数量。
        sessions = []
        for path in self.root.glob("*/session.json"):
            if path.parent.name.startswith(("dream-", "delegate-", "review-")):
                continue
            try:
                session = self.load(path.parent.name)
            except (OSError, json.JSONDecodeError):
                continue
            session_id = str(session.get("id") or path.parent.name)
            title = str(session.get("title") or "").strip()
            updated_at = str(session.get("updated_at") or session.get("created_at") or "").strip()
            sessions.append(
                {
                    "id": session_id,
                    "title": title,
                    "title_slug": str(session.get("title_slug") or _title_slug(title)),
                    "created_at": str(session.get("created_at") or "").strip(),
                    "updated_at": updated_at,
                    "path": path,
                    "mtime": path.stat().st_mtime,
                }
            )
        return sorted(sessions, key=lambda item: (item["mtime"], item["id"]), reverse=True)

    def resolve(self, query):
        # 将用户输入的 id、id 前缀、标题或标题 slug 解析成唯一 session。
        # 返回 (session_id, matches)，当 session_id 为 None 且 matches 非空时表示歧义。
        value = str(query or "").strip()
        if not value:
            return None, []
        sessions = self.list_sessions()
        by_id = [item for item in sessions if item["id"] == value]
        if len(by_id) == 1:
            return by_id[0]["id"], by_id
        slug = _title_slug(value)
        matches = [
            item
            for item in sessions
            if item["id"].startswith(value)
            or (item["title"] and item["title"] == value)
            or (item["title_slug"] and item["title_slug"] == slug)
        ]
        if len(matches) == 1:
            return matches[0]["id"], matches
        return None, matches

    def rename(self, session_id, title):
        session = self.load(session_id)
        session["title"] = str(title or "").strip()
        session["title_slug"] = _title_slug(session["title"])
        session["updated_at"] = datetime.now().astimezone().isoformat()
        return self.save(session)
