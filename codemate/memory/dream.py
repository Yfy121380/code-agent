# 后台 dream 记忆整理。
# 本文件负责 candidate-based dream 的触发判断、候选读取、prompt 生成和状态推进。
# Dream 整理 runtime 提取的结构化 candidates。
# 真正执行工具和启动子 agent 的流程仍在 runtime/dream.py 中。

from __future__ import annotations

import json
from datetime import datetime, timezone

from .long_term import candidates_dir, ensure_long_term_memory, load_dream_state, memory_root, save_dream_state
from ..workspace import clip, now

DREAM_MIN_UNPROCESSED_CANDIDATES = 10
DREAM_INTERVAL_SECONDS = 24 * 60 * 60


def unprocessed_candidates(workspace_root):
    """读取 dream_state cursor 之后的候选记忆。

    Candidate JSONL 是 append-only 文件。runtime 用文件名和行号推进 cursor，
    失败时不推进，下一次 dream 会重新处理同一批候选。
    """
    ensure_long_term_memory(workspace_root)
    state = load_dream_state(workspace_root)
    cursor = state.get("last_processed_candidate") or {}
    cursor_file = str(cursor.get("file", "") or "")
    cursor_line = int(cursor.get("line", 0) or 0)

    result = []
    for path in sorted(candidates_dir(workspace_root).glob("*.jsonl")):
        if cursor_file and path.name < cursor_file:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if cursor_file and path.name == cursor_file and line_number <= cursor_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = _normalize_candidate(data)
            if candidate is None:
                continue
            candidate.update({"file": path.name, "line": line_number})
            result.append(candidate)
    return result


def latest_candidate_cursor(candidates):
    candidates = list(candidates or [])
    if not candidates:
        return {"file": "", "line": 0}
    last = candidates[-1]
    return {"file": str(last.get("file", "")), "line": int(last.get("line", 0) or 0)}


def should_run_dream(workspace_root):
    candidates = unprocessed_candidates(workspace_root)
    count = len(candidates)
    if count <= 0:
        return False, "no_unprocessed_candidates"
    if count >= DREAM_MIN_UNPROCESSED_CANDIDATES:
        return True, "candidate_threshold"

    state = load_dream_state(workspace_root)
    last_at = str(state.get("last_dream_at", "") or "").strip()
    if not last_at:
        return False, "not_enough_candidates"
    try:
        parsed = datetime.fromisoformat(last_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except Exception:
        return True, "invalid_state_with_candidates"
    if age >= DREAM_INTERVAL_SECONDS:
        return True, "time_and_candidate_interval"
    return False, "not_due"


def mark_dream_complete(workspace_root, processed_candidates, status="ok"):
    state = load_dream_state(workspace_root)
    state.update(
        {
            "last_dream_at": now(),
            "last_status": str(status),
            "last_processed_candidate": latest_candidate_cursor(processed_candidates),
        }
    )
    save_dream_state(workspace_root, state)
    return state


def mark_dream_failed(workspace_root, status="error"):
    state = load_dream_state(workspace_root)
    state["last_status"] = str(status)
    save_dream_state(workspace_root, state)


def render_candidate_batch(candidates):
    candidates = list(candidates or [])
    if not candidates:
        return "No candidate memories to process."
    lines = []
    current_file = ""
    for index, candidate in enumerate(candidates, 1):
        file_name = str(candidate.get("file", "") or "")
        if file_name != current_file:
            current_file = file_name
            if lines:
                lines.append("")
            lines.append(f"Source: candidates/{current_file}")
            lines.append("")
        lines.extend(
            [
                f"{index}. line: {int(candidate.get('line', 0) or 0)}",
                f"   created_at: {candidate['created_at']}",
                f"   type: {candidate['type']}",
                f"   memory: {candidate['memory']}",
                f"   evidence: {candidate['evidence']}",
                f"   confidence: {candidate['confidence']}",
            ]
        )
    return "\n".join(lines)


def dream_system_prompt(memory_root_path):
    memory_root_text = str(memory_root_path or "").strip() or str(memory_root("."))
    return f"""You are performing a dream — a reflective memory consolidation pass for Codemate.

Your job is to synthesize structured candidate memories into durable, well-organized long-term memories so that future sessions can orient quickly.

Memory root: {memory_root_text}

You may read and update only these formal long-term memory files:
- user_profile.md
- feedback_workflow.md
- project_context.md

The candidate memories are pre-filtered signals. They are not final memory. Your job is to decide which candidates are durable enough to promote, merge, rewrite, or discard.

Formal memory categories:

1. user_profile

Stable information about the user:
- role or identity
- technical background
- knowledge level
- learning needs
- long-term interests or goals
- durable facts about how the user tends to think or work

2. feedback_workflow

Reusable guidance about how Codemate should work with the user:
- response style
- planning style
- coding workflow
- testing and verification expectations
- documentation style
- tool-use preferences
- corrections about previous agent behavior
- successful approaches the user explicitly validated
- things the user wants Codemate to avoid

3. project_context

Durable information about this project:
- project goals
- architecture decisions
- long-lived design constraints
- module responsibilities
- storage layout decisions
- permission model decisions
- feature direction
- important project-specific terminology

Formal memory storage format:
- Keep each long-term memory file as a short Markdown bullet list.
- Every memory bullet must use exactly this single-line format:
  - [created_at] memory
- Use the created_at from the source candidate.
- When merging compatible candidates, use the newest created_at among the merged candidates.
- Do not use tables, YAML blocks, arbitrary sections, nested bullets, or pasted raw candidate dumps.
- Do not write evidence fields into formal memory. Evidence is only for judging whether the candidate is worth keeping.

Tool use:
- You may use todo_write to plan and track the dream phases when it helps keep the work organized.

Phase 1 — Orient

- List memory_root.
- Read user_profile.md, feedback_workflow.md, and project_context.md.
- Understand what is already remembered before editing.
- Prefer updating existing memories over creating duplicates.

Phase 2 — Extract durable signal from candidates

Review the candidate memories provided in the request.

Promote candidates that are useful across future sessions:
- stable user profile information
- reusable workflow feedback
- durable project context
- important project decisions or constraints

Discard candidates that are:
- temporary task progress
- current todos
- raw tool output
- one-off debugging details
- already derivable from current code, docs, or git history
- too vague to be useful
- low confidence without support
- only useful in the current conversation

Candidate confidence:
- high: usually promote if it is durable and not duplicated
- medium: promote only if clearly useful
- low: usually discard unless strongly supported by existing memory or repeated candidates

Phase 3 — Consolidate, deduplicate, resolve conflicts

For each useful candidate:
- Add it to the correct formal memory file.
- Merge it with existing memory if it overlaps.
- Rewrite duplicated entries into one clearer entry.
- If a candidate conflicts with existing memory, keep the newer information based on created_at.
- Remove or replace contradicted older memory.
- Keep formal memory concise and directly useful.
- Do not preserve candidate wording if a clearer durable memory can be written.

Conflict handling:
- Newer created_at wins when two memories contradict each other.
- Compatible memories should be merged instead of duplicated.
- If a candidate is already fully represented by existing memory, do not add it again.
- If existing memory has become stale or misleading, update or remove it.

Examples:

Example 1 — promote workflow feedback

Candidate:
{{
  "created_at": "2026-07-25T10:12:00+08:00",
  "type": "feedback_workflow",
  "memory": "用户希望代码修改前先讨论方案，确认后再动手实现。",
  "evidence": "用户明确要求未来修改代码前先给方案。",
  "confidence": "high"
}}

Formal memory:
feedback_workflow.md
- [2026-07-25T10:12:00+08:00] 用户希望代码修改前先讨论方案，确认后再动手实现。

Example 2 — merge duplicate memories

Existing memory:
- [2026-07-20T09:00:00+08:00] 用户不喜欢无意义的小函数拆分。

Candidate:
{{
  "created_at": "2026-07-25T11:00:00+08:00",
  "type": "feedback_workflow",
  "memory": "用户不希望为一次性短逻辑新增过多小函数，除非该逻辑明确可复用。",
  "evidence": "用户明确说明未来写代码时对函数拆分的偏好。",
  "confidence": "high"
}}

Updated formal memory:
- [2026-07-25T11:00:00+08:00] 用户不希望为一次性短逻辑新增过多小函数，除非该逻辑明确可复用。

Example 3 — discard temporary information

Candidate:
{{
  "created_at": "2026-07-25T12:00:00+08:00",
  "type": "project_context",
  "memory": "刚才 pytest 通过了 181 个测试。",
  "evidence": "工具输出显示测试通过。",
  "confidence": "medium"
}}

Action:
Discard it. This is temporary validation state, not durable long-term memory.

Example 4 — resolve conflict by newer memory

Existing memory:
- [2026-07-20T10:00:00+08:00] Codemate 的长期记忆同时包含项目级和用户级记忆。

Candidate:
{{
  "created_at": "2026-07-25T13:00:00+08:00",
  "type": "project_context",
  "memory": "Codemate 的长期记忆设计为项目级记忆，不设置用户级长期记忆。",
  "evidence": "用户明确指定长期记忆的项目绑定范围。",
  "confidence": "high"
}}

Updated formal memory:
- [2026-07-25T13:00:00+08:00] Codemate 的长期记忆设计为项目级记忆，不设置用户级长期记忆。

Example 5 — classify an unspecified manual memory

Candidate:
{{
  "created_at": "2026-07-25T15:25:00+08:00",
  "type": "unspecified",
  "memory": "以后回答问题时先说结论，再给解释。",
  "evidence": "用户通过 /remember 要求记住这条信息。",
  "confidence": "high"
}}

Formal memory:
feedback_workflow.md
- [2026-07-25T15:25:00+08:00] 用户希望回答问题时先说结论，再给解释。"""


def dream_prompt(candidate_batch):
    candidate_batch = str(candidate_batch or "").strip() or "No candidate memories to process."
    return (
        "Consolidate the candidate memories below into Codemate's formal long-term memory files.\n\n"
        "You should:\n"
        "1. Read the existing formal memory files.\n"
        "2. Decide which candidates are durable enough to promote.\n"
        "3. Classify unspecified candidates into user_profile, feedback_workflow, or project_context when they are worth keeping.\n"
        "4. Merge, deduplicate, update, or discard candidates according to the dream rules.\n"
        "5. Update user_profile.md, feedback_workflow.md, and project_context.md as needed.\n"
        "6. Return a brief summary of what you consolidated, updated, merged, discarded, or left unchanged. If nothing changed, say so.\n\n"
        "Candidate memories to process:\n\n"
        f"{candidate_batch}"
    )


def _normalize_candidate(data):
    if not isinstance(data, dict):
        return None
    candidate_type = str(data.get("type", "") or "").strip()
    if candidate_type not in {"user_profile", "feedback_workflow", "project_context", "unspecified"}:
        return None
    memory = clip(str(data.get("memory", "") or "").strip(), 800)
    if not memory:
        return None
    confidence = str(data.get("confidence", "") or "").strip()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    return {
        "created_at": str(data.get("created_at", "") or "").strip() or now(),
        "type": candidate_type,
        "memory": memory,
        "evidence": clip(str(data.get("evidence", "") or "").strip(), 500),
        "confidence": confidence,
    }
