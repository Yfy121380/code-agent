from .errors import StorageUnavailable


class DocumentRepository:
    def __init__(self, gateway, *, table):
        self.gateway = gateway
        self.table = table

    def fetch(self, tenant_id, document_id, *, include_deleted):
        try:
            return self.gateway.get(
                table=self.table,
                tenant_id=tenant_id,
                document_id=document_id,
                include_deleted=include_deleted,
            )
        except TimeoutError as exc:
            raise StorageUnavailable(document_id) from exc
