# Message dispatch helper

Run the public tests with `pytest -q dispatch_public_tests.py`.

`dispatch_messages` accepts an optional `timeout_ms`:

- `None` means to use `DispatchSettings.default_timeout_ms`.
- Explicit values must be non-boolean positive integers.
- Invalid input must fail before the transport is called.

Keep the public API dependency-free. Preserve normal delivery and `validate_only`, which
returns the delivery plan without calling the transport.
