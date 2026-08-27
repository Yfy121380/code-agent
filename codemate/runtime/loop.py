# Agent 主循环。
# 本文件负责一次 ask() 的完整执行过程：记录用户请求、构建 messages、
# 调模型、执行工具、写 trace/task_state，并在 final 或停止条件出现时结束。
# 它只编排流程，不实现具体工具、审批规则或记忆文件格式。

import time

from .. import tools as toolkit
from ..models import ModelStreamIncompleteError
from ..storage import (
    PersistenceError,
    STATUS_RUNNING,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_PERSISTENCE_ERROR,
    STOP_REASON_STREAM_INCOMPLETE,
    STOP_REASON_UNEXPECTED_ERROR,
    STOP_REASON_USER_INTERRUPTED,
    TaskState,
)
from ..workspace import clip, now
from .errors import ModelRequestError


class RuntimeLoopMixin:
    def _emit_run_finished(self, task_state, final, run_started_at):
        """Best-effort terminal trace after durable state and user output succeed."""
        try:
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
        except Exception:
            pass

    def _finish_failed_run(self, task_state, stop_reason, error, run_started_at):
        """Best-effort finalization for a run that cannot continue."""
        if task_state is None or task_state.status != STATUS_RUNNING:
            return
        if stop_reason == STOP_REASON_USER_INTERRUPTED:
            task_state.stop_interrupted()
        elif stop_reason == STOP_REASON_MODEL_ERROR:
            task_state.stop_model_error()
        else:
            task_state.stop_failed(stop_reason)
        payload = {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "error": clip(str(error), 4000),
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
        }
        try:
            self.run_store.write_task_state(task_state)
        except Exception:
            pass
        try:
            self.emit_trace(task_state, "run_finished", payload)
        except Exception:
            pass

    def _run_post_completion_maintenance(
        self,
        task_state,
        user_message,
        final,
        *,
        memory_maintained_this_run,
    ):
        """Run optional maintenance without changing an already completed result."""
        operations = [
            ("session_title", lambda: self.maybe_generate_session_title(user_message, final)),
        ]
        operations.append(
            (
                "long_term_memory",
                lambda: self.memory_backend.after_completion(
                    task_state,
                    already_maintained=memory_maintained_this_run,
                ),
            )
        )
        for operation, callback in operations:
            try:
                callback()
            except Exception as exc:
                try:
                    self.emit_trace(
                        task_state,
                        "maintenance_failed",
                        {"operation": operation, "error": clip(str(exc), 4000)},
                    )
                except Exception:
                    pass

    def _emit_commentary_trace(self, task_state, commentary, *, source):
        """把用户可见的中间进展写入 trace，便于事后复盘 agent 决策过程。"""
        text = str(commentary or "").strip()
        if not text:
            return
        self.emit_trace(
            task_state,
            "assistant_commentary",
            {
                "source": source,
                "content": clip(text, 4000),
                "content_chars": len(text),
            },
        )

    def _complete_model_response(self, messages, system, prompt_cache_key=None, prompt_cache_retention=None):
        """调用模型并返回完整 response，同时按需把文本 delta 流式展示到 UI。

        流式输出只是一层展示优化：工具调用必须等完整响应结束后才解析、
        校验和执行，history/trace 也只保存完整 assistant 消息。
        """
        model_started_at = time.monotonic()
        self.ui.model_start()
        use_stream = (
            bool(getattr(self, "stream", False))
            and bool(getattr(self.model_client, "supports_streaming", False))
            and hasattr(self.model_client, "stream_complete")
        )
        if not use_stream:
            try:
                response = self.model_client.complete(
                    messages,
                    self.max_new_tokens,
                    tools=self.model_tools(),
                    system=system,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
            except (Exception, KeyboardInterrupt):
                self.ui.model_end(kind="error", metadata={})
                raise
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            completion_metadata.update(dict(getattr(response, "metadata", {}) or {}))
            kind = getattr(response, "kind", "final")
            self.ui.model_end(kind=kind, metadata=completion_metadata)
            return response, completion_metadata, int((time.monotonic() - model_started_at) * 1000), 0

        response = None
        stream_metadata = {}
        streamed_text_chars = 0
        streamed_phase_chars = {"commentary": 0, "final_answer": 0}
        streamed_unphased_chars = 0
        self.ui.stream_start()
        try:
            for event in self.model_client.stream_complete(
                messages,
                self.max_new_tokens,
                tools=self.model_tools(),
                system=system,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            ):
                kind = getattr(event, "kind", "")
                if kind == "text_delta":
                    delta = str(getattr(event, "text", "") or "")
                    streamed_text_chars += len(delta)
                    phase = str(getattr(event, "phase", "") or "")
                    if phase in streamed_phase_chars:
                        streamed_phase_chars[phase] += len(delta)
                    else:
                        streamed_unphased_chars += len(delta)
                    self.ui.stream_delta(delta, phase=phase)
                elif kind == "done":
                    response = getattr(event, "response", None)
                    stream_metadata.update(dict(getattr(event, "metadata", {}) or {}))
                    break
                elif kind == "error":
                    raise RuntimeError(str(getattr(event, "text", "") or "model streaming error"))
        except (Exception, KeyboardInterrupt):
            self.ui.stream_end(kind="error")
            self.ui.model_end(kind="error", metadata={})
            raise

        if response is None:
            self.ui.stream_end(kind="error")
            self.ui.model_end(kind="error", metadata={})
            raise ModelStreamIncompleteError(
                "stream_incomplete: model stream ended without a completed response"
            )

        completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
        completion_metadata.update(stream_metadata)
        completion_metadata.update(dict(getattr(response, "metadata", {}) or {}))
        completion_metadata["streamed_text_chars"] = streamed_text_chars
        kind = getattr(response, "kind", "final")
        only_unphased_text = streamed_unphased_chars > 0 and not any(
            streamed_phase_chars.values()
        )
        completion_metadata["streamed_commentary"] = (
            streamed_phase_chars["commentary"] > 0
            or (kind in {"commentary", "tool_calls"} and only_unphased_text)
        )
        completion_metadata["streamed_final_answer"] = (
            streamed_phase_chars["final_answer"] > 0
            or (kind == "final" and only_unphased_text)
        )
        self.ui.stream_end(kind=kind, metadata=completion_metadata)
        self.ui.model_end(kind=kind, metadata=completion_metadata)
        return response, completion_metadata, int((time.monotonic() - model_started_at) * 1000), streamed_text_chars

    @staticmethod
    def _interactive_tool_batch_error(calls):
        """Reject responses that mix an exclusive boundary tool with other tools."""
        names = {call.name for call in calls}
        exclusive = names.intersection(toolkit.EXCLUSIVE_TOOL_CALLS)
        if len(calls) <= 1 or not exclusive:
            return ""
        tool_names = ", ".join(sorted(exclusive))
        return (
            f"error: {tool_names} must each be the only tool call in a model response; "
            "no tools in this response were executed"
        )

    def _record_tool_outcome(
        self,
        task_state,
        call,
        args,
        result,
        *,
        started_at,
        metadata=None,
        content_blocks=None,
    ):
        """Persist one tool result and its UI/trace metadata in one place."""
        metadata = dict(self._last_tool_result_metadata if metadata is None else metadata)
        content_blocks = list(
            self._last_tool_result_content_blocks if content_blocks is None else content_blocks
        )
        tool_result_tokens_added = self.add_tool_result_token_estimate(result)
        tool_record = {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result,
            "created_at": now(),
        }
        if content_blocks:
            tool_record["content_blocks"] = content_blocks
        # UI metadata 不进入工具正文，但会随会话恢复重新显示单次修改片段。
        if isinstance(metadata.get("change_preview"), dict):
            tool_record["ui_metadata"] = {
                "change_preview": dict(metadata["change_preview"]),
            }
        self.record(tool_record)
        self.flush_pending_internal_context()
        self.run_store.write_task_state(task_state)
        self.ui.tool_result(call.name, args, result, metadata=metadata)
        self.emit_trace(
            task_state,
            "tool_executed",
            {
                "name": call.name,
                "args": args,
                "tool_call_id": call.id,
                "result": clip(result, 4000),
                "content_blocks": content_blocks,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "tool_result_tokens_added": tool_result_tokens_added,
                "token_usage": self.last_token_usage.to_dict(),
                **metadata,
            },
        )

    def ask(
        self,
        user_message,
        *,
        source_user_request=None,
        editor_context="",
        response_annotations=None,
    ):
        """Run one turn while retaining its external user request separately."""
        run_started_at = time.monotonic()
        try:
            return self._run_agent_loop(
                user_message,
                run_started_at,
                source_user_request=source_user_request,
                editor_context=editor_context,
                response_annotations=response_annotations,
            )
        except KeyboardInterrupt as exc:
            self._finish_failed_run(
                self.current_task_state,
                STOP_REASON_USER_INTERRUPTED,
                exc or "interrupted by user",
                run_started_at,
            )
            raise
        except ModelStreamIncompleteError as exc:
            self._finish_failed_run(
                self.current_task_state,
                STOP_REASON_STREAM_INCOMPLETE,
                exc,
                run_started_at,
            )
            raise
        except ModelRequestError as exc:
            self._finish_failed_run(
                self.current_task_state,
                STOP_REASON_MODEL_ERROR,
                exc,
                run_started_at,
            )
            raise
        except PersistenceError as exc:
            self._finish_failed_run(
                self.current_task_state,
                STOP_REASON_PERSISTENCE_ERROR,
                exc,
                run_started_at,
            )
            raise RuntimeError(f"Agent persistence failed: {exc}") from exc
        except Exception as exc:
            self._finish_failed_run(
                self.current_task_state,
                STOP_REASON_UNEXPECTED_ERROR,
                exc,
                run_started_at,
            )
            raise RuntimeError(f"Agent run failed unexpectedly: {exc}") from exc
        finally:
            self.finish_change_tracking()

    def _run_agent_loop(
        self,
        user_message,
        run_started_at,
        *,
        source_user_request=None,
        editor_context="",
        response_annotations=None,
    ):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把“用户提一个请求”扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；主 agent 不限制工具步数，受控子流程
          仍会在达到步数上限时返回停止原因

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 codemate 是怎么“从一句话跑成一个 agent 流程”的，
        这里就是最关键的入口。
        """
        # 1. 登记本次 ask：先把用户请求写入 session，再创建 run 工件。
        self._current_conversation_id = self.new_conversation_id()
        explicit_user_inputs = [
            str(source_user_request if source_user_request is not None else user_message)
        ]
        explicit_user_inputs.extend(
            str(item.get("comment") or "")
            for item in (response_annotations or [])
            if isinstance(item, dict) and str(item.get("comment") or "").strip()
        )
        # Core memory may cite composer text and annotation comments, but never
        # selected assistant text or the annotation-rendering template.
        self._current_source_user_request = "\n".join(explicit_user_inputs)
        task_state = TaskState.create(
            run_id=self.new_run_id(),
            task_id=self.new_task_id(),
            user_request=user_message,
            source_user_request=source_user_request,
        )
        self.current_task_state = task_state
        self.current_run_dir = self.run_store.start_run(task_state)
        self.begin_change_tracking(task_state)
        self.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        if str(editor_context or "").strip():
            self.record(
                {
                    "role": "user",
                    "kind": "editor_context",
                    "content": str(editor_context).strip(),
                    "created_at": now(),
                }
            )
        # Keep the actual request as the final user message. Besides matching
        # model semantics, compaction can then pin the correct message rather
        # than the preceding editor evidence block.
        user_record = {"role": "user", "content": user_message, "created_at": now()}
        if response_annotations:
            # Model history keeps the complete annotated request; the durable UI
            # transcript separately retains the clean composer text and cards.
            user_record["display_content"] = str(source_user_request or "")
            user_record["response_annotations"] = [
                dict(item) for item in response_annotations
            ]
        self.record(user_record)
        self.memory_backend.prepare_request(user_message, task_state)

        tool_steps = 0
        attempts = 0
        memory_maintained_this_run = False
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
                maintenance_result = compact_result.get("memory_maintenance")
                memory_maintained_this_run = (
                    isinstance(maintenance_result, dict)
                    and maintenance_result.get("status") == "ok"
                )
                if compact_result.get("status") == "error":
                    final = f"History compaction failed: {compact_result.get('reason', 'unknown error')}"
                    task_state.stop_retry_limit(final)
                    self.record(
                        {
                            "role": "assistant",
                            "kind": "final",
                            "content": final,
                            "created_at": now(),
                        }
                    )
                    self.run_store.write_task_state(task_state)
                    self.ui.final_answer(final)
                    self._emit_run_finished(task_state, final, run_started_at)
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
            if (
                self.feature_enabled("prompt_cache")
                and getattr(self.model_client, "supports_prompt_cache", False)
            ):
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            try:
                response, completion_metadata, model_duration_ms, _streamed_text_chars = self._complete_model_response(
                    messages,
                    system,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=None,
                )
            except ModelStreamIncompleteError:
                raise
            except Exception as exc:
                raise ModelRequestError(str(exc)) from exc
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
                    "duration_ms": model_duration_ms,
                },
            )

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
                if not completion_metadata.get("streamed_commentary", False):
                    self.ui.commentary(commentary)
                self._emit_commentary_trace(task_state, commentary, source="commentary")
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
                batch_error = self._interactive_tool_batch_error(calls)
                calls_to_execute = calls
                if limit_steps and not batch_error:
                    calls_to_execute = calls[: max(0, self.max_steps - tool_steps)]
                commentary = str(getattr(response, "text", "") or "")
                if commentary.strip():
                    commentary = commentary.strip()
                    if not completion_metadata.get("streamed_commentary", False):
                        self.ui.commentary(commentary)
                    self._emit_commentary_trace(task_state, commentary, source="tool_calls")
                self.record(
                    {
                        "role": "assistant",
                        "kind": "tool_calls",
                        "content": commentary,
                        "tool_calls": [call.to_dict() for call in calls_to_execute],
                        "created_at": now(),
                    }
                )
                if batch_error:
                    metadata = {
                        "tool_status": "rejected",
                        "tool_error_code": "interactive_tool_batch",
                        "security_event_type": "interactive_tool_batch",
                    }
                    for call in calls_to_execute:
                        tool_steps += 1
                        args = dict(call.args or {})
                        task_state.record_tool(call.name)
                        self._last_tool_result_metadata = dict(metadata)
                        self._last_tool_result_content_blocks = []
                        self._record_tool_outcome(
                            task_state,
                            call,
                            args,
                            batch_error,
                            started_at=time.monotonic(),
                            metadata=metadata,
                            content_blocks=[],
                        )
                    continue
                for call in calls_to_execute:
                    tool_steps += 1
                    name = call.name
                    args = dict(call.args or {})
                    task_state.record_tool(name)
                    tool_started_at = time.monotonic()
                    result = self.run_tool(name, args, current_tool_call_id=call.id)
                    self._record_tool_outcome(
                        task_state,
                        call,
                        args,
                        result,
                        started_at=tool_started_at,
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
                final_commentary = str(
                    getattr(response, "commentary_text", "") or ""
                ).strip()
                if final_commentary:
                    if (
                        not completion_metadata.get("streamed_commentary", False)
                        and not completion_metadata.get("streamed_final_answer", False)
                    ):
                        self.ui.commentary(final_commentary)
                    self._emit_commentary_trace(
                        task_state,
                        final_commentary,
                        source="final",
                    )
                    self.record(
                        {
                            "role": "assistant",
                            "kind": "commentary",
                            "content": final_commentary,
                            "created_at": now(),
                        }
                    )
                self.record({"role": "assistant", "kind": "final", "content": final, "created_at": now()})
                task_state.finish_success(final)
                self.run_store.write_task_state(task_state)
                if not completion_metadata.get("streamed_final_answer", False):
                    self.ui.final_answer(final)
                self._emit_run_finished(task_state, final, run_started_at)
                self._run_post_completion_maintenance(
                    task_state,
                    user_message,
                    final,
                    memory_maintained_this_run=memory_maintained_this_run,
                )
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
        self.record(
            {
                "role": "assistant",
                "kind": "final",
                "content": final,
                "created_at": now(),
            }
        )
        self.run_store.write_task_state(task_state)
        self.ui.final_answer(final)
        self._emit_run_finished(task_state, final, run_started_at)
        self._run_post_completion_maintenance(
            task_state,
            user_message,
            final,
            memory_maintained_this_run=memory_maintained_this_run,
        )
        return final
