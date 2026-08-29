class BatchExecutor:
    def __init__(self, writer):
        self.writer = writer

    def apply(self, items, batch_size, *, dry_run=False):
        batches = [
            items[index : index + batch_size]
            for index in range(0, len(items), batch_size)
        ]
        if not dry_run:
            for batch in batches:
                self.writer.write(batch)
        return {
            "processed": len(items),
            "batches": len(batches),
            "written": 0 if dry_run else len(items),
        }
