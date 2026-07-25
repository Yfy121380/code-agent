# 上下文管理主流程。
# 本文件负责把系统前缀、技能列表、工作记忆、长期记忆、历史摘要和 recent history
# 组装成模型请求。这里不再做旧版分层预算裁剪；history 的规模由 compact 机制控制，
# 其他上下文层由各自的生成逻辑限制大小。

from __future__ import annotations

import copy
import json

from .history import HistoryContextRenderer
from .types import (
    CURRENT_REQUEST_SECTION,
    HISTORY_SUMMARY_SECTION,
    LONG_TERM_MEMORY_SOURCES,
    RELEVANT_MEMORY_LIMIT,
    SECTION_ORDER,
    MessageBuild,
    SectionRender,
)


class ContextManager:
    def __init__(self, agent):
        self.agent = agent
        self.history_renderer = HistoryContextRenderer(agent)

    def build(self, user_message):
        user_message = str(user_message)
        rendered, selected_notes = self._render_sections(user_message)
        prompt = self._assemble_prompt(rendered)
        metadata = self._metadata(prompt=prompt, rendered=rendered, selected_notes=selected_notes, user_message=user_message)
        return prompt, metadata

    def build_messages(self, user_message):
        user_message = str(user_message)
        rendered, selected_notes = self._render_sections(user_message)
        system, messages = self._assemble_messages(rendered)
        prompt_view = self._messages_prompt_view(system, messages)
        metadata = self._metadata(prompt=prompt_view, rendered=rendered, selected_notes=selected_notes, user_message=user_message)
        metadata["message_count"] = len(messages)
        metadata["system_chars"] = len(system)
        metadata["messages_chars"] = sum(len(str(message.get("content", ""))) for message in messages)
        return MessageBuild(system=system, messages=messages, metadata=metadata)

    def _render_sections(self, user_message):
        # 统一渲染上下文各层。prefix 作为 system，skills/memory/relevant_memory
        # 作为 runtime context，history_summary 和 history 保持独立消息顺序。
        memory_enabled = True
        relevant_memory_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")

        memory_text = "Working memory:\n- disabled"
        if memory_enabled:
            if hasattr(self.agent, "prompt_memory_text"):
                memory_text = str(self.agent.prompt_memory_text())
            else:
                memory_text = str(self.agent.memory_text())

        skills_text = "Available skills:\n- none"
        if hasattr(self.agent, "available_skills_text"):
            skills_text = str(self.agent.available_skills_text())

        selected_notes = []
        if memory_enabled and relevant_memory_enabled:
            selected_notes = list(getattr(self.agent, "relevant_long_term_memory", []) or [])[:RELEVANT_MEMORY_LIMIT]

        relevant_raw, relevant_details = self._format_relevant_memory(selected_notes)
        history_summary_text = self._history_summary_text()
        history_render = self.history_renderer.render(user_message)
        rendered = {
            "prefix": SectionRender(raw=str(getattr(self.agent, "prefix", "")), budget=0, rendered=str(getattr(self.agent, "prefix", "")), details={}),
            "skills": SectionRender(raw=skills_text, budget=0, rendered=skills_text, details=self._skills_details(skills_text)),
            "memory": SectionRender(raw=memory_text, budget=0, rendered=memory_text, details={}),
            "relevant_memory": SectionRender(raw=relevant_raw, budget=0, rendered=relevant_raw, details=relevant_details),
            HISTORY_SUMMARY_SECTION: SectionRender(raw=history_summary_text, budget=0, rendered=history_summary_text, details={"has_summary": bool(history_summary_text.strip())}),
            "history": history_render,
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=f"Current user request:\n{user_message}",
                budget=0,
                rendered=f"Current user request:\n{user_message}",
                details={},
            ),
        }
        return rendered, selected_notes

    def _history_summary_text(self):
        summary = str(getattr(self.agent, "session", {}).get("history_summary", "") or "").strip()
        if not summary:
            return ""
        if hasattr(self.agent, "wrap_history_summary"):
            return self.agent.wrap_history_summary(summary)
        return f"Summary:\n{summary}"

    def _skills_details(self, text):
        entries = []
        for line in str(text).splitlines()[1:]:
            item = line.strip()
            if item.startswith("- ") and item != "- none":
                entries.append(item[2:])
        return {
            "selected_count": len(entries),
            "rendered_count": len(entries),
            "description_budget": 0,
        }

    def _format_relevant_memory(self, selected_notes):
        note_items = []
        for note in selected_notes or []:
            source = str(note.get("source", "")).strip()
            text = str(note.get("text", "")).strip()
            created_at = str(note.get("created_at", "") or "").strip()
            reason = str(note.get("reason", "") or "").strip()
            if source not in LONG_TERM_MEMORY_SOURCES or not text:
                continue
            note_items.append({"source": source, "created_at": created_at, "text": text, "reason": reason})

        grouped = {source: [] for source in LONG_TERM_MEMORY_SOURCES}
        for item in note_items:
            grouped[item["source"]].append(item)
        lines = ["Relevant memory:"]
        rendered_notes = []
        for source in LONG_TERM_MEMORY_SOURCES:
            lines.append(f"{source}:")
            items = grouped[source]
            if not items:
                lines.append("- none")
                continue
            for item in items:
                text = item["text"]
                created_at = item["created_at"]
                rendered = f"[{created_at}] {text}" if created_at else text
                lines.append(f"- {rendered}")
                rendered_notes.append(rendered)
        selected_texts = [item["text"] for item in note_items]
        return "\n".join(lines), {
            "note_items": note_items,
            "selected_notes": selected_texts,
            "selected_created_at": [item["created_at"] for item in note_items],
            "selected_reasons": [item["reason"] for item in note_items],
            "rendered_notes": rendered_notes,
            "selected_count": len(selected_texts),
            "rendered_count": len(rendered_notes),
            "group_counts": {source: len(grouped[source]) for source in LONG_TERM_MEMORY_SOURCES},
        }

    def _assemble_prompt(self, rendered):
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["skills"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered[HISTORY_SUMMARY_SECTION].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _assemble_messages(self, rendered):
        context_content = "\n\n".join(
            [
                "This message is runtime context, not a new user request. Use it as background.",
                rendered["skills"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
            ]
        ).strip()
        messages = [{"role": "user", "content": context_content}]
        if rendered[HISTORY_SUMMARY_SECTION].details.get("has_summary"):
            messages.append({"role": "user", "content": rendered[HISTORY_SUMMARY_SECTION].rendered})
        messages.extend(copy.deepcopy(rendered["history"].details.get("messages", [])))
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

    def _metadata(self, prompt, rendered, selected_notes, user_message):
        # metadata 只用于 trace、/budget 和测试观察，不参与模型输入。
        sections = {}
        for section in SECTION_ORDER:
            sections[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": None,
                "rendered_chars": rendered[section].rendered_chars,
            }
        return {
            "prompt_chars": len(prompt),
            "section_order": list(SECTION_ORDER),
            "sections": sections,
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_created_at": [str(note.get("created_at", "") or "").strip() for note in selected_notes],
                "selected_reasons": [str(note.get("reason", "") or "").strip() for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "long_term")).strip() or "long_term" for note in selected_notes],
                "retrieval_status": str(getattr(self.agent, "long_term_memory_status", "not_run")),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "skills": {
                "raw_chars": rendered["skills"].raw_chars,
                "rendered_chars": rendered["skills"].rendered_chars,
                "selected_count": int(rendered["skills"].details.get("selected_count", 0)),
                "rendered_count": int(rendered["skills"].details.get("rendered_count", 0)),
                "description_budget": 0,
            },
            "history_summary": {
                "raw_chars": rendered[HISTORY_SUMMARY_SECTION].raw_chars,
                "rendered_chars": rendered[HISTORY_SUMMARY_SECTION].rendered_chars,
                "has_summary": bool(rendered[HISTORY_SUMMARY_SECTION].details.get("has_summary")),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
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
