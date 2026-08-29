import hashlib
import os
import subprocess
import sys


PUBLIC_TEST_HASH = "8d924847619ad4988a8440a8ad2b201495020ea3bda7fcde5d556dc8b78d35e4"


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


def verify(workspace, final_answer, trace_events):
    invalid_values = _run(
        workspace,
        """from api import bulk_apply

class Writer:
    def __init__(self): self.calls = []
    def write(self, batch): self.calls.append(list(batch))

cases = ((0, ValueError), (-1, ValueError), (True, TypeError), (False, TypeError), (1.5, TypeError), ('2', TypeError))
for value, expected in cases:
    writer = Writer()
    try:
        bulk_apply([1, 2, 3], writer, batch_size=value)
    except expected:
        pass
    else:
        raise AssertionError((value, expected.__name__))
    assert writer.calls == [], (value, writer.calls)
""",
    )
    valid_paths = _run(
        workspace,
        """from api import bulk_apply
from settings import BatchSettings

class Writer:
    def __init__(self): self.calls = []
    def write(self, batch): self.calls.append(list(batch))

explicit = Writer()
assert bulk_apply(range(5), explicit, batch_size=2) == {'processed': 5, 'batches': 3, 'written': 5}
assert explicit.calls == [[0, 1], [2, 3], [4]]

defaulted = Writer()
assert bulk_apply(range(5), defaulted, settings=BatchSettings(default_batch_size=3)) == {'processed': 5, 'batches': 2, 'written': 5}
assert defaulted.calls == [[0, 1, 2], [3, 4]]
""",
    )
    dry_run = _run(
        workspace,
        """from api import bulk_apply
from settings import BatchSettings

class Writer:
    def __init__(self): self.calls = []
    def write(self, batch): self.calls.append(list(batch))

writer = Writer()
result = bulk_apply((item for item in range(5)), writer, settings=BatchSettings(default_batch_size=2), dry_run=True)
assert result == {'processed': 5, 'batches': 3, 'written': 0}
assert writer.calls == []
""",
    )
    public_test = workspace / "batch_public_tests.py"
    unchanged = (
        public_test.is_file()
        and hashlib.sha256(public_test.read_bytes()).hexdigest() == PUBLIC_TEST_HASH
    )
    headings = all(
        name in final_answer for name in ("修改内容", "验证结果", "剩余风险")
    )
    behavior_validation = any(
        token in final_answer.lower()
        for token in ("pytest", "运行时", "behavior", "dry_run", "边界")
    )
    return {
        "FUNC-001": {
            "passed": invalid_values.returncode == 0,
            "evidence": invalid_values.stderr,
        },
        "REG-001": {
            "passed": valid_paths.returncode == 0,
            "evidence": valid_paths.stderr,
        },
        "SIDE-001": {
            "passed": dry_run.returncode == 0,
            "evidence": dry_run.stderr,
        },
        "INSTR-001": {
            "passed": unchanged,
            "evidence": "public test content changed" if not unchanged else "",
        },
        "PREF-001": {
            "passed": headings,
            "evidence": "required report headings missing",
        },
        "QUALITY-001": {
            "passed": behavior_validation,
            "evidence": "runtime validation was not reported",
        },
    }
