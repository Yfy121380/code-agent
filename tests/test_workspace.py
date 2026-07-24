from datetime import datetime

from codemate.workspace import now


def test_now_uses_china_timezone():
    timestamp = now()
    parsed = datetime.fromisoformat(timestamp)

    assert parsed.utcoffset().total_seconds() == 8 * 60 * 60
    assert timestamp.endswith("+08:00") or "+08:00" in timestamp
