# 后台 dream 记忆整理。
# 本文件负责判断 dream 是否需要运行，并提供 memory consolidation 的任务提示词。
# 真正的 agent 控制循环仍由 runtime 负责，这里不直接执行工具，也不修改项目代码。

from __future__ import annotations

from datetime import datetime, timezone

from .long_term import daily_logs_dir, load_dream_state, save_dream_state
from ..workspace import now

DREAM_INTERVAL_SESSIONS = 5
DREAM_INTERVAL_SECONDS = 24 * 60 * 60


def latest_daily_log_cursor(workspace_root):
    log_dir = daily_logs_dir(workspace_root)
    files = sorted(path for path in log_dir.glob("*.md") if path.is_file())
    if not files:
        return {"file": "", "line": 0}
    latest = files[-1]
    line_count = len(latest.read_text(encoding="utf-8", errors="replace").splitlines())
    return {"file": latest.name, "line": line_count}


def render_daily_log_cursor(state):
    cursor = state.get("last_processed_daily_log") or {}
    file_name = str(cursor.get("file", "")).strip()
    line = int(cursor.get("line", 0) or 0)
    if not file_name:
        return (
            "Previous dream processed daily logs through: none.\n"
            "All daily log files are unprocessed."
        )
    return (
        "Previous dream processed daily logs through:\n"
        f"- file: {file_name}\n"
        f"- line: {line}\n\n"
        "Only consolidate daily log entries after this cursor:\n"
        f"- For `{file_name}`, start from line {line + 1}.\n"
        f"- For daily log files later than `{file_name}`, process all lines.\n"
        f"- For daily log files earlier than `{file_name}`, ignore them."
    )


def should_run_dream(workspace_root, session_count):
    state = load_dream_state(workspace_root)
    last_session_count = int(state.get("last_dream_session_count", 0) or 0)
    new_sessions = int(session_count) - last_session_count
    if new_sessions < DREAM_INTERVAL_SESSIONS:
        return False, "not_enough_sessions"

    last_at = str(state.get("last_dream_at", "")).strip()
    if not last_at:
        return True, "first_run_session_threshold"
    try:
        parsed = datetime.fromisoformat(last_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except Exception:
        return True, "invalid_state_session_threshold"
    if age >= DREAM_INTERVAL_SECONDS:
        return True, "time_and_session_interval"
    return False, "not_due"


def mark_dream_complete(workspace_root, session_count, status="ok"):
    state = load_dream_state(workspace_root)
    state.update(
        {
            "last_dream_at": now(),
            "last_dream_session_count": int(session_count),
            "last_status": str(status),
            "last_processed_daily_log": latest_daily_log_cursor(workspace_root),
        }
    )
    save_dream_state(workspace_root, state)
    return state


def mark_dream_failed(workspace_root, status="error"):
    state = load_dream_state(workspace_root)
    state["last_status"] = str(status)
    save_dream_state(workspace_root, state)


def dream_prompt(processing_state_text=""):
    processing_state_text = str(processing_state_text or "").strip()
    if not processing_state_text:
        processing_state_text = (
            "Previous dream processed daily logs through: none.\n"
            "All daily log files are unprocessed."
        )
    return (
        "Consolidate codemate long-term memory.\n\n"
        "Allowed workspace scope: `.codemate/memory/` only.\n"
        "Use Runtime context for current_local_datetime, current_local_date, timezone, and today_daily_log_path.\n"
        "Use todo_write if it helps plan this consolidation.\n\n"
        "Daily log processing state:\n"
        f"{processing_state_text}\n\n"
        "Long-term memory files:\n"
        "- `user_profile.md`: user profile facts, including the user's role, goals, knowledge background, skill level, stable preferences, expression style, and collaboration preferences.\n"
        "- `feedback_workflow.md`: stable feedback about how the agent should work, verify, report, ask before acting, and avoid repeating past mistakes.\n"
        "- `project_context.md`: long-lived project background, goals, naming, architecture direction, constraints, and decisions not directly derivable from current code or git state.\n\n"
        "Daily logs are raw append-only memory signals, not final long-term memory files. Daily log entries use this format:\n"
        "- [created_at] memory\n\n"
        "Long-term memory storage format:\n"
        "- Keep each long-term memory file as a short Markdown bullet list.\n"
        "- Every memory bullet must use exactly this single-line format: `- [created_at] memory`.\n"
        "- Use the created_at from the source daily log entry. When merging compatible entries, use the newest created_at among the merged entries.\n"
        "- Do not use tables, YAML blocks, arbitrary sections, or pasted raw log dumps.\n\n"
        "Storage examples:\n"
        "`user_profile.md`:\n"
        "- [2026-07-08T14:23:10+08:00] User is preparing for interviews and wants implementation ideas explained with code flow.\n"
        "- [2026-07-08T14:26:00+08:00] User prefers Chinese explanations and code-grounded reasoning.\n\n"
        "`feedback_workflow.md`:\n"
        "- [2026-07-08T14:20:00+08:00] Ask before fixing extra issues outside the requested scope.\n"
        "- [2026-07-08T14:21:30+08:00] After editing Python files, run py_compile before finishing.\n\n"
        "`project_context.md`:\n"
        "- [2026-07-08T14:25:42+08:00] The project is named codemate and is a local coding assistant.\n"
        "- [2026-07-08T14:28:10+08:00] Long-term memory uses daily logs plus background dream consolidation.\n\n"
        "Phases:\n"
        "1. Orient: list `.codemate/memory`, read the three long-term memory files, and inspect recent daily logs.\n"
        "2. Gather: extract long-term information only from unprocessed daily log entries after the cursor above. Ignore one-off task details, current todos, raw tool output, secrets, and transient debugging notes.\n"
        "3. Consolidate: classify useful daily log entries into `user_profile.md`, `feedback_workflow.md`, and `project_context.md` with stable, deduplicated facts.\n"
        "4. Prune: remove stale, duplicated, contradicted, or overly verbose entries. Keep each file concise.\n\n"
        "Conflict handling:\n"
        "- Every long-term memory bullet must have created_at.\n"
        "- If two memories conflict, keep the newer one based on created_at and remove or replace the older contradictory memory.\n"
        "- If two memories are compatible but duplicated, merge them into one clearer memory and use the newest created_at.\n"
        "- Do not keep both old and new contradictory bullets.\n\n"
        "Do not rewrite daily logs. They are append-only raw records.\n"
        "Finish with a short summary of what changed."
    )
