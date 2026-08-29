def split_legacy(items, size=50):
    """Compatibility helper retained for an older import path."""
    return [items[index : index + size] for index in range(0, len(items), size)]
