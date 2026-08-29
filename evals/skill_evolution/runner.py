"""End-to-end induction and Baseline/Forced transfer evaluation runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from codemate import cli as codemate_cli
from codemate.ui.terminal import NullUI

from .feedback import build_round_feedback
from .reporting import write_json, write_task_result
from .schema import TaskPair, load_task_pair
from .verification import run_hidden_verifier


DEFAULT_TASK_ROOT = Path(__file__).resolve().parent / "tasks"


@contextmanager
def _temporary_environment(values):
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update({name: str(value) for name, value in values.items()})
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _load_trace(run_dir):
    path = Path(run_dir) / "trace.jsonl"
    events = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _metrics(agent, events):
    state = agent.current_task_state
    finished = next(
        (item for item in reversed(events) if item.get("event") == "run_finished"),
        {},
    )
    completion_metadata = [
        dict(item.get("completion_metadata") or {})
        for item in events
        if item.get("event") == "model_parsed"
    ]

    def total(name):
        value = 0
        for metadata in completion_metadata:
            try:
                value += int(metadata.get(name) or 0)
            except (TypeError, ValueError):
                continue
        return value

    return {
        "run_id": state.run_id,
        "status": state.status,
        "stop_reason": state.stop_reason,
        "attempts": state.attempts,
        "tool_steps": state.tool_steps,
        "duration_ms": int(finished.get("run_duration_ms") or 0),
        "model_calls": len(completion_metadata),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cached_tokens": total("cached_tokens"),
    }


def _wait_for_evolution(agent, timeout):
    """Wait for only Skill-evolution workers before inspecting generated files."""
    deadline = time.monotonic() + float(timeout)
    while True:
        with agent._session_lock:
            threads = [
                item
                for item in agent._background_threads
                if item.name.startswith("codemate-skill_evolution")
            ]
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Skill evolution maintenance did not finish in time")
        for thread in threads:
            thread.join(timeout=min(0.2, remaining))


def _copy_workspace(source, destination):
    """Copy a fixture without carrying CodeMate configuration or learned state."""
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    shutil.rmtree(destination / ".codemate", ignore_errors=True)
    return destination


def _skill_snapshot(workspace):
    """Return stable hashes for project Skills visible at one evaluation boundary."""
    root = Path(workspace) / ".codemate" / "skills"
    return {
        path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("*/SKILL.md"))
    }


def _validate_skill_isolation(agent, workspace, home):
    """Fail before evaluation if Skill roots escape the isolated stage paths."""
    expected = {
        "CODEMATE_HOME": Path(home).resolve(),
        "user Skills": (Path(home) / "skills").resolve(),
        "project Skills": (Path(workspace) / ".codemate" / "skills").resolve(),
    }
    actual = {
        "CODEMATE_HOME": Path(agent.paths.home_root).resolve(),
        "user Skills": Path(agent.paths.user_skills).resolve(),
        "project Skills": Path(agent.paths.project_skills).resolve(),
    }
    mismatches = [
        f"{name}: expected {expected[name]}, found {actual[name]}"
        for name in expected
        if expected[name] != actual[name]
    ]
    target = str(agent.skill_evolution.target)
    if target != "project":
        mismatches.append(f"evolution target: expected project, found {target}")
    if mismatches:
        raise RuntimeError(
            "evaluation Skill path isolation failed:\n- " + "\n- ".join(mismatches)
        )


def _new_skill_names(before, after):
    """Identify newly created Skill names without treating edits as creation."""
    return sorted(set(after) - set(before))


def _managed_new_skill_names(store, before, after):
    """Require every new evaluation Skill to be owned by the evolution runtime."""
    names = _new_skill_names(before, after)
    unmanaged = [name for name in names if not store.is_managed(name)]
    if unmanaged:
        raise RuntimeError(
            "evaluation found Skills not created by Skill evolution: "
            + ", ".join(unmanaged)
        )
    return names


def _copy_skills(source_workspace, target_workspace, names):
    target_root = Path(target_workspace) / ".codemate" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = Path(source_workspace) / ".codemate" / "skills" / name
        if source.is_dir():
            shutil.copytree(source, target_root / name, dirs_exist_ok=True)


def _force_load_skills(agent, names):
    loaded = []
    for name in names:
        skill = agent.load_skill(name)
        loaded.append(name)
        agent.record(
            {
                "role": "user",
                "kind": "skill_context",
                "content": (
                    f"Skill instructions:\nName: {skill['name']}\n"
                    f"Root: {skill['root']}\n\n{skill['content'].strip()}"
                ),
            }
        )
    return loaded


def classify_transfer(baseline, forced):
    """Classify quality first; efficiency only breaks equal-quality ties."""
    base_checks = {item["id"]: item for item in baseline["verification"]["checks"]}
    forced_checks = {item["id"]: item for item in forced["verification"]["checks"]}
    regressed = any(
        item["required"] and item["passed"] and not forced_checks[check_id]["passed"]
        for check_id, item in base_checks.items()
    )
    base_score = float(baseline["verification"]["total_score"])
    forced_score = float(forced["verification"]["total_score"])
    if (
        regressed
        or forced_score < base_score
        or (
            baseline["verification"]["completed"]
            and not forced["verification"]["completed"]
        )
    ):
        return "harmful"
    if forced_score > base_score:
        return "improved"
    base_metrics = baseline["metrics"]
    forced_metrics = forced["metrics"]
    steps_non_worse = forced_metrics["tool_steps"] <= base_metrics["tool_steps"]
    attempts_non_worse = forced_metrics["attempts"] <= base_metrics["attempts"]
    execution_improved = (
        forced_metrics["tool_steps"] < base_metrics["tool_steps"]
        or forced_metrics["attempts"] < base_metrics["attempts"]
    )
    tokens_improved = forced_metrics.get("input_tokens", 0) + forced_metrics.get(
        "output_tokens", 0
    ) < base_metrics.get("input_tokens", 0) + base_metrics.get("output_tokens", 0)
    if (
        steps_non_worse
        and attempts_non_worse
        and (execution_improved or tokens_improved)
    ):
        return "more_efficient"
    return "neutral"


class SkillEvaluationRunner:
    """Coordinate isolated workspaces while reusing the production Agent runtime."""

    def __init__(self, args, *, agent_factory=None):
        self.args = args
        self.agent_factory = agent_factory or codemate_cli.build_agent
        self.output_root = Path(args.output_dir).resolve()
        configured_work_root = getattr(args, "work_dir", None)
        base_work_root = (
            Path(configured_work_root).expanduser().resolve()
            if configured_work_root
            else Path(tempfile.gettempdir()) / "codemate-skill-evolution-eval"
        )
        output_key = hashlib.sha256(str(self.output_root).encode("utf-8")).hexdigest()[
            :12
        ]
        self.work_root = base_work_root / output_key

    @staticmethod
    def _progress(task_id, message):
        print(f"[{task_id}] {message}", flush=True)

    def _agent_args(self, workspace, *, evolution):
        options = [
            "--cwd",
            str(workspace),
            "--provider",
            self.args.provider,
            "--approval",
            "full",
            "--max-steps",
            str(self.args.max_steps),
            "--max-new-tokens",
            str(self.args.max_new_tokens),
            "--memory-backend",
            "disabled",
            "--benchmark",
            "--no-stream",
            "--skill-evolution" if evolution else "--no-skill-evolution",
        ]
        for name in ("model", "base_url"):
            value = getattr(self.args, name, None)
            if value:
                options.extend(["--" + name.replace("_", "-"), str(value)])
        return codemate_cli.build_arg_parser().parse_args(options)

    @contextmanager
    def _build_agent(self, workspace, home, *, evolution, expected_skills=()):
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        write_json(
            home / "settings.json",
            {
                "mcp": {"servers": {}},
                "sandbox": {"mode": "disabled"},
                # Evaluation Skills always belong to the copied task workspace.
                # The isolated user Skill root must remain empty in every arm.
                "skill_evolution": {
                    "enabled": bool(evolution),
                    "target": "project",
                },
                "permissions": {
                    "read": {"allow": [], "deny": []},
                    "write": {"allow": [], "deny": []},
                },
            },
        )
        with _temporary_environment({"CODEMATE_HOME": home}):
            agent = self.agent_factory(
                self._agent_args(workspace, evolution=evolution), ui=NullUI()
            )
            try:
                _validate_skill_isolation(agent, workspace, home)
                visible_skills = sorted(
                    item["name"] for item in agent.available_skills()
                )
                expected = sorted(str(name) for name in expected_skills)
                if visible_skills != expected:
                    raise RuntimeError(
                        "evaluation Skill isolation failed: "
                        f"expected {expected}, found {visible_skills}"
                    )
                yield agent
            finally:
                agent.close()

    def _run_once(self, agent, stage, workspace, request):
        final = agent.ask(request)
        events = _load_trace(agent.current_run_dir)
        report = run_hidden_verifier(stage, workspace, final, events)
        return (
            {
                "request": request,
                "final_answer": final,
                "metrics": _metrics(agent, events),
                "verification": report.to_dict(),
            },
            report,
        )

    @staticmethod
    def _preserve_workspace(workspace, destination):
        """Copy the final external workspace back into the durable artifacts."""
        destination = Path(destination)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(workspace, destination)
        return destination

    def _run_induction(self, task: TaskPair, task_dir, work_task_dir):
        workspace = _copy_workspace(
            task.induction.workspace, work_task_dir / "induction" / "workspace"
        )
        home = task_dir / "induction" / "home"
        rounds = []
        request = task.induction.request
        initial_skills = _skill_snapshot(workspace)
        with self._build_agent(
            workspace, home, evolution=True, expected_skills=()
        ) as agent:
            for round_number in range(1, task.induction.max_work_rounds + 1):
                self._progress(
                    task.id,
                    f"induction round {round_number}: running Agent",
                )
                result, report = self._run_once(
                    agent, task.induction, workspace, request
                )
                self._progress(
                    task.id,
                    "induction round "
                    f"{round_number}: score={report.total_score:.2f}, "
                    f"completed={'yes' if report.completed else 'no'}",
                )
                _wait_for_evolution(agent, self.args.maintenance_timeout)
                result["round"] = round_number
                rounds.append(result)
                if result["verification"]["completed"]:
                    terminal_feedback = task.induction.pass_feedback
                    break
                if round_number == task.induction.max_work_rounds:
                    terminal_feedback = task.induction.fail_feedback
                    break
                request = build_round_feedback(report)
            self._progress(
                task.id,
                "submitting terminal feedback and waiting for Skill extraction",
            )
            agent.ask(terminal_feedback)
            _wait_for_evolution(agent, self.args.maintenance_timeout)
            final_skills = _skill_snapshot(workspace)
            skills = _managed_new_skill_names(
                agent.skill_evolution.store,
                initial_skills,
                final_skills,
            )
            self._progress(
                task.id,
                "generated Skills: " + (", ".join(skills) if skills else "none"),
            )
        artifact_workspace = self._preserve_workspace(
            workspace, task_dir / "induction" / "workspace"
        )
        return {
            "workspace": str(artifact_workspace),
            "rounds": rounds,
            "terminal_feedback": terminal_feedback,
            "generated_skills": skills,
        }

    def _run_transfer_arm(self, task, task_dir, work_task_dir, arm, induction):
        workspace = _copy_workspace(
            task.transfer.workspace,
            work_task_dir / "transfer" / arm / "workspace",
        )
        home = task_dir / "transfer" / arm / "home"
        names = list(induction["generated_skills"])
        if arm == "forced":
            _copy_skills(induction["workspace"], workspace, names)
        expected_skills = names if arm == "forced" else ()
        self._progress(
            task.id,
            f"transfer {arm}: running Agent"
            + (f" with {len(names)} forced Skill(s)" if arm == "forced" else ""),
        )
        with self._build_agent(
            workspace,
            home,
            evolution=False,
            expected_skills=expected_skills,
        ) as agent:
            loaded = _force_load_skills(agent, names) if arm == "forced" else []
            result, _report = self._run_once(
                agent, task.transfer, workspace, task.transfer.request
            )
        self._progress(
            task.id,
            f"transfer {arm}: score={result['verification']['total_score']:.2f}, "
            f"completed={'yes' if result['verification']['completed'] else 'no'}",
        )
        result["loaded_skills"] = loaded
        result["workspace"] = str(
            self._preserve_workspace(
                workspace, task_dir / "transfer" / arm / "workspace"
            )
        )
        return result

    def run_task(self, task: TaskPair):
        task_dir = self.output_root / task.id
        if task_dir.exists():
            shutil.rmtree(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        work_task_dir = self.work_root / task.id
        if work_task_dir.exists():
            shutil.rmtree(work_task_dir)
        work_task_dir.mkdir(parents=True, exist_ok=True)
        self._progress(task.id, f"execution workspace: {work_task_dir}")
        induction = self._run_induction(task, task_dir, work_task_dir)
        if not induction["generated_skills"]:
            result = {
                "task_id": task.id,
                "category": task.category,
                "skill_target": task.skill_target,
                "learned_preferences": list(task.learned_preferences),
                "induction": induction,
                "transfer": {"baseline": None, "forced": None},
                "outcome": "no_skill_generated",
            }
            write_task_result(task_dir, result)
            self._progress(
                task.id,
                "stopped before transfer: induction produced no managed Skill",
            )
            return result
        baseline = self._run_transfer_arm(
            task, task_dir, work_task_dir, "baseline", induction
        )
        forced = self._run_transfer_arm(
            task, task_dir, work_task_dir, "forced", induction
        )
        result = {
            "task_id": task.id,
            "category": task.category,
            "skill_target": task.skill_target,
            "learned_preferences": list(task.learned_preferences),
            "induction": induction,
            "transfer": {"baseline": baseline, "forced": forced},
        }
        result["outcome"] = classify_transfer(baseline, forced)
        write_task_result(task_dir, result)
        return result


def discover_tasks(root=DEFAULT_TASK_ROOT):
    return [load_task_pair(path) for path in sorted(Path(root).glob("*/*/task.json"))]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--task", type=Path, help="Path to one task.json")
    selection.add_argument(
        "--all", action="store_true", help="Run all bundled task pairs"
    )
    parser.add_argument("--output-dir", default="runs/skill-evolution-eval")
    parser.add_argument(
        "--work-dir",
        help="External execution root (defaults to the system temporary directory)",
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai", "anthropic", "deepseek"),
        default="openai",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--max-new-tokens", type=int, default=8192)
    parser.add_argument("--maintenance-timeout", type=float, default=600.0)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    tasks = discover_tasks() if args.all else [load_task_pair(args.task)]
    runner = SkillEvaluationRunner(args)
    results = []
    for task in tasks:
        print(f"[{task.id}] running induction and transfer arms...", flush=True)
        result = runner.run_task(task)
        results.append(result)
        print(f"[{task.id}] {result['outcome']}", flush=True)
    write_json(Path(args.output_dir) / "summary.json", results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
