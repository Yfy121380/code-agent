# 会话存储：负责把每个 session 的状态和运行目录组织在同一个本地文件夹中。
# 每个 session 拥有独立目录，目录下保存 session.json 和该会话产生的 runs。
# 这样排查历史时可以直接从一次会话进入对应的运行工件，而不是在全局 runs 中查找。

import json
from pathlib import Path


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
        files = sorted(
            (
                path
                for path in self.root.glob("*/session.json")
                if not path.parent.name.startswith("dream-")
            ),
            key=lambda path: path.stat().st_mtime,
        )
        return files[-1].parent.name if files else None

    def count(self):
        files = [
            path
            for path in self.root.glob("*/session.json")
            if not path.parent.name.startswith("dream-")
        ]
        return len(files)
