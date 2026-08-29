class DocumentMissing(LookupError):
    pass


class StorageUnavailable(RuntimeError):
    pass


class HttpError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
