"""终端 UI 实现模块。

这里提供默认的空 UI 和面向交互式终端的 TerminalUI。
runtime 通过这些方法汇报模型请求、工具调用、审批和最终回答，
TerminalUI 再使用 rich 与 prompt_toolkit 把信息渲染成清晰的终端输出。
"""

from prompt_toolkit import prompt
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from .summaries import summarize_read_tool_result, summarize_tool_call, summarize_tool_result


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

    def approval_request(self, name, args, metadata=None):
        return False

    def final_answer(self, text):
        pass


class TerminalUI(NullUI):
    """交互式终端展示层，负责把 agent 事件打印成可读的运行过程。"""

    def __init__(self, console=None):
        self.console = console or Console()

    def model_start(self):
        self.console.print("[dim]Thinking...[/dim]")

    def model_end(self, kind="", metadata=None):
        pass

    def tool_start(self, name, args, risk_level=""):
        summary = summarize_tool_call(name, args)
        if name in {"list_files", "read_file", "grep"}:
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
        if name in {"list_files", "read_file", "grep"} and status == "ok":
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
        try:
            answer = prompt("Approve? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return False
        return answer.strip().lower() in {"y", "yes"}

    def final_answer(self, text):
        text = str(text or "").strip()
        if not text:
            return
        self.console.print(Panel(Markdown(text), title="codemate", border_style="green"))
