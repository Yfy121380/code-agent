from policy import resolve_timeout_ms
from settings import DispatchSettings
from transport import MessageDispatcher


def dispatch_messages(
    messages,
    transport,
    *,
    settings=None,
    timeout_ms=None,
    validate_only=False,
):
    settings = settings or DispatchSettings()
    timeout = resolve_timeout_ms(timeout_ms, settings.default_timeout_ms)
    return MessageDispatcher(transport).dispatch(
        messages,
        timeout,
        validate_only=validate_only,
    )
