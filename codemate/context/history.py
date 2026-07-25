# 历史上下文处理。
# 本文件负责把 session history 组织成稳定的消息组，保证 tool_call/tool_result
# 不会被拆散；同时提供 prompt 渲染、只读工具观察结果清理，以及 history compact
# 所需的 recent/older 切分能力。

from __future__ import annotations

import copy
import json

from ..tools.constants import WEB_TOOL_NAMES
from ..workspace import clip
from .types import (
    MAX_RECENT_OBSERVATION_TOOL_RESULTS,
    OLD_TOOL_RESULT_CLEARED,
    RECENT_HISTORY_MIN_CHARS,
    RECENT_HISTORY_MIN_MESSAGES,
    SectionRender,
)


class HistoryContextRenderer:
    def __init__(self, agent):
        self.agent = agent

    def render(self, user_message=""):
        # 渲染当前保留的 recent history。
        # 渲染前会对重复只读工具调用做折叠，并清理过旧的只读工具结果。
        del user_message
        history = self.history_for_request()
        raw = self.raw_text(history)
        prepared_groups, details = self.prepare_groups(self.history_groups(history))
        messages = []
        for group in prepared_groups:
            messages.extend(self.group_messages(group))
        rendered = self.render_messages(messages)
        return SectionRender(
            raw=raw,
            budget=len(rendered),
            rendered=rendered,
            details={
                "messages": messages,
                "rendered_entries": rendered.splitlines()[1:] if rendered.startswith("Transcript:") else rendered.splitlines(),
                **details,
            },
        )

    def split_for_compaction(self, min_messages=RECENT_HISTORY_MIN_MESSAGES, min_chars=RECENT_HISTORY_MIN_CHARS):
        """把历史切成待压缩旧消息和需要原样保留的新消息。

        recent 选择以 group 为单位从新到旧推进，满足最小消息数或最小字符数后停止。
        这样既保留足够近的上下文，又不会留下孤立 tool result。
        """
        groups = self.history_groups(self.history_for_request())
        if not groups:
            return {
                "history_to_compact": [],
                "recent_history": [],
                "history_to_compact_groups": [],
                "recent_groups": [],
            }

        recent_reversed = []
        message_count = 0
        char_count = 0
        for group in reversed(groups):
            recent_reversed.append(group)
            group_messages = self.group_messages(group)
            message_count += len(group_messages)
            char_count += len(self.render_messages(group_messages))
            if message_count >= int(min_messages) or char_count >= int(min_chars):
                break

        recent_groups = list(reversed(recent_reversed))
        older_groups = groups[: max(0, len(groups) - len(recent_groups))]
        recent_dedupe_keys = set()
        for group in recent_groups:
            keys, group_can_be_collapsed = self.dedupe_keys_for_group(group)
            if group_can_be_collapsed:
                recent_dedupe_keys.update(keys)
        filtered_older_groups = []
        for group in older_groups:
            keys, group_can_be_collapsed = self.dedupe_keys_for_group(group)
            if group_can_be_collapsed and keys and all(key in recent_dedupe_keys for key in keys):
                continue
            filtered_older_groups.append(group)
        return {
            "history_to_compact": self.groups_to_messages(filtered_older_groups, prepare=True),
            "recent_history": self.groups_to_messages(recent_groups, prepare=False),
            "history_to_compact_groups": filtered_older_groups,
            "recent_groups": recent_groups,
        }

    def recent_messages_for_retrieval(self, max_messages=10, tool_result_chars=300):
        """为长期记忆召回提供轻量 recent history。

        召回只需要理解当前对话方向，不需要完整工具输出。这里按 group 从后往前取，
        assistant tool_calls 和紧随其后的 tool results 保持在一起，避免后续模型适配时
        出现孤立 tool_result；工具结果内容只保留短摘要，防止召回请求被观察数据撑大。
        """
        selected_reversed = []
        message_count = 0
        for group in reversed(self.history_groups(self.history_for_request())):
            messages = self.group_messages(group)
            if not messages:
                continue
            selected_reversed.append(group)
            message_count += len(messages)
            if message_count >= int(max_messages):
                break

        result = []
        for group in reversed(selected_reversed):
            for message in self.group_messages(group):
                item = copy.deepcopy(message)
                if item.get("role") == "tool":
                    item["content"] = clip(item.get("content", ""), int(tool_result_chars))
                result.append(item)
        return result

    def history_for_request(self):
        return [copy.deepcopy(item) for item in getattr(self.agent, "session", {}).get("history", [])]

    def history_groups(self, history):
        # 将扁平 history 切成裁剪/压缩单元。
        # assistant tool_calls 会和后续同 id 的 tool results 绑定成一个 group。
        groups = []
        index = 0
        while index < len(history):
            item = history[index]
            role = item.get("role")
            if role == "assistant" and item.get("tool_calls"):
                calls = [dict(call or {}) for call in item.get("tool_calls") or []]
                call_ids = {str(call.get("id", "")) for call in calls}
                assistant_message = {
                    "role": "assistant",
                    "kind": str(item.get("kind", "tool_calls") or "tool_calls"),
                    "content": str(item.get("content", "")),
                    "tool_calls": calls,
                }
                messages = [assistant_message]
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

    def prepare_groups(self, groups):
        # 对历史 group 做上下文级清理：重复只读工具只保留最新一次；
        # 成功的本地读取工具和网络读取工具结果只保留最新若干条。
        selected_reversed = []
        seen_dedupe_keys = set()
        kept_observation_results = 0
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_tool_results": 0,
            "cleared_old_tool_results": 0,
        }
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
                    if not self.is_compactable_observation(name, call, message):
                        continue
                    observation_results_in_group += 1
                    if kept_observation_results + observation_results_in_group > MAX_RECENT_OBSERVATION_TOOL_RESULTS:
                        message["content"] = OLD_TOOL_RESULT_CLEARED
                        cleared_in_group += 1

            selected_reversed.append(group)
            seen_dedupe_keys.update(dedupe_keys)
            kept_observation_results += observation_results_in_group
            details["cleared_old_tool_results"] += cleared_in_group

        return list(reversed(selected_reversed)), details

    def is_compactable_observation(self, name, call, message):
        content = str(message.get("content", ""))
        lowered = content.lstrip().lower()
        ok_result = not (lowered.startswith("error:") or lowered.startswith("rejected:"))
        if not ok_result:
            return False
        if name in {"list_files", "read_file", "grep"} or name in WEB_TOOL_NAMES:
            return True
        return False

    def groups_to_messages(self, groups, prepare=False):
        selected_groups = groups
        if prepare:
            selected_groups, _details = self.prepare_groups(groups)
        messages = []
        for group in selected_groups:
            messages.extend(self.group_messages(group))
        return messages

    def group_messages(self, group):
        return [copy.deepcopy(message) for message in group.get("messages", [])]

    def dedupe_keys_for_group(self, group):
        # 只读工具去重要求工具名和关键参数完全一致。
        # 任何有副作用或语义不明的工具都不参与折叠。
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
                if args.get("read_all", False):
                    keys.append(("read_file", path, "all"))
                else:
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
            elif name in WEB_TOOL_NAMES:
                keys.append((name, json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))))
            else:
                return keys, False
        return keys, bool(keys) and len(keys) == len(calls)

    def raw_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return self.render_messages(history)

    def render_messages(self, messages):
        # 文本 transcript 只用于调试、预算展示和 prompt 视图。
        # 真实模型请求仍使用结构化 messages，避免丢失 tool_call 关系。
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
