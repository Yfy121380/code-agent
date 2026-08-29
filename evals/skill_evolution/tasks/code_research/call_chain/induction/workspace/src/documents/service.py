from .errors import DocumentMissing


class DocumentService:
    def __init__(self, repository, cache, renderer, policy_factory):
        self.repository = repository
        self.cache = cache
        self.renderer = renderer
        self.policy_factory = policy_factory

    def preview(self, document_id, context):
        cache_key = (context.tenant_id, document_id, context.include_deleted)
        record = self.cache.get(cache_key)
        if record is None:
            record = self.repository.fetch(
                context.tenant_id,
                document_id,
                include_deleted=context.include_deleted,
            )
            if record is None:
                raise DocumentMissing(document_id)
            self.cache.put(cache_key, record)
        policy = self.policy_factory.for_tenant(context.tenant_id)
        return self.renderer.render(
            record,
            locale=context.locale,
            redact=policy.redact,
        )
