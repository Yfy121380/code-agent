"""终端启动状态渲染。

这里生成一个紧凑的 Rich 状态区，只读取现有 agent 属性，不参与配置、
权限或 session 生命周期。CLI 启动和切换运行配置后都会重新渲染它。
"""

from urllib.parse import urlsplit, urlunsplit

from rich.console import Group
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


PRIMARY = "#e6e8eb"
MUTED = "#9298a1"
FAINT = "#646b74"
BORDER = "#34383d"


def _safe_endpoint(value):
    """显示 endpoint 时移除 URL 中可能存在的用户名和密码。"""
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if not parsed.hostname:
        return text
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return text
    if port:
        host = f"{host}:{port}"
    # 查询参数常被用于传递 token；状态区只需显示协议、主机和 API 路径。
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _sandbox_label(agent):
    sandbox = getattr(getattr(agent, "settings", None), "sandbox", {}) or {}
    enabled = bool(sandbox.get("enabled", True))
    if enabled and str(getattr(agent, "approval_policy", "")) == "full":
        return "bypassed (full)"
    return "on" if enabled else "off"


def _session_label(agent):
    session_id = str(agent.session.get("id", "") or "")
    title = str(agent.session.get("title", "") or "").strip()
    short_id = session_id[-6:] if session_id else "-"
    return f"{title} · {short_id}" if title else short_id


def build_welcome(agent, model, host):
    """构造无外框的启动状态区，展示当前任务环境中的关键运行信息。"""
    header = Text()
    header.append("CODEMATE", style=f"bold {PRIMARY}")
    header.append("  LOCAL CODING AGENT", style=FAINT)

    details = Table.grid(expand=True, padding=(0, 2))
    details.add_column(width=10, no_wrap=True, style=FAINT)
    details.add_column(ratio=1, overflow="fold", style=PRIMARY)
    details.add_row("WORKSPACE", str(agent.workspace.cwd))
    details.add_row("MODEL", str(model or "-"))
    details.add_row("ENDPOINT", _safe_endpoint(host))
    details.add_row("BRANCH", str(agent.workspace.branch or "-"))
    details.add_row("SESSION", _session_label(agent))

    mode = str(agent.session.get("workflow_mode", "agent") or "agent").upper()
    stream = "on" if bool(getattr(agent, "stream", False)) else "off"
    runtime = Text()
    runtime.append(mode, style=f"bold {PRIMARY}")
    runtime.append("  ·  approval ", style=MUTED)
    runtime.append(str(agent.approval_policy), style=PRIMARY)
    runtime.append("  ·  stream ", style=MUTED)
    runtime.append(stream, style=PRIMARY)
    runtime.append("  ·  sandbox ", style=MUTED)
    runtime.append(_sandbox_label(agent), style=PRIMARY)
    details.add_row("RUNTIME", runtime)

    return Group(
        Padding(header, (1, 1, 0, 1)),
        Rule(style=BORDER),
        Padding(details, (0, 1)),
        Rule(style=BORDER),
    )
