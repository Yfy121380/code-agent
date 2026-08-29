from dataclasses import dataclass

from .publisher import EventPublisher
from .repository import OutboxRepository


@dataclass
class Container:
    settings: object
    router: object
    receipts: object
    outbox_gateway: object


def build_publisher(container):
    outbox = OutboxRepository(
        container.outbox_gateway,
        stream=container.settings.outbox_stream,
    )
    return EventPublisher(container.router, container.receipts, outbox)
