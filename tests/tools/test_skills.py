"""Skill discovery, invocation, and history integration tests."""

from codemate import ModelResponse
from tests.helpers import build_agent, write_skill


def test_skill_load_returns_full_instructions_and_tracks_recent_invocation(tmp_path):
    write_skill(tmp_path, body="Use references/guide.md for details.")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("skill_load", {"name": "backend"})

    assert "Skill loaded: backend" in result
    assert f"Root: {agent.paths.project_skills / 'backend'}" in result
    assert "Instructions:" in result
    assert "Use references/guide.md for details." in result
    assert [item["name"] for item in agent.session["invoked_skills"]] == ["backend"]


def test_reinvoking_skill_refreshes_it_without_duplicate_state(tmp_path):
    write_skill(tmp_path, body="First instructions.")
    agent = build_agent(tmp_path, [])
    agent.run_tool("skill_load", {"name": "backend"})
    write_skill(tmp_path, body="Updated instructions.")

    result = agent.run_tool("skill_load", {"name": "backend"})

    assert "Updated instructions." in result
    assert len(agent.session["invoked_skills"]) == 1
    assert "Updated instructions." in agent.session["invoked_skills"][0]["content"]


def test_skill_state_keeps_only_three_most_recent_invocations(tmp_path):
    for name in ("one", "two", "three", "four"):
        write_skill(tmp_path, name=name, body=f"Instructions for {name}.")
    agent = build_agent(tmp_path, [])

    for name in ("one", "two", "three", "four"):
        agent.run_tool("skill_load", {"name": name})

    assert [item["name"] for item in agent.session["invoked_skills"]] == ["two", "three", "four"]


def test_read_only_policy_allows_skill_load(tmp_path):
    write_skill(tmp_path, body="Use references/guide.md for details.")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("skill_load", {"name": "backend"})

    assert "Skill loaded: backend" in result
    assert agent._last_tool_result_metadata["tool_status"] == "ok"


def test_skill_unload_is_not_exposed_to_model(tmp_path):
    write_skill(tmp_path)
    agent = build_agent(tmp_path, [])

    assert "skill_load" in agent.tools
    assert "skill_unload" not in agent.tools


def test_skill_load_rejects_frontmatter_name_mismatch(tmp_path):
    skill_dir = tmp_path / ".codemate" / "skills" / "backend"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-backend\ndescription: Bad fixture.\n---\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("skill_load", {"name": "backend"})

    assert "frontmatter name must match directory name" in result


def test_skill_load_emits_trace_event(tmp_path):
    write_skill(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("skill_load", {"name": "backend"}),
            ModelResponse.final("done"),
        ],
    )

    assert agent.ask("Use the backend skill") == "done"

    trace_text = (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "skill_loaded"' in trace_text
    assert '"skill": "backend"' in trace_text
