"""Skill 加载生命周期测试。

覆盖模块：tools/handlers skill_load/skill_unload、context working memory、trace。
重点边界：加载/重复加载/卸载、read_only 策略允许、frontmatter 校验、trace 事件。
"""

from codemate import ModelResponse
from tests.helpers import build_agent, write_skill


def test_skill_load_and_unload_update_active_skills(tmp_path):
    write_skill(tmp_path, body="Use references/guide.md for details.")
    agent = build_agent(tmp_path, [])

    loaded = agent.run_tool("skill_load", {"name": "backend"})
    duplicate = agent.run_tool("skill_load", {"name": "backend"})
    memory_text = agent.memory_text()
    unloaded = agent.run_tool("skill_unload", {"name": "backend", "reason": "task switched"})
    missing = agent.run_tool("skill_unload", {"name": "backend"})

    assert "skill loaded: backend" in loaded
    assert "skill already active" in duplicate
    assert "active_skills:" in memory_text
    assert f"Root: {agent.paths.project_skills / 'backend'}" in memory_text
    assert "Use references/guide.md" in memory_text
    assert "skill unloaded: backend" in unloaded
    assert "skill is not active" in missing

def test_read_only_policy_allows_skill_load_and_unload(tmp_path):
    write_skill(tmp_path, body="Use references/guide.md for details.")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    loaded = agent.run_tool("skill_load", {"name": "backend"})
    unloaded = agent.run_tool("skill_unload", {"name": "backend", "reason": "not needed"})

    assert "skill loaded: backend" in loaded
    assert "skill unloaded: backend" in unloaded
    assert agent.session["active_skills"] == []

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

def test_skill_load_and_unload_emit_trace_events(tmp_path):
    write_skill(tmp_path)
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("skill_load", {"name": "backend"}),
            ModelResponse.tool_call("skill_unload", {"name": "backend", "reason": "unrelated task"}),
            ModelResponse.final("done"),
        ],
    )

    assert agent.ask("Use backend skill briefly") == "done"

    trace_text = (agent.current_run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert '"event": "skill_loaded"' in trace_text
    assert '"skill": "backend"' in trace_text
    assert '"event": "skill_unloaded"' in trace_text
    assert '"reason": "unrelated task"' in trace_text
