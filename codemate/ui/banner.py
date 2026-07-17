"""终端状态栏渲染模块。

本文件负责生成 codemate 启动和状态变更时展示的顶部信息块。
它只处理展示布局，不创建 agent、不读取配置，也不参与 REPL 控制流程。
CLI 在启动、切换模型或调整审批策略后复用这里的渲染函数。
"""

import shutil

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
    审批策略和 session。调用方负责传入已经格式化好的模型名称，
    例如 `openai:gpt-5.4`。
    """
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width
    del host

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])
