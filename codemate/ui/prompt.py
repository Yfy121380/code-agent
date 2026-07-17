"""交互式输入提示模块。

这里集中放置 prompt_toolkit 相关配置：斜杠命令补全、帮助文本、
补全菜单样式和回车键行为。CLI 主循环只负责调用这些对象，
具体的终端输入体验由本文件维护。
"""

import textwrap

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style


SLASH_COMMANDS = [
    ("/help", "/help", "Show this help message."),
    ("/approval", "/approval", "Show current approval policy."),
    ("/approval ask", "/approval ask", "Ask before risky tool calls."),
    ("/approval auto", "/approval auto", "Auto-approve ordinary risky tool calls."),
    ("/approval never", "/approval never", "Deny calls that require approval."),
    ("/approval full", "/approval full", "Auto-approve all approval prompts in this process."),
    ("/provider", "/provider", "Show current model provider."),
    ("/provider openai", "/provider openai", "Use OpenAI-compatible provider."),
    ("/provider anthropic", "/provider anthropic", "Use Anthropic-compatible provider."),
    ("/provider deepseek", "/provider deepseek", "Use DeepSeek provider."),
    ("/model", "/model", "Show current model and available models."),
    ("/model gpt-5.4", "/model gpt-5.4", "Use gpt-5.4."),
    ("/model gpt-5.5", "/model gpt-5.5", "Use gpt-5.5."),
    ("/model claude-sonnet-4-6", "/model claude-sonnet-4-6", "Use claude-sonnet-4-6."),
    ("/model claude-opus-4-8", "/model claude-opus-4-8", "Use claude-opus-4-8."),
    ("/model deepseek-v4-pro", "/model deepseek-v4-pro", "Use deepseek-v4-pro."),
    ("/memory", "/memory", "Show the agent's distilled working memory."),
    ("/remember ", "/remember <text>", "Append a memory entry to today's daily log."),
    ("/dream", "/dream", "Run dream memory consolidation in foreground."),
    ("/dream --background", "/dream --background", "Run dream memory consolidation in background."),
    ("/session", "/session", "Show the path to the saved session file."),
    ("/reset", "/reset", "Clear the current session history and memory."),
    ("/exit", "/exit", "Exit the agent."),
]

HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help                Show this help message.
    /approval            Show current approval policy.
    /approval <mode>     Set approval policy: ask, auto, never, or full.
    /provider            Show current provider and available providers.
    /provider <name>     Set provider: openai, anthropic, or deepseek.
    /model               Show current model and available models.
    /model <name>        Set model from the current provider's allowed model list.
    /memory              Show the agent's distilled working memory.
    /remember <text>     Append a memory entry to today's daily log.
    /dream               Run dream memory consolidation in foreground.
    /dream --background  Run dream memory consolidation in background.
    /session             Show the path to the saved session file.
    /reset               Clear the current session history and memory.
    /exit                Exit the agent.
    """
).strip()

PROMPT_STYLE = Style.from_dict(
    {
        "completion-menu.completion": "fg:#cbd5e1 bg:#111827",
        "completion-menu.completion.current": "fg:#e5e7eb bg:#334155",
        "completion-menu.meta.completion": "fg:#64748b bg:#111827",
        "completion-menu.meta.completion.current": "fg:#cbd5e1 bg:#334155",
    }
)


class SlashCommandCompleter(Completer):
    """为 REPL 中的 `/` 命令提供带说明的补全项。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for inserted_text, display_text, description in SLASH_COMMANDS:
            if display_text.startswith(text) or inserted_text.startswith(text):
                yield Completion(
                    inserted_text,
                    start_position=-len(text),
                    display=display_text,
                    display_meta=description,
                )


def build_prompt_key_bindings():
    """构造输入框按键行为，主要处理补全菜单打开时的回车选择。"""
    bindings = KeyBindings()

    @bindings.add("enter")
    def _(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is not None:
            completion = state.current_completion
            if completion is not None:
                buffer.apply_completion(completion)
            else:
                buffer.cancel_completion()
            return
        buffer.validate_and_handle()

    return bindings
