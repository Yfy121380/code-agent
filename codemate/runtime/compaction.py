# History 压缩运行时。
# 本文件负责把旧 history 交给独立的 compact 子请求生成结构化摘要。
# 子请求不暴露任何工具，成功后只保留 recent history，失败时不改动原 session。

from __future__ import annotations

import time

from ..context.history import HistoryContextRenderer
from ..context.types import (
    HISTORY_SUMMARY_SECTIONS,
    MAX_COMPACT_RETRIES,
    RECENT_HISTORY_MIN_CHARS,
    RECENT_HISTORY_MIN_MESSAGES,
)
from ..workspace import clip


SUMMARY_WRAPPER_PREFIX = (
    "This session is being continued from a previous conversation that ran out of context.\n"
    "The summary below covers the earlier portion of the conversation.\n\n"
    "Summary:\n"
)

COMPACT_SYSTEM_PROMPT = f"""Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests, constraints, decisions, and the assistant's previous actions.

This summary will be used to continue a coding session after older context has been removed. It should preserve the technical details, codebase state, decisions, and current work needed to continue without losing context.

You must not continue the user's coding task. You must not call tools. You already have all the context you need in the messages. Tool calls are not allowed.

Output Markdown only. Use exactly these sections:

## Working Directory
Capture the workspace path, project identity, branch, environment, or runtime facts if they are important for future work.

## User Preferences And Constraints
Capture the user's communication preferences, coding style preferences, explicit constraints, forbidden changes, naming constraints, and process rules.

## Current State
Capture the active task, recent progress, current design discussion or implementation state, and any pending next action that follows directly from the compressed messages.

## Key Decisions
Capture confirmed design decisions, technical choices, rejected alternatives, experiment settings, API contracts, permission rules, behavior rules, and important conclusions.

## Changed Files
Capture files created, modified, deleted, or reviewed. Include concise per-file summaries and important functions, classes, config keys, or command names.

## Validation And Issues
Capture commands run, test results, syntax checks, failures, bugs found, fixes attempted, unresolved issues, and warnings.

Rules:
- Preserve concrete paths, function names, class names, command names, config keys, model names, tool names, and exact user constraints.
- Keep bullets concise and information-dense.
- Prefer concrete details over general narration.
- Do not include long tool outputs, full file contents, repeated dialogue, or irrelevant conversation.
- Do not invent completed work, test results, files, or decisions.
- Newer information overrides older conflicting information.
- If a section has no useful information, write "- None".
- Do not mention this summarization task or these instructions.
- Return only the Markdown summary.

Example scenario:
A coding agent is working on a local Python project named CodeMate. The user has been iteratively designing and modifying context management, tool schemas, CLI commands, and history compaction. Several files have been edited and tests have been run. Older conversation messages are now being compacted so the main agent can continue the same coding session later.

Example output:

## Working Directory
- `/home/user/projects/codemate`: local coding agent project.

## User Preferences And Constraints
- User prefers Chinese, direct and pragmatic explanations.
- Do not modify unrelated files without asking first.
- After editing Python files, run `python -m py_compile` on changed files.
- Avoid excessive tiny helper functions unless the logic is clearly reusable.

## Current State
- Designing history compaction for CodeMate.
- Agreed direction: keep recent history verbatim and compact older history into a structured summary.
- Current open question: how to build the compact sub-agent prompt and where to place the compact request.

## Key Decisions
- Only history should be compacted; prefix, skills, working memory, and relevant memory stay outside compact.
- Compact summary should use six sections.
- Recent messages should not be passed into the compact request.

## Changed Files
- `codemate/context/token_budget.py`: added token budget helpers and `/budget` report formatting.
- `codemate/runtime/agent.py`: added token usage tracking and budget report generation.
- `tests/test_token_budget.py`: added budget reporting tests.

## Validation And Issues
- `python -m py_compile ...` passed for changed Python files.
- `uv run pytest tests/test_token_budget.py tests/test_context_manager.py` passed.
- Tool schema chars were initially missing from `/budget`; fixed by adding Tool schemas report.
"""

COMPACT_REQUEST = """Please summarize the conversation history above using the required structure.

The newest conversation messages will be preserved separately after this summary, so only summarize the messages provided above. Focus on information needed to continue future coding work without losing context.

Return only the Markdown summary."""


class HistoryCompactionMixin:
    def wrap_history_summary(self, summary):
        summary = self.normalize_history_summary(summary)
        if not summary:
            return ""
        return SUMMARY_WRAPPER_PREFIX + summary

    def normalize_history_summary(self, text):
        # session 内只保存纯字段内容；展示和再次 compact 时再统一加 wrapper。
        value = str(text or "").strip()
        if value.startswith(SUMMARY_WRAPPER_PREFIX):
            value = value[len(SUMMARY_WRAPPER_PREFIX):].strip()
        if value.startswith("Summary:\n"):
            value = value[len("Summary:\n"):].strip()
        return value

    def compact_history(self, reason="manual", task_state=None):
        """压缩旧 history，成功后只保留 recent history。

        compact 子请求使用同一个 model_client，但 tools 传空列表，避免模型在压缩阶段调用工具。
        只有拿到包含固定六个 section 的 summary 后才会改写 session。
        """
        started_at = time.monotonic()
        renderer = HistoryContextRenderer(self)
        split = renderer.split_for_compaction(
            min_messages=RECENT_HISTORY_MIN_MESSAGES,
            min_chars=RECENT_HISTORY_MIN_CHARS,
        )
        history_to_compact = list(split["history_to_compact"])
        recent_history = list(split["recent_history"])
        before_messages = len(self.session.get("history", []))
        if not history_to_compact:
            result = {
                "status": "skipped",
                "reason": "not_enough_old_history",
                "history_before_messages": before_messages,
                "history_after_messages": before_messages,
                "summary_chars": len(str(self.session.get("history_summary", "") or "")),
                "attempts": 0,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }
            self._emit_compact_trace(task_state, "history_compact", result)
            return result

        self.ui.compact_start(reason=reason)
        original_history = list(self.session.get("history", []))
        original_summary = str(self.session.get("history_summary", "") or "")
        last_error = ""
        attempts = 0
        for attempts in range(1, MAX_COMPACT_RETRIES + 1):
            try:
                summary = self._run_compact_model(history_to_compact, original_summary)
                self._validate_history_summary(summary)
                self.session["history_summary"] = self.normalize_history_summary(summary)
                self.session["history"] = recent_history
                self.session_path = self.session_store.save(self.session)
                self.reset_token_usage()
                result = {
                    "status": "ok",
                    "reason": reason,
                    "history_before_messages": before_messages,
                    "history_after_messages": len(recent_history),
                    "history_compacted_messages": len(history_to_compact),
                    "summary_chars": len(self.session["history_summary"]),
                    "attempts": attempts,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                }
                self._emit_compact_trace(task_state, "history_compact", result)
                self.ui.compact_end(status="ok", metadata=result)
                return result
            except Exception as exc:
                last_error = str(exc)

        self.session["history"] = original_history
        self.session["history_summary"] = original_summary
        self.session_path = self.session_store.save(self.session)
        result = {
            "status": "error",
            "reason": last_error or "compact_failed",
            "history_messages": before_messages,
            "attempts": attempts,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }
        self._emit_compact_trace(task_state, "history_compact_failed", result)
        self.ui.compact_end(status="error", metadata=result)
        return result

    def _run_compact_model(self, history_to_compact, existing_summary):
        messages = []
        wrapped_summary = self.wrap_history_summary(existing_summary)
        if wrapped_summary:
            messages.append({"role": "user", "content": wrapped_summary})
        messages.extend(history_to_compact)
        messages.append({"role": "user", "content": COMPACT_REQUEST})
        response = self.model_client.complete(
            messages,
            self.max_new_tokens,
            tools=[],
            system=COMPACT_SYSTEM_PROMPT,
            prompt_cache_key=None,
            prompt_cache_retention=None,
        )
        if getattr(response, "kind", "final") != "final":
            raise RuntimeError("compact model returned tool calls instead of a summary")
        summary = self.normalize_history_summary(getattr(response, "text", "") or "")
        if not summary:
            raise RuntimeError("compact model returned empty summary")
        return summary

    def _validate_history_summary(self, summary):
        missing = [section for section in HISTORY_SUMMARY_SECTIONS if f"## {section}" not in str(summary or "")]
        if missing:
            raise RuntimeError("compact summary missing sections: " + ", ".join(missing))

    def _emit_compact_trace(self, task_state, event, payload):
        if task_state is not None:
            self.emit_trace(task_state, event, {key: clip(value, 1000) if isinstance(value, str) else value for key, value in dict(payload).items()})
