from .errors import UnknownTopic


class EventPublisher:
    def __init__(self, router, receipts, outbox):
        self.router = router
        self.receipts = receipts
        self.outbox = outbox

    def publish(self, topic, payload, context):
        receipt_key = (context.tenant_id, context.request_id)
        previous = self.receipts.get(receipt_key)
        if previous is not None:
            return {"event": previous, "duplicate": True}
        handler = self.router.resolve(topic)
        if handler is None:
            raise UnknownTopic(topic)
        event = handler(payload, tenant_id=context.tenant_id)
        self.outbox.append(context.tenant_id, topic, event)
        self.receipts.put(receipt_key, event)
        return {"event": event, "duplicate": False}
