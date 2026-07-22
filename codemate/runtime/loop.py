# Agent 主循环。
# 本文件负责一次 ask() 的完整执行过程：记录用户请求、构建 messages、
# 调模型、执行工具、写 trace/task_state，并在 final 或停止条件出现时结束。
# 它只编排流程，不实现具体工具、审批规则或记忆文件格式。

import time

from ..storage import TaskState
from ..workspace import clip, now


class RuntimeLoopMixin:
    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把“用户提一个请求”扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；主 agent 不限制工具步数，delegate/dream
          这类受控子流程仍会在达到步数上限时返回停止原因

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 codemate 是怎么“从一句话跑成一个 agent 流程”的，
        这里就是最关键的入口。
        """
        # 1. 登记本次 ask：先把用户请求写入 session，再创建 run 工件。
        run_started_at = time.monotonic()
        self.memory.set_task_summary(user_message)
        self.record({"role": "user", "content": user_message, "created_at": now()})
        self.expire_process_notes()
        task_state = TaskState.create(run_id=self.new_run_id(), task_id=self.new_task_id(), user_request=user_message)
        self.current_task_state = task_state
        self.current_run_dir = self.run_store.start_run(task_state)
        self.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        self.retrieve_long_term_memory_for_request(user_message, task_state)

        tool_steps = 0
        attempts = 0
        limit_steps = not (self.runtime_mode == "agent" and self.depth == 0)
        max_attempts = max(self.max_steps * 3, self.max_steps + 4) if limit_steps else None

        # 主循环按“感知 -> 决策 -> 行动 -> 记录”推进：
        # 重新组 prompt，调用模型，执行工具或接收 final，再把结果写回状态。
        while (not limit_steps or tool_steps < self.max_steps) and (max_attempts is None or attempts < max_attempts):
            attempts += 1
            task_state.record_attempt()
            self.run_store.write_task_state(task_state)
            system, messages, prompt_metadata = self._build_messages_and_metadata(user_message)
            prompt_metadata["context_budget"] = self.context_budget_status()
            if prompt_metadata["context_budget"].get("compact_needed"):
                compact_result = self.compact_history(reason="auto", task_state=task_state)
                if compact_result.get("status") == "error":
                    final = f"History compaction failed: {compact_result.get('reason', 'unknown error')}"
                    task_state.stop_retry_limit(final)
                    self.record({"role": "assistant", "content": final, "created_at": now()})
                    self.run_store.write_task_state(task_state)
                    self.emit_trace(
                        task_state,
                        "run_finished",
                        {
                            "status": task_state.status,
                            "stop_reason": task_state.stop_reason,
                            "final_answer": final,
                            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                        },
                    )
                    self.ui.final_answer(final)
                    return final
                if compact_result.get("status") == "ok":
                    system, messages, prompt_metadata = self._build_messages_and_metadata(user_message)
                    prompt_metadata["context_budget"] = self.context_budget_status()
            self.emit_trace(
                task_state,
                "prompt_build",
                {
                    "prompt_metadata": prompt_metadata,
                    "system": system,
                    "messages": messages[0],
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(self.model_client, "supports_prompt_cache", False):
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            self.ui.model_start()
            response = self.model_client.complete(
                messages,
                self.max_new_tokens,
                tools=self.model_tools(),
                system=system,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            response_metadata = dict(getattr(response, "metadata", {}) or {})
            completion_metadata.update(response_metadata)
            token_usage = self.update_token_usage_from_model(completion_metadata)
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            prompt_metadata["token_usage"] = token_usage
            prompt_metadata["context_budget"] = self.context_budget_status()
            self.last_prompt_metadata = prompt_metadata
            kind = getattr(response, "kind", "final")
            self.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "tool_call_count": len(getattr(response, "tool_calls", []) or []),
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            self.ui.model_end(kind=kind, metadata=completion_metadata)

            if kind == "commentary":
                commentary = str(getattr(response, "text", "") or "").strip()
                if not commentary:
                    self.record(
                        {
                            "role": "assistant",
                            "content": "Runtime notice: model returned an empty commentary message.",
                            "created_at": now(),
                        }
                    )
                    self.run_store.write_task_state(task_state)
                    continue
                self.ui.commentary(commentary)
                self.record({"role": "assistant", "kind": "commentary", "content": commentary, "created_at": now()})
                self.run_store.write_task_state(task_state)
                continue

            if kind == "tool_calls":
                calls = list(getattr(response, "tool_calls", []) or [])
                if not calls:
                    self.record(
                        {
                            "role": "assistant",
                            "content": "Runtime notice: model returned an empty tool call list.",
                            "created_at": now(),
                        }
                    )
                    self.run_store.write_task_state(task_state)
                    continue
                calls_to_execute = calls
                if limit_steps:
                    calls_to_execute = calls[: max(0, self.max_steps - tool_steps)]
                commentary = str(getattr(response, "text", "") or "")
                if commentary.strip():
                    self.ui.commentary(commentary.strip())
                self.record(
                    {
                        "role": "assistant",
                        "kind": "tool_calls",
                        "content": commentary,
                        "tool_calls": [call.to_dict() for call in calls_to_execute],
                        "created_at": now(),
                    }
                )
                for call in calls_to_execute:
                    tool_steps += 1
                    name = call.name
                    args = dict(call.args or {})
                    task_state.record_tool(name)
                    tool_started_at = time.monotonic()
                    result = self.run_tool(name, args, current_tool_call_id=call.id)
                    tool_result_tokens_added = self.add_tool_result_token_estimate(result)
                    self.ui.tool_result(name, args, result, metadata=dict(self._last_tool_result_metadata or {}))
                    self.record(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": name,
                            "content": result,
                            "created_at": now(),
                        }
                    )
                    self.run_store.write_task_state(task_state)
                    self.emit_trace(
                        task_state,
                        "tool_executed",
                        {
                            "name": name,
                            "args": args,
                            "tool_call_id": call.id,
                            "result": clip(result, 4000),
                            "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                            "tool_result_tokens_added": tool_result_tokens_added,
                            "token_usage": self.last_token_usage.to_dict(),
                            **dict(self._last_tool_result_metadata or {}),
                        },
                    )
                continue

            if kind == "final":
                final = str(getattr(response, "text", "") or "").strip()
                if not final:
                    self.record(
                        {
                            "role": "assistant",
                            "content": "Runtime notice: model returned an empty final answer.",
                            "created_at": now(),
                        }
                    )
                    self.run_store.write_task_state(task_state)
                    continue
                self.record({"role": "assistant", "kind": "final", "content": final, "created_at": now()})
                task_state.finish_success(final)
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "run_finished",
                    {
                        "status": task_state.status,
                        "stop_reason": task_state.stop_reason,
                        "final_answer": final,
                        "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                    },
                )
                self.schedule_dream_if_needed(task_state)
                self.ui.final_answer(final)
                self.maybe_generate_session_title(user_message, final)
                return final

            self.record(
                {
                    "role": "assistant",
                    "content": f"Runtime notice: unknown model response kind {kind!r}.",
                    "created_at": now(),
                }
            )
            self.run_store.write_task_state(task_state)

        if max_attempts is not None and attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        self.record({"role": "assistant", "content": final, "created_at": now()})
        self.run_store.write_task_state(task_state)
        self.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.schedule_dream_if_needed(task_state)
        self.ui.final_answer(final)
        return final
