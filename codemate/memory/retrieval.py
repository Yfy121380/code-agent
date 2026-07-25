# 长期记忆模型召回。
# 本文件负责把三类长期记忆文件交给模型筛选，得到当前请求相关的记忆片段。
# 召回失败不会阻断主任务；runtime 会把失败记录到 trace，然后继续执行用户请求。

from __future__ import annotations

import json
import re
import time

from .long_term import LONG_TERM_MEMORY_FILES, has_long_term_content, read_long_term_memory

RETRIEVAL_MAX_ITEMS = 20
RETRIEVAL_MAX_TEXT_CHARS = 300
RETRIEVAL_MAX_REASON_CHARS = 300
RETRIEVAL_MAX_TOKENS = 1200

RETRIEVAL_SYSTEM_PROMPT = """You are Codemate's long-term memory retrieval module.

Your job is to select long-term memories that are useful for handling the latest user request in the current conversation.

You are not answering the user. You are only selecting relevant memories from the provided long-term memory content.

Memory categories:

1. user_profile
Stable facts about the user, including identity, knowledge background, learning needs, long-term goals, and durable preferences that affect how Codemate should explain or collaborate.

2. feedback_workflow
Reusable guidance about how Codemate should work with the user, including response style, planning style, code modification workflow, testing expectations, documentation style, tool-use preferences, and corrections about previous agent behavior.

3. project_context
Durable project-level context, including project goals, architecture decisions, constraints, storage layout, permission model decisions, feature direction, and important terminology.

Retrieval rules:
- Select only memories explicitly present in the provided memory content.
- Do not invent, rewrite, infer, or create new memories.
- Select memories that are likely to help with the latest user request or the current conversation direction.
- Avoid selecting memories that are clearly unrelated.
- If no memory is useful, return an empty list.
- If the user says to ignore memory, do not select any memory.
- If memories conflict, prefer the one with the newer created_at.
- Return at most 20 selected memories.

Output JSON only."""

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
                    "created_at": {"type": "string"},
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["source", "created_at", "text", "reason"],
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
        created_at = str(item.get("created_at", "")).strip()
        text = str(item.get("text", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if source not in allowed_sources or not text:
            continue
        if text.startswith("- "):
            text = text[2:].strip()
        if text.startswith("[") and "]" in text:
            prefix, _separator, remainder = text.partition("]")
            if not created_at:
                created_at = prefix.lstrip("[").strip()
            text = remainder.strip()
        selected.append(
            {
                "source": source,
                "created_at": created_at,
                "text": text[:RETRIEVAL_MAX_TEXT_CHARS],
                "reason": reason[:RETRIEVAL_MAX_REASON_CHARS],
                "kind": "long_term",
            }
        )
        if len(selected) >= RETRIEVAL_MAX_ITEMS:
            break
    return selected


def retrieve_long_term_memory(model_client, workspace_root, user_message, recent_messages=None):
    """使用一次独立模型请求筛选当前请求相关的长期记忆。

    recent_messages 只提供当前对话方向；真正可选的记忆只来自三类长期记忆文件。
    函数返回结构化条目，ContextManager 再决定如何渲染给主 agent。
    """
    started_at = time.monotonic()
    memory_files = read_long_term_memory(workspace_root)
    if not has_long_term_content(memory_files):
        return {
            "status": "skipped_empty",
            "selected": [],
            "memory_files": memory_files,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    memory_sections = []
    for source, text in memory_files.items():
        memory_sections.append(f"## {source}\n{text}")
    prompt = (
        "Select relevant long-term memories for the latest user request.\n\n"
        "Recent conversation messages are provided above only to clarify the user's current intent. They are not memory content.\n\n"
        "Latest user request:\n"
        f"{user_message}\n\n"
        "Long-term memory content:\n\n"
        + "\n\n".join(memory_sections)
        + "\n\n"
        "Return JSON only using exactly this format:\n\n"
        "{\n"
        '  "selected": [\n'
        '    {\n'
        '      "source": "user_profile | feedback_workflow | project_context",\n'
        '      "created_at": "created_at from the selected memory, or empty string if absent",\n'
        '      "text": "the selected memory text without created_at prefix",\n'
        '      "reason": "short reason why this memory is useful now"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        f"- Return at most {RETRIEVAL_MAX_ITEMS} selected memories.\n"
        "- Use the exact source category.\n"
        "- Preserve created_at from the selected memory when present.\n"
        "- Keep text concise.\n"
        '- If no memory is useful, return {"selected": []}.'
    )
    messages = list(recent_messages or [])
    messages.append({"role": "user", "content": prompt})
    response = model_client.complete(
        messages,
        RETRIEVAL_MAX_TOKENS,
        tools=[],
        system=RETRIEVAL_SYSTEM_PROMPT,
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
