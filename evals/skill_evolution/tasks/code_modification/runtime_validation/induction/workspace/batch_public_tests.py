from api import bulk_apply
from settings import BatchSettings


class Writer:
    def __init__(self):
        self.batches = []

    def write(self, batch):
        self.batches.append(list(batch))


def test_bulk_apply_uses_explicit_batch_size():
    writer = Writer()

    result = bulk_apply(range(5), writer, batch_size=2)

    assert writer.batches == [[0, 1], [2, 3], [4]]
    assert result == {"processed": 5, "batches": 3, "written": 5}


def test_bulk_apply_uses_default_without_writing_in_dry_run():
    writer = Writer()

    result = bulk_apply(
        range(5),
        writer,
        settings=BatchSettings(default_batch_size=2),
        dry_run=True,
    )

    assert writer.batches == []
    assert result == {"processed": 5, "batches": 3, "written": 0}
