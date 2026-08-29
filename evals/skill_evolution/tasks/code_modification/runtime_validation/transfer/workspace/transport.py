class MessageDispatcher:
    def __init__(self, transport):
        self.transport = transport

    def dispatch(self, messages, timeout_ms, *, validate_only=False):
        plan = [dict(message) for message in messages]
        if not validate_only:
            for message in plan:
                self.transport.send(message, timeout_ms=timeout_ms)
        return {
            "messages": len(plan),
            "timeout_ms": timeout_ms,
            "sent": 0 if validate_only else len(plan),
        }
