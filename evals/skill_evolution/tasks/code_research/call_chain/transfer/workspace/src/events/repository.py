from .errors import OutboxUnavailable


class OutboxRepository:
    def __init__(self, gateway, *, stream):
        self.gateway = gateway
        self.stream = stream

    def append(self, tenant_id, topic, event):
        try:
            self.gateway.append(
                stream=self.stream,
                tenant_id=tenant_id,
                topic=topic,
                event=event,
            )
        except ConnectionError as exc:
            raise OutboxUnavailable(topic) from exc
