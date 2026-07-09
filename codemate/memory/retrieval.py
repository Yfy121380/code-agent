# 长期记忆模型召回。
# 本文件负责把三类长期记忆文件交给模型筛选，得到当前请求相关的记忆片段。
# 召回失败不会阻断主任务；runtime 会把失败记录到 trace，然后继续执行用户请求。

from __future__ import annotations

import json
import re
import time

from .long_term import LONG_TERM_MEMORY_FILES, has_long_term_content, read_long_term_memory

RETRIEVAL_MAX_ITEMS = 8
RETRIEVAL_MAX_TEXT_CHARS = 300
RETRIEVAL_MAX_TOKENS = 1200

RETRIEVAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["user_profile", "feedback_workflow", "project_context"],
                    },
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["source", "text", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["selected"],
    "additionalProperties": False,
}


def _json_from_text(text):
    text = str(text or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _normalize_selected(raw_items):
    selected = []
    allowed_sources = set(LONG_TERM_MEMORY_FILES)
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        text = str(item.get("text", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if source not in allowed_sources or not text:
            continue
        selected.append(
            {
                "source": source,
                "text": text[:RETRIEVAL_MAX_TEXT_CHARS],
                "reason": reason[:RETRIEVAL_MAX_TEXT_CHARS],
                "kind": "long_term",
            }
        )
        if len(selected) >= RETRIEVAL_MAX_ITEMS:
            break
    return selected


def retrieve_long_term_memory(model_client, workspace_root, user_message):
    started_at = time.monotonic()
    memory_files = read_long_term_memory(workspace_root)
    if not has_long_term_content(memory_files):
        return {
            "status": "skipped_empty",
            "selected": [],
            "memory_files": memory_files,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    system = (
        "You are a memory retrieval module for codemate. Select only long-term memory entries "
        "that are relevant to the current user request. Return valid JSON only."
    )
    memory_sections = []
    for source, text in memory_files.items():
        memory_sections.append(f"## {source}\n{text}")
    prompt = (
        "Current user request:\n"
        f"{user_message}\n\n"
        "Long-term memory files:\n"
        + "\n\n".join(memory_sections)
        + "\n\n"
        "Memory categories:\n"
        "- user_profile: facts about the user's role, goals, knowledge background, skill level, stable preferences, expression style, and collaboration preferences.\n"
        "- feedback_workflow: stable feedback about how the agent should work, verify, report, ask before acting, and avoid repeating past mistakes.\n"
        "- project_context: long-lived project background, goals, naming, architecture direction, constraints, and decisions not directly derivable from current code or git state.\n\n"
        "Rules:\n"
        "- Only select information explicitly present in the provided memory files.\n"
        "- Do not invent new memories.\n"
        "- Prefer workflow feedback when it affects how the agent should perform the request.\n"
        "- Prefer user profile memories when they affect the user's role, knowledge background, tone, format, collaboration style, or decisions.\n"
        "- Prefer project context when it affects naming, architecture, constraints, or implementation choices.\n"
        "- Ignore stale, task-local, duplicated, or irrelevant entries.\n"
        f"- Return at most {RETRIEVAL_MAX_ITEMS} selected items.\n\n"
        "Output schema:\n"
        "{\n"
        '  "selected": [\n'
        '    {"source": "user_profile | feedback_workflow | project_context", "text": "...", "reason": "..."}\n'
        "  ]\n"
        "}"
    )
    response = model_client.complete(
        [{"role": "user", "content": prompt}],
        RETRIEVAL_MAX_TOKENS,
        tools=None,
        system=system,
        structured_output={
            "name": "long_term_memory_retrieval",
            "schema": RETRIEVAL_OUTPUT_SCHEMA,
        },
    )
    data = _json_from_text(getattr(response, "text", ""))
    selected = _normalize_selected(data.get("selected", []))
    return {
        "status": "ok",
        "selected": selected,
        "memory_files": memory_files,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
    }
