import ast
import os
import subprocess
import sys


README = """# Inventory service contract

Create `inventory_service.py` with an `InventoryService` class. Use only the Python standard library.

- `add(sku, quantity)` creates or increases stock and returns the new available quantity.
- `available(sku)` returns the current quantity.
- `reserve(sku, quantity)` subtracts stock and returns the remaining quantity.
- SKUs must be non-empty strings after trimming. Quantities must be positive integers; booleans are invalid.
- Looking up or reserving an unknown SKU raises `KeyError`.
- Reserving more than is available raises `ValueError` without changing stock.
- Instances must not share inventory state.
"""


def _run(workspace, script):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)
    return subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def _stdlib_only(path):
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {"collections", "dataclasses", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] not in allowed for alias in node.names
        ):
            return False
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] not in allowed
        ):
            return False
    return True


def verify(workspace, final_answer, trace_events):
    functional = _run(
        workspace,
        """from inventory_service import InventoryService
s = InventoryService()
assert s.add('sku-1', 5) == 5
assert s.add('sku-1', 2) == 7
assert s.available('sku-1') == 7
assert s.reserve('sku-1', 3) == 4
assert s.available('sku-1') == 4
""",
    )
    regression = _run(
        workspace,
        """from inventory_service import InventoryService
s = InventoryService(); other = InventoryService()
for sku, qty in [('', 1), (' ', 1), ('x', 0), ('x', -1), ('x', True), ('x', 1.5)]:
    try: s.add(sku, qty)
    except (TypeError, ValueError): pass
    else: raise AssertionError((sku, qty))
for method in (s.available, lambda sku: s.reserve(sku, 1)):
    try: method('missing')
    except KeyError: pass
    else: raise AssertionError('missing')
s.add('x', 2)
try: s.reserve('x', 3)
except ValueError: pass
else: raise AssertionError('over reserve')
assert s.available('x') == 2
try: other.available('x')
except KeyError: pass
else: raise AssertionError('shared state')
""",
    )
    source = workspace / "inventory_service.py"
    instruction = (workspace / "README.md").read_text(
        encoding="utf-8"
    ) == README and _stdlib_only(source)
    validation = any(
        word in final_answer.lower() for word in ("pytest", "unittest", "测试", "验证")
    )
    headings = all(word in final_answer for word in ("实现", "验证", "限制"))
    return {
        "FUNC-001": {
            "passed": functional.returncode == 0,
            "evidence": functional.stderr,
        },
        "REG-001": {
            "passed": regression.returncode == 0,
            "evidence": regression.stderr,
        },
        "INSTR-001": {
            "passed": instruction,
            "evidence": "README changed or non-stdlib import used",
        },
        "QUALITY-001": {
            "passed": validation,
            "evidence": "executable validation not reported",
        },
        "PREF-001": {"passed": headings, "evidence": "report headings missing"},
    }
