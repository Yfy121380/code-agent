from dataclasses import dataclass

from .repository import DocumentRepository
from .service import DocumentService


@dataclass
class Container:
    settings: object
    gateway: object
    cache: object
    renderer: object
    policy_factory: object


def build_service(container):
    repository = DocumentRepository(
        container.gateway,
        table=container.settings.document_table,
    )
    return DocumentService(
        repository,
        container.cache,
        container.renderer,
        container.policy_factory,
    )
