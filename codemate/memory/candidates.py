# 长期记忆候选提取。
# 本文件负责把一段完整对话交给模型筛选，生成候选长期记忆。
# 候选记忆不是正式记忆，只是 dream 后续整理的原始信号。
# 文件写入由 runtime 控制，模型只返回结构化 JSON，避免把 JSONL 写乱。

from __future__ import annotations

import json
import re
from collections import OrderedDict

from ..workspace import clip, now
from .constants import (
    MEMORY_CANDIDATE_EXTRACT_INTERVAL_TURNS,
    MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES,
    MEMORY_CANDIDATE_EXTRACT_MIN_CHARS,
    MEMORY_CANDIDATE_MAX_EVIDENCE_CHARS,
    MEMORY_CANDIDATE_MAX_ITEMS,
    MEMORY_CANDIDATE_MAX_TEXT_CHARS,
)
from .long_term import candidate_log_path, ensure_long_term_memory


AUTO_CANDIDATE_MEMORY_TYPES = ("user_profile", "feedback_workflow", "project_context")
MANUAL_CANDIDATE_MEMORY_TYPE = "unspecified"

CANDIDATE_MEMORY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(AUTO_CANDIDATE_MEMORY_TYPES)},
                    "memory": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["type", "memory", "evidence", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

CANDIDATE_MEMORY_EXTRACT_SYSTEM_PROMPT = """You are a memory candidate extractor for Codemate.

Your job is to inspect recent conversation messages and extract candidate long-term memories that may be useful in future sessions of this project.

You are NOT writing final long-term memory. You are only producing candidates. A later dream process will merge, deduplicate, discard, rewrite, or promote these candidates into formal long-term memory.

The goal is to preserve durable information that can help future Codemate sessions understand the user, follow the user's preferred workflow, or understand long-lived project context.

Memory categories:

1. user_profile

Use this category for stable information about the user.

This includes:
- the user's role or identity
- the user's technical background
- the user's knowledge level
- the user's learning needs
- the user's long-term interests or goals
- durable facts about how the user tends to think or work

Examples:
- The user is building a coding agent and cares about context management, permissions, memory, MCP, and skills.
- The user has limited background in Python packaging and wants concepts like uv and pyproject explained clearly.
- The user is preparing project notes for interviews and wants design ideas explained in a way that can be retold.

2. feedback_workflow

Use this category for guidance about how Codemate should work with the user.

This includes:
- response style preferences
- planning preferences
- code modification workflow preferences
- tool-use preferences
- testing and verification expectations
- documentation style preferences
- corrections about previous agent behavior
- successful approaches the user explicitly validates
- things the user wants the agent to avoid in future work

Examples:
- The user wants implementation plans to be discussed before code is modified.
- The user dislikes unnecessary small helper functions when logic is only used once.
- The user wants old unused logic removed instead of keeping compatibility code that is no longer needed.
- The user prefers concise but informative progress updates during long investigations.
- The user wants documentation to be summary-oriented rather than filled with source-file details.

3. project_context

Use this category for durable information about the current project.

This includes:
- project goals
- architecture decisions
- long-lived design constraints
- module responsibilities
- storage layout decisions
- permission model decisions
- feature direction that affects future implementation
- important project-specific terminology

Examples:
- Codemate stores project memory under the project-specific directory in the user's Codemate home.
- Codemate's long-term memory is project-scoped and does not use user-level long-term memory.
- Codemate's permission model uses read/write allow and deny rules, with deny taking precedence.
- Codemate supports skills stored under project and user skill directories.
- MCP configuration is planned to live in settings.json.

Pay special attention to messages containing phrases such as:
- remember
- from now on
- next time
- don't
- avoid
- I prefer
- 以后
- 记住
- 不要
- 下次
- 我希望
- 以后都
- 别再

These phrases do not automatically mean a memory should be extracted. They only mean the surrounding message deserves careful review.

Extract a candidate only when the information is likely to be useful in future sessions.

Do NOT extract:
- temporary task progress
- current todo items
- command output
- stack traces
- raw file contents
- line numbers
- current editor state
- recent git commits
- temporary test results
- transient debugging details
- speculation or uncertain guesses
- information only useful for the current turn
- facts easily derived from reading the current code, docs, or git history

If the user gives an instruction that only applies to the current task, do not extract it.
If the user gives a reusable preference, workflow rule, or project decision, extract it.
If the conversation contains no useful long-term memory candidates, return an empty candidates list.

Memory text rules:
- Write each memory in Chinese.
- Keep each memory concise and self-contained.
- Prefer one sentence per memory.
- Do not include unnecessary source-message wording.
- Do not include private chain-of-thought or speculation.
- Convert relative dates to absolute dates when the date matters.
- Preserve important constraints and reasons when they affect future behavior.

Output JSON only.

The output must match this schema:

{
  "candidates": [
    {
      "type": "user_profile | feedback_workflow | project_context",
      "memory": "one concise Chinese sentence",
      "evidence": "short Chinese explanation of what message this came from",
      "confidence": "high | medium | low"
    }
  ]
}

Positive examples:

Input:
user: 以后修改代码之前先给我方案，讨论通过之后再动手。

Output:
{
  "candidates": [
    {
      "type": "feedback_workflow",
      "memory": "用户希望代码修改前先讨论方案，确认后再动手实现。",
      "evidence": "用户明确要求未来修改代码前先给方案。",
      "confidence": "high"
    }
  ]
}

Input:
user: 我基础比较缺乏，像 pyproject 中的字段、uv 和 pip 的关系都要讲清楚一点。

Output:
{
  "candidates": [
    {
      "type": "user_profile",
      "memory": "用户对 Python 包管理、pyproject、uv 与 pip 的关系等基础概念需要更清晰的解释。",
      "evidence": "用户说明自己在 Python 包管理方面基础较缺乏。",
      "confidence": "high"
    }
  ]
}

Input:
user: 以后写代码时，除非是明确可复用的逻辑片段，否则不要新增一堆小函数。

Output:
{
  "candidates": [
    {
      "type": "feedback_workflow",
      "memory": "用户不希望为一次性短逻辑新增过多小函数，除非该逻辑明确可复用。",
      "evidence": "用户明确说明未来写代码时对函数拆分的偏好。",
      "confidence": "high"
    }
  ]
}

Input:
user: codemate 的长期记忆只和项目绑定，不设置用户级别的长期记忆。

Output:
{
  "candidates": [
    {
      "type": "project_context",
      "memory": "Codemate 的长期记忆设计为项目级记忆，不设置用户级长期记忆。",
      "evidence": "用户明确指定长期记忆的项目绑定范围。",
      "confidence": "high"
    }
  ]
}

Input:
user: 文档里不要提太多源码路径，我更想要总结性质的笔记，一眼能看到模块重点。

Output:
{
  "candidates": [
    {
      "type": "feedback_workflow",
      "memory": "用户希望项目文档偏总结性，突出模块重点，而不是堆大量源码路径和实现细节。",
      "evidence": "用户明确说明文档写作风格偏好。",
      "confidence": "high"
    }
  ]
}

Negative examples:

Input:
user: 我吃饭去了，刚才没点确认。

Output:
{
  "candidates": []
}

Input:
assistant: pytest 通过了，13 passed, 1 warning。

Output:
{
  "candidates": []
}

Input:
user: 现在先把 README.md 后面加上“你好”。

Output:
{
  "candidates": []
}

Input:
tool: read_file codemate/runtime.py -> ok, 120 lines

Output:
{
  "candidates": []
}

Input:
user: 这个 trace 在 .codemate/sessions/20260716-220258-b53a1b/runs/xxx/trace.jsonl。

Output:
{
  "candidates": []
}
"""

CANDIDATE_MEMORY_EXTRACT_REQUEST = """Please extract candidate long-term memories from the conversation above.

Do not continue the conversation.
Do not answer the user's original request.
Do not summarize the whole conversation.
Only identify information that may be useful in future Codemate sessions.

Extract candidates only for these categories:
- user_profile
- feedback_workflow
- project_context

Return an empty candidates list if there is nothing worth remembering.

Return JSON only, using exactly this format:

{
  "candidates": [
    {
      "type": "user_profile | feedback_workflow | project_context",
      "memory": "one concise Chinese sentence",
      "evidence": "short Chinese explanation of what message this came from",
      "confidence": "high | medium | low"
    }
  ]
}
"""


def default_candidate_extract_state():
    return {
        "last_extracted_conversation_id": "",
        "last_extracted_at": "",
        "user_turns_since_last_extract": 0,
        "chars_since_last_extract": 0,
    }


def normalize_candidate_extract_state(state):
    state = state if isinstance(state, dict) else {}
    normalized = default_candidate_extract_state()
    normalized.update(
        {
            "last_extracted_conversation_id": str(state.get("last_extracted_conversation_id", "") or ""),
            "last_extracted_at": str(state.get("last_extracted_at", "") or ""),
            "user_turns_since_last_extract": int(state.get("user_turns_since_last_extract", 0) or 0),
            "chars_since_last_extract": int(state.get("chars_since_last_extract", 0) or 0),
        }
    )
    return normalized


def conversations_since_checkpoint(session, include_incomplete=False):
    """按 conversation_id 找到上次提取之后的完整对话。

    history compact 会改变 message 数量，因此这里不使用下标作为边界。
    每轮 ask 写入的 user、assistant、tool 消息共享同一个 conversation_id，
    候选提取只处理已结束的完整对话，避免把当前半截任务写入候选池。
    """
    history = list((session or {}).get("history", []) or [])
    state = normalize_candidate_extract_state((session or {}).get("memory_candidate_extract", {}))
    checkpoint = state["last_extracted_conversation_id"]

    grouped = OrderedDict()
    for message in history:
        conversation_id = str(message.get("conversation_id", "") or "")
        if not conversation_id:
            continue
        grouped.setdefault(conversation_id, []).append(message)

    conversation_ids = list(grouped)
    if checkpoint and checkpoint in grouped:
        selected_ids = conversation_ids[conversation_ids.index(checkpoint) + 1:]
        checkpoint_missing = False
    else:
        selected_ids = conversation_ids
        checkpoint_missing = bool(checkpoint)

    selected = []
    for conversation_id in selected_ids:
        messages = grouped[conversation_id]
        complete = any(
            item.get("role") == "assistant"
            and (
                str(item.get("kind", "") or "") == "final"
                or (not item.get("tool_calls") and str(item.get("kind", "") or "") != "commentary")
                or str(item.get("content", "") or "").startswith("Stopped after ")
                or str(item.get("content", "") or "").startswith("History compaction failed:")
            )
            for item in messages
        )
        if complete or include_incomplete:
            selected.append({"id": conversation_id, "messages": messages})

    return {
        "conversations": selected,
        "checkpoint_missing": checkpoint_missing,
        "last_available_conversation_id": conversation_ids[-1] if conversation_ids else "",
    }


def update_candidate_extract_counters(session):
    info = conversations_since_checkpoint(session)
    messages = [message for conversation in info["conversations"] for message in conversation["messages"]]
    user_turns = sum(1 for item in messages if item.get("role") == "user")
    chars = sum(len(_message_text_for_count(item)) for item in messages)
    state = normalize_candidate_extract_state((session or {}).get("memory_candidate_extract", {}))
    state["user_turns_since_last_extract"] = user_turns
    state["chars_since_last_extract"] = chars
    session["memory_candidate_extract"] = state
    return state


def should_extract_candidates(session):
    state = update_candidate_extract_counters(session)
    return (
        state["user_turns_since_last_extract"] >= MEMORY_CANDIDATE_EXTRACT_INTERVAL_TURNS
        or state["chars_since_last_extract"] >= MEMORY_CANDIDATE_EXTRACT_MIN_CHARS
    )


def extract_candidate_memories(model_client, conversations, max_new_tokens=1200):
    """调用一次候选提取模型，并对结构化输出做严格校验。

    模型只负责根据对话生成 JSON；文件写入、时间戳和 checkpoint 更新都由
    runtime 完成。格式错误会抛出异常，由上层负责最多三次重试。
    """
    messages = []
    for conversation in conversations or []:
        messages.extend(_messages_for_model(conversation["messages"]))
    messages.append({"role": "user", "content": CANDIDATE_MEMORY_EXTRACT_REQUEST})
    response = model_client.complete(
        messages,
        max_new_tokens,
        tools=[],
        system=CANDIDATE_MEMORY_EXTRACT_SYSTEM_PROMPT,
        structured_output={
            "name": "memory_candidate_extraction",
            "schema": CANDIDATE_MEMORY_OUTPUT_SCHEMA,
        },
    )
    if getattr(response, "tool_calls", None):
        raise RuntimeError("candidate memory extractor returned tool calls")
    data = _json_from_text(getattr(response, "text", "") or "")
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise RuntimeError("candidate memory extractor returned invalid JSON shape")
    return _normalize_candidates(data.get("candidates", []))


def append_candidate_memories(workspace_root, candidates):
    ensure_long_term_memory(workspace_root)
    candidates = list(candidates or [])
    if not candidates:
        return {"path": "", "written_count": 0}
    path = candidate_log_path(workspace_root, date=now()[:10])
    path.parent.mkdir(parents=True, exist_ok=True)
    created_at = now()
    with path.open("a", encoding="utf-8") as handle:
        for item in candidates:
            payload = {
                "created_at": created_at,
                "type": item["type"],
                "memory": item["memory"],
                "evidence": item["evidence"],
                "confidence": item["confidence"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return {"path": str(path), "written_count": len(candidates)}


def append_manual_candidate(workspace_root, text):
    """把 /remember 输入写成高置信度候选记忆。

    用户手动要求记住的信息直接进入候选池，type 使用 unspecified，
    后续由 dream 根据内容分类到三类正式长期记忆。
    """
    value = str(text or "").strip()
    if not value:
        raise ValueError("memory text must not be empty")
    result = append_candidate_memories(
        workspace_root,
        [
            {
                "type": MANUAL_CANDIDATE_MEMORY_TYPE,
                "memory": clip(value, MEMORY_CANDIDATE_MAX_TEXT_CHARS),
                "evidence": "用户通过 /remember 要求记住这条信息。",
                "confidence": "high",
            }
        ],
    )
    return {
        "path": result["path"],
        "entry": value,
        "type": MANUAL_CANDIDATE_MEMORY_TYPE,
    }


def mark_candidate_extracted(session, conversations):
    state = normalize_candidate_extract_state((session or {}).get("memory_candidate_extract", {}))
    if conversations:
        state["last_extracted_conversation_id"] = str(conversations[-1]["id"])
    state["last_extracted_at"] = now()
    state["user_turns_since_last_extract"] = 0
    state["chars_since_last_extract"] = 0
    session["memory_candidate_extract"] = state
    return state


def _json_from_text(text):
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("candidate memory extractor returned empty output")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _normalize_candidates(raw_items):
    result = []
    seen = set()
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "")).strip()
        memory = clip(str(item.get("memory", "")).strip(), MEMORY_CANDIDATE_MAX_TEXT_CHARS)
        evidence = clip(str(item.get("evidence", "")).strip(), MEMORY_CANDIDATE_MAX_EVIDENCE_CHARS)
        confidence = str(item.get("confidence", "")).strip()
        if kind not in AUTO_CANDIDATE_MEMORY_TYPES or confidence not in {"high", "medium", "low"} or not memory:
            continue
        key = (kind, memory)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "type": kind,
                "memory": memory,
                "evidence": evidence,
                "confidence": confidence,
            }
        )
        if len(result) >= MEMORY_CANDIDATE_MAX_ITEMS:
            break
    return result


def _messages_for_model(messages):
    result = []
    for item in messages or []:
        role = item.get("role", "user")
        if role == "tool":
            result.append({"role": "user", "content": f"[tool:{item.get('name', '')}] {clip(item.get('content', ''), 1200)}"})
        elif role == "assistant" and item.get("tool_calls"):
            content = str(item.get("content", "") or "").strip()
            tool_calls = json.dumps(item.get("tool_calls") or [], ensure_ascii=False)
            result.append({"role": "assistant", "content": clip("\n".join(part for part in (content, f"[tool_calls] {tool_calls}") if part), 1200)})
        else:
            result.append({"role": role, "content": clip(item.get("content", ""), 2000)})
    return result


def _message_text_for_count(message):
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return str(message.get("content", "") or "") + json.dumps(message.get("tool_calls") or [], ensure_ascii=False)
    return str(message.get("content", "") or "")
