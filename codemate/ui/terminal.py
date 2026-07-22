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
from rich.panel import Panel
from rich.syntax import Syntax

from .summaries import COMPACT_RESULT_TOOLS, summarize_read_tool_result, summarize_tool_call, summarize_tool_result


class NullUI:
    """无输出 UI，用于测试、benchmark 或不需要交互展示的调用场景。"""

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
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

    def session_menu(self, sessions, current_id=""):
        return None

    def final_answer(self, text):
        pass


class TerminalUI(NullUI):
    """交互式终端展示层，负责把 agent 事件打印成可读的运行过程。"""

    def __init__(self, console=None):
        self.console = console or Console()

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
                    "selected": "fg:#e5e7eb bg:#334155",
                }
            ),
            full_screen=False,
            erase_when_done=True,
        )
        return app.run()

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def compact_start(self, reason=""):
        suffix = f" ({reason})" if reason else ""
        self.console.print(f"[dim]Compacting history{suffix}...[/dim]")

    def compact_end(self, status="", metadata=None):
        metadata = dict(metadata or {})
        if status == "ok":
            self.console.print(
                "[green]  -> history compacted: "
                f"{metadata.get('history_before_messages', 0)} -> {metadata.get('history_after_messages', 0)} messages, "
                f"summary {metadata.get('summary_chars', 0)} chars[/green]"
            )
        elif status == "error":
            self.console.print(f"[yellow]  -> history compact failed: {metadata.get('reason', 'unknown error')}[/yellow]")

    def commentary(self, text):
        text = str(text or "").strip()
        if text:
            self.console.print(f"[dim]{text}[/dim]")

    def tool_start(self, name, args, risk_level=""):
        summary = summarize_tool_call(name, args)
        if name in COMPACT_RESULT_TOOLS:
            first_line = summary.splitlines()[0] if summary else name
            self.console.print(f"[cyan]◇ {first_line}[/cyan]")
            return
        title = f"tool: {name}"
        if risk_level:
            title += f" ({risk_level})"
        self.console.print(Panel(Syntax(summary, "text", word_wrap=True), title=title, border_style="cyan"))

    def tool_result(self, name, args, result, metadata=None):
        metadata = dict(metadata or {})
        status = metadata.get("tool_status", "ok")
        if name in COMPACT_RESULT_TOOLS and status == "ok":
            self.console.print(f"[green]  -> {summarize_read_tool_result(name, result, metadata)}[/green]")
            return
        border_style = "green" if status == "ok" else "yellow"
        summary = summarize_tool_result(name, result, metadata)
        self.console.print(Panel(Syntax(summary, "text", word_wrap=True), title=f"result: {name}", border_style=border_style))

    def approval_request(self, name, args, metadata=None):
        metadata = dict(metadata or {})
        risk = metadata.get("risk_level", "")
        title = f"approve: {name}"
        if risk:
            title += f" ({risk})"
        summary = summarize_tool_call(name, args)
        if metadata.get("outside_workspace"):
            summary += "\n\nWarning: target path is outside the current workspace."
        self.console.print(Panel(Syntax(summary, "text", word_wrap=True), title=title, border_style="yellow"))
        access = str(metadata.get("approval_access", "") or "").strip()
        allow_dir = str(metadata.get("suggested_allow_dir", "") or "").strip()
        choices = [
            ("Allow once", {"allowed": True}),
        ]
        if access in {"read", "write"} and allow_dir:
            choices.append(
                (
                    f"Allow {access} for {allow_dir} this session",
                    {"allowed": True, "remember": {"access": access, "path": allow_dir}},
                )
            )
        choices.append(("Deny", {"allowed": False}))
        try:
            return self.approval_menu(choices)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return {"allowed": False}

    def final_answer(self, text):
        text = str(text or "").strip()
        if not text:
            return
        self.console.print(Panel(Markdown(text), title="codemate", border_style="green"))
