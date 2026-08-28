"""Replay-based Skill evaluation following BearCode's baseline/candidate flow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..storage.atomic import atomic_write_json, atomic_write_text
from ..workspace import now
from .online import _parse_json_object, skill_documents
from .prompts import (
    EVAL_JUDGE_SYSTEM_PROMPT,
    EVAL_MUTATION_SYSTEM_PROMPT,
    REPLAY_REQUEST_TEMPLATE,
)
from .store import ONLINE_PROVENANCE_LOG


MIN_REPLAY_SAMPLES = 2
MIN_PROMOTION_TESTS = 1


def _read_jsonl(path):
    rows = []
    path = Path(path)
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _sample_id(skill, messages):
    payload = json.dumps({"skill": skill, "messages": messages}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _split_score(sample_id):
    return int(str(sample_id)[:8], 16) / float(16**8 - 1)


def _assign_splits(samples):
    values = [dict(item) for item in samples]
    if len(values) < 2:
        for item in values:
            item["split"] = "mutate_dev"
        return values
    for item in values:
        item["split"] = (
            "mutate_dev"
            if _split_score(item["sample_id"]) < 0.75
            else "promotion_test"
        )
    if not any(item["split"] == "promotion_test" for item in values):
        values[-1]["split"] = "promotion_test"
    if not any(item["split"] == "mutate_dev" for item in values):
        values[0]["split"] = "mutate_dev"
    return values


def _freeze_replay_samples(store, skill, samples):
    """Persist the replay pool and assign deterministic BearCode-style splits."""
    lineage = hashlib.sha1(skill.encode("utf-8")).hexdigest()[:16]
    path = store.root / "online-eval" / "datasets" / lineage / "replay_pool.jsonl"
    existing = {
        str(item.get("sample_id") or ""): item
        for item in _read_jsonl(path)
        if str(item.get("sample_id") or "")
    }
    for item in samples:
        existing[item["sample_id"]] = dict(item)
    frozen = _assign_splits(list(existing.values()))
    frozen.sort(key=lambda item: item["sample_id"])
    atomic_write_text(
        path,
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in frozen
        ),
    )
    return frozen


def _replay_samples(store, skill, rows):
    samples = []
    seen = set()
    for row in rows:
        if str(row.get("skill") or "") != skill:
            continue
        messages = [
            {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
            for item in row.get("messages", [])
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and str(item.get("content") or "").strip()
        ]
        assistant = next(
            (item["content"] for item in reversed(messages) if item["role"] == "assistant"),
            "",
        )
        user = next(
            (item["content"] for item in reversed(messages) if item["role"] == "user"),
            "",
        )
        if not user or not assistant:
            continue
        sample_id = _sample_id(skill, messages)
        if sample_id in seen:
            continue
        seen.add(sample_id)
        samples.append(
            {
                "sample_id": sample_id,
                "messages": messages,
                "latest_user": user,
                "baseline_response": assistant,
            }
        )
    return _freeze_replay_samples(store, skill, samples)


def _skill_status(samples):
    """Report whether replay evidence is sufficient for candidate promotion."""
    promotion_tests = sum(item.get("split") == "promotion_test" for item in samples)
    if not samples:
        return "unobserved", ["no replay samples yet"]
    reasons = []
    if len(samples) < MIN_REPLAY_SAMPLES:
        reasons.append(f"only {len(samples)} replay sample(s)")
    if promotion_tests < MIN_PROMOTION_TESTS:
        reasons.append(f"only {promotion_tests} promotion-test sample(s)")
    if reasons:
        return "incubating", reasons
    return "healthy", ["replay gates passed"]


def _judge(side_query, requirement, skill_name, sample, response):
    payload = {
        "requirement": requirement,
        "skill_name": skill_name,
        "latest_user_message": sample.get("latest_user", ""),
        "response": str(response or ""),
    }
    parsed = _parse_json_object(
        side_query(
            EVAL_JUDGE_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            500,
        )
    )
    return {
        "pass": bool(parsed.get("pass", False)),
        "reason": str(parsed.get("reason") or "judge returned no reason")[:500],
    }


def _rate(values):
    return sum(1 for item in values if item.get("pass")) / len(values) if values else 0.0


def _candidate(side_query, skill, failures):
    if not failures:
        return None
    payload = {
        "skill": {
            "name": skill["name"],
            "description": skill["description"],
            "when_to_use": skill.get("when_to_use", ""),
            "instructions": skill.get("instructions", ""),
        },
        "rules": [
            {
                "rule_id": "skill_instruction_alignment",
                "description": skill.get("instructions", ""),
            }
        ],
        "failures": failures[:4],
    }
    parsed = _parse_json_object(
        side_query(
            EVAL_MUTATION_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            1600,
        )
    )
    instructions = str(parsed.get("instructions") or "").strip()
    if not instructions:
        return None
    return {
        "description": str(parsed.get("description") or skill["description"]),
        "instructions": instructions,
        "notes": str(parsed.get("notes") or "LLM-guided mutation")[:1000],
    }


def _candidate_response(side_query, candidate, sample):
    history = "\n\n".join(
        f"[{item['role']}] {item['content']}" for item in sample.get("messages", [])
    )
    return side_query(
        candidate["instructions"],
        REPLAY_REQUEST_TEMPLATE.format(history=history),
        2000,
    )


def _evaluate_split(side_query, skill, candidate, samples):
    requirement = skill.get("instructions") or skill.get("description", "")
    baseline, generated = [], []
    for sample in samples:
        baseline_judgment = _judge(
            side_query,
            requirement,
            skill["name"],
            sample,
            sample["baseline_response"],
        )
        response = _candidate_response(side_query, candidate, sample)
        candidate_judgment = _judge(
            side_query,
            requirement,
            skill["name"],
            sample,
            response,
        )
        baseline.append({"sample_id": sample["sample_id"], **baseline_judgment})
        generated.append(
            {
                "sample_id": sample["sample_id"],
                "response": response,
                **candidate_judgment,
            }
        )
    return {
        "baseline": baseline,
        "candidate": generated,
        "baseline_pass_rate": round(_rate(baseline), 4),
        "candidate_pass_rate": round(_rate(generated), 4),
    }


def evaluate_online_skills(agent, store, side_query):
    """Evaluate observed Skills without replacing their active files."""
    rows = _read_jsonl(store.root / ONLINE_PROVENANCE_LOG)
    documents = {item["name"]: item for item in skill_documents(agent)}
    names = sorted(
        set(documents)
        | {str(row.get("skill") or "") for row in rows if str(row.get("skill") or "")}
    )
    results = []
    for name in names:
        skill = documents.get(name)
        if not skill:
            continue
        samples = _replay_samples(store, name, rows)
        dev = [item for item in samples if item["split"] == "mutate_dev"]
        test = [item for item in samples if item["split"] == "promotion_test"]
        status, status_reasons = _skill_status(samples)
        requirement = skill.get("instructions") or skill.get("description", "")
        baseline_dev = [
            _judge(side_query, requirement, name, sample, sample["baseline_response"])
            for sample in dev
        ]
        failures = [item for item in baseline_dev if not item.get("pass")]
        candidate = _candidate(side_query, skill, failures)
        dev_eval = _evaluate_split(side_query, skill, candidate, dev) if candidate else {}
        test_eval = _evaluate_split(side_query, skill, candidate, test) if candidate and test else {}
        promoted = bool(
            candidate
            and status == "healthy"
            and dev_eval.get("candidate_pass_rate", 0) >= dev_eval.get("baseline_pass_rate", 0) + 0.01
            and (
                not test
                or test_eval.get("candidate_pass_rate", 0) >= test_eval.get("baseline_pass_rate", 0)
            )
        )
        lineage = {
            "skill": name,
            "samples": len(samples),
            "dev_samples": len(dev),
            "test_samples": len(test),
            "baseline_dev_pass_rate": round(_rate(baseline_dev), 4),
            "candidate": candidate or {},
            "dev": dev_eval,
            "test": test_eval,
            "status": status,
            "status_reasons": status_reasons,
            "promotion": "champion" if promoted else "not_promoted",
        }
        if promoted:
            champion_dir = store.root / "online-eval" / "champions" / name
            champion_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                champion_dir / "champion.json",
                {"generated_at": now(), **lineage},
                sort_keys=True,
            )
        results.append(lineage)
    report = {
        "generated_at": now(),
        "mode": "online_skill_lineage_eval",
        "skills": results,
        "aggregate": {
            "skills": len(results),
            "replay_samples": sum(item["samples"] for item in results),
            "champions": sum(1 for item in results if item["promotion"] == "champion"),
        },
    }
    atomic_write_json(store.root / "online_eval_report.json", report, sort_keys=True)
    return report


def format_online_skill_eval(report):
    aggregate = report.get("aggregate", {})
    lines = [
        "Online Skill evaluation:",
        f"  skills={aggregate.get('skills', 0)}, replay_samples={aggregate.get('replay_samples', 0)}, champions={aggregate.get('champions', 0)}",
    ]
    for item in report.get("skills", []):
        lines.append(
            f"  {item['skill']}: status={item['status']}, samples={item['samples']}, baseline_dev={item['baseline_dev_pass_rate']:.1%}, promotion={item['promotion']}"
        )
    if not report.get("skills"):
        lines.append("  no online skill lineage or replay samples found yet")
    return "\n".join(lines)
