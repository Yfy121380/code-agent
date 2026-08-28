"""BearCode-compatible retrieval, extraction, maintenance, and usage judging."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .prompts import EXTRACTOR_SYSTEM_PROMPT, MANAGER_SYSTEM_PROMPT, USAGE_JUDGE_SYSTEM_PROMPT
from .store import parse_skill_document


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
MAX_MANAGER_SKILLS = 20
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


@dataclass(frozen=True)
class OnlineSkillCandidate:
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)


def _parse_json_object(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(raw[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def _token_list(text):
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    values = [match.group(0) for match in _TOKEN_RE.finditer(raw)]
    return [value for value in values if value not in _STOP_TOKENS]


def _normalize_identity(text):
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", raw).strip()


def skill_documents(agent):
    """Read discovered Skills into the common retrieval representation."""
    documents = []
    for skill in agent.available_skills():
        path = Path(skill["root"]) / "SKILL.md"
        try:
            metadata, body = parse_skill_document(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        documents.append(
            {
                **skill,
                "when_to_use": str(metadata.get("when-to-use") or metadata.get("when_to_use") or ""),
                "instructions": body,
                "skill_dir": str(path.parent),
            }
        )
    return documents


def retrieve_relevant_skills(agent, query, *, limit=3, min_score=0.08):
    """Use BearCode's lightweight BM25 ranking across every active Skill."""
    query_tokens = set(_token_list(query))
    if not query_tokens:
        return []
    docs = []
    document_frequency = Counter()
    for skill in skill_documents(agent):
        metadata_terms = _token_list(
            "\n".join([skill["name"], skill["description"], skill["when_to_use"]])
        )
        body_terms = _token_list(skill["instructions"][:2500])
        terms = metadata_terms * 3 + body_terms
        if not terms:
            continue
        docs.append((skill, terms))
        document_frequency.update(set(terms))
    if not docs:
        return []

    average_length = sum(len(terms) for _, terms in docs) / len(docs)
    hits = []
    for skill, terms in docs:
        counts = Counter(terms)
        overlap = query_tokens.intersection(counts)
        if not overlap:
            continue
        raw_score = 0.0
        for token in overlap:
            frequency = counts[token]
            inverse = math.log(
                1 + (len(docs) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.4 * (1 - 0.75 + 0.75 * len(terms) / max(1.0, average_length))
            raw_score += inverse * (frequency * 2.4) / max(denominator, 0.0001)
        name_bonus = 0.15 if skill["name"].lower() in str(query).lower() else 0.0
        score = min(1.0, raw_score / max(3.0, len(query_tokens)) + name_bonus)
        if score < float(min_score):
            continue
        hits.append(
            {
                "score": score,
                "name": skill["name"],
                "description": skill["description"],
                "when_to_use": skill["when_to_use"],
                "source": skill.get("scope", ""),
                "context": "inline",
                "skill_dir": skill["skill_dir"],
            }
        )
    hits.sort(key=lambda item: float(item["score"]), reverse=True)
    return hits[: max(1, int(limit))]


def _coerce_candidate(value):
    if not isinstance(value, dict):
        return None
    raw_name = str(value.get("name") or "").strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name).strip("-.")[:120]
    description = str(value.get("description") or "").strip()
    instructions = str(value.get("instructions") or value.get("prompt") or "").strip()
    if not name or not description or not instructions:
        return None
    raw_tags = value.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [part.strip() for part in re.split(r"[,，]", raw_tags) if part.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(part).strip() for part in raw_tags if str(part).strip()]
    else:
        tags = []
    return OnlineSkillCandidate(
        name=name,
        description=description,
        when_to_use=str(value.get("when_to_use") or value.get("when-to-use") or "").strip(),
        instructions=instructions,
        evidence=str(value.get("evidence") or "").strip(),
        tags=tags[:8],
    )


def extract_candidate(messages, side_query, *, loaded_skill_references=None, hint=""):
    payload = {
        "messages": messages,
        "hint": hint,
        "loaded_skill_references": list(loaded_skill_references or []),
    }
    parsed = _parse_json_object(
        side_query(EXTRACTOR_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), 2200)
    )
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not skills:
        return None
    return _coerce_candidate(skills[0])


def _exact_identity_match(candidate, skills):
    candidate_values = {
        _normalize_identity(candidate.name),
        _normalize_identity(candidate.description),
        _normalize_identity(candidate.when_to_use),
    }
    candidate_values.discard("")
    for skill in skills:
        values = {
            _normalize_identity(skill.get("name")),
            _normalize_identity(skill.get("description")),
            _normalize_identity(skill.get("when_to_use")),
        }
        values.discard("")
        if candidate_values.intersection(values):
            return str(skill.get("name") or "")
    return ""


def _manager_skill_context(
    skills,
    similar_hits,
    exact_target="",
    loaded_skill_references=None,
    *,
    limit=MAX_MANAGER_SKILLS,
):
    """Attach complete bodies for exact, loaded, and similar Skills."""
    skills_by_name = {str(skill.get("name") or ""): skill for skill in skills}
    hits_by_name = {str(hit.get("name") or ""): hit for hit in similar_hits}
    ordered_names = []
    if exact_target:
        ordered_names.append(exact_target)
    ordered_names.extend(
        str(item.get("name") or "")
        for item in list(loaded_skill_references or [])
        if isinstance(item, dict)
    )
    ordered_names.extend(str(hit.get("name") or "") for hit in similar_hits)
    # BM25 may miss synonymous capabilities with no shared token. Fill the
    # remaining review slots so the manager still receives a broad comparison
    # set instead of silently treating a small hit list as exhaustive.
    ordered_names.extend(str(skill.get("name") or "") for skill in skills)

    context = []
    seen = set()
    for name in ordered_names:
        if not name or name in seen:
            continue
        skill = skills_by_name.get(name)
        if skill is None:
            continue
        hit = hits_by_name.get(name, {})
        context.append(
            {
                "name": name,
                "description": skill["description"],
                "when_to_use": skill["when_to_use"],
                "source": skill.get("scope", ""),
                "context": "inline",
                "score": float(hit.get("score", 1.0 if name == exact_target else 0.0)),
                "instructions": skill["instructions"],
            }
        )
        seen.add(name)
        if len(context) == max(1, int(limit)):
            break
    return context


def maintain_candidate(
    agent,
    store,
    candidate,
    side_query,
    *,
    loaded_skill_references=None,
    target="project",
    confirm_write=None,
):
    skills = skill_documents(agent)
    exact_target = _exact_identity_match(candidate, skills)
    similar_hits = retrieve_relevant_skills(
        agent,
        "\n".join([candidate.name, candidate.description, candidate.when_to_use, candidate.instructions, " ".join(candidate.tags)]),
        limit=MAX_MANAGER_SKILLS,
        min_score=0.03,
    )
    payload = {
        "candidate": asdict(candidate),
        "similar_skills": _manager_skill_context(
            skills,
            similar_hits,
            exact_target,
            loaded_skill_references,
            limit=MAX_MANAGER_SKILLS,
        ),
    }
    decision = _parse_json_object(
        side_query(MANAGER_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), 2200)
    )
    action = str(decision.get("action") or "").strip().lower()
    target_skill = str(decision.get("target_skill") or "").strip()
    if exact_target:
        action, target_skill = "merge", exact_target
    if action not in {"add", "merge", "discard"}:
        action = "discard"
    if action == "merge" and not target_skill:
        return {
            "ok": False,
            "action": "invalid_decision",
            "skill": "",
            "error": "manager returned merge without target_skill",
            "decision": decision,
        }
    if action == "discard":
        return {"ok": True, "action": action, "skill": "", "decision": decision}

    path = (
        store.managed_path(target_skill)
        if action == "merge" and target_skill
        else store.skill_path_for_create(candidate.name, target)
    )
    if action == "merge" and path is None:
        return {
            "ok": False,
            "action": "merge_denied",
            "skill": target_skill,
            "error": "automatic evolution may update only CodeMate-managed skills",
            "decision": decision,
        }
    if confirm_write is not None and not confirm_write(action, target_skill or candidate.name, path):
        return {
            "ok": False,
            "action": f"{action}_denied",
            "skill": target_skill or candidate.name,
            "error": "permission denied",
            "decision": decision,
        }
    if action == "merge":
        result = store.evolve_skill(
            name=target_skill,
            lesson=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer merge"),
            instructions=str(decision.get("merged_instructions") or candidate.instructions),
            description=str(decision.get("merged_description") or ""),
            when_to_use=str(decision.get("merged_when_to_use") or candidate.when_to_use),
            tags=candidate.tags,
            actor="online",
        )
    else:
        result = store.create_skill(
            name=candidate.name,
            description=candidate.description,
            instructions=candidate.instructions,
            when_to_use=candidate.when_to_use,
            target=target,
            context="inline",
            user_invocable=False,
            evidence=candidate.evidence,
            actor="online",
            tags=candidate.tags,
        )
    return {"action": action, "candidate": asdict(candidate), "decision": decision, **result}


def online_ingest(
    agent,
    store,
    messages,
    side_query,
    *,
    loaded_skill_references=None,
    hint="",
    target="project",
    confirm_write=None,
):
    candidate = None
    try:
        candidate = extract_candidate(
            messages,
            side_query,
            loaded_skill_references=loaded_skill_references,
            hint=hint,
        )
        if candidate is None:
            result = {"ok": True, "action": "none"}
        else:
            result = maintain_candidate(
                agent,
                store,
                candidate,
                side_query,
                loaded_skill_references=loaded_skill_references,
                target=target,
                confirm_write=confirm_write,
            )
    except Exception as exc:
        result = {
            "ok": False,
            "action": "failed",
            "skill": candidate.name if candidate else "",
            "error": str(exc),
        }
    store.record_provenance(
        action=result.get("action", "none"),
        result=result,
        messages=messages,
        loaded_skill_references=loaded_skill_references,
        decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        error="" if result.get("ok") else result.get("error", ""),
    )
    return result


def judge_retrieved_skill_usage(hits, user_message, assistant_text, side_query):
    if not hits:
        return []
    payload = {
        "user_message": user_message,
        "assistant_reply": assistant_text,
        "retrieved_skills": hits,
    }
    parsed = _parse_json_object(
        side_query(USAGE_JUDGE_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), 700)
    )
    values = parsed.get("judgments") if isinstance(parsed.get("judgments"), list) else []
    by_name = {str(item.get("name") or ""): item for item in values if isinstance(item, dict)}
    return [
        {
            "name": hit["name"],
            "source": hit.get("source", ""),
            "skill_dir": hit.get("skill_dir", ""),
            "retrieved": True,
            "relevant": bool(by_name.get(hit["name"], {}).get("relevant")),
            "used": bool(by_name.get(hit["name"], {}).get("used")),
            "score": float(hit.get("score", 0)),
            "reason": str(by_name.get(hit["name"], {}).get("reason") or ""),
        }
        for hit in hits
    ]
