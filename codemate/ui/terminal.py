"""终端 UI 实现模块。

这里提供默认的空 UI 和面向交互式终端的 TerminalUI。
runtime 通过这些方法汇报模型请求、工具调用、审批和最终回答，
TerminalUI 再使用 rich 与 prompt_toolkit 把信息渲染成清晰的终端输出。
"""

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from .markdown_stream import COMMENTARY_STYLE, MarkdownStreamRenderer
from .summaries import (
    COMPACT_RESULT_TOOLS,
    summarize_approval_call,
    summarize_model_usage,
    summarize_read_tool_result,
    summarize_tool_call,
    summarize_tool_result,
)


PRIMARY_STYLE = "#e6e8eb"
MUTED_STYLE = "#9298a1"
FAINT_STYLE = "#646b74"
BORDER_STYLE = "#34383d"
SUCCESS_STYLE = "#75c995"
WARNING_STYLE = "#d9b36c"
ERROR_STYLE = "#e58181"
TOOL_CALL_STYLE = "#a7adb5"
TOOL_RESULT_STYLE = "#9298a1"
TOOL_DETAIL_STYLE = "#737a83"
TOOL_ERROR_STYLE = ERROR_STYLE
ASSISTANT_MARKER_STYLE = f"bold {PRIMARY_STYLE}"
ASSISTANT_MARKER_TEXT = "● CodeMate"


class NullUI:
    """无输出 UI，用于测试、benchmark 或不需要交互展示的调用场景。"""

    def welcome(self, renderable):
        pass

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def stream_start(self, phase=""):
        pass

    def stream_delta(self, text, phase=""):
        pass

    def stream_end(self, kind="", metadata=None):
        pass

    def tool_start(self, name, args, risk_level=""):
        pass

    def tool_result(self, name, args, result, metadata=None):
        pass

    def compact_start(self, reason=""):
        pass

    def compact_end(self, status="", metadata=None):
        pass

    def commentary(self, text):
        pass

    def approval_request(self, name, args, metadata=None):
        return False

    def request_user_input(self, questions):
        return {"status": "cancelled", "answers": {}}

    def editor_diagnostics(self, path, *, wait_for_update=False):
        """Return no diagnostics when the active UI has no editor integration."""
        return {"status": "unavailable", "diagnostics": []}

    def plan_review(self, title, plan):
        return {"decision": "cancelled"}

    def review_start(self):
        pass

    def review_end(self, status="", metadata=None):
        pass

    def session_menu(self, sessions, current_id=""):
        return None

    def final_answer(self, text):
        pass


class TerminalUI(NullUI):
    """交互式终端展示层，负责把 agent 事件打印成可读的运行过程。"""

    def __init__(self, console=None):
        self.console = console or Console()
        self._stream_phase = ""
        self._stream_marker_printed = False
        self._stream_renderer = MarkdownStreamRenderer(self.console)
        self._model_status = None
        self._streamed_response = False
        self._pending_model_usage = ""

    def welcome(self, renderable):
        """输出启动或运行配置切换后的状态区。"""
        self.console.print(renderable)

    def approval_menu(self, choices):
        # 审批菜单使用 prompt_toolkit 接管按键：
        # 上下键选择，Enter 确认，Ctrl+C 按拒绝处理。
        return self._selection_menu(
            choices,
            hint="Use ↑/↓ to select, Enter to confirm.",
            selected_prefix="> ",
            cancel_result={"allowed": False},
        )

    def session_menu(self, sessions, current_id=""):
        choices = []
        for item in sessions:
            marker = "*" if item.get("id") == current_id else " "
            title = item.get("title") or "(untitled)"
            timestamp = str(item.get("updated_at") or item.get("created_at") or "-").replace("T", " ")[:16]
            choices.append((f"{marker} {item.get('id')}  {title}  {timestamp}", item))
        return self._selection_menu(choices, hint="Use ↑/↓ to select a session, Enter to resume.", cancel_result=None)

    def _selection_menu(self, choices, hint="", selected_prefix="> ", cancel_result=None):
        # 通用终端选择框：审批和 session resume 都走这里，
        # 但具体返回值仍由调用方决定，避免 UI 了解业务结构。
        if not choices:
            return None
        selected = 0
        bindings = KeyBindings()

        def menu_text():
            fragments = [("class:hint", f"{hint}\n")]
            for index, (_label, _decision) in enumerate(choices):
                prefix = selected_prefix if index == selected else " " * len(selected_prefix)
                style = "class:selected" if index == selected else ""
                fragments.append((style, f"{prefix}{_label}\n"))
            return FormattedText(fragments)

        control = FormattedTextControl(menu_text)

        @bindings.add("up")
        def _(event):
            nonlocal selected
            selected = (selected - 1) % len(choices)
            event.app.invalidate()

        @bindings.add("down")
        def _(event):
            nonlocal selected
            selected = (selected + 1) % len(choices)
            event.app.invalidate()

        @bindings.add("enter")
        def _(event):
            event.app.exit(result=choices[selected][1])

        @bindings.add("c-c")
        def _(event):
            event.app.exit(result=cancel_result)

        app = Application(
            layout=Layout(Window(content=control, always_hide_cursor=True)),
            key_bindings=bindings,
            style=Style.from_dict(
                {
                    "hint": "fg:#64748b",
                    "selected": "fg:#f0f2f4 bg:#303236 bold",
                }
            ),
            full_screen=False,
            erase_when_done=True,
        )
        return app.run()

    def model_start(self):
        self._stop_model_status()
        self._pending_model_usage = ""
        self._streamed_response = False
        status_factory = getattr(self.console, "status", None)
        if not callable(status_factory):
            return
        self._model_status = status_factory(
            "[italic #9298a1]Thinking...[/]",
            spinner="dots",
            spinner_style=FAINT_STYLE,
        )
        self._model_status.start()

    def model_end(self, kind="", metadata=None):
        self._stop_model_status()
        if kind == "error":
            self._pending_model_usage = ""
            self._streamed_response = False
            return
        self._pending_model_usage = summarize_model_usage(metadata)
        if self._streamed_response:
            self._flush_model_usage()
        self._streamed_response = False

    def stream_start(self, phase=""):
        # 流式文本是正式输出，但 Markdown 需要分块渲染：
        # renderer 会先缓冲未完成语法块，history/trace 仍保存完整原文。
        self._stream_phase = str(phase or "")
        self._stream_marker_printed = False
        self._streamed_response = True
        self._stop_model_status()
        self._stream_renderer.reset()

    def _stop_model_status(self):
        if self._model_status is None:
            return
        self._model_status.stop()
        self._model_status = None

    def _flush_model_usage(self):
        if not self._pending_model_usage:
            return
        line = Text("    model  ", style=FAINT_STYLE)
        line.append(self._pending_model_usage, style=TOOL_DETAIL_STYLE)
        self.console.print(line)
        self._pending_model_usage = ""

    def _print_assistant_marker(self):
        # Commentary 和 final 使用同一标识，避免依赖 provider-specific phase。
        self.console.print(Text(ASSISTANT_MARKER_TEXT, style=ASSISTANT_MARKER_STYLE))

    def stream_delta(self, text, phase=""):
        text = str(text or "")
        if not text:
            return
        target_phase = str(phase or self._stream_phase or "")
        if phase:
            if self._stream_phase and phase != self._stream_phase:
                self._stream_renderer.finish(phase=self._stream_phase)
                self._stream_marker_printed = False
            self._stream_phase = target_phase
        if not self._stream_marker_printed:
            self._print_assistant_marker()
            self._stream_marker_printed = True
        self._stream_renderer.write(text, phase=self._stream_phase)

    def stream_end(self, kind="", metadata=None):
        del kind, metadata
        self._stream_renderer.finish(phase=self._stream_phase)
        self._stream_phase = ""
        self._stream_marker_printed = False

    def compact_start(self, reason=""):
        suffix = f" ({reason})" if reason else ""
        self.console.print(Text(f"  ├─ compact  running{suffix}", style=MUTED_STYLE))

    def compact_end(self, status="", metadata=None):
        metadata = dict(metadata or {})
        if status == "ok":
            text = (
                f"  └─ compact  {metadata.get('history_before_messages', 0)} → "
                f"{metadata.get('history_after_messages', 0)} messages · "
                f"{metadata.get('summary_chars', 0)} summary chars"
            )
            self.console.print(Text(text, style=SUCCESS_STYLE))
        elif status == "error":
            message = f"  └─ compact failed  {metadata.get('reason', 'unknown error')}"
            self.console.print(Text(message, style=ERROR_STYLE))

    def review_start(self):
        self.console.print(Text("  ├─ review  inspecting current changes", style=MUTED_STYLE))
        self._flush_model_usage()

    def review_end(self, status="", metadata=None):
        metadata = dict(metadata or {})
        if status == "ok":
            message = (
                f"  └─ review complete  "
                f"{metadata.get('review_report_chars', 0)} chars"
            )
            self.console.print(Text(message, style=SUCCESS_STYLE))
        elif status == "step_limit":
            self.console.print(Text("  └─ review stopped  step limit reached", style=WARNING_STYLE))
        else:
            self.console.print(Text("  └─ review failed", style=ERROR_STYLE))

    def commentary(self, text):
        text = str(text or "").strip()
        if text:
            self._print_assistant_marker()
            self.console.print(Markdown(text, style=COMMENTARY_STYLE))
            self._flush_model_usage()

    def tool_start(self, name, args, risk_level=""):
        if name in {"submit_plan", "review"}:
            # These tools render their own workflow status. Hide the generic
            # start line so the terminal does not print duplicate events.
            return
        summary = summarize_tool_call(name, args)
        lines = summary.splitlines() or [name]
        title = Text("  ├─ ", style=FAINT_STYLE)
        title.append(lines[0], style=TOOL_CALL_STYLE)
        if risk_level and risk_level != "low":
            risk_style = WARNING_STYLE if risk_level == "high" else TOOL_DETAIL_STYLE
            title.append(f"  {risk_level}", style=risk_style)
        self.console.print(title)
        for line in lines[1:]:
            self.console.print(Text(f"  │  {line.strip()}", style=TOOL_DETAIL_STYLE))
        self._flush_model_usage()

    def tool_result(self, name, args, result, metadata=None):
        del args
        metadata = dict(metadata or {})
        status = metadata.get("tool_status", "ok")
        if name == "review" and metadata.get("review_status"):
            # review_end() already rendered the child status. The complete
            # report remains in history/trace for the parent model.
            return
        if name in COMPACT_RESULT_TOOLS and status == "ok":
            summary = summarize_read_tool_result(name, result, metadata)
            truncation = self._truncation_note(metadata)
            if truncation:
                summary += f" · {truncation}"
            self.console.print(Text(f"  └─ {summary}", style=TOOL_RESULT_STYLE))
            return
        summary = summarize_tool_result(name, result, metadata)
        lines = summary.splitlines()
        if lines and lines[0].startswith("status:"):
            lines = lines[1:]
        truncation = self._truncation_note(metadata)
        if truncation and not any("truncated:" in line for line in lines):
            lines.append(truncation)
        style = TOOL_RESULT_STYLE if status == "ok" else TOOL_ERROR_STYLE
        status_style = SUCCESS_STYLE if status == "ok" else style
        result_line = Text("  └─ ", style=FAINT_STYLE)
        result_line.append(str(status), style=f"bold {status_style}")
        result_line.append(f"  {name}", style=TOOL_RESULT_STYLE)
        self.console.print(result_line)
        for line in lines:
            detail_style = TOOL_DETAIL_STYLE if status == "ok" else style
            self.console.print(Text(f"      {line}", style=detail_style))
        self._render_change_preview(metadata.get("change_preview"))

    @staticmethod
    def _truncation_note(metadata):
        if not metadata.get("tool_result_truncated"):
            return ""
        original = int(metadata.get("tool_result_original_chars", 0) or 0)
        returned = int(metadata.get("tool_result_returned_chars", 0) or 0)
        return f"truncated {original} → {returned} chars"

    def _render_change_preview(self, raw_preview):
        """显示已有变更统计，不把彩色 diff 正文铺到终端。"""
        if not isinstance(raw_preview, dict):
            return
        path = str(raw_preview.get("path", "") or "").strip()
        if not path:
            return
        additions = int(raw_preview.get("additions", 0) or 0)
        deletions = int(raw_preview.get("deletions", 0) or 0)
        heading = Text("      Δ ", style=FAINT_STYLE)
        heading.append(path, style=PRIMARY_STYLE)
        heading.append(f"  +{additions}", style=SUCCESS_STYLE)
        heading.append(f" -{deletions}", style=ERROR_STYLE)
        self.console.print(heading)

    def approval_request(self, name, args, metadata=None):
        metadata = dict(metadata or {})
        risk = metadata.get("risk_level", "")
        title = Text("APPROVAL", style=f"bold {WARNING_STYLE}")
        title.append(f"  {name}", style=PRIMARY_STYLE)
        if risk:
            title.append(f"  ·  {risk}", style=WARNING_STYLE)
        summary = summarize_approval_call(name, args)
        if metadata.get("outside_workspace"):
            summary += "\n\nWarning: target path is outside the current workspace."
        self.console.print(Rule(title, style=BORDER_STYLE))
        self.console.print(Syntax(summary, "text", word_wrap=True, background_color="default", padding=(0, 1)))
        access = str(metadata.get("approval_access", "") or "").strip()
        allow_dir = str(metadata.get("suggested_allow_dir", "") or "").strip()
        shell_subject = str(metadata.get("suggested_shell_subject", "") or "").strip()
        choices = [
            ("Allow once", {"allowed": True}),
        ]
        if shell_subject:
            choices.append(
                (
                    f"Allow all `{shell_subject}` commands this session",
                    {"allowed": True, "remember": {"shell_subject": shell_subject}},
                )
            )
        if access in {"read", "write"} and allow_dir:
            choices.append(
                (
                    f"Allow {access} for {allow_dir} this session",
                    {"allowed": True, "remember": {"access": access, "path": allow_dir}},
                )
            )
        if shell_subject and access in {"read", "write"} and allow_dir:
            choices.append(
                (
                    f"Allow all `{shell_subject}` commands and {access} for {allow_dir} this session",
                    {
                        "allowed": True,
                        "remember": {
                            "shell_subject": shell_subject,
                            "access": access,
                            "path": allow_dir,
                        },
                    },
                )
            )
        choices.append(("Deny", {"allowed": False}))
        try:
            return self.approval_menu(choices)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return {"allowed": False}

    def request_user_input(self, questions):
        """Collect bounded planning decisions without exposing terminal details to runtime."""
        answers = {}
        try:
            for question in questions:
                header = str(question.get("header", "")).strip()
                prompt = str(question.get("question", "")).strip()
                title = Text("QUESTION", style=f"bold {PRIMARY_STYLE}")
                if header:
                    title.append(f"  {header}", style=MUTED_STYLE)
                self.console.print(Rule(title, style=BORDER_STYLE))
                self.console.print(Markdown(prompt))

                choices = []
                for option in question.get("options", []):
                    label = str(option.get("label", "")).strip()
                    description = str(option.get("description", "")).strip()
                    suffix = " (Recommended)" if bool(option.get("recommended", False)) else ""
                    choices.append(
                        (
                            f"{label}{suffix} - {description}",
                            {"type": "option", "value": label},
                        )
                    )
                choices.append(("Other - Enter a custom answer", {"type": "other"}))
                selected = self._selection_menu(
                    choices,
                    hint="Use ↑/↓ to select, Enter to confirm, Ctrl+C to cancel.",
                    cancel_result=None,
                )
                if not selected:
                    return {"status": "cancelled", "answers": {}}
                if selected.get("type") == "other":
                    custom = self.console.input("Other: ").strip()
                    if not custom:
                        return {"status": "cancelled", "answers": {}}
                    selected = {"type": "custom", "value": custom}
                answers[str(question.get("id", "")).strip()] = selected
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return {"status": "cancelled", "answers": {}}
        return {"status": "answered", "answers": answers}

    def plan_review(self, title, plan):
        """Render a submitted plan and return the user's approval decision."""
        heading = Text("PLAN", style=f"bold {PRIMARY_STYLE}")
        heading.append(f"  {title}", style=MUTED_STYLE)
        self.console.print(Rule(heading, style=BORDER_STYLE))
        self.console.print(Markdown(plan))
        self.console.print(Rule(style=BORDER_STYLE))
        self._flush_model_usage()
        choices = [
            ("Approve and implement", {"decision": "approved"}),
            ("Revise plan", {"decision": "revision_requested"}),
            ("Cancel", {"decision": "cancelled"}),
        ]
        try:
            decision = self._selection_menu(
                choices,
                hint="Use ↑/↓ to select, Enter to confirm.",
                cancel_result={"decision": "cancelled"},
            )
            decision = decision or {"decision": "cancelled"}
            if decision.get("decision") == "revision_requested":
                while True:
                    feedback = self.console.input("Revision feedback: ").strip()
                    if feedback:
                        return {"decision": "revision_requested", "feedback": feedback}
                    self.console.print("Revision feedback cannot be empty.")
            return decision
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return {"decision": "cancelled"}

    def final_answer(self, text):
        text = str(text or "").strip()
        if not text:
            return
        self._print_assistant_marker()
        self.console.print(Markdown(text))
        self._flush_model_usage()
