class UnknownTopic(LookupError):
    pass


class InvalidPayload(ValueError):
    pass


class OutboxUnavailable(RuntimeError):
    pass


class HttpError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
