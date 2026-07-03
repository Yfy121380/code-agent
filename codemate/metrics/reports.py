# 指标报告：汇总实验结果，写出 JSON artifact 和 Markdown benchmark 报告。

from .common import *
from .context_experiments import *
from .memory_experiments import *
from .provider_experiments import *
from .security_experiments import *

def collect_resume_metrics(
    benchmark_artifact_path,
    runs_root,
    provider_experiments=None,
    memory_repetitions=3,
    large_memory_repetitions=5,
    context_repetitions=5,
    security_repetitions=3,
    experiment_mode="synthetic",
    real_provider="gpt",
):
    benchmark = aggregate_benchmark_artifact(benchmark_artifact_path)
    runs = aggregate_run_artifacts(runs_root)
    experiment_mode = str(experiment_mode)
    real_provider = str(real_provider)
    if experiment_mode == "real":
        memory_large = run_real_memory_experiment(provider=real_provider, repetitions=large_memory_repetitions)
        memory = {name: dict(values) for name, values in memory_large["variants"].items()}
        context = run_real_context_experiment(provider=real_provider, repetitions=context_repetitions)
        security = run_real_security_experiment_suite(provider=real_provider, repetitions=security_repetitions)
        stress = {
            "full": {"prompt_chars": int(round(context["summary"].get("avg_full_prompt_chars", 0.0)))},
            "no_context_reduction": {"prompt_chars": int(round(context["summary"].get("avg_raw_prompt_chars", 0.0)))},
        }
    else:
        stress = build_stress_agent_metrics()
        memory = run_memory_dependency_experiment(repetitions=memory_repetitions)
        memory_large = run_large_scale_memory_experiment(repetitions=large_memory_repetitions)
        context = run_context_stress_matrix(repetitions=context_repetitions)
        security = run_security_experiment_suite(repetitions=security_repetitions)
    provider_payload = {"providers": []}
    if provider_experiments:
        provider_payload = json.loads(Path(provider_experiments).read_text(encoding="utf-8"))
    return {
        "experiment_mode": experiment_mode,
        "real_provider": real_provider if experiment_mode == "real" else "",
        "facts": {
            "model_backend_count": 3,
            "tool_count": 7,
            "run_artifact_count": 3,
        },
        "benchmark": benchmark,
        "runs": runs,
        "stress_ablation": stress,
        "memory_experiment": memory,
        "memory_large_experiment": memory_large,
        "context_experiment": context,
        "security_experiment": security,
        "provider_experiments": provider_payload,
        "resume_highlights": [
            f"Built a fixed benchmark harness with {benchmark['task_count']} tasks and automated pass/fail, verifier, and budget summaries.",
            f"Recorded 3 run artifacts per execution and structured runtime metadata across {runs['run_count']} aggregated runs.",
            f"Observed prompt-cache telemetry with average cached tokens of {runs['avg_cached_tokens']:.1f} and cache-hit rate of {runs['cache_hit_rate']:.2%} when available.",
            (
                f"In a real-model long-context experiment ({real_provider}), context reduction shrank average prompt size from "
                f"{stress['no_context_reduction']['prompt_chars']} to {stress['full']['prompt_chars']} chars."
                if experiment_mode == "real"
                else f"In a synthetic long-context stress scenario, context reduction shrank prompt size from {stress['no_context_reduction']['prompt_chars']} to {stress['full']['prompt_chars']} chars."
            ),
            f"In the memory dependency experiment, repeated follow-up reads dropped from {memory['memory_off']['repeated_reads']} to {memory['memory_on']['repeated_reads']}.",
            f"In the large-scale memory experiment, repeated reads dropped from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']} across {memory_large['task_count']} tasks.",
        ],
    }


def render_resume_metrics_markdown(metrics):
    benchmark = metrics["benchmark"]
    runs = metrics["runs"]
    stress = metrics["stress_ablation"]
    memory = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    provider_payload = metrics.get("provider_experiments", {})
    lines = [
        "# CodeMate Resume Metrics",
        "",
        "## Key Numbers",
        f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}",
        f"- Model backends: {metrics['facts']['model_backend_count']}",
        f"- Tool types: {metrics['facts']['tool_count']}",
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Fixed benchmark pass rate: {benchmark['pass_rate']:.2%}",
        f"- Aggregated runs: {runs['run_count']}",
        f"- Average tool steps per run: {runs['avg_tool_steps']:.2f}",
        f"- Average attempts per run: {runs['avg_attempts']:.2f}",
        f"- Cache hit rate: {runs['cache_hit_rate']:.2%}",
        (
            f"- Real-model prompt chars (full vs no context reduction): {stress['full']['prompt_chars']} / {stress['no_context_reduction']['prompt_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic prompt chars (full vs no context reduction): {stress['full']['prompt_chars']} / {stress['no_context_reduction']['prompt_chars']}"
        ),
        f"- Memory repeated reads (on vs off): {memory['memory_on']['repeated_reads']} / {memory['memory_off']['repeated_reads']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context matrix configs: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Resume Highlights",
    ]
    lines.extend(f"- {line}" for line in metrics["resume_highlights"])
    providers = provider_payload.get("providers", [])
    if providers:
        lines.extend(["", "## Provider Experiments"])
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})")
    lines.append("")
    return "\n".join(lines)


def render_large_scale_experiment_report(metrics):
    benchmark = metrics["benchmark"]
    memory_small = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    providers = metrics.get("provider_experiments", {}).get("providers", [])
    report_provider = (
        metrics.get("real_provider")
        or context.get("provider")
        or memory_large.get("provider")
        or security.get("provider")
        or "unknown"
    )
    lines = [
        "# CodeMate Large-Scale Experiment Report",
        "",
        "## Executive Summary",
        (
            f"- Experiment mode: real-model (provider: {report_provider})"
            if metrics.get("experiment_mode") == "real"
            else f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}"
        ),
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context stress configurations: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Context Governance",
        (
            f"- Real-model prompt chars ({report_provider}): {metrics['stress_ablation']['full']['prompt_chars']} vs {metrics['stress_ablation']['no_context_reduction']['prompt_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic stress prompt chars: {metrics['stress_ablation']['full']['prompt_chars']} vs {metrics['stress_ablation']['no_context_reduction']['prompt_chars']}"
        ),
        f"- Average prompt compression ratio across context matrix: {context['summary']['avg_prompt_compression_ratio']:.2%}",
        f"- Max prompt compression ratio across context matrix: {context['summary']['max_prompt_compression_ratio']:.2%}",
        "",
        "## Memory Experiments",
        f"- Small memory experiment repeated reads: {memory_small['memory_on']['repeated_reads']} vs {memory_small['memory_off']['repeated_reads']}",
        f"- Large memory experiment repeated reads: {memory_large['variants']['memory_on']['repeated_reads']} vs {memory_large['variants']['memory_off']['repeated_reads']}",
        f"- Large memory experiment avg tool steps: {memory_large['variants']['memory_on']['avg_tool_steps']:.2f} vs {memory_large['variants']['memory_off']['avg_tool_steps']:.2f}",
        "",
        "## Security Experiments",
        f"- Security event counts: {json.dumps(security['security_event_counts'], sort_keys=True)}",
        f"- Tool error code counts: {json.dumps(security['tool_error_code_counts'], sort_keys=True)}",
        "",
        "## Provider Experiments",
    ]
    if providers:
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Resume-Safe Claims",
            f"- Long-context stress scenario: prompt length reduced from {metrics['stress_ablation']['no_context_reduction']['prompt_chars']} to {metrics['stress_ablation']['full']['prompt_chars']}.",
            f"- Large-scale memory experiment: repeated reads reduced from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']}.",
            f"- Platform facts: {benchmark['task_count']} benchmark tasks, {metrics['facts']['tool_count']} tool types, {metrics['facts']['run_artifact_count']} run artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_context_ablation_v2(artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH, repetitions=5):
    payload = run_context_stress_matrix(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "context-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "config_count": payload["config_count"],
        "configs": payload["configs"],
        "summary": payload["summary"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_memory_ablation_v2(artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH, repetitions=5):
    payload = run_large_scale_memory_experiment(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "memory-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "task_count": payload["task_count"],
        "runs_per_variant": payload["runs_per_variant"],
        "category_counts": payload["category_counts"],
        "variants": payload["variants"],
        "rows": payload["rows"],
    }
    return _write_json_artifact(artifact_path, artifact)


def write_benchmark_core_report(
    report_path=DEFAULT_CORE_REPORT_PATH,
    harness_artifact_path=DEFAULT_HARNESS_REGRESSION_V2_PATH,
    context_artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH,
    memory_artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH,
):
    harness = json.loads(Path(harness_artifact_path).read_text(encoding="utf-8"))
    context = json.loads(Path(context_artifact_path).read_text(encoding="utf-8"))
    memory = json.loads(Path(memory_artifact_path).read_text(encoding="utf-8"))

    lines = [
        "# CodeMate Benchmark Core Report",
        "",
        "这轮 benchmark 只收缩到 Harness regression、context ablation 和 working memory ablation 三层，不把 provider、run aggregation 或 durable memory 的别的结论揉进来。",
        "",
        "## Harness Regression",
        f"- 固定 regression 任务数：{harness['summary']['total_tasks']}",
        f"- pass_rate：{harness['summary']['pass_rate']:.2%}",
        f"- within_budget_rate：{harness['summary']['within_budget_rate']:.2%}",
        f"- verifier_pass_rate：{harness['summary']['verifier_pass_rate']:.2%}",
        "",
        "## Context Ablation",
        f"- 配置数：{context['config_count']}",
        f"- avg_full_prompt_chars：{context['summary']['avg_full_prompt_chars']:.2f}",
        f"- avg_raw_prompt_chars：{context['summary']['avg_raw_prompt_chars']:.2f}",
        f"- avg_prompt_compression_ratio：{context['summary']['avg_prompt_compression_ratio']:.2%}",
        f"- max_prompt_compression_ratio：{context['summary']['max_prompt_compression_ratio']:.2%}",
        f"- current_request_preserved_rate：{context['summary']['current_request_preserved_rate']:.2%}",
        "",
        "## Working Memory Ablation",
        f"- memory_on repeated_reads：{memory['variants']['memory_on']['repeated_reads']}",
        f"- memory_off repeated_reads：{memory['variants']['memory_off']['repeated_reads']}",
        f"- memory_on avg_tool_steps：{memory['variants']['memory_on']['avg_tool_steps']:.2f}",
        f"- memory_on correct_rate：{memory['variants']['memory_on']['correct_rate']:.2%}",
        f"- memory_hit_rate：{memory['variants']['memory_on']['memory_hit_rate']:.2%}",
        "",
        "## 可以安全写进简历的指标",
        "- avg_full_prompt_chars",
        "- avg_raw_prompt_chars",
        "- avg_prompt_compression_ratio",
        "- max_prompt_compression_ratio",
        "- repeated_reads",
        "- avg_tool_steps",
        "- correct_rate",
        "",
        "## 只适合放文档/面试展开的指标",
        "- current_request_preserved_rate",
        "- memory_hit_rate",
        "- failure_category_counts",
        "",
        "## 口径边界",
        "- Harness regression 只证明 runtime 合同稳定，不证明 provider 上限。",
        "- Context、memory 这两层只证明模块收益，不和 provider benchmark 混写。",
    ]
    report_text = "\n".join(lines) + "\n"
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return report_text


__all__ = [name for name in globals() if not name.startswith("__")]
