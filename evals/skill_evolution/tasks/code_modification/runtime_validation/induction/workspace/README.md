# Batch application helper

Run the public tests with `pytest -q batch_public_tests.py`.

`bulk_apply` accepts an optional `batch_size`:

- `None` means to use `BatchSettings.default_batch_size`.
- Explicit values must be non-boolean positive integers.
- Invalid input must fail before the writer receives any batch.

Keep the public API dependency-free. Preserve normal writes and `dry_run`, which plans
the same batches without calling the writer.
