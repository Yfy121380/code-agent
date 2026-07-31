"""Runtime-level failures that need distinct run lifecycle handling."""


class ModelRequestError(RuntimeError):
    """The model request failed before a complete response was available."""
