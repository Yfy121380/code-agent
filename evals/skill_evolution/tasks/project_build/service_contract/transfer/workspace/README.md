# Inventory service contract

Create `inventory_service.py` with an `InventoryService` class. Use only the Python standard library.

- `add(sku, quantity)` creates or increases stock and returns the new available quantity.
- `available(sku)` returns the current quantity.
- `reserve(sku, quantity)` subtracts stock and returns the remaining quantity.
- SKUs must be non-empty strings after trimming. Quantities must be positive integers; booleans are invalid.
- Looking up or reserving an unknown SKU raises `KeyError`.
- Reserving more than is available raises `ValueError` without changing stock.
- Instances must not share inventory state.
