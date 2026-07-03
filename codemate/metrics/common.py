# 指标通用能力：聚合 benchmark/run artifacts、基础统计函数和特性开关 ablation 辅助。

import json
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..config import load_project_env, provider_env
from ..evaluator import run_fixed_benchmark
from ..models import AnthropicCompatibleModelClient, FakeModelClient, ModelResponse, OpenAICompatibleModelClient
from ..runtime import CodeMate
from ..storage import SessionStore
from ..workspace import WorkspaceContext

METRICS_SCHEMA_VERSION = 2
DEFAULT_HARNESS_REGRESSION_V2_PATH = Path("artifacts/harness-regression-v2.json")
DEFAULT_CONTEXT_ABLATION_V2_PATH = Path("artifacts/context-ablation-v2.json")
DEFAULT_MEMORY_ABLATION_V2_PATH = Path("artifacts/memory-ablation-v2.json")
DEFAULT_CORE_REPORT_PATH = Path("docs/metrics/codemate-benchmark-core-report.md")


def _safe_mean(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_ratio(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def _parse_iso8601(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def aggregate_benchmark_artifact(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = list(payload.get("rows", []))
    summary = dict(payload.get("summary", {}))
    task_count = int(summary.get("total_tasks", len(rows) or 0))
    tool_steps = [int(row.get("tool_steps", 0)) for row in rows]
    attempts = [int(row.get("attempts", 0)) for row in rows]
    categories = {}
    for row in rows:
        category = str(row.get("category", "")).strip()
        if not category:
            continue
        categories[category] = categories.get(category, 0) + 1
    return {
        "task_count": task_count,
        "passed": int(summary.get("passed", 0)),
        "failed": int(summary.get("failed", 0)),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "within_budget": int(summary.get("within_budget", 0)),
        "verifier_passes": int(summary.get("verifier_passes", 0)),
        "failure_category_counts": dict(summary.get("failure_category_counts", {})),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "category_counts": categories,
        "rows": rows,
    }


def _infer_run_duration_ms(events):
    finished = next((event for event in reversed(events) if event.get("event") == "run_finished"), None)
    if finished and finished.get("run_duration_ms") is not None:
        return float(finished["run_duration_ms"])
    started = next((event for event in events if event.get("event") == "run_started"), None)
    if not started or not finished:
        return 0.0
    start_dt = _parse_iso8601(started.get("created_at"))
    end_dt = _parse_iso8601(finished.get("created_at"))
    if start_dt is None or end_dt is None:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def aggregate_run_artifacts(runs_root):
    runs_root = Path(runs_root)
    run_dirs = sorted(path for path in runs_root.glob("*") if path.is_dir())
    reports = []
    tool_status_counts = {}
    tool_name_counts = {}
    security_event_counts = {}
    run_durations = []
    tool_durations = []
    prompt_durations = []
    stop_reasons = {}

    for run_dir in run_dirs:
        report_path = run_dir / "report.json"
        trace_path = run_dir / "trace.jsonl"
        if report_path.exists():
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        events = []
        if trace_path.exists():
            events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        run_durations.append(_infer_run_duration_ms(events))
        for event in events:
            if event.get("event") == "prompt_built" and event.get("duration_ms") is not None:
                prompt_durations.append(float(event["duration_ms"]))
            if event.get("event") != "tool_executed":
                continue
            tool_name = str(event.get("name", "")).strip()
            if tool_name:
                tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1
            tool_status = str(event.get("tool_status", "")).strip()
            if tool_status:
                tool_status_counts[tool_status] = tool_status_counts.get(tool_status, 0) + 1
            security_event = str(event.get("security_event_type", "")).strip()
            if security_event:
                security_event_counts[security_event] = security_event_counts.get(security_event, 0) + 1
            if event.get("duration_ms") is not None:
                tool_durations.append(float(event["duration_ms"]))

    tool_steps = [int(report.get("tool_steps", 0)) for report in reports]
    attempts = [int(report.get("attempts", 0)) for report in reports]
    prompt_chars = [int((report.get("prompt_metadata") or {}).get("prompt_chars", 0)) for report in reports]
    cached_tokens = [int((report.get("prompt_metadata") or {}).get("cached_tokens", 0) or 0) for report in reports]
    cache_hits = [bool((report.get("prompt_metadata") or {}).get("cache_hit")) for report in reports]
    input_tokens = [int((report.get("prompt_metadata") or {}).get("input_tokens", 0) or 0) for report in reports]
    prefix_reused = [
        not bool((report.get("prompt_metadata") or {}).get("prefix_changed"))
        for report in reports
        if "prefix_changed" in (report.get("prompt_metadata") or {})
    ]
    for report in reports:
        stop_reason = str(report.get("stop_reason", "")).strip()
        if stop_reason:
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1

    return {
        "run_count": len(reports) if reports else len(run_dirs),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "avg_prompt_chars": _safe_mean(prompt_chars),
        "cache_hit_rate": _safe_ratio(sum(1 for hit in cache_hits if hit), len(cache_hits)),
        "cached_token_ratio": _safe_ratio(sum(cached_tokens), sum(input_tokens)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "prefix_reuse_rate": _safe_ratio(sum(1 for reused in prefix_reused if reused), len(prefix_reused)),
        "tool_status_counts": tool_status_counts,
        "tool_name_counts": tool_name_counts,
        "security_event_counts": security_event_counts,
        "stop_reason_counts": stop_reasons,
        "avg_run_duration_ms": _safe_mean(run_durations),
        "avg_tool_duration_ms": _safe_mean(tool_durations),
        "avg_prompt_build_duration_ms": _safe_mean(prompt_durations),
    }


@contextmanager
def _temporary_feature_flags(agent, updates):
    previous = dict(getattr(agent, "feature_flags", {}))
    merged = dict(previous)
    merged.update(updates)
    agent.feature_flags = merged
    try:
        yield
    finally:
        agent.feature_flags = previous


def measure_feature_ablation_metrics(agent, user_message):
    variants = {
        "full": {},
        "no_context_reduction": {"context_reduction": False},
        "no_memory": {"memory": False, "relevant_memory": False},
    }
    results = {}
    for name, updates in variants.items():
        with _temporary_feature_flags(agent, updates):
            prompt, metadata = agent._build_prompt_and_metadata(user_message)
        results[name] = {
            "prompt_chars": int(metadata.get("prompt_chars", 0)),
            "memory_chars": int(metadata.get("sections", {}).get("memory", {}).get("rendered_chars", 0)),
            "history_chars": int(metadata.get("sections", {}).get("history", {}).get("rendered_chars", 0)),
            "relevant_selected_count": int(metadata.get("relevant_memory", {}).get("selected_count", 0)),
            "budget_reduction_count": len(metadata.get("budget_reductions", [])),
            "current_request_preserved": prompt.endswith(f"Current user request:\n{user_message}"),
        }
    return results


def build_stress_agent_metrics():
    with tempfile.TemporaryDirectory(prefix="codemate-metrics-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(workspace_root)
        store = SessionStore(workspace_root / ".codemate" / "sessions")
        agent = CodeMate(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
        )
        for index in range(12):
            agent.memory.promote_durable([("project-conventions", f"stress-note-{index}-" + ("A" * 180))])
            agent.record(
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"stress-history-{index}-" + ("B" * 220),
                    "created_at": f"2026-04-08T11:{index:02d}:00+00:00",
                }
            )
        return measure_feature_ablation_metrics(agent, "recall")


__all__ = [name for name in globals() if not name.startswith("__")]
