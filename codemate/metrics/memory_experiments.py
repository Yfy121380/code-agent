# 工作记忆实验：构造记忆依赖任务，衡量重复读文件、正确率和 memory hit rate。

from .common import *

class _MemoryExperimentModelClient(FakeModelClient):
    def __init__(self, expected_fact, filename):
        super().__init__([])
        self.expected_fact = str(expected_fact).strip().lower()
        self.filename = str(filename).strip()
        self.phase = "bootstrap_tool"
        self.followup_reads = 0

    def complete(self, messages, max_new_tokens, tools=None, system=None, **kwargs):
        del max_new_tokens, tools, kwargs
        prompt = "\n".join([str(system or "")] + [str(message.get("content", "")) for message in messages or []])
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        if self.phase == "bootstrap_tool":
            self.phase = "bootstrap_final"
            return ModelResponse.tool_call("read_file", {"path": self.filename, "start": 1, "end": 20})
        if self.phase == "bootstrap_final":
            self.phase = "question"
            return ModelResponse.final("Done.")
        if self.phase == "question":
            context_lower = str((messages or [{}])[0].get("content", "")).lower()
            memory_view = ""
            if "memory:" in context_lower and "\n\nrelevant memory:" in context_lower:
                memory_view = context_lower.split("memory:", 1)[1].split("\n\nrelevant memory:", 1)[0]
            relevant_view = ""
            if "relevant memory:" in context_lower:
                relevant_view = context_lower.split("relevant memory:", 1)[1]
            if self.expected_fact in memory_view or self.expected_fact in relevant_view:
                return ModelResponse.final(f"{self.expected_fact.capitalize()}.")
            self.phase = "question_after_read"
            self.followup_reads += 1
            return ModelResponse.tool_call("read_file", {"path": self.filename, "start": 1, "end": 20})
        if self.phase == "question_after_read":
            self.phase = "done"
            return ModelResponse.final(f"{self.expected_fact.capitalize()}.")
        return ModelResponse.final(f"{self.expected_fact.capitalize()}.")


def _build_memory_experiment_agent(workspace_root, expected_fact, filename):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".codemate" / "sessions")
    return CodeMate(
        model_client=_MemoryExperimentModelClient(expected_fact, filename),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def _set_irrelevant_memory(agent):
    state = agent.memory.to_dict()
    state["file_summaries"] = {}
    agent.memory.state = state
    agent.memory.promote_durable([("project-conventions", "team mascot is blue")])
    agent.session["memory"] = agent.memory.to_dict()


def _run_memory_variant(mode):
    with tempfile.TemporaryDirectory(prefix="codemate-memory-experiment-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        (workspace_root / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
        agent = _build_memory_experiment_agent(workspace_root, "deploy key is red", "facts.txt")
        assert agent.ask("Read facts.txt and remember the key fact.") == "Done."

        if mode == "memory_off":
            agent.feature_flags["memory"] = False
            agent.feature_flags["relevant_memory"] = False
        elif mode == "memory_irrelevant":
            _set_irrelevant_memory(agent)

        result = agent.ask("What color is the deploy key?")
        task_state = agent.current_task_state
        model_client = agent.model_client
        return {
            "correct": result.strip().lower() == "deploy key is red.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(getattr(model_client, "followup_reads", 0)),
        }


def run_memory_dependency_experiment(repetitions=3):
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for _ in range(int(repetitions)):
        for variant in variants:
            variants[variant].append(_run_memory_variant(variant))

    results = {}
    for variant, rows in variants.items():
        results[variant] = {
            "repeated_reads": sum(row["repeated_reads"] for row in rows),
            "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
            "avg_attempts": _safe_mean(row["attempts"] for row in rows),
            "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
        }
    return results


MEMORY_EXPERIMENT_TASKS = [
    {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
    {"id": "fact_api", "category": "fact_lookup", "filename": "settings.txt", "fact": "api base path is /v1/internal"},
    {
        "id": "fact_budget",
        "category": "fact_lookup",
        "filename": "limits.txt",
        "fact": "default step budget is 6",
        "summary_covered": False,
    },
    {"id": "fact_timeout", "category": "fact_lookup", "filename": "runtime.txt", "fact": "timeout ceiling is 120 seconds"},
    {"id": "edit_intro", "category": "edit_dependency", "filename": "README.md", "fact": "first bullet is the locked intro line"},
    {"id": "edit_token", "category": "edit_dependency", "filename": "sample.txt", "fact": "second token is placeholder"},
    {
        "id": "edit_field",
        "category": "edit_dependency",
        "filename": "config.txt",
        "fact": "fixed field name is benchmark_schema",
        "summary_covered": False,
    },
    {"id": "edit_line", "category": "edit_dependency", "filename": "notes.txt", "fact": "locked marker is on line three"},
    {"id": "history_file", "category": "history_reference", "filename": "history.txt", "fact": "deploy fact came from facts.txt"},
    {"id": "history_line", "category": "history_reference", "filename": "history.txt", "fact": "benchmark note came from line two"},
    {
        "id": "history_token",
        "category": "history_reference",
        "filename": "history.txt",
        "fact": "placeholder token was beta",
        "summary_covered": False,
    },
    {"id": "history_tool", "category": "history_reference", "filename": "history.txt", "fact": "inspection tool was read_file"},
]


def _write_memory_task_files(workspace_root, task):
    filename = task["filename"]
    payload = task["fact"]
    if task.get("summary_covered", True) is False:
        payload = "\n".join(
            [
                "overview: this benchmark file intentionally starts with filler",
                "details: the useful fact is below the short summary window",
                "notes: working memory should not store the whole file",
                "padding: " + ("context " * 40).strip(),
                payload,
            ]
        )
    (workspace_root / filename).write_text(payload + "\n", encoding="utf-8")


def _bootstrap_prompt(task):
    return f"Read {task['filename']} and remember the key fact."


def _followup_prompt(task):
    if task["category"] == "fact_lookup":
        return f"What does {task['filename']} say?"
    if task["category"] == "edit_dependency":
        return f"Use the remembered constraint from {task['filename']} to continue without rereading."
    return f"What was the conclusion we already established from {task['filename']}?"


def _set_irrelevant_memory_for_task(agent):
    state = agent.memory.to_dict()
    state["file_summaries"] = {}
    agent.memory.state = state
    agent.memory.promote_durable([("project-conventions", "the team mascot is blue")])
    agent.session["memory"] = agent.memory.to_dict()


def _run_memory_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="codemate-memory-large-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        _write_memory_task_files(workspace_root, task)
        agent = _build_memory_experiment_agent(workspace_root, task["fact"], task["filename"])
        assert agent.ask(_bootstrap_prompt(task)) == "Done."
        if variant == "memory_off":
            agent.feature_flags["memory"] = False
            agent.feature_flags["relevant_memory"] = False
        elif variant == "memory_irrelevant":
            _set_irrelevant_memory_for_task(agent)
        result = agent.ask(_followup_prompt(task))
        task_state = agent.current_task_state
        return {
            "correct": result.strip().lower() == f"{task['fact']}.",
            "tool_steps": int(task_state.tool_steps),
            "attempts": int(task_state.attempts),
            "repeated_reads": int(getattr(agent.model_client, "followup_reads", 0)),
        }


def run_large_scale_memory_experiment(repetitions=5):
    repetitions = int(repetitions)
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for task in MEMORY_EXPERIMENT_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                row = _run_memory_task_variant(task, variant)
                row["task_id"] = task["id"]
                row["category"] = task["category"]
                variants[variant].append(row)
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
    return {
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
                "memory_hit_rate": _safe_ratio(sum(1 for row in rows if row["repeated_reads"] == 0), len(rows)),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
