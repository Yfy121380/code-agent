from .errors import InvalidPayload


def normalize_user(payload, *, tenant_id):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise InvalidPayload("name is required")
    return {"kind": "user", "tenant_id": tenant_id, "name": name}
