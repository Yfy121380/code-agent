from api import dispatch_messages
from settings import DispatchSettings


class Transport:
    def __init__(self):
        self.calls = []

    def send(self, message, *, timeout_ms):
        self.calls.append((dict(message), timeout_ms))


def test_dispatch_messages_uses_explicit_timeout():
    transport = Transport()
    messages = [{"id": 1}, {"id": 2}]

    result = dispatch_messages(messages, transport, timeout_ms=250)

    assert transport.calls == [({"id": 1}, 250), ({"id": 2}, 250)]
    assert result == {"messages": 2, "timeout_ms": 250, "sent": 2}


def test_validate_only_uses_default_without_sending():
    transport = Transport()

    result = dispatch_messages(
        [{"id": 1}],
        transport,
        settings=DispatchSettings(default_timeout_ms=750),
        validate_only=True,
    )

    assert transport.calls == []
    assert result == {"messages": 1, "timeout_ms": 750, "sent": 0}
