"""Write stable JSON artifacts and a human-readable Skill evaluation report."""

from __future__ import annotations

import json
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _check_rows(baseline, forced):
    baseline_by_id = {item["id"]: item for item in baseline["verification"]["checks"]}
    forced_by_id = {item["id"]: item for item in forced["verification"]["checks"]}
    rows = []
    for check_id, baseline_check in baseline_by_id.items():
        forced_check = forced_by_id[check_id]
        rows.append(
            "| {id} | {category} | {baseline} | {forced} |".format(
                id=check_id,
                category=baseline_check["category"],
                baseline="PASS" if baseline_check["passed"] else "FAIL",
                forced="PASS" if forced_check["passed"] else "FAIL",
            )
        )
    return rows


def render_report(result):
    """Render one task pair without exposing private verifier evidence."""
    induction = result["induction"]
    baseline = result["transfer"]["baseline"]
    forced = result["transfer"]["forced"]
    lines = [
        f"# Skill Evolution Evaluation: {result['task_id']}",
        "",
        f"- Category: `{result['category']}`",
        f"- Skill target: {result['skill_target']}",
        f"- Outcome: **{result['outcome']}**",
        f"- Generated skills: {', '.join(induction['generated_skills']) or 'none'}",
        "",
        "## Induction",
        "",
        "| Round | Score | Completed | Attempts | Tool steps |",
        "| ---: | ---: | :---: | ---: | ---: |",
    ]
    for item in induction["rounds"]:
        lines.append(
            f"| {item['round']} | {item['verification']['total_score']:.2f} | "
            f"{'yes' if item['verification']['completed'] else 'no'} | "
            f"{item['metrics']['attempts']} | {item['metrics']['tool_steps']} |"
        )
    if baseline is None or forced is None:
        lines.extend(
            [
                "",
                "## Transfer",
                "",
                "Transfer was skipped because induction produced no managed Skill. "
                "Running Baseline and Forced without a Skill would not create a valid comparison.",
                "",
            ]
        )
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "",
            "## Transfer",
            "",
            "| Arm | Score | Completed | Attempts | Tool steps | Input tokens | Output tokens | Duration ms |",
            "| --- | ---: | :---: | ---: | ---: | ---: | ---: | ---: |",
            f"| Baseline | {baseline['verification']['total_score']:.2f} | "
            f"{'yes' if baseline['verification']['completed'] else 'no'} | "
            f"{baseline['metrics']['attempts']} | {baseline['metrics']['tool_steps']} | "
            f"{baseline['metrics']['input_tokens']} | {baseline['metrics']['output_tokens']} | "
            f"{baseline['metrics']['duration_ms']} |",
            f"| Forced | {forced['verification']['total_score']:.2f} | "
            f"{'yes' if forced['verification']['completed'] else 'no'} | "
            f"{forced['metrics']['attempts']} | {forced['metrics']['tool_steps']} | "
            f"{forced['metrics']['input_tokens']} | {forced['metrics']['output_tokens']} | "
            f"{forced['metrics']['duration_ms']} |",
            "",
            "## Hidden Checks",
            "",
            "| Check | Category | Baseline | Forced |",
            "| --- | --- | :---: | :---: |",
            *_check_rows(baseline, forced),
            "",
            "Private verifier evidence is stored in JSON artifacts and is never sent to the Agent.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_task_result(output_dir, result):
    output_dir = Path(output_dir)
    write_json(output_dir / "result.json", result)
    (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    return output_dir
