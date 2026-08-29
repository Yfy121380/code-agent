import hashlib
import re


SOURCE_HASHES = {
    "__init__.py": "f4f7146de7af78052c45148a56b1eb9c0c58a9c9c0953a053ae95b657b8bb3bd",
    "api.py": "0c2de696eb5e6a8149678ff96f20a29a4b833f91c5659728b57aafabf839befa",
    "container.py": "ddfdaec62d52f3a7ffa3a258121145a51395a08642740da9ca9c07915742cd94",
    "context.py": "3c34b6fee2a24760f9c3d65ef582fc21c50c016f22a60cdd65801fc6610be449",
    "controller.py": "545e310369e2e4ed4be0ca5988b8727932daae9540e8c0f3fb7b60fbaf6dd233",
    "errors.py": "46708249205ad9923ee7576dcfeff8d5290cddd01f9da0ca3c127a3f78c2841c",
    "legacy.py": "b8e75727cceb9ebc5ada7db44ef1ce6ebe289a46bcb20eca79cb18aecc49f099",
    "repository.py": "dd63f9fa54345da31d1a1a012b30e31cceb621d5db3b4ffda9edc5cc0f347a59",
    "service.py": "a4062995ace75ad39b1e838af4bc5e69421d89dc51766cae312eb530fd438e6e",
}
REFERENCE_RANGES = {
    "api.py": (6, 9),
    "container.py": (16, 26),
    "context.py": (11, 16),
    "controller.py": (8, 14),
    "repository.py": (9, 18),
    "service.py": (11, 28),
}


def _reference_files(workspace, final_answer):
    root = workspace / "src" / "documents"
    found = set()
    for name, line_text in re.findall(
        r"src/documents/([a-z_]+\.py):(\d+)", final_answer
    ):
        path = root / name
        if not path.is_file() or name not in REFERENCE_RANGES:
            continue
        line = int(line_text)
        start, end = REFERENCE_RANGES[name]
        if start <= line <= end:
            found.add(name)
    return found


def verify(workspace, final_answer, trace_events):
    source_root = workspace / "src" / "documents"
    unchanged = all(
        (source_root / name).is_file()
        and hashlib.sha256((source_root / name).read_bytes()).hexdigest() == digest
        for name, digest in SOURCE_HASHES.items()
    )
    active_chain = all(
        term in final_answer
        for term in (
            "preview_document",
            "RequestContext.from_headers",
            "build_service",
            "DocumentController",
            "DocumentService.preview",
            "DocumentRepository.fetch",
            "gateway.get",
            "renderer.render",
        )
    )
    branches_and_configuration = all(
        term in final_answer
        for term in (
            "document_table",
            "include_deleted",
            "cache.get",
            "cache.put",
            "for_tenant",
            "locale",
            "redact",
        )
    )
    error_flow = all(
        term in final_answer
        for term in (
            "DocumentMissing",
            "TimeoutError",
            "StorageUnavailable",
            "HttpError",
            "404",
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
            "evidence": "active request chain incomplete",
        },
        "FUNC-002": {
            "passed": branches_and_configuration,
            "evidence": "cache, context, policy, or configuration flow missing",
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
