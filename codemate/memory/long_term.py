# 长期记忆文件管理。
# 本文件负责项目绑定的长期记忆目录、daily log 目录和 dream 状态文件。
# 长期记忆物理位置位于用户级 codemate 状态目录下，并按项目 id 隔离。
# 它不负责模型召回，也不判断哪些信息值得记忆。

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..config.paths import codemate_paths
from ..workspace import now

LONG_TERM_MEMORY_FILES = {
    "user_profile": {
        "filename": "user_profile.md",
        "title": "User Profile",
        "description": "Stable facts about the user's role, goals, knowledge background, preferences, and collaboration style.",
    },
    "feedback_workflow": {
        "filename": "feedback_workflow.md",
        "title": "Feedback Workflow",
        "description": "Stable feedback about how the agent should work with the user.",
    },
    "project_context": {
        "filename": "project_context.md",
        "title": "Project Context",
        "description": "Stable project goals, constraints, architecture notes, and naming decisions.",
    },
}

DREAM_STATE_FILENAME = ".dream_state.json"
DREAM_LOCK_FILENAME = ".dream.lock"
DREAM_LOCK_TTL_SECONDS = 60 * 30


def memory_root(workspace_root):
    return codemate_paths(workspace_root).memory_root


def daily_logs_dir(workspace_root):
    return memory_root(workspace_root) / "daily_logs"


def daily_log_path(workspace_root, date=None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    return daily_logs_dir(workspace_root) / f"{date}.md"


def long_term_file_path(workspace_root, source):
    spec = LONG_TERM_MEMORY_FILES[str(source)]
    return memory_root(workspace_root) / spec["filename"]


def ensure_long_term_memory(workspace_root):
    root = memory_root(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    daily_logs_dir(workspace_root).mkdir(parents=True, exist_ok=True)
    for spec in LONG_TERM_MEMORY_FILES.values():
        path = root / spec["filename"]
        if spec["filename"] == "user_profile.md":
            legacy_path = root / "user_preferences.md"
            if legacy_path.exists() and not path.exists():
                legacy_path.rename(path)
        if path.exists():
            continue
        path.write_text(f"# {spec['title']}\n\n", encoding="utf-8")
    return root


def read_long_term_memory(workspace_root, per_file_limit=12000):
    ensure_long_term_memory(workspace_root)
    result = {}
    for source in LONG_TERM_MEMORY_FILES:
        path = long_term_file_path(workspace_root, source)
        text = path.read_text(encoding="utf-8", errors="replace")
        if per_file_limit and len(text) > per_file_limit:
            text = text[:per_file_limit] + f"\n...[truncated {len(text) - per_file_limit} chars]"
        result[source] = text
    return result


def has_long_term_content(memory_files):
    # 默认创建的标题和说明不算可召回记忆。
    # 只有 bullet、正文段落或用户写入的额外内容出现后，才触发模型召回。
    for source, text in (memory_files or {}).items():
        spec = LONG_TERM_MEMORY_FILES.get(source, {})
        ignored = {
            f"# {spec.get('title', '')}".strip(),
            "",
        }
        for line in str(text).splitlines():
            if line.strip() not in ignored:
                return True
    return False


def is_memory_path(workspace_root, path):
    root = memory_root(workspace_root).resolve()
    resolved = Path(path).expanduser().resolve()
    try:
        return os.path.commonpath([str(root), str(resolved)]) == str(root)
    except ValueError:
        return False


def load_dream_state(workspace_root):
    ensure_long_term_memory(workspace_root)
    path = memory_root(workspace_root) / DREAM_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_dream_state(workspace_root, state):
    ensure_long_term_memory(workspace_root)
    path = memory_root(workspace_root) / DREAM_STATE_FILENAME
    path.write_text(json.dumps(dict(state or {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def dream_lock(workspace_root):
    ensure_long_term_memory(workspace_root)
    path = memory_root(workspace_root) / DREAM_LOCK_FILENAME
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age <= DREAM_LOCK_TTL_SECONDS:
            yield False
            return
        path.unlink(missing_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(now() + "\n")
    except FileExistsError:
        yield False
        return
    try:
        yield True
    finally:
        path.unlink(missing_ok=True)
