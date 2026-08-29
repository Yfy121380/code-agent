from .container import build_service
from .context import RequestContext
from .controller import DocumentController


def preview_document(document_id, headers, container):
    context = RequestContext.from_headers(headers)
    controller = DocumentController(build_service(container))
    return controller.preview(document_id, context)
