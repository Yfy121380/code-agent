"""Agent 运行时核心逻辑。

CodeMate 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import textwrap
import threading
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import memory as memorylib
from .memory import dream as dreamlib
from .memory import long_term as longterm
from .context import ContextManager
from .storage import RunStore, SessionStore, TaskState
from .ui import NullUI
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_LOCAL_TIMEZONE = "Asia/Shanghai"
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "long_term_memory": True,
    "memory_dream": True,
    "context_reduction": True,
    "prompt_cache": True,
}
@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


class CodeMate:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=20,
        max_new_tokens=4096,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        ui=None,
        allowed_tools=None,
        memory_scope_only=False,
        runtime_mode="agent",
        timezone_name=DEFAULT_LOCAL_TIMEZONE,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        # 子agent深度
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.allowed_tools = None if allowed_tools is None else {str(name) for name in allowed_tools}
        self.memory_scope_only = bool(memory_scope_only)
        self.runtime_mode = str(runtime_mode or "agent")
        self.timezone_name = str(timezone_name or DEFAULT_LOCAL_TIMEZONE)
        # UI 是 runtime 的展示出口，默认空实现，避免测试和批处理产生额外输出。
        self.ui = ui or NullUI()
        # 允许传给shell的环境变量
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        # 需要脱敏的环境变量名称
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        # 功能开关，测试中可临时关掉某些功能
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(), # 运行时的结构化笔记
            "todos": [],
        }
        # 用于保存单次 ask() 的运行状态。默认放在当前 session 目录下，
        # 让 session.json 和该会话产生的 runs 保持在同一个文件夹中。
        self.run_store = run_store or RunStore(self.session_store.runs_dir(self.session["id"]))
        # 补齐字段
        self._ensure_session_shape()
        # 负责管理当前会话的短期工作记忆、文件摘要、长期记忆并进行相关的笔记召回，为session的一部分
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        # 工具描述 {"name": {"schema":"", "risky":"", "description":"", "run":""}}
        self.tools = self.build_tools()
        # 可复用的前缀提示词，包括 agent的身份、可用的工具、git仓库状态等等
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        # 上下文管理器，组装上下文，进行上下文压缩
        self.context_manager = ContextManager(self)
        self.invalidate_stale_memory()
        # 保存当前session状态文件
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.relevant_long_term_memory = []
        self.long_term_memory_status = "not_run"
        self.long_term_memory_metadata = {}
        self._long_term_memory_cache_key = None
        self._last_tool_result_metadata = {}
        self._last_shell_analysis = None
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        self.session.setdefault("todos", [])
        self.session.pop("checkpoints", None)
        self.session.pop("resume_state", None)
        self.session.pop("runtime_identity", None)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def current_memory_turn(self):
        return sum(1 for item in self.session.get("history", []) if item.get("role") == "user")

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        tools = toolkit.build_tool_registry(self)
        if self.allowed_tools is not None:
            tools = {name: tool for name, tool in tools.items() if name in self.allowed_tools}
        return tools

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "input_schema": tool["input_schema"],
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def model_tools(self):
        specs = []
        for name, tool in self.tools.items():
            specs.append(
                {
                    "name": name,
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                    "risky": tool["risky"],
                }
            )
        return specs

    def build_prefix(self):
        if self.runtime_mode == "dream":
            tool_use_rules = textwrap.dedent(
                """\
                Tool use:
                - Use tools only for memory consolidation under `.codemate/memory/`.
                - Use todo_write if it helps plan the consolidation.
                - Read existing memory files before patching or overwriting them.
                - Do not rewrite daily logs; daily logs are append-only raw records.
                - Use Runtime context for current_local_datetime, current_local_date, timezone, and today_daily_log_path.
                - Return a final answer once memory files are consolidated and checked.
                """
            ).strip()
            text = textwrap.dedent(
                f"""\
                You are codemate's background dream process for long-term memory consolidation.

                {tool_use_rules}

                Your scope is `.codemate/memory/` only. Do not inspect or edit project files outside that directory.
                """
            ).strip()
            return PromptPrefix(
                text=text,
                hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                workspace_fingerprint=self.workspace.fingerprint(),
                tool_signature=self.tool_signature(),
                built_at=now(),
            )

        tool_use_rules = textwrap.dedent(
            """\
            Tool use:
            - Use tools only while they are needed to make progress on the user's request.
            - Stop using tools and return a final answer when you have completed user's request or have sufficient information to respond fully without further tool invocation.
            - Do not repeat reads or searches merely to reconfirm information you already have.
            - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
            - Use the provided tools for workspace work.
            - To inspect a file, use read_file.
            - To search file contents, use grep.
            - To edit an existing file, use patch_file after read_file.
            - To create, replace, or append to a file, use write_file.
            - Use todo_write for complex multi-step work, explicit task tracking requests, and multi-part user requests.
            - When current_todos appears in Working memory, follow those items until completed or no longer relevant.
            - Do not say tools are unavailable when they are provided by the runtime.
            - If a tool result says a repeated identical call was rejected, do not retry the same investigation; either choose a materially different action or provide the final answer.
            """
        ).strip()
        workflow_rules = textwrap.dedent(
            """\
            Workflow rules:
            - After a successful tool result, treat it as an observation and continue with the next required action unless the user's request is already complete.
            - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
            - Before overwriting an existing file with write_file or editing with patch_file, read that exact file first; grep/list_files results are not enough.
            - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
            - New files should be complete and runnable, including obvious imports.
            - After editing Python code, run `python -m py_compile` on the changed Python files to verify syntax before finishing.
            """
        ).strip()
        long_term_rules = textwrap.dedent(
            """\
            Long-term memory:
            - If the current interaction contains information worth remembering across sessions, append it to today's daily log before returning the final answer.
            - Use current_local_datetime and today_daily_log_path from Runtime context. Use write_file with mode="append".
            - Daily log entry format: `- [current_local_datetime] memory`.
            - Daily logs are append-only. Do not rewrite or reorganize daily log files.
            - Treat explicit phrases like "remember", "from now on", "next time", "以后", "记住", and "下次" as strong signals to append a daily log entry.
            - What to log: user profile signals, workflow feedback, and project context that is useful across future sessions.
            - User profile signals include the user's role, goals, knowledge background, skill level, stable preferences, expression style, and collaboration preferences.
            - Workflow feedback includes how the agent should work, verify, report, ask before acting, and avoid repeating past mistakes.
            - Project context includes long-lived project background, goals, naming, architecture direction, constraints, and decisions not directly derivable from current code or git state.
            - Do not save secrets, one-off task progress, current todos, raw tool output, temporary debugging noise, large code snippets, or facts only useful in the current turn.
            """
        ).strip()
        answer_rules = textwrap.dedent(
            """\
            Answering:
            - Never invent tool results.
            - Keep answers concise and concrete.
            - When the task is complete, answer with a brief summary of what changed and any verification performed.
            """
        ).strip()
        # prefix 可以理解成 agent 的“工作手册”：
        # 它是谁、如何使用工具、当前仓库是什么状态，都写在这里。
        text = textwrap.dedent(
            f"""\
            You are codemate, a small local coding agent working inside a local repository.

            {tool_use_rules}

            {workflow_rules}

            {long_term_rules}

            {answer_rules}

            {self.workspace.text()}
            """
        ).strip()
        return PromptPrefix(
            text=text,
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint=self.workspace.fingerprint(),
            tool_signature=self.tool_signature(),
            built_at=now(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        lines = self.memory.render_memory_text().splitlines()
        todos = self.session.get("todos") or []
        if todos:
            lines.append("- current_todos: follow these phases and tasks until completed")
            for index, item in enumerate(todos, 1):
                phase = str(item.get("phase", "")).strip()
                status = str(item.get("status", "")).strip()
                if phase and status:
                    lines.append(f"  {index}. [{status}] {phase}")
                for task in item.get("tasks") or []:
                    description = str(task.get("description", "")).strip()
                    task_status = str(task.get("status", "")).strip()
                    if description and task_status:
                        lines.append(f"     - [{task_status}] {description}")
        else:
            lines.append("- current_todos: -")
        return "\n".join(lines)

    def local_now(self):
        try:
            timezone = ZoneInfo(self.timezone_name)
        except Exception:
            timezone = ZoneInfo(DEFAULT_LOCAL_TIMEZONE)
        return datetime.now(timezone)

    def runtime_context_text(self):
        local_now = self.local_now()
        current_date = local_now.date().isoformat()
        daily_log = memorylib.daily_log_path(self.root, date=current_date).relative_to(self.root).as_posix()
        return textwrap.dedent(
            f"""\
            Runtime context:
            - current_local_datetime: {local_now.isoformat(timespec="seconds")}
            - current_local_date: {current_date}
            - timezone: {self.timezone_name}
            - today_daily_log_path: {daily_log}
            """
        ).strip()

    def prompt_memory_text(self):
        return "\n\n".join([self.runtime_context_text(), self.memory_text()])

    def remember_long_term(self, text):
        text = str(text or "").strip()
        if not text:
            raise ValueError("memory text must not be empty")
        memorylib.ensure_long_term_memory(self.root)
        local_now = self.local_now()
        timestamp = local_now.isoformat(timespec="seconds")
        path = memorylib.daily_log_path(self.root, date=local_now.date().isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = f"- [{timestamp}] {text}"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
        return {
            "path": path.relative_to(self.root).as_posix(),
            "entry": entry,
        }

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        recent_start = max(0, len(history) - 20)
        for index, item in enumerate(history):
            recent = index >= recent_start
            limit = 3000 if recent else 1000
            role = item.get("role", "")
            if role == "assistant" and item.get("tool_calls"):
                lines.append(f"[assistant:tool_calls] {json.dumps(item.get('tool_calls'), sort_keys=True, ensure_ascii=False)}")
            elif role == "tool":
                lines.append(f"[tool:{item.get('name', '')}] {item.get('tool_call_id', '')}")
                lines.append(clip(item.get("content", ""), limit))
            else:
                lines.append(f"[{role}] {clip(item.get('content', ''), limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def configured_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if str(name).upper() in self.secret_env_names and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def detected_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if self.is_secret_env_name(name) and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.configured_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def detected_secret_env_summary(self):
        names = [name for name, _ in self.detected_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def redact_text(self, text):
        text = str(text)
        for _, value in sorted(self.detected_secret_env_items(), key=lambda item: len(item[1]), reverse=True):
            text = text.replace(value, REDACTED_VALUE)
        return text

    def redact_artifact(self, value, key=None):
        if key and self.is_secret_env_name(key):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {
                str(item_key): self.redact_artifact(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, str):
            redacted = self.redact_text(value)
            return redacted
        return value

    def shell_env(self):
        env = {
            name: os.environ[name]
            for name in self.shell_env_allowlist
            if name in os.environ
        }
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return env

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.invalidate_stale_memory()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.prompt_memory_text()),
                "runtime_context_chars": len(self.runtime_context_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def _build_messages_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.invalidate_stale_memory()
        message_build = self.context_manager.build_messages(user_message)
        metadata = dict(message_build.metadata)
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.prompt_memory_text()),
                "runtime_context_chars": len(self.runtime_context_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return message_build.system, message_build.messages, metadata

    def emit_trace(self, task_state, event, payload=None):
        """
        业务事件 payload
        -> 脱敏
        -> 添加 event/created_at
        -> 写 trace.jsonl
        """
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        return payload

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            # 最近访问过的文件
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            # 读文件之后保存摘要，前三个非空行最多 180 字符的结果
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            # 改动过的文件去除摘要
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def expire_process_notes(self):
        if not self.feature_enabled("memory"):
            return
        self.memory.expire_process_notes(self.current_memory_turn())
        self.session["memory"] = self.memory.to_dict()

    def record_process_note_for_tool(self, name, args, metadata, message):
        if not self.feature_enabled("memory"):
            return
        self.memory.record_process_note(name, args, metadata, message, self.current_memory_turn())
        self.session["memory"] = self.memory.to_dict()

    def resolve_process_notes_after_success(self, name, args):
        if not self.feature_enabled("memory"):
            return
        self.memory.resolve_process_notes_after_success(name, args, self.current_memory_turn())
        self.session["memory"] = self.memory.to_dict()

    def retrieve_long_term_memory_for_request(self, user_message, task_state):
        """为当前 ask 执行一次长期记忆模型召回。

        召回只发生在用户请求开始时，后续工具循环复用 `relevant_long_term_memory`，
        避免每次重新组 prompt 都额外调用模型。失败不会中断主任务，只会降级为空召回。
        """
        self.relevant_long_term_memory = []
        self.long_term_memory_status = "disabled"
        self.long_term_memory_metadata = {}
        if not (self.feature_enabled("memory") and self.feature_enabled("relevant_memory") and self.feature_enabled("long_term_memory")):
            return
        if self.runtime_mode != "agent":
            self.long_term_memory_status = "skipped_runtime_mode"
            return

        memory_files = memorylib.read_long_term_memory(self.root)
        cache_payload = json.dumps(
            {"user_message": str(user_message), "memory_files": memory_files},
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
        if self._long_term_memory_cache_key == cache_key:
            return

        self.emit_trace(task_state, "memory_retrieval_started", {"memory_hash": cache_key})
        try:
            result = memorylib.retrieve_long_term_memory(self.model_client, self.root, user_message)
        except Exception as exc:
            self.long_term_memory_status = "failed"
            self.long_term_memory_metadata = {"error": str(exc), "memory_hash": cache_key}
            self.emit_trace(task_state, "memory_retrieval_failed", self.long_term_memory_metadata)
            self._long_term_memory_cache_key = cache_key
            return

        selected = list(result.get("selected", []) or [])
        self.relevant_long_term_memory = selected
        self.long_term_memory_status = str(result.get("status", "ok"))
        self.long_term_memory_metadata = {
            "status": self.long_term_memory_status,
            "selected_count": len(selected),
            "selected_sources": [str(item.get("source", "")) for item in selected],
            "duration_ms": int(result.get("duration_ms", 0) or 0),
            "memory_hash": cache_key,
        }
        self.emit_trace(task_state, "memory_retrieval_finished", self.long_term_memory_metadata)
        self._long_term_memory_cache_key = cache_key

    def schedule_dream_if_needed(self, task_state):
        if not self.feature_enabled("memory_dream") or self.runtime_mode != "agent":
            return
        session_count = self.session_store.count()
        due, reason = dreamlib.should_run_dream(self.root, session_count)
        if not due:
            return
        self.start_dream_background(reason=reason)
        self.emit_trace(task_state, "dream_scheduled", {"reason": reason, "session_count": session_count})

    def start_dream_background(self, reason="manual"):
        thread = threading.Thread(target=self.run_dream_once, kwargs={"reason": reason, "foreground": False}, daemon=True)
        thread.start()
        return thread

    def run_dream_once(self, reason="manual", foreground=True):
        with longterm.dream_lock(self.root) as acquired:
            if not acquired:
                return "dream skipped: another dream process is already running"
            try:
                state = longterm.load_dream_state(self.root)
                cursor_text = dreamlib.render_daily_log_cursor(state)
                dream_session = {
                    "id": "dream-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
                    "created_at": now(),
                    "workspace_root": str(self.root),
                    "history": [],
                    "memory": memorylib.default_memory_state(),
                    "todos": [],
                    "runtime_mode": "dream",
                    "reason": str(reason),
                }
                child = CodeMate(
                    model_client=self.model_client,
                    workspace=self.workspace,
                    session_store=self.session_store,
                    session=dream_session,
                    approval_policy="auto",
                    max_steps=12,
                    max_new_tokens=self.max_new_tokens,
                    depth=0,
                    max_depth=0,
                    read_only=False,
                    shell_env_allowlist=self.shell_env_allowlist,
                    secret_env_names=self.secret_env_names,
                    feature_flags={
                        **self.feature_flags,
                        "long_term_memory": False,
                        "relevant_memory": False,
                        "memory_dream": False,
                    },
                    allowed_tools={"list_files", "read_file", "grep", "write_file", "patch_file", "todo_write"},
                    memory_scope_only=True,
                    runtime_mode="dream",
                    timezone_name=self.timezone_name,
                    ui=self.ui if foreground else NullUI(),
                )
                child.ask(dreamlib.dream_prompt(cursor_text))
                updated = dreamlib.mark_dream_complete(self.root, self.session_store.count(), status="ok")
                cursor = updated.get("last_processed_daily_log") or {}
                file_name = cursor.get("file") or ""
                line = cursor.get("line", 0) or 0
                return f"dream completed: processed through {file_name or 'no daily logs'} line {line}"
            except Exception as exc:
                dreamlib.mark_dream_failed(self.root, status="error")
                return f"dream failed: {exc}"

    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把“用户提一个请求”扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace/report、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；如果中途达到步数上限或重试上限，
          返回的是一条停止原因说明

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
        # 记录当前ask的执行状态，工具和模型调用次数、任务完成情况等
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
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < self.max_steps and attempts < max_attempts:
            # 2. 每一轮先落盘当前 attempt，再组装模型要看的 messages。
            attempts += 1
            task_state.record_attempt()
            self.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            system, messages, prompt_metadata = self._build_messages_and_metadata(user_message)
            # print(f"system:{system}")
            # print(f"message:{messages}")
            self.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "system": system,
                    "messages": messages,
                    # "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
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
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            self.last_completion_metadata = completion_metadata
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
                for call in calls:
                    if tool_steps >= self.max_steps:
                        break
                    self.record(
                        {
                            "role": "assistant",
                            "content": str(getattr(response, "text", "") or ""),
                            "tool_calls": [call.to_dict()],
                            "created_at": now(),
                        }
                    )
                    tool_steps += 1
                    name = call.name
                    args = dict(call.args or {})
                    task_state.record_tool(name)
                    tool_started_at = time.monotonic()
                    result = self.run_tool(name, args, current_tool_call_id=call.id)
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
                self.record({"role": "assistant", "content": final, "created_at": now()})
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
                self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
                self.ui.final_answer(final)
                return final

            self.record(
                {
                    "role": "assistant",
                    "content": f"Runtime notice: unknown model response kind {kind!r}.",
                    "created_at": now(),
                }
            )
            self.run_store.write_task_state(task_state)

        # 9. 没拿到 final 时安全停机：区分格式重试耗尽和工具步数耗尽。
        if attempts >= max_attempts and tool_steps < self.max_steps:
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
        self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
        self.ui.final_answer(final)
        return final

    def shell_analysis_metadata(self):
        analysis = getattr(self, "_last_shell_analysis", None)
        if analysis is None or not hasattr(analysis, "to_metadata"):
            return {}
        return analysis.to_metadata()

    def tool_risk_level(self, name, tool):
        if name == "run_shell":
            analysis = getattr(self, "_last_shell_analysis", None)
            if analysis is not None and getattr(analysis, "kind", "") == "read":
                return "low"
        return "high" if tool["risky"] else "low"

    def tool_metadata_read_only(self, name, tool):
        if name == "run_shell":
            analysis = getattr(self, "_last_shell_analysis", None)
            return bool(analysis is not None and getattr(analysis, "kind", "") == "read")
        return not tool["risky"]

    def approval_decision(self, name, args, tool):
        if name != "run_shell":
            if not tool["risky"]:
                return "allow"
            if self.read_only:
                return "reject"
            if self.approval_policy == "auto":
                return "allow"
            if self.approval_policy == "never":
                return "reject"
            return "ask"

        analysis = getattr(self, "_last_shell_analysis", None)
        if analysis is None:
            analysis = toolkit.analyze_shell_command(self, args.get("command", ""))
            self._last_shell_analysis = analysis
        if getattr(analysis, "blocked", False):
            return "reject"
        if analysis.kind == "read":
            return "allow"
        if self.read_only:
            return "reject"
        if analysis.kind == "risky":
            if self.approval_policy == "auto":
                return "allow"
            if self.approval_policy == "never":
                return "reject"
            return "ask"
        if self.approval_policy == "never":
            return "reject"
        return "ask"

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
        tool = self.tools.get(name)
        if tool is None:
            message = f"error: unknown tool '{name}'"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
                "risk_level": "high",
                "read_only": False,
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
            return message
        try:
            # 路径限制在workspace内 + 参数有效性校验
            self.validate_tool(name, args)
        except Exception as exc:
            message = f"error: invalid arguments for {name}: {exc}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
                **self.shell_analysis_metadata(),
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
            return message
        # 拦截重复调用过两次的工具，若最近两次工具调用请求均和当前一样(包括args)，则拒绝当次请求
        if self.repeated_tool_call(name, args, exclude_call_id=current_tool_call_id):
            message = f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
                **self.shell_analysis_metadata(),
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
            return message
        # 根据工具和 shell 分析结果决定是否需要审批。
        decision = self.approval_decision(name, args, tool)
        if decision == "reject":
            message = f"error: approval denied for {name}"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
                **self.shell_analysis_metadata(),
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
            return message
        asked_for_approval = decision == "ask"
        if asked_for_approval and not self.prompt_approval(name, args):
            message = f"error: approval denied for {name}"
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
                **self.shell_analysis_metadata(),
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
            return message
        if not asked_for_approval:
            self.ui.tool_start(name, args, risk_level=self.tool_risk_level(name, tool))
        # 危险工具执行前记录仓库文件快照计算 sha256，用于比较哪些文件有改变
        before_snapshot = self.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            result = str(tool["run"](args))
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            # 返回有哪些文件不同，修改了、删除了、新增了哪些文件
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", result)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            
            # 提取高价值信息
            # 最近接触过哪些文件
            # 某个文件刚刚读到的短摘要
            # 写文件后旧摘要需要失效
            self.update_memory_after_tool(name, args, result)
            self._last_tool_result_metadata = {
                "tool_status": tool_status,
                "tool_error_code": tool_error_code,
                "security_event_type": "",
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
                **self.shell_analysis_metadata(),
            }
            if tool_status == "ok":
                self.resolve_process_notes_after_success(name, args)
            else:
                self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, result)
            return result
        except Exception as exc:
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            message = f"error: tool {name} failed: {exc}"
            self._last_tool_result_metadata = {
                "tool_status": "partial_success" if workspace_changed else "error",
                "tool_error_code": "tool_partial_success" if workspace_changed else "tool_failed",
                "security_event_type": security_event_type,
                "risk_level": self.tool_risk_level(name, tool),
                "read_only": self.tool_metadata_read_only(name, tool),
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
                **self.shell_analysis_metadata(),
            }
            self.record_process_note_for_tool(name, args, self._last_tool_result_metadata, message)
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

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        # 记录 (1)运行结果, (2)循环次数, (3)提示词指标, (4)长期记忆提取
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "long_term_memory": dict(self.long_term_memory_metadata),
            "redacted_env": self.detected_secret_env_summary(),
        }

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self, name, args)
        if name in {"write_file", "patch_file"}:
            self.require_fresh_read_before_edit(name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def require_fresh_read_before_edit(self, name, args):
        path = self.path(args["path"])
        if name == "write_file" and not path.exists():
            return
        if name == "write_file" and str(args.get("mode", "overwrite")) == "append":
            return
        if not self.memory.has_fresh_file_summary(args["path"]):
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

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def prompt_approval(self, name, args):
        if self.read_only:
            return False
        metadata = {
            "risk_level": self.tool_risk_level(name, self.tools.get(name, {"risky": True})),
            **self.shell_analysis_metadata(),
        }
        return bool(self.ui.approval_request(name, args, metadata=metadata))

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        return self.prompt_approval(name, args)

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        if self.memory_scope_only and not longterm.is_memory_path(self.root, resolved):
            raise ValueError(f"path outside memory scope: {raw_path}")
        return resolved


MiniAgent = CodeMate
