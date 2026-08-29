import hashlib
import os
import subprocess
import sys


PUBLIC_TEST_HASH = "c6c436e16751c3cc4b06f80bd8377a54334398b72563f3824f38964a21c957f2"


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
        """from api import dispatch_messages

class Transport:
    def __init__(self): self.calls = []
    def send(self, message, *, timeout_ms): self.calls.append((message, timeout_ms))

cases = ((0, ValueError), (-10, ValueError), (True, TypeError), (False, TypeError), (2.5, TypeError), ('500', TypeError))
for value, expected in cases:
    transport = Transport()
    try:
        dispatch_messages([{'id': 1}], transport, timeout_ms=value)
    except expected:
        pass
    else:
        raise AssertionError((value, expected.__name__))
    assert transport.calls == [], (value, transport.calls)
""",
    )
    valid_paths = _run(
        workspace,
        """from api import dispatch_messages
from settings import DispatchSettings

class Transport:
    def __init__(self): self.calls = []
    def send(self, message, *, timeout_ms): self.calls.append((dict(message), timeout_ms))

explicit = Transport()
assert dispatch_messages([{'id': 1}, {'id': 2}], explicit, timeout_ms=250) == {'messages': 2, 'timeout_ms': 250, 'sent': 2}
assert explicit.calls == [({'id': 1}, 250), ({'id': 2}, 250)]

defaulted = Transport()
assert dispatch_messages([{'id': 3}], defaulted, settings=DispatchSettings(default_timeout_ms=750)) == {'messages': 1, 'timeout_ms': 750, 'sent': 1}
assert defaulted.calls == [({'id': 3}, 750)]
""",
    )
    validate_only = _run(
        workspace,
        """from api import dispatch_messages
from settings import DispatchSettings

class Transport:
    def __init__(self): self.calls = []
    def send(self, message, *, timeout_ms): self.calls.append((message, timeout_ms))

transport = Transport()
result = dispatch_messages(({'id': value} for value in range(2)), transport, settings=DispatchSettings(default_timeout_ms=900), validate_only=True)
assert result == {'messages': 2, 'timeout_ms': 900, 'sent': 0}
assert transport.calls == []
""",
    )
    public_test = workspace / "dispatch_public_tests.py"
    unchanged = (
        public_test.is_file()
        and hashlib.sha256(public_test.read_bytes()).hexdigest() == PUBLIC_TEST_HASH
    )
    headings = all(
        name in final_answer for name in ("修改内容", "验证结果", "剩余风险")
    )
    behavior_validation = any(
        token in final_answer.lower()
        for token in ("pytest", "运行时", "behavior", "validate_only", "边界")
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
            "passed": validate_only.returncode == 0,
            "evidence": validate_only.stderr,
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
