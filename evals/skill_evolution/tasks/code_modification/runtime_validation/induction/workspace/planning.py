def resolve_batch_size(value, default):
    """Resolve the caller override before the executor creates batches."""
    return value or default
