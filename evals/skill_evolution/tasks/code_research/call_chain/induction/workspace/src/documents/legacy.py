def preview_document(document_id, database):
    """Legacy entry point retained for callers outside the current service."""
    return database.get(document_id)
