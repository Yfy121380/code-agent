from dataclasses import dataclass


@dataclass(frozen=True)
class PublishContext:
    tenant_id: str
    request_id: str

    @classmethod
    def from_headers(cls, headers):
        return cls(
            tenant_id=headers["X-Tenant"],
            request_id=headers["X-Request-Id"],
        )
