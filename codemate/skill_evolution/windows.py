"""Build bounded Skill-extraction windows from the durable transcript."""

from __future__ import annotations

import json
from collections import OrderedDict


MAX_WINDOW_CONVERSATIONS = 5
MAX_WINDOW_CHARS = 50_000
MAX_TOOL_ARGUMENT_CHARS = 600
MAX_TOOL_RESULT_CHARS = 1_200


def _strict_clip(value, limit):
    """Return a readable summary whose total length never exceeds ``limit``."""
    text = str(value or "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _json_summary(value, limit):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value or "")
    return _strict_clip(text, limit)


def _conversation_is_complete(messages):
    """A Skill window may only use a turn with a persisted terminal answer."""
    return any(
        item.get("role") == "assistant" and str(item.get("kind") or "") == "final"
        for item in messages
    )


def _group_complete_conversations(transcript):
    grouped = OrderedDict()
    for raw_item in transcript or []:
        if not isinstance(raw_item, dict):
            continue
        conversation_id = str(raw_item.get("conversation_id") or "")
        if not conversation_id:
            continue
        grouped.setdefault(conversation_id, []).append(raw_item)
    return [
        (conversation_id, messages)
        for conversation_id, messages in grouped.items()
        if _conversation_is_complete(messages)
    ]


def _project_conversation(conversation_id, messages):
    """Separate readable dialogue from bounded tool observations for extraction."""
    dialogue = []
    calls = []
    results = {}
    orphan_results = []

    for item in messages:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        if role in {"user", "assistant"} and content.strip():
            dialogue.append(
                {
                    "role": role,
                    "kind": str(item.get("kind") or ""),
                    "content": content,
                }
            )
        for raw_call in item.get("tool_calls") or []:
            if isinstance(raw_call, dict):
                calls.append(raw_call)
        if role == "tool":
            call_id = str(item.get("tool_call_id") or "")
            if call_id:
                results[call_id] = item
            else:
                orphan_results.append(item)

    tool_events = []
    loaded_skill_names = []
    paired_result_ids = set()
    for call in calls:
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if name == "skill_load":
            if call_id:
                paired_result_ids.add(call_id)
            skill_name = str(args.get("name") or "").strip()
            if skill_name:
                loaded_skill_names.append(skill_name)
            continue
        result = results.get(call_id)
        if result is not None:
            paired_result_ids.add(call_id)
            result_text = str(result.get("content") or "")
        else:
            result_text = "result was not recorded"
        tool_events.append(
            {
                "name": name,
                "tool_call_id": call_id,
                "arguments": _json_summary(args, MAX_TOOL_ARGUMENT_CHARS),
                "result": _strict_clip(result_text, MAX_TOOL_RESULT_CHARS),
            }
        )
    for call_id, result in results.items():
        if call_id in paired_result_ids:
            continue
        if str(result.get("name") or "") == "skill_load":
            continue
        tool_events.append(
            {
                "name": str(result.get("name") or ""),
                "tool_call_id": call_id,
                "arguments": "",
                "result": _strict_clip(
                    result.get("content") or "", MAX_TOOL_RESULT_CHARS
                ),
            }
        )
    for result in orphan_results:
        tool_events.append(
            {
                "name": str(result.get("name") or ""),
                "tool_call_id": "",
                "arguments": "",
                "result": _strict_clip(
                    result.get("content") or "", MAX_TOOL_RESULT_CHARS
                ),
            }
        )

    projected = {
        "id": conversation_id,
        "messages": dialogue,
        "tool_events": tool_events,
    }
    return projected, list(dict.fromkeys(loaded_skill_names))


def _projected_chars(conversation):
    return len(json.dumps(conversation, ensure_ascii=False, sort_keys=True))


def _loaded_skill_references(
    selected,
    loaded_names_by_conversation,
    available_skills,
    current_loaded_skills,
    focus_conversation_id,
):
    available = {
        str(item.get("name") or ""): item
        for item in available_skills or []
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    current = {
        str(item.get("name") or ""): item
        for item in current_loaded_skills or []
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    references = []
    for conversation in selected:
        conversation_id = conversation["id"]
        for name in loaded_names_by_conversation.get(conversation_id, []):
            source = (
                current.get(name, {})
                if conversation_id == focus_conversation_id
                else {}
            )
            metadata = source or available.get(name, {})
            references.append(
                {
                    "conversation_id": conversation_id,
                    "name": name,
                    "description": str(metadata.get("description") or ""),
                    "when_to_use": str(metadata.get("when_to_use") or ""),
                    "source": str(
                        metadata.get("source") or metadata.get("scope") or ""
                    ),
                }
            )
    return references


def build_pending_window(
    transcript,
    focus_conversation_id,
    *,
    available_skills=None,
    current_loaded_skills=None,
    session_id="",
):
    """Select the latest complete turns, keeping the threshold-crossing turn whole."""
    focus_conversation_id = str(focus_conversation_id or "")
    complete = _group_complete_conversations(transcript)
    focus_index = next(
        (
            index
            for index, (conversation_id, _messages) in enumerate(complete)
            if conversation_id == focus_conversation_id
        ),
        None,
    )
    if focus_index is None:
        return None

    projected_reversed = []
    loaded_names_by_conversation = {}
    total_chars = 0
    for conversation_id, messages in reversed(complete[: focus_index + 1]):
        projected, loaded_names = _project_conversation(conversation_id, messages)
        projected_reversed.append(projected)
        loaded_names_by_conversation[conversation_id] = loaded_names
        total_chars += _projected_chars(projected)
        if (
            len(projected_reversed) >= MAX_WINDOW_CONVERSATIONS
            or total_chars >= MAX_WINDOW_CHARS
        ):
            break

    selected = list(reversed(projected_reversed))
    focus = selected[-1]
    return {
        "focus_conversation": focus,
        "supporting_conversations": selected[:-1],
        "next_user_feedback": "",
        "loaded_skill_references": _loaded_skill_references(
            selected,
            loaded_names_by_conversation,
            available_skills,
            current_loaded_skills,
            focus_conversation_id,
        ),
        "source_char_count": total_chars,
        "session_id": str(session_id or ""),
    }


def flatten_window_messages(window):
    """Return dialogue-only messages for compact provenance and replay support."""
    conversations = [
        *list(window.get("supporting_conversations") or []),
        dict(window.get("focus_conversation") or {}),
    ]
    messages = []
    for conversation in conversations:
        for item in conversation.get("messages") or []:
            if isinstance(item, dict):
                messages.append(
                    {
                        "role": str(item.get("role") or ""),
                        "content": str(item.get("content") or ""),
                    }
                )
    feedback = str(window.get("next_user_feedback") or "").strip()
    if feedback:
        messages.append({"role": "user", "content": feedback})
    return messages
