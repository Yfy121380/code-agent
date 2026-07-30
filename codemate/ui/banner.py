"""终端状态栏渲染模块。

本文件负责生成 codemate 启动和状态变更时展示的顶部信息块。
它只处理展示布局，不创建 agent、不读取配置，也不参与 REPL 控制流程。
CLI 在启动、切换模型或调整审批策略后复用这里的渲染函数。
"""

import shutil
import unicodedata

from ..workspace import middle


WELCOME_ART = (
    "        /\\_/\\",
    "       ( >w< )",
    "       /|  V  |\\",
    "       /_|_____|_\\",
    "         Ciallo~",
)


def build_welcome(agent, model, host):
    """构造当前终端状态块。

    状态块展示的是运行期用户最关心的几项信息：工作区、模型、分支、
    工作流模式、审批策略和 session。调用方负责传入已经格式化好的模型名称，
    例如 `openai:gpt-5.4`。
    """
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width
    del host

    def display_width(text):
        width = 0
        for char in str(text):
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    def pad_display(text, size):
        text = str(text)
        return text + " " * max(0, size - display_width(text))

    def clip_display(text, size):
        text = str(text)
        if display_width(text) <= size:
            return text
        if size <= 3:
            result = ""
            for char in text:
                char_width = display_width(char)
                if display_width(result) + char_width > size:
                    break
                result += char
            return result
        result = ""
        for char in text:
            char_width = display_width(char)
            if display_width(result) + char_width > size - 3:
                break
            result += char
        return result + "..."

    def row(text):
        body = middle(text, width - 4)
        return f"| {pad_display(body, width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = clip_display(text, inner)
        left = max(0, (inner - display_width(body)) // 2)
        right = max(0, inner - display_width(body) - left)
        return f"| {' ' * left}{body}{' ' * right} |"

    def clip_cell_text(text, size):
        return clip_display(text, size)

    def cell(label, value, size):
        label_text = f"{label:<9} "
        value_size = max(0, size - len(label_text))
        body = label_text + clip_cell_text(value, value_size)
        return pad_display(body, size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    session_title = str(agent.session.get("title", "") or "").strip()
    session_label = agent.session["id"]
    if session_title:
        session_label = f"{session_title} ({agent.session['id'][-6:]})"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("MODE", str(agent.session.get("workflow_mode", "agent")).upper(), "APPROVAL", agent.approval_policy),
            row("SESSION    " + session_label),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])
