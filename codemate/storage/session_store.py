# 会话存储：负责把每个 session 的状态和运行目录组织在同一个本地文件夹中。
# 每个 session 拥有独立目录，目录下保存 session.json 和该会话产生的 runs。
# 这样排查历史时可以直接从一次会话进入对应的运行工件，而不是在全局 runs 中查找。

import json
from pathlib import Path
import re
from datetime import datetime


def _title_slug(text):
    value = str(text or "").strip().lower()
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", value)
    value = value.strip("-")
    return value[:80]


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id):
        return self.root / str(session_id)

    def path(self, session_id):
        return self.session_dir(session_id) / "session.json"

    def runs_dir(self, session_id):
        return self.session_dir(session_id) / "runs"

    def save(self, session):
        path = self.path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        sessions = self.list_sessions()
        return sessions[0]["id"] if sessions else None

    def count(self):
        return len(self.list_sessions())

    def list_sessions(self):
        # 返回当前项目下的人类可读 session 列表，按最近更新时间倒序。
        # dream session 属于后台记忆整理流程，不参与用户会话恢复。
        sessions = []
        for path in self.root.glob("*/session.json"):
            if path.parent.name.startswith("dream-"):
                continue
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
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
