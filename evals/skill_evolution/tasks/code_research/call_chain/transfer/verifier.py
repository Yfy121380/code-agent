import hashlib
import re


SOURCE_HASHES = {
    "__init__.py": "7aa655dddf282fe686a076a0bc6a86cf309df7d85648ce6a38aaae3cd4e590bf",
    "api.py": "78dbb2d376b4e8b9ee30642f2673f06316210cde8765d209169eb6c6652b42be",
    "container.py": "a30b142c7a36dcc274a76cd398af0b70e72d6f335950f85f9dff8185dec411df",
    "context.py": "5d526c9564b879c80c53304bb23e5e7994b5fc558923a073732042a71307abbc",
    "controller.py": "3998aad588664e087ef910251f315cfeffa1f04804243985aaeb805025465849",
    "errors.py": "a6a82ab20defa05578f083db4c3c506114a1e3e0503865a8fc805308e7d9488e",
    "handlers.py": "0e4d905914167bb37354e08160f02a3a77d5aabfa4e9546d974c534f2ff4a7ce",
    "legacy.py": "1a7a341b924cea6b96b297afe03cc7fe241ffd4a0d067fad47c98427973c4e58",
    "publisher.py": "79f7bc12c7ad922c4d91cd542ee3db6d29940259580d14b484aaaee41b3d7f1c",
    "repository.py": "4cd0a3f039918d3e8221868c10e8cc49f1cb30069fce59ba1abbc15bf1fa0866",
}
REFERENCE_RANGES = {
    "api.py": (6, 9),
    "container.py": (15, 20),
    "context.py": (10, 14),
    "controller.py": (8, 16),
    "publisher.py": (10, 21),
    "repository.py": (9, 18),
}


def _reference_files(workspace, final_answer):
    root = workspace / "src" / "events"
    found = set()
    for name, line_text in re.findall(r"src/events/([a-z_]+\.py):(\d+)", final_answer):
        path = root / name
        if not path.is_file() or name not in REFERENCE_RANGES:
            continue
        line = int(line_text)
        start, end = REFERENCE_RANGES[name]
        if start <= line <= end:
            found.add(name)
    return found


def verify(workspace, final_answer, trace_events):
    source_root = workspace / "src" / "events"
    unchanged = all(
        (source_root / name).is_file()
        and hashlib.sha256((source_root / name).read_bytes()).hexdigest() == digest
        for name, digest in SOURCE_HASHES.items()
    )
    active_chain = all(
        term in final_answer
        for term in (
            "publish_event",
            "PublishContext.from_headers",
            "build_publisher",
            "PublishController",
            "EventPublisher.publish",
            "router.resolve",
            "outbox.append",
            "receipts.put",
        )
    )
    branches_and_configuration = all(
        term in final_answer
        for term in (
            "outbox_stream",
            "tenant_id",
            "request_id",
            "receipts.get",
            "duplicate",
            "handler",
        )
    )
    error_flow = all(
        term in final_answer
        for term in (
            "UnknownTopic",
            "InvalidPayload",
            "ConnectionError",
            "OutboxUnavailable",
            "HttpError",
            "404",
            "422",
            "503",
        )
    )
    reference_files = _reference_files(workspace, final_answer)
    references = set(REFERENCE_RANGES).issubset(reference_files)
    headings = all(
        name in final_answer for name in ("入口与装配", "核心调用与分支", "错误传播")
    )
    return {
        "FUNC-001": {
            "passed": active_chain,
            "evidence": "active event chain incomplete",
        },
        "FUNC-002": {
            "passed": branches_and_configuration,
            "evidence": "idempotency, context, handler, or configuration flow missing",
        },
        "FUNC-003": {
            "passed": error_flow,
            "evidence": "error translation chain incomplete",
        },
        "INSTR-001": {
            "passed": unchanged,
            "evidence": "investigated source files changed",
        },
        "QUALITY-001": {
            "passed": references,
            "evidence": "missing valid references for active-path modules",
        },
        "PREF-001": {
            "passed": headings,
            "evidence": "required research headings missing",
        },
    }
