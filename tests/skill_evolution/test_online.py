"""Online Skill evolution lifecycle, ownership, and replay tests."""

import hashlib
import json
from dataclasses import replace

from codemate.skill_evolution.evaluation import evaluate_online_skills
from codemate.skill_evolution.online import (
    OnlineSkillCandidate,
    extract_candidate,
    maintain_candidate,
    online_ingest,
)
from codemate.skill_evolution.prompts import (
    EVAL_JUDGE_SYSTEM_PROMPT,
    EVAL_MUTATION_SYSTEM_PROMPT,
    EXTRACTOR_SYSTEM_PROMPT,
    MANAGER_SYSTEM_PROMPT,
    REPLAY_REQUEST_TEMPLATE,
    SKILL_EVOLUTION_PROMPT_RULES,
    USAGE_JUDGE_SYSTEM_PROMPT,
)
from codemate.skill_evolution.store import SkillEvolutionStore
from tests.helpers import build_agent, write_skill


def test_skill_evolution_prompt_snapshots_remain_reviewed():
    expected = {
        EXTRACTOR_SYSTEM_PROMPT: "852e6b3b3054120059b7e5eed80f3cf6e69bbf640aa3c1830ca1111cc8ea8aec",
        MANAGER_SYSTEM_PROMPT: "2e8df59092b5ef7a9781fc560b5eb35096710e925a020dd83bb7c75cf97d74a7",
        USAGE_JUDGE_SYSTEM_PROMPT: "ead3c8c1c339b2fcf7a9c4a5652e52d3c938e74f661fb2563064992009d4dbff",
        EVAL_JUDGE_SYSTEM_PROMPT: "82e610f98f05bc2251a41d7dadfcb40167cc33e63afb2580c7e075f834b9ae2a",
        EVAL_MUTATION_SYSTEM_PROMPT: "3dc2dc1d2e03df5fa5d74ba8ae22710ec7fad8501b79ddee4d4234d344f45eb2",
        REPLAY_REQUEST_TEMPLATE: "9b12c22e639fb668492429d531df423a19b7e3215139847381c93e268f57f4a1",
        SKILL_EVOLUTION_PROMPT_RULES: "cf8130dd735f853baadf3608e08aa4af326f9528a4f3a02a1c432595b6564afd",
    }
    for prompt, digest in expected.items():
        assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == digest


def test_pending_window_receives_next_user_feedback_without_request_retrieval(tmp_path):
    write_skill(
        tmp_path,
        name="python-testing",
        description="Run focused Python tests",
        body="Use pytest for focused Python validation.",
    )
    agent = build_agent(tmp_path, [])
    agent.session["skill_evolution_pending"] = {
        "messages": [
            {"role": "user", "content": "Run tests"},
            {"role": "assistant", "content": "I ran syntax checks"},
        ],
        "loaded_skill_references": [],
    }

    agent.skill_evolution.prepare_request("Use pytest instead")

    assert agent.skill_evolution._ready_window["messages"][-1] == {
        "role": "user",
        "content": "Use pytest instead",
    }
    assert "retrieved_skills" not in agent.runtime_context_text()
    assert agent.session["skill_evolution_pending"] is None


def test_loaded_skills_are_saved_as_next_window_identity_references(tmp_path):
    write_skill(
        tmp_path,
        name="python-testing",
        description="Run focused Python tests",
        body="Use pytest for focused Python validation.",
        when_to_use="When validating Python changes",
    )
    agent = build_agent(tmp_path, [])
    agent.skill_evolution.prepare_request("Validate this change")

    agent.run_tool("skill_load", {"name": "python-testing"})
    agent.session["history"] = [
        {"role": "user", "content": "Validate this change"},
        {"role": "assistant", "content": "Validation complete"},
    ]
    agent.skill_evolution.after_completion(
        None, "Validate this change", "Validation complete"
    )

    references = agent.session["skill_evolution_pending"]["loaded_skill_references"]
    assert references == [
        {
            "name": "python-testing",
            "description": "Run focused Python tests",
            "when_to_use": "When validating Python changes",
            "source": "project",
        }
    ]
    assert "instructions" not in references[0]


def test_extractor_receives_loaded_skill_identity_without_full_body():
    captured = {}

    def side_query(_system, user, _max_tokens):
        captured.update(json.loads(user))
        return '{"skills": []}'

    candidate = extract_candidate(
        [{"role": "user", "content": "Use focused tests"}],
        side_query,
        loaded_skill_references=[
            {
                "name": "python-testing",
                "description": "Run focused Python tests",
                "when_to_use": "When validating Python changes",
                "source": "project",
            }
        ],
    )

    assert candidate is None
    assert captured["loaded_skill_references"][0]["name"] == "python-testing"
    assert "instructions" not in captured["loaded_skill_references"][0]


def test_plan_prefix_does_not_describe_hidden_skill_mutation_tools(tmp_path):
    agent = build_agent(tmp_path, [])

    assert "# Skill Evolution" in agent.prefix_states["agent"].text
    assert "# Skill Evolution" not in agent.prefix_states["plan"].text
    assert "skill_create" not in {item["name"] for item in agent.model_tool_specs_by_mode["plan"]}


def test_online_add_then_merge_updates_only_managed_skill(tmp_path):
    agent = build_agent(tmp_path, [])
    responses = iter(
        [
            json.dumps(
                {
                    "skills": [
                        {
                            "name": "focused-testing",
                            "description": "Run focused tests before broad validation",
                            "when_to_use": "When validating a code change",
                            "instructions": "# Workflow\n\nRun focused tests first.",
                            "evidence": "The user requested focused tests.",
                            "tags": ["testing"],
                        }
                    ]
                }
            ),
            json.dumps({"action": "add", "reason": "new durable workflow"}),
            json.dumps(
                {
                    "skills": [
                        {
                            "name": "focused-testing",
                            "description": "Run focused tests before broad validation",
                            "instructions": "# Workflow\n\nRun focused tests, then related tests.",
                            "evidence": "The user refined the workflow.",
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "action": "merge",
                    "target_skill": "focused-testing",
                    "reason": "refinement",
                    "merged_instructions": "# Workflow\n\nRun focused tests, then related tests.",
                }
            ),
        ]
    )

    def side_query(_system, _user, _max_tokens):
        return next(responses)

    messages = [
        {"role": "user", "content": "Validate narrowly first"},
        {"role": "assistant", "content": "Done"},
        {"role": "user", "content": "Also run related tests"},
    ]
    added = online_ingest(
        agent,
        agent.skill_evolution.store,
        messages,
        side_query,
        confirm_write=lambda *_args: True,
    )
    merged = online_ingest(
        agent,
        agent.skill_evolution.store,
        messages,
        side_query,
        confirm_write=lambda *_args: True,
    )

    path = agent.paths.project_skills / "focused-testing" / "SKILL.md"
    assert added["action"] == "add"
    assert merged["action"] == "merge"
    assert "Run focused tests, then related tests." in path.read_text(encoding="utf-8")
    assert agent.skill_evolution.store.is_managed("focused-testing", path)


def test_external_or_user_modified_skill_cannot_be_evolved(tmp_path):
    write_skill(tmp_path, name="external")
    agent = build_agent(tmp_path, [])

    rejected = agent.run_tool(
        "skill_evolve",
        {"skill_name": "external", "lesson": "Change it"},
    )
    assert "not managed by CodeMate" in rejected

    created = agent.run_tool(
        "skill_create",
        {
            "name": "managed",
            "description": "Managed workflow",
            "instructions": "# Workflow\n\nOriginal.",
        },
    )
    assert '"ok": true' in created
    path = agent.paths.project_skills / "managed" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")

    rejected = agent.run_tool(
        "skill_evolve",
        {"skill_name": "managed", "lesson": "Overwrite it"},
    )
    assert "modified externally" in rejected


def test_managed_registry_cannot_redirect_evolution_outside_skill_roots(tmp_path):
    agent = build_agent(tmp_path, [])
    store = agent.skill_evolution.store
    store.create_skill(
        name="managed",
        description="Managed workflow",
        instructions="# Workflow\n\nOriginal.",
    )
    registry_path = store.root / "managed_skills.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    victim = tmp_path / "victim.md"
    victim.write_text("do not overwrite\n", encoding="utf-8")
    registry["managed"]["path"] = str(victim)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    assert store.managed_path("managed") is None
    assert victim.read_text(encoding="utf-8") == "do not overwrite\n"
    updated = json.loads(registry_path.read_text(encoding="utf-8"))
    assert updated["managed"]["status"] == "invalid_path"


def test_background_auto_mode_can_write_only_inside_user_skill_root(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    user_skill = agent.paths.user_skills / "generated" / "SKILL.md"

    assert agent.skill_evolution._background_write_allowed(
        "add", "generated", user_skill
    )
    assert not agent.skill_evolution._background_write_allowed(
        "add", "generated", tmp_path.parent / "outside.md"
    )


def test_background_write_honors_explicit_path_rules_before_approval_policy(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    project_skill = agent.paths.project_skills / "generated" / "SKILL.md"
    user_skill = agent.paths.user_skills / "generated" / "SKILL.md"

    assert agent.skill_evolution._background_write_allowed(
        "add", "generated", project_skill
    )
    assert not agent.skill_evolution._background_write_allowed(
        "add", "generated", user_skill
    )

    agent.permission_rules = replace(
        agent.permission_rules,
        write_deny=(*agent.permission_rules.write_deny, agent.paths.project_skills),
    )
    assert not agent.skill_evolution._background_write_allowed(
        "add", "generated", project_skill
    )


def test_manager_receives_twenty_complete_skills_with_loaded_skill_priority(tmp_path):
    long_body = "Run Python validation with focused tests.\n\n" + "Keep detail.\n" * 600
    for index in range(24):
        write_skill(
            tmp_path,
            name=f"python-validation-{index}",
            description=f"Python validation workflow number {index}",
            body=(
                long_body
                if index == 0
                else f"Run Python validation workflow {index} with focused tests."
            ),
        )
    write_skill(
        tmp_path,
        name="loaded-specialist",
        description="An unrelated workflow loaded during the task",
        body="Preserve this complete loaded Skill body.",
    )
    agent = build_agent(tmp_path, [])
    candidate = OnlineSkillCandidate(
        name="python-validation-0",
        description="Validate Python changes with focused tests",
        when_to_use="When validating Python code changes",
        instructions="Run focused Python tests before broader validation.",
    )
    manager_payload = {}

    def side_query(system, user, _max_tokens):
        assert system == MANAGER_SYSTEM_PROMPT
        manager_payload.update(json.loads(user))
        return json.dumps({"action": "discard", "reason": "test"})

    maintain_candidate(
        agent,
        agent.skill_evolution.store,
        candidate,
        side_query,
        loaded_skill_references=[{"name": "loaded-specialist"}],
        confirm_write=lambda *_args: True,
    )

    assert set(manager_payload) == {"candidate", "similar_skills"}
    assert len(manager_payload["similar_skills"]) == 20
    assert all(item["instructions"] for item in manager_payload["similar_skills"])
    loaded_skill = next(
        item
        for item in manager_payload["similar_skills"]
        if item["name"] == "loaded-specialist"
    )
    assert loaded_skill["instructions"] == "Preserve this complete loaded Skill body."
    exact_skill = next(
        item
        for item in manager_payload["similar_skills"]
        if item["name"] == "python-validation-0"
    )
    assert exact_skill["instructions"] == long_body


def test_manager_rejects_merge_without_explicit_target(tmp_path):
    write_skill(tmp_path, name="existing", description="Existing workflow")
    agent = build_agent(tmp_path, [])
    candidate = OnlineSkillCandidate(
        name="new-workflow",
        description="A distinct candidate",
        instructions="Follow the candidate workflow.",
    )

    result = maintain_candidate(
        agent,
        agent.skill_evolution.store,
        candidate,
        lambda *_args: json.dumps({"action": "merge", "reason": "missing target"}),
        confirm_write=lambda *_args: True,
    )

    assert result["ok"] is False
    assert result["action"] == "invalid_decision"
    assert "target_skill" in result["error"]


def test_manager_reviews_fallback_skills_and_keeps_explicit_add_decision(tmp_path):
    for name in ("paper-summary", "frontend-layout", "database-migration"):
        write_skill(
            tmp_path,
            name=name,
            description=f"Existing {name} workflow",
            body=f"Complete instructions for {name}.",
        )
    agent = build_agent(tmp_path, [])
    candidate = OnlineSkillCandidate(
        name="release-checklist",
        description="Prepare a release checklist",
        instructions="Verify release readiness before publishing.",
    )
    captured = {}

    def side_query(_system, user, _max_tokens):
        captured.update(json.loads(user))
        return json.dumps({"action": "add", "reason": "distinct workflow"})

    result = maintain_candidate(
        agent,
        agent.skill_evolution.store,
        candidate,
        side_query,
        confirm_write=lambda *_args: True,
    )

    assert result["action"] == "add"
    assert {item["name"] for item in captured["similar_skills"]} == {
        "paper-summary",
        "frontend-layout",
        "database-migration",
    }
    assert all(item["instructions"] for item in captured["similar_skills"])


def test_evolve_target_must_match_registered_scope(tmp_path):
    agent = build_agent(tmp_path, [])
    store = agent.skill_evolution.store
    store.create_skill(
        name="managed",
        description="Managed workflow",
        instructions="# Workflow\n\nOriginal.",
        target="project",
    )

    try:
        store.evolve_skill(name="managed", lesson="Refine it", target="user")
    except ValueError as exc:
        assert "project scope" in str(exc)
    else:
        raise AssertionError("scope mismatch should be rejected")


def test_usage_pruning_moves_only_managed_skills(tmp_path):
    store = SkillEvolutionStore(
        tmp_path / "state",
        tmp_path / "project-skills",
        tmp_path / "user-skills",
        prune_min_retrieved=2,
        prune_max_used=0,
    )
    store.create_skill(
        name="unused",
        description="Unused workflow",
        instructions="# Workflow\n\nDo work.",
    )
    external = tmp_path / "project-skills" / "external"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\nname: external\ndescription: External\n---\n",
        encoding="utf-8",
    )
    def judgment(name, path):
        return {
            "name": name,
            "skill_dir": str(path),
            "source": "project",
            "relevant": False,
            "used": False,
        }

    store.record_usage(
        [judgment("unused", tmp_path / "project-skills" / "unused")],
        confirm_prune=lambda *_args: True,
    )
    result = store.record_usage(
        [
            judgment("unused", tmp_path / "project-skills" / "unused"),
            judgment("external", external),
            judgment("external", external),
        ],
        confirm_prune=lambda *_args: True,
    )

    assert result["pruned"] == ["unused"]
    assert not (tmp_path / "project-skills" / "unused").exists()
    assert external.exists()


def test_usage_pruning_requires_write_policy_approval(tmp_path):
    store = SkillEvolutionStore(
        tmp_path / "state",
        tmp_path / "project-skills",
        tmp_path / "user-skills",
        prune_min_retrieved=1,
        prune_max_used=0,
    )
    store.create_skill(
        name="unused",
        description="Unused workflow",
        instructions="# Workflow\n\nDo work.",
    )
    result = store.record_usage(
        [{"name": "unused", "relevant": False, "used": False}],
        confirm_prune=lambda *_args: False,
    )

    assert result["pruned"] == []
    assert (tmp_path / "project-skills" / "unused" / "SKILL.md").is_file()


def test_replay_evaluation_can_record_a_candidate_champion(tmp_path):
    agent = build_agent(tmp_path, [])
    store = agent.skill_evolution.store
    created = store.create_skill(
        name="concise-answer",
        description="Answer concisely",
        instructions="Always answer concisely and include the result.",
    )
    for index in range(2):
        store.record_provenance(
            action="add",
            result=created,
            messages=[
                {"role": "user", "content": f"Question {index}"},
                {"role": "assistant", "content": "A vague baseline"},
            ],
        )

    def side_query(system, user, _max_tokens):
        if system == EVAL_JUDGE_SYSTEM_PROMPT:
            payload = json.loads(user)
            passed = "clear candidate" in payload["response"]
            return json.dumps({"pass": passed, "reason": "clear" if passed else "vague"})
        if system == EVAL_MUTATION_SYSTEM_PROMPT:
            return json.dumps(
                {
                    "description": "Answer clearly and concisely",
                    "instructions": "Return a clear candidate answer.",
                    "notes": "clarify output",
                }
            )
        return "clear candidate"

    report = evaluate_online_skills(agent, store, side_query)

    assert report["aggregate"]["champions"] == 1
    assert (
        store.root / "online-eval" / "champions" / "concise-answer" / "champion.json"
    ).is_file()


def test_replay_candidate_stays_incubating_without_enough_replay_samples(tmp_path):
    agent = build_agent(tmp_path, [])
    store = agent.skill_evolution.store
    created = store.create_skill(
        name="incubating",
        description="Incubating workflow",
        instructions="Return a clear result.",
    )
    for index in range(1):
        store.record_provenance(
            action="add",
            result=created,
            messages=[
                {"role": "user", "content": f"Question {index}"},
                {"role": "assistant", "content": "A vague baseline"},
            ],
        )

    def side_query(system, user, _max_tokens):
        if system == EVAL_JUDGE_SYSTEM_PROMPT:
            response = json.loads(user)["response"]
            return json.dumps({"pass": response == "clear candidate", "reason": "checked"})
        if system == EVAL_MUTATION_SYSTEM_PROMPT:
            return json.dumps(
                {
                    "description": "Clear workflow",
                    "instructions": "Return a clear result.",
                    "notes": "clarify",
                }
            )
        return "clear candidate"

    report = evaluate_online_skills(agent, store, side_query)

    assert report["skills"][0]["status"] == "incubating"
    assert report["skills"][0]["promotion"] == "not_promoted"
    assert report["aggregate"]["champions"] == 0


def test_completion_does_not_schedule_usage_judging(tmp_path):
    agent = build_agent(tmp_path, [])
    runtime = agent.skill_evolution
    runtime._ready_window = {"messages": [{"role": "user", "content": "feedback"}]}
    operations = []
    runtime._schedule = lambda operation, _callback, _task_state: operations.append(operation)

    runtime.after_completion(None, "request", "answer")

    assert operations == ["skill_evolution"]
