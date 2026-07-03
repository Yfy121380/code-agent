# 历史上下文处理。
# 本文件负责将会话历史组织成可发送给模型的结构化消息。
# 在预算受限时优先保留最新对话和有效工具观察结果。
# 同时处理重复只读工具结果折叠、旧工具结果清理和 transcript 渲染。

import copy
import json

from .. import tools as toolkit
from .types import (
    MAX_RECENT_OBSERVATION_TOOL_RESULTS,
    OLD_TOOL_RESULT_CLEARED,
    OMITTED_TOOL_RESULT,
    SectionRender,
    _tail_clip,
)


class HistoryContextRenderer:
    def __init__(self, agent):
        self.agent = agent

    def render(self, budget, user_message=""):
        # 按预算渲染历史上下文。
        # 历史以 group 为单位从新到旧选择，避免 assistant tool_call 和对应 tool result 被拆散。
        # 对可重复的只读工具结果做去重，并把过旧的大段观察结果替换为清理占位符。
        history = self.history_for_request(user_message)
        raw = self.raw_text(history)
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

        groups = self.history_groups(history)
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
            dedupe_keys, group_can_be_collapsed = self.dedupe_keys_for_group(group)
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

            group_messages = self.group_messages(group)
            group_rendered = self.render_messages(group_messages)
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
                clipped = self.clip_tool_group(group, available)
                clipped_messages = self.group_messages(clipped)
                clipped_rendered = self.render_messages(clipped_messages)
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
                clipped = self.clip_text_group(group, max(20, budget - len("Transcript:\n")))
                selected_groups.append(clipped)
            break

        selected_messages = []
        for group in reversed(selected_groups):
            selected_messages.extend(self.group_messages(group))
        rendered = self.render_messages(selected_messages)
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

    def history_for_request(self, user_message):
        # 读取当前 session 中已经记录的历史消息。
        # 当前请求本身应当已经作为 user message 存在于 history 中，这里不再额外追加。
        # 返回深拷贝，保证后续裁剪和清理不会改写原始 session history。
        del user_message
        return [copy.deepcopy(item) for item in getattr(self.agent, "session", {}).get("history", [])]

    def history_groups(self, history):
        # 将扁平 history 切分为裁剪单元。
        # 普通 user/assistant 消息单独成组；assistant tool_calls 与后续对应 tool result 绑定成组。
        # 这样预算裁剪时不会留下孤立的 tool result 或缺少结果的 tool_call。
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

    def group_messages(self, group):
        return [copy.deepcopy(message) for message in group.get("messages", [])]

    def dedupe_keys_for_group(self, group):
        # 计算只读工具交互的去重键。
        # read_file 和 grep 只有在路径、范围、模式和上下文参数完全一致时才视为重复。
        # 其他工具不参与折叠，避免把有副作用或语义不明的调用误删。
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

    def clip_text_group(self, group, budget):
        clipped = copy.deepcopy(group)
        for message in clipped.get("messages", []):
            if message.get("role") in {"user", "assistant"} and not message.get("tool_calls"):
                message["content"] = _tail_clip(message.get("content", ""), budget)
        return clipped

    def clip_tool_group(self, group, budget):
        # 在单个工具交互 group 超出剩余预算时裁剪 tool result。
        # assistant tool_call 结构保持完整，只缩短对应 tool 消息内容。
        # 如果预算过小，则用固定占位文本表示结果因上下文预算被省略。
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

    def raw_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return self.render_messages(history)

    def render_messages(self, messages):
        # 将结构化历史消息渲染为 transcript 文本。
        # 这个文本用于 prompt 视图、预算估算和 metadata 调试，不替代真正发送给模型的 messages。
        # tool_call 和 tool result 使用明确标记，便于观察裁剪后结构是否仍然完整。
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


