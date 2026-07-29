"""grep 工具测试。

覆盖模块：tools/handlers.grep、tools/validators。
重点边界：三种输出模式、context 参数规则、workspace 外搜索、显式搜索 memory/skills 目录。
"""

from tests.helpers import build_agent, write_skill


def test_search_tool_name_is_not_registered(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("search", {"pattern": "demo", "path": "."})

    assert result == "error: unknown tool 'search'"

def test_grep_files_with_matches_returns_only_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("nothing\nneedle too\n", encoding="utf-8")
    (tmp_path / "src" / "gamma.py").write_text("nothing\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "needle", "path": "src", "mode": "files_with_matches"})

    assert "src/alpha.py" in result
    assert "src/beta.py" in result
    assert "src/gamma.py" not in result
    assert "needle here" not in result

def test_grep_count_returns_per_file_counts_and_total(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("needle needle\nneedle\n", encoding="utf-8")
    (tmp_path / "src" / "beta.py").write_text("needle\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "needle", "path": "src", "mode": "count"})

    assert "total_matches: 4" in result
    assert "src/alpha.py: 3" in result
    assert "src/beta.py: 1" in result

def test_grep_can_search_memory_directory_explicitly(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "User Profile", "path": str(agent.paths.memory_root), "mode": "content"})

    assert "memory/user_profile.md" in result

def test_list_and_grep_can_access_skills_directory_explicitly(tmp_path):
    write_skill(tmp_path, body="Use scripts/run.py when validating.")
    agent = build_agent(tmp_path, [])

    listing = agent.run_tool("list_files", {"path": ".codemate/skills/backend"})
    result = agent.run_tool("grep", {"pattern": "scripts/run.py", "path": ".codemate/skills", "mode": "content"})

    assert "SKILL.md" in listing
    assert ".codemate/skills/backend/SKILL.md" in result

def test_grep_content_context_supports_before_after_priority_over_context(tmp_path):
    (tmp_path / "sample.txt").write_text(
        "one\ntwo\nneedle\nfour\nfive\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "grep",
        {"pattern": "needle", "path": "sample.txt", "mode": "content", "context": 2, "before": 1, "after": 0},
    )

    assert "sample.txt-2-two" in result
    assert "sample.txt:3:needle" in result
    assert "sample.txt-1-one" not in result
    assert "sample.txt-4-four" not in result

def test_grep_rejects_context_for_non_content_modes(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "demo", "path": ".", "mode": "count", "context": 1})

    assert "before/after/context are only valid when mode='content'" in result
