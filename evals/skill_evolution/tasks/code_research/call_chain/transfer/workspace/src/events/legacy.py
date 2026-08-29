def publish_event(topic, payload, registry, store):
    """Legacy synchronous publisher outside the current container path."""
    event = registry[topic](payload)
    store.append((topic, event))
    return event
