"""工作区基础工具测试。

覆盖模块：workspace.now。
重点边界：运行时间使用 Asia/Shanghai 时区，保证 trace/session 时间可读。
"""

from datetime import datetime

from codemate.workspace import now


def test_now_uses_china_timezone():
    timestamp = now()
    parsed = datetime.fromisoformat(timestamp)

    assert parsed.utcoffset().total_seconds() == 8 * 60 * 60
    assert timestamp.endswith("+08:00") or "+08:00" in timestamp
