EXPECTED = {
    "S1.md": "request correlation identifier",
    "S2.md": "request rate, error rate, and latency",
    "S3.md": "Trace context should cross service boundaries",
}


def _supported(text, concept_terms, citation):
    """Require every concept and its citation in the same report line."""
    return any(
        all(
            any(term.lower() in line.lower() for term in alternatives)
            for alternatives in concept_terms
        )
        and citation in line
        for line in text.splitlines()
        if line.strip()
    )


def verify(workspace, final_answer, trace_events):
    report_path = workspace / "report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    sources_unchanged = all(
        marker in (workspace / "sources" / name).read_text(encoding="utf-8")
        for name, marker in EXPECTED.items()
    )
    facts = all(
        (
            _supported(
                report,
                (("structured", "结构化"), ("correlation", "关联标识")),
                "[S1]",
            ),
            _supported(
                report,
                (("error rate", "错误率"), ("latency", "延迟")),
                "[S2]",
            ),
            _supported(
                report,
                (("trace", "追踪"), ("service", "服务")),
                "[S3]",
            ),
        )
    )
    citation_quality = (
        all(cite in report for cite in ("[S1]", "[S2]", "[S3]")) and facts
    )
    unsupported = any(
        name.lower() in report.lower()
        for name in ("prometheus", "grafana", "jaeger", "opentelemetry")
    )
    headings = all(name in report for name in ("结论", "证据", "限制"))
    return {
        "FUNC-001": {
            "passed": facts,
            "evidence": "required source-backed recommendations missing",
        },
        "INSTR-001": {
            "passed": bool(report) and sources_unchanged,
            "evidence": "report missing or sources changed",
        },
        "QUALITY-001": {
            "passed": citation_quality,
            "evidence": "citation mapping incomplete",
        },
        "QUALITY-002": {
            "passed": not unsupported,
            "evidence": "unsupported named technology added",
        },
        "PREF-001": {"passed": headings, "evidence": "report headings missing"},
    }
