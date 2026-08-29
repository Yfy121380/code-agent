from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    locale: str
    include_deleted: bool

    @classmethod
    def from_headers(cls, headers):
        return cls(
            tenant_id=headers["X-Tenant"],
            locale=headers.get("X-Locale", "en"),
            include_deleted=headers.get("X-Include-Deleted") == "1",
        )
