from .errors import DocumentMissing, HttpError, StorageUnavailable


class DocumentController:
    def __init__(self, service):
        self.service = service

    def preview(self, document_id, context):
        try:
            return self.service.preview(document_id, context)
        except DocumentMissing as exc:
            raise HttpError(404, "document not found") from exc
        except StorageUnavailable as exc:
            raise HttpError(503, "document storage unavailable") from exc
