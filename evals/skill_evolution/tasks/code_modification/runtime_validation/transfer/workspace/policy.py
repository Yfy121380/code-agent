def resolve_timeout_ms(value, default):
    """Resolve a per-call timeout before dispatch begins."""
    return value or default
