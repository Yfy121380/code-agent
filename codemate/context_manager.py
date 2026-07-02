"""Prompt/messages 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。runtime 使用结构化 messages；指标实验仍可
使用文本 prompt 视图来观察分层裁剪效果。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

from . import tools as toolkit


DEFAULT_TOTAL_BUDGET = 128000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 10000,
    "memory": 16000,
    "relevant_memory": 14000,
    "history": 88000,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 5000,
    "memory": 3000,
    "relevant_memory": 3000,
    "history": 20000,
}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3
OMITTED_TOOL_RESULT = "[tool result omitted due to context budget]"
OLD_TOOL_RESULT_CLEARED = "Old tool result content cleared."
MAX_RECENT_OBSERVATION_TOOL_RESULTS = 20


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


@dataclass
class MessageBuild:
    system: str
    messages: list[dict]
    metadata: dict


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        user_message = str(user_message)
        rendered, budgets, reduction_log, selected_notes = self._render_with_reduction(user_message, for_messages=False)
        prompt = self._assemble_prompt(rendered)
        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=self._section_texts(user_message),
        )
        return prompt, metadata

    def build_messages(self, user_message):
        user_message = str(user_message)
        rendered, budgets, reduction_log, selected_notes = self._render_with_reduction(user_message, for_messages=True)
        system, messages = self._assemble_messages(rendered, user_message)
        prompt_view = self._messages_prompt_view(system, messages)
        metadata = self._metadata(
            prompt=prompt_view,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=self._section_texts(user_message),
        )
        metadata["message_count"] = len(messages)
        metadata["system_chars"] = len(system)
        metadata["messages_chars"] = sum(len(str(message.get("content", ""))) for message in messages)
        return MessageBuild(system=system, messages=messages, metadata=metadata)

    def _render_with_reduction(self, user_message, for_messages):
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = self._section_texts(user_message, memory_enabled=memory_enabled)
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes, user_message=user_message)
            budgets = {section: rendered[section].budget for section in SECTION_ORDER if section != CURRENT_REQUEST_SECTION}
            return rendered, budgets, [], selected_notes

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, user_message=user_message)
        current_size = self._rendered_size(rendered, user_message, for_messages=for_messages)
        reduction_log = []

        while current_size > self.total_budget:
            overflow = current_size - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, user_message=user_message)
                current_size = self._rendered_size(rendered, user_message, for_messages=for_messages)
                reduced = True
                break
            if not reduced:
                break
        return rendered, budgets, reduction_log, selected_notes

    def _section_texts(self, user_message, memory_enabled=True):
        return {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Working memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

    def _rendered_size(self, rendered, user_message, for_messages):
        if not for_messages:
            return len(self._assemble_prompt(rendered))
        system, messages = self._assemble_messages(rendered, user_message)
        return len(self._messages_prompt_view(system, messages))

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, user_message=""):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = self._history_for_request(user_message)
        history_raw = self._raw_history_text(history)
        history_render = self._render_history_section(len(history_raw), user_message=user_message)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": history_render,
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {section: max(20, int(budget) // 4) for section, budget in self.section_budgets.items()}
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None, user_message=""):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0), user_message=user_message)
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=raw,
                details={"selected_notes": [], "rendered_notes": [], "selected_count": 0, "rendered_count": 0, "note_budget": 0},
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget, user_message=""):
        history = self._history_for_request(user_message)
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "messages": [],
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_tool_results": 0,
                    "cleared_old_tool_results": 0,
                },
            )

        groups = self._history_groups(history)
        selected_groups = []
        selected_body_len = 0
        seen_dedupe_keys = set()
        kept_observation_results = 0
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_tool_results": 0,
            "cleared_old_tool_results": 0,
        }
        transcript_header = "Transcript:\n"

        for group in reversed(groups):
            dedupe_keys, group_can_be_collapsed = self._dedupe_keys_for_group(group)
            if group_can_be_collapsed and all(key in seen_dedupe_keys for key in dedupe_keys):
                details["collapsed_duplicate_tool_results"] += 1
                continue

            group = copy.deepcopy(group)
            cleared_in_group = 0
            observation_results_in_group = 0
            if group.get("type") == "tool_interaction":
                assistant = group.get("messages", [{}])[0]
                calls_by_id = {str(call.get("id", "")): call for call in assistant.get("tool_calls", []) or []}
                for message in reversed(group.get("messages", [])[1:]):
                    if message.get("role") != "tool":
                        continue
                    call = calls_by_id.get(str(message.get("tool_call_id", "")), {})
                    name = str(message.get("name", "") or call.get("name", ""))
                    content = str(message.get("content", ""))
                    lowered = content.lstrip().lower()
                    ok_result = not (lowered.startswith("error:") or lowered.startswith("rejected:"))
                    compactable = ok_result and name in {"list_files", "read_file", "grep"}
                    if ok_result and name == "run_shell" and content.lstrip().startswith("exit_code: 0"):
                        try:
                            analysis = toolkit.analyze_shell_command(self.agent, (call.get("args") or {}).get("command", ""))
                            compactable = not analysis.blocked and analysis.kind == "read"
                        except Exception:
                            compactable = False
                    if not compactable:
                        continue
                    observation_results_in_group += 1
                    if kept_observation_results + observation_results_in_group > MAX_RECENT_OBSERVATION_TOOL_RESULTS:
                        message["content"] = OLD_TOOL_RESULT_CLEARED
                        cleared_in_group += 1

            group_messages = self._group_messages(group)
            group_rendered = self._render_history_messages(group_messages)
            group_body = group_rendered[len(transcript_header):] if group_rendered.startswith(transcript_header) else group_rendered
            candidate_body_len = len(group_body) if selected_body_len == 0 else len(group_body) + 1 + selected_body_len
            if len(transcript_header) + candidate_body_len <= budget:
                selected_groups.append(group)
                selected_body_len = candidate_body_len
                seen_dedupe_keys.update(dedupe_keys)
                kept_observation_results += observation_results_in_group
                details["cleared_old_tool_results"] += cleared_in_group
                continue

            if group.get("type") == "tool_interaction":
                separator = 1 if selected_body_len else 0
                available = max(0, budget - len(transcript_header) - selected_body_len - separator)
                clipped = self._clip_tool_group(group, available)
                clipped_messages = self._group_messages(clipped)
                clipped_rendered = self._render_history_messages(clipped_messages)
                clipped_body = clipped_rendered[len(transcript_header):] if clipped_rendered.startswith(transcript_header) else clipped_rendered
                candidate_body_len = len(clipped_body) if selected_body_len == 0 else len(clipped_body) + 1 + selected_body_len
                if len(transcript_header) + candidate_body_len <= budget or not selected_groups:
                    selected_groups.append(clipped)
                    selected_body_len = candidate_body_len
                    seen_dedupe_keys.update(dedupe_keys)
                    kept_observation_results += observation_results_in_group
                    details["cleared_old_tool_results"] += cleared_in_group
                break

            if not selected_groups:
                clipped = self._clip_text_group(group, max(20, budget - len("Transcript:\n")))
                selected_groups.append(clipped)
            break

        selected_messages = []
        for group in reversed(selected_groups):
            selected_messages.extend(self._group_messages(group))
        rendered = self._render_history_messages(selected_messages)
        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "messages": selected_messages,
                "rendered_entries": rendered.splitlines()[1:] if rendered.startswith("Transcript:") else rendered.splitlines(),
                **details,
            },
        )

    def _history_for_request(self, user_message):
        history = [copy.deepcopy(item) for item in getattr(self.agent, "session", {}).get("history", [])]
        if history and history[-1].get("role") == "user" and str(history[-1].get("content", "")) == str(user_message):
            history = history[:-1]
        return history

    def _history_groups(self, history):
        groups = []
        index = 0
        while index < len(history):
            item = history[index]
            role = item.get("role")
            if role == "assistant" and item.get("tool_calls"):
                calls = [dict(call or {}) for call in item.get("tool_calls") or []]
                call_ids = {str(call.get("id", "")) for call in calls}
                messages = [{"role": "assistant", "content": str(item.get("content", "")), "tool_calls": calls}]
                index += 1
                while index < len(history) and history[index].get("role") == "tool":
                    tool_item = dict(history[index])
                    if str(tool_item.get("tool_call_id", "")) not in call_ids:
                        break
                    messages.append(tool_item)
                    index += 1
                groups.append({"type": "tool_interaction", "messages": messages})
                continue
            if role == "tool":
                index += 1
                continue
            groups.append({"type": "message", "messages": [copy.deepcopy(item)]})
            index += 1
        return groups

    def _group_messages(self, group):
        return [copy.deepcopy(message) for message in group.get("messages", [])]

    def _dedupe_keys_for_group(self, group):
        keys = []
        if group.get("type") != "tool_interaction":
            return keys, False
        assistant = group.get("messages", [{}])[0]
        calls = assistant.get("tool_calls", []) or []
        for call in calls:
            name = call.get("name")
            args = call.get("args") or {}
            if name == "read_file":
                path = str(args.get("path", "")).strip()
                keys.append(("read_file", path, int(args.get("start", 1)), int(args.get("end", 200))))
            elif name == "grep":
                keys.append(
                    (
                        "grep",
                        str(args.get("pattern", "")),
                        str(args.get("path", ".")).strip() or ".",
                        str(args.get("mode", "content")),
                        int(args.get("before", 0)),
                        int(args.get("after", 0)),
                        int(args.get("context", 0)),
                    )
                )
            else:
                return keys, False
        return keys, bool(keys) and len(keys) == len(calls)

    def _clip_text_group(self, group, budget):
        clipped = copy.deepcopy(group)
        for message in clipped.get("messages", []):
            if message.get("role") in {"user", "assistant"} and not message.get("tool_calls"):
                message["content"] = _tail_clip(message.get("content", ""), budget)
        return clipped

    def _clip_tool_group(self, group, budget):
        clipped = copy.deepcopy(group)
        messages = clipped.get("messages", [])
        if len(messages) < 2:
            return clipped
        tool_messages = [message for message in messages[1:] if message.get("role") == "tool"]
        per_tool_budget = max(1, budget // max(1, len(tool_messages)))
        for tool_message in tool_messages:
            content = str(tool_message.get("content", ""))
            if per_tool_budget <= len(OMITTED_TOOL_RESULT) + 8:
                content = OMITTED_TOOL_RESULT
            else:
                content = _tail_clip(content, per_tool_budget)
            tool_message["content"] = content
        return clipped

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return self._render_history_messages(history)

    def _render_history_messages(self, messages):
        if not messages:
            return "Transcript:\n- empty"
        lines = ["Transcript:"]
        for item in messages:
            role = item.get("role")
            if role == "assistant" and item.get("tool_calls"):
                lines.append(f"[assistant:tool_calls] {json.dumps(item.get('tool_calls'), sort_keys=True, ensure_ascii=False)}")
            elif role == "tool":
                lines.append(f"[tool:{item.get('name', '')}] {item.get('tool_call_id', '')}")
                lines.append(str(item.get("content", "")))
            else:
                lines.append(f"[{role}] {item.get('content', '')}")
        return "\n".join(lines)

    def _assemble_prompt(self, rendered):
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _assemble_messages(self, rendered, user_message):
        context_content = "\n\n".join(
            [
                "This message is runtime context, not a new user request. Use it as background.",
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
            ]
        ).strip()
        messages = [{"role": "user", "content": context_content}]
        messages.extend(copy.deepcopy(rendered["history"].details.get("messages", [])))
        messages.append({"role": "user", "content": str(user_message)})
        return rendered["prefix"].rendered, messages

    def _messages_prompt_view(self, system, messages):
        lines = [system]
        for message in messages:
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                lines.append(f"[assistant:tool_calls] {json.dumps(message.get('tool_calls'), ensure_ascii=False, sort_keys=True)}")
            elif role == "tool":
                lines.append(f"[tool:{message.get('name', '')}] {message.get('content', '')}")
            else:
                lines.append(f"[{role}] {message.get('content', '')}")
        return "\n\n".join(lines).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_tool_results": int(rendered["history"].details.get("collapsed_duplicate_tool_results", 0)),
                "cleared_old_tool_results": int(rendered["history"].details.get("cleared_old_tool_results", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
