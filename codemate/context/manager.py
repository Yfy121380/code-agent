# 上下文管理主流程。
# 本文件负责根据当前 agent 状态构造模型请求上下文。
# 输入内容包括系统前缀、工作记忆、长期记忆、历史对话和当前请求。
# 同时负责按预算裁剪各层内容，并生成用于追踪和评估的 metadata。

from __future__ import annotations

import copy
import json

from .history import HistoryContextRenderer
from .types import (
    CURRENT_REQUEST_SECTION,
    DEFAULT_REDUCTION_ORDER,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_TOTAL_BUDGET,
    RELEVANT_MEMORY_LIMIT,
    SECTION_ORDER,
    MessageBuild,
    SectionRender,
    _tail_clip,
)

LONG_TERM_MEMORY_SOURCES = ("user_profile", "feedback_workflow", "project_context")


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
        self.history_renderer = HistoryContextRenderer(agent)

    def build(self, user_message):
        # 构造文本 prompt 形式的上下文。
        # 这个入口主要供指标实验、调试视图和不使用结构化 tool calling 的场景使用。
        # 返回的 metadata 会记录各层上下文长度、预算分配和裁剪过程，便于复盘。
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
        # 构造标准 messages 形式的模型输入。
        # system 承载稳定前缀，messages 承载运行时上下文和历史消息。
        # 返回结果同时包含 prompt 视图 metadata，用于保持文本 prompt 与 messages 的可比性。
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
        # 分层渲染和预算裁剪的主流程。
        # 先生成各层原始文本，再根据 feature flags 决定是否启用记忆和裁剪。
        # 当总长度超出预算时，按照 reduction_order 逐层压缩，直到满足预算或达到各层下限。
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
        if memory_enabled and relevant_memory_enabled:
            selected_notes = list(getattr(self.agent, "relevant_long_term_memory", []) or [])[:RELEVANT_MEMORY_LIMIT]

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
        memory_text = "Working memory:\n- disabled"
        if memory_enabled:
            if hasattr(self.agent, "prompt_memory_text"):
                memory_text = str(self.agent.prompt_memory_text())
            else:
                memory_text = str(self.agent.memory_text())
        skills_text = "Available skills:\n- none"
        if hasattr(self.agent, "available_skills_text"):
            skills_text = str(self.agent.available_skills_text())
        return {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "skills": skills_text,
            "memory": memory_text,
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

    def _rendered_size(self, rendered, user_message, for_messages):
        if not for_messages:
            return len(self._assemble_prompt(rendered))
        system, messages = self._assemble_messages(rendered, user_message)
        return len(self._messages_prompt_view(system, messages))

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, user_message=""):
        # 构造未裁剪的各层 SectionRender。
        # 这个路径用于关闭 context_reduction 的实验场景，需要保留完整原始长度。
        # 即使不做预算裁剪，history 仍会通过统一渲染器输出结构化 transcript。
        selected_notes = selected_notes or []
        relevant_raw, relevant_details = self._format_relevant_memory(selected_notes, note_budget=0)
        history = self.history_renderer.history_for_request(user_message)
        history_raw = self.history_renderer.raw_text(history)
        history_render = self.history_renderer.render(len(history_raw), user_message=user_message)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "skills": SectionRender(raw=section_texts["skills"], budget=len(section_texts["skills"]), rendered=section_texts["skills"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details=relevant_details,
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
        # 根据当前预算渲染每个上下文层。
        # prefix、working memory 采用尾部截断，relevant memory 使用按条目均分预算，
        # history 交给历史渲染器按 group 选择和裁剪，当前请求始终完整保留。
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "skills":
                rendered[section] = self._render_skills(section_texts[section], int(budget or 0))
            elif section == "history":
                rendered[section] = self.history_renderer.render(int(budget or 0), user_message=user_message)
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_skills(self, raw, budget):
        # Skills section 是可用能力目录，不承载当前任务状态。
        # 这里按条目均分 description 预算，尽量保留每个 skill name，避免 tail clip 把某些 skill 整条裁掉。
        if budget <= 0:
            return SectionRender(raw=raw, budget=budget, rendered="", details={"skill_items": [], "selected_count": 0, "rendered_count": 0, "description_budget": 0})
        lines = str(raw).splitlines()
        entries = []
        for line in lines[1:]:
            text = line.strip()
            if not text.startswith("- "):
                continue
            body = text[2:].strip()
            if not body or body == "none":
                continue
            name, separator, description = body.partition(":")
            entries.append({"name": name.strip(), "description": description.strip() if separator else ""})
        details = {
            "skill_items": list(entries),
            "selected_count": len(entries),
            "rendered_count": 0,
            "description_budget": 0,
        }
        if not entries:
            text = "Available skills:\n- none"
            return SectionRender(raw=raw, budget=budget, rendered=_tail_clip(text, budget), details=details)

        fixed_overhead = len("Available skills:\n") + sum(len(f"- {entry['name']}") + 1 for entry in entries)
        description_count = sum(1 for entry in entries if entry["description"])
        per_description_budget = max(0, (budget - fixed_overhead - (2 * description_count)) // max(1, description_count))
        while True:
            rendered_lines = ["Available skills:"]
            for entry in entries:
                if entry["description"] and per_description_budget > 0:
                    rendered_lines.append(f"- {entry['name']}: {_tail_clip(entry['description'], per_description_budget)}")
                else:
                    rendered_lines.append(f"- {entry['name']}")
            rendered = "\n".join(rendered_lines)
            if len(rendered) <= budget or per_description_budget <= 0:
                break
            per_description_budget -= 1
        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(rendered, budget)
        details["rendered_count"] = len(entries)
        details["description_budget"] = per_description_budget
        return SectionRender(raw=raw, budget=budget, rendered=rendered, details=details)

    def _render_relevant_memory(self, selected_notes, budget):
        # 渲染长期记忆召回结果。
        # 按三类长期记忆来源分组展示，让模型能区分用户偏好、工作流反馈和项目背景。
        # 预算仍然在整个 section 内统一控制，过长时先按每条记忆裁剪，再兜底截断。
        raw, raw_details = self._format_relevant_memory(selected_notes, note_budget=0)
        note_items = raw_details["note_items"]
        if not note_items:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=raw,
                details=raw_details,
            )

        grouped_sources = {item["source"] for item in note_items}
        fixed_overhead = len("Relevant memory:\n")
        for source in LONG_TERM_MEMORY_SOURCES:
            fixed_overhead += len(f"{source}:\n")
            fixed_overhead += 0 if source in grouped_sources else len("- none\n")
        fixed_overhead += len("- ") * len(note_items)
        per_note_budget = max(1, (max(0, budget - fixed_overhead) // len(note_items)))
        while True:
            rendered_items = [
                {"source": item["source"], "text": _tail_clip(item["text"], per_note_budget)}
                for item in note_items
            ]
            rendered, rendered_details = self._format_relevant_memory_items(rendered_items, per_note_budget)
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_details = dict(raw_details)
            rendered_details["rendered_notes"] = [rendered]
            rendered_details["rendered_count"] = 1
            rendered_details["note_budget"] = per_note_budget

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details=rendered_details,
        )

    def _format_relevant_memory(self, selected_notes, note_budget):
        note_items = []
        for note in selected_notes or []:
            source = str(note.get("source", "")).strip()
            text = str(note.get("text", "")).strip()
            if source not in LONG_TERM_MEMORY_SOURCES or not text:
                continue
            note_items.append({"source": source, "text": text})
        return self._format_relevant_memory_items(note_items, note_budget)

    def _format_relevant_memory_items(self, note_items, note_budget):
        grouped = {source: [] for source in LONG_TERM_MEMORY_SOURCES}
        for item in note_items:
            grouped[item["source"]].append(item["text"])
        lines = ["Relevant memory:"]
        rendered_notes = []
        for source in LONG_TERM_MEMORY_SOURCES:
            lines.append(f"{source}:")
            items = grouped[source]
            if not items:
                lines.append("- none")
                continue
            for item_text in items:
                lines.append(f"- {item_text}")
                rendered_notes.append(item_text)
        selected_texts = [item["text"] for item in note_items]
        return "\n".join(lines), {
            "note_items": list(note_items),
            "selected_notes": selected_texts,
            "rendered_notes": rendered_notes,
            "selected_count": len(selected_texts),
            "rendered_count": len(rendered_notes),
            "note_budget": note_budget,
            "group_counts": {source: len(grouped[source]) for source in LONG_TERM_MEMORY_SOURCES},
        }

    def _assemble_prompt(self, rendered):
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["skills"].rendered,
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
                rendered["skills"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
            ]
        ).strip()
        del user_message
        messages = [{"role": "user", "content": context_content}]
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

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        # 汇总本轮上下文构建的可观测指标。
        # metadata 不参与模型输入，只用于 trace、report、benchmark 和后续问题定位。
        # 这里保留每层 raw/rendered 字符数、预算变化、召回来源和 history 裁剪统计。
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
                "description_budget": int(rendered["skills"].details.get("description_budget", 0)),
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
