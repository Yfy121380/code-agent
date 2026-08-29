from .container import build_publisher
from .context import PublishContext
from .controller import PublishController


def publish_event(topic, payload, headers, container):
    context = PublishContext.from_headers(headers)
    controller = PublishController(build_publisher(container))
    return controller.publish(topic, payload, context)
