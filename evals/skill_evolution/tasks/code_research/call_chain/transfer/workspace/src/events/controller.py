from .errors import HttpError, InvalidPayload, OutboxUnavailable, UnknownTopic


class PublishController:
    def __init__(self, publisher):
        self.publisher = publisher

    def publish(self, topic, payload, context):
        try:
            return self.publisher.publish(topic, payload, context)
        except UnknownTopic as exc:
            raise HttpError(404, "unknown topic") from exc
        except InvalidPayload as exc:
            raise HttpError(422, "invalid event payload") from exc
        except OutboxUnavailable as exc:
            raise HttpError(503, "outbox unavailable") from exc
