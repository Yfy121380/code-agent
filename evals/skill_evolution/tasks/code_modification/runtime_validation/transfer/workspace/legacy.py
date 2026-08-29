def send_legacy(transport, message):
    """Older fire-and-forget path unrelated to dispatch_messages."""
    transport.send(message, timeout_ms=1000)
