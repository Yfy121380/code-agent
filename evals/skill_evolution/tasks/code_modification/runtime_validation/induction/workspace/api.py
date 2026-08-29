from executor import BatchExecutor
from planning import resolve_batch_size
from settings import BatchSettings


def bulk_apply(items, writer, *, settings=None, batch_size=None, dry_run=False):
    settings = settings or BatchSettings()
    size = resolve_batch_size(batch_size, settings.default_batch_size)
    return BatchExecutor(writer).apply(list(items), size, dry_run=dry_run)
