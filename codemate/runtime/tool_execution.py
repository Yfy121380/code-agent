# 工具执行总闸口。
# 本文件负责把模型返回的工具调用转换成一次受控执行：
# 参数校验、权限 gate、重复调用拦截、审批、执行、结果 metadata 和工作记忆更新。
# 具体工具行为仍在 codemate.tools 内部，本文件只处理 runtime 编排。

import re
import uuid
from datetime import datetime

from .. import tools as toolkit
from ..tools.file_state import has_current_file_state, record_file_state


class ToolExecutionMixin:
    def shell_analysis_metadata(self):
        analysis = getattr(self, "_last_shell_analysis", None)
        if analysis is None or not hasattr(analysis, "to_metadata"):
            return {}
        return analysis.to_metadata()

    def tool_risk_level(self, name, tool):
        if str(name).startswith("mcp__"):
            return "high"
        if name in toolkit.WEB_TOOL_NAMES:
            return "low"
        if name == "run_shell":
            analysis = getattr(self, "_last_shell_analysis", None)
            if analysis is not None and getattr(analysis, "kind", "") == "read":
                return "low"
        return "high" if tool["risky"] else "low"

    def tool_metadata_read_only(self, name, tool):
        if str(name).startswith("mcp__"):
            return False
        if name in toolkit.WEB_TOOL_NAMES:
            return True
        if name in {"list_files", "read_file", "grep"}:
            return True
        if name in {"todo_write", "todo_list", "skill_load", "skill_unload", "delegate", "review"}:
            return True
        if name in toolkit.PLAN_INTERACTION_TOOLS:
            return True
        if name == "run_shell":
            analysis = getattr(self, "_last_shell_analysis", None)
            return bool(analysis is not None and getattr(analysis, "kind", "") == "read")
        return False

    def tool_runtime_metadata(self, name, tool):
        metadata = {
            "risk_level": self.tool_risk_level(name, tool),
            "read_only": self.tool_metadata_read_only(name, tool),
        }
        if str(name).startswith("mcp__"):
            metadata.update(
                {
                    "mcp_server": tool.get("mcp_server", ""),
                    "mcp_tool": tool.get("mcp_tool", ""),
                }
            )
        return metadata

    def truncate_tool_result(self, result):
        """统一限制工具返回规模，防止单次工具调用把模型上下文撑爆。"""
        text = str(result)
        limit = toolkit.MAX_TOOL_RESULT_CHARS
        if len(text) <= limit:
            return text, {"tool_result_truncated": False, "tool_result_original_chars": len(text), "tool_result_returned_chars": len(text)}

        header = (
            f"Tool result truncated from {len(text)} chars to {limit} chars.\n"
            "Use narrower parameters, smaller ranges, or a more specific query if more detail is needed.\n\n"
        )
        marker = "\n\n... omitted middle content ...\n\n"
        available = max(0, limit - len(header) - len(marker))
        head_chars = available // 2
        tail_chars = available - head_chars
        truncated = f"{header}{text[:head_chars]}{marker}{text[-tail_chars:] if tail_chars else ''}"
        return truncated[:limit], {
            "tool_result_truncated": True,
            "tool_result_original_chars": len(text),
            "tool_result_returned_chars": len(truncated[:limit]),
            "tool_result_max_chars": limit,
        }

    def run_tool(self, name, args, current_tool_call_id=None):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        # 工具执行不是“直接调函数”，而是一条带护栏的流水线：
        # 工具是否存在 -> 参数是否合法 -> 是否重复调用 -> 是否通过审批
        # -> 真正执行 -> 更新记忆。
        self._last_shell_analysis = None
        self._last_tool_gate = None
        self._last_delegate_metadata = {}
        self._last_review_metadata = {}
        self._last_tool_result_content_blocks = []
        tool = self.tools.get(name)
        if tool is None:
            message = f"error: unknown tool '{name}'"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
                "risk_level": "high",
                "read_only": False,
            }
            return message
        try:
            # 参数合法性、路径硬边界和审批门禁统一在 validate_tool 中完成。
            gate = self.validate_tool(name, args)
            self._last_tool_gate = gate
        except toolkit.ToolPolicyError as exc:
            message = f"error: {exc}"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": exc.code,
                "security_event_type": exc.security_event_type,
                **self.tool_runtime_metadata(name, tool),
                **self.shell_analysis_metadata(),
            }
            return message
        except Exception as exc:
            message = f"error: invalid arguments for {name}: {exc}"
            security_event_type = "path_denied" if "path outside" in str(exc) or "path is sensitive" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
                **self.tool_runtime_metadata(name, tool),
                **self.shell_analysis_metadata(),
            }
            return message
        if self.repeated_tool_call(name, args, exclude_call_id=current_tool_call_id):
            message = f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
                **self.tool_runtime_metadata(name, tool),
                **self.shell_analysis_metadata(),
                **gate.to_metadata(),
            }
            return message
        asked_for_approval = gate.action == "ask"
        if asked_for_approval and not self.approval_decision(name, args, tool):
            message = f"error: approval denied for {name}"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "approval_denied",
                **self.tool_runtime_metadata(name, tool),
                **self.shell_analysis_metadata(),
                **gate.to_metadata(),
            }
            return message
        if not asked_for_approval:
            self.ui.tool_start(name, args, risk_level=self.tool_risk_level(name, tool))
        try:
            raw_output = toolkit.normalize_tool_output(tool["run"](args))
            result, truncation_metadata = self.truncate_tool_result(raw_output.content)
            content_blocks = list(raw_output.content_blocks or [])
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", result)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            delegate_metadata = dict(getattr(self, "_last_delegate_metadata", {}) or {})
            if name == "delegate" and delegate_metadata:
                delegate_status = str(delegate_metadata.get("delegate_status", "ok"))
                if delegate_status != "ok":
                    tool_status = delegate_status
                    tool_error_code = "delegate_failed"
            review_metadata = dict(getattr(self, "_last_review_metadata", {}) or {})
            if name == "review" and review_metadata:
                review_status = str(review_metadata.get("review_status", "ok"))
                if review_status != "ok":
                    tool_status = review_status
                    tool_error_code = "review_failed"

            if tool_status == "ok" and name in {"read_file", "write_file", "patch_file"}:
                record_file_state(self.session, self.root, args["path"])
            self._last_tool_result_content_blocks = content_blocks
            self._last_tool_result_metadata = {
                "tool_status": tool_status,
                "tool_error_code": tool_error_code,
                "security_event_type": "",
                **self.tool_runtime_metadata(name, tool),
                "workspace_fingerprint": self.workspace.fingerprint(),
                **self.shell_analysis_metadata(),
                **gate.to_metadata(),
                **delegate_metadata,
                **review_metadata,
                **dict(raw_output.metadata or {}),
                **truncation_metadata,
            }
            return result
        except Exception as exc:
            security_event_type = "path_denied" if "path outside" in str(exc) or "path is sensitive" in str(exc) else ""
            message = f"error: tool {name} failed: {exc}"
            self._last_tool_result_metadata = {
                "tool_status": "error",
                "tool_error_code": "tool_failed",
                "security_event_type": security_event_type,
                **self.tool_runtime_metadata(name, tool),
                "workspace_fingerprint": self.workspace.fingerprint(),
                **self.shell_analysis_metadata(),
                **gate.to_metadata(),
            }
            return message

    def recent_tool_calls(self, exclude_call_id=None):
        calls = []
        for item in self.session["history"]:
            if item.get("role") != "assistant":
                continue
            for call in item.get("tool_calls", []) or []:
                if exclude_call_id is not None and str(call.get("id", "")) == str(exclude_call_id):
                    continue
                calls.append({"name": call.get("name", ""), "args": dict(call.get("args", {}) or {})})
        return calls

    def repeated_tool_call(self, name, args, exclude_call_id=None):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 新 history 中以 assistant.tool_calls 代表模型的工具请求，tool 消息只记录结果。
        tool_calls = self.recent_tool_calls(exclude_call_id=exclude_call_id)
        if len(tool_calls) < 2:
            return False
        recent = tool_calls[-2:]
        return all(call["name"] == name and call["args"] == args for call in recent)

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        if bool(getattr(self, "is_plan_mode", lambda: False)()) and name not in self.active_tool_names("plan"):
            raise toolkit.ToolPolicyError(
                f"tool is not available in Plan Mode: {name}",
                code="plan_tool_unavailable",
                security_event_type="plan_tool_unavailable",
            )
        gate = toolkit.validate_tool(self, name, args)
        if self.approval_policy == "read_only" and not self.tool_metadata_read_only(name, self.tools.get(name, {"risky": True})):
            raise toolkit.ToolPolicyError(
                "tool is blocked in read-only approval mode",
                code="read_only_block",
                security_event_type="read_only_block",
            )
        if name in {"write_file", "patch_file"}:
            self.require_fresh_read_before_edit(name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")
        return gate

    def require_fresh_read_before_edit(self, name, args):
        path = self.path(args["path"])
        if name == "write_file" and not path.exists():
            return
        if not has_current_file_state(self.session, self.root, args["path"]):
            raise ValueError(
                "existing files must be read with read_file before editing; "
                "grep/list_files results are not enough"
            )

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_grep(self, args):
        return toolkit.tool_grep(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_skill_load(self, args):
        return toolkit.tool_skill_load(self, args)

    def tool_todo_list(self, args):
        return toolkit.tool_todo_list(self, args)

    def tool_skill_unload(self, args):
        return toolkit.tool_skill_unload(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def tool_review(self, args):
        return toolkit.tool_review(self, args)
