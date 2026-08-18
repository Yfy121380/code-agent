"""文件编辑工具测试。

覆盖模块：patch_file、write_file 和文件版本防护。
重点边界：编辑前必须 read_file、grep 不满足读取要求、外部修改导致 stale、append/overwrite 语义。
"""

from codemate.ui import NullUI
from tests.helpers import build_agent


class EditorDiagnosticsUI(NullUI):
    """Return deterministic pre/post diagnostic snapshots for edit tests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def editor_diagnostics(self, path, *, wait_for_update=False):
        self.calls.append((path, wait_for_update))
        return self.responses.pop(0)


def test_patch_file_requires_fresh_read_first(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "must be read with read_file before editing" in result
    assert agent.session["read_files"] == {}
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"

def test_grep_does_not_satisfy_edit_read_requirement(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    grep_result = agent.run_tool("grep", {"pattern": "alpha", "path": "target.txt"})
    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "target.txt:1:alpha" in grep_result
    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"

def test_patch_file_allows_freshly_read_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    read_args = {"path": "target.txt", "start": 1, "end": 10}
    read_result = agent.run_tool("read_file", read_args)
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_read_target", "name": "read_file", "args": read_args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_read_target", "name": "read_file", "content": read_result, "created_at": "2026-04-09T00:00:00+00:00"})
    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert result == "patched target.txt"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "beta\n"
    preview = agent._last_tool_result_metadata["change_preview"]
    assert preview["path"] == "target.txt"
    assert preview["additions"] == 1
    assert preview["deletions"] == 1
    assert "+beta" in preview["diff"]

def test_patch_file_rejects_stale_read_after_external_change(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    read_args = {"path": "target.txt", "start": 1, "end": 10}
    read_result = agent.run_tool("read_file", read_args)
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_read_stale", "name": "read_file", "args": read_args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_read_stale", "name": "read_file", "content": read_result, "created_at": "2026-04-09T00:00:00+00:00"})
    (tmp_path / "target.txt").write_text("alpha changed\n", encoding="utf-8")

    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha changed\n"

def test_write_file_allows_new_file_without_prior_read(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "created.txt", "content": "new\n"})

    assert result == "wrote created.txt (4 chars)"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "new\n"
    preview = agent._last_tool_result_metadata["change_preview"]
    assert preview["status"] == "added"
    assert preview["additions"] == 1


def test_write_file_appends_only_new_editor_errors_to_tool_result(tmp_path):
    ui = EditorDiagnosticsUI(
        [
            {
                "status": "ok",
                "diagnostics": [
                    {
                        "path": "created.py",
                        "line": 2,
                        "column": 1,
                        "severity": "error",
                        "message": "Existing error",
                        "source": "pyright",
                        "code": "old",
                    }
                ],
            },
            {
                "status": "ok",
                "diagnostics": [
                    {
                        "path": "created.py",
                        "line": 7,
                        "column": 1,
                        "severity": "error",
                        "message": "Existing error",
                        "source": "pyright",
                        "code": "old",
                    },
                    {
                        "path": "created.py",
                        "line": 3,
                        "column": 5,
                        "severity": "error",
                        "message": '"missing" is not defined',
                        "source": "pyright",
                        "code": "reportUndefinedVariable",
                    },
                    {
                        "path": "created.py",
                        "line": 4,
                        "column": 1,
                        "severity": "warning",
                        "message": "Unused import",
                    },
                ],
            },
        ]
    )
    agent = build_agent(tmp_path, [], ui=ui)

    result = agent.run_tool(
        "write_file",
        {"path": "created.py", "content": "value = missing\n"},
    )

    assert result.startswith("wrote created.py")
    assert "New editor errors detected after this edit:" in result
    assert 'created.py:3:5 [error] "missing" is not defined' in result
    assert "Existing error" not in result
    assert "Unused import" not in result
    assert [wait for _path, wait in ui.calls] == [False, True]
    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    assert agent._last_tool_result_metadata["editor_diagnostics_checked"] is True
    assert agent._last_tool_result_metadata["editor_diagnostics_new_errors"] == 1


def test_edit_succeeds_when_editor_diagnostics_are_unavailable(tmp_path):
    ui = EditorDiagnosticsUI(
        [{"status": "unavailable", "diagnostics": []}]
    )
    agent = build_agent(tmp_path, [], ui=ui)

    result = agent.run_tool(
        "write_file",
        {"path": "created.py", "content": "value = 1\n"},
    )

    assert result == "wrote created.py (10 chars)"
    assert len(ui.calls) == 1
    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    assert "editor_diagnostics_checked" not in agent._last_tool_result_metadata


def test_repeated_edits_keep_the_original_diagnostic_baseline(tmp_path):
    ui = EditorDiagnosticsUI(
        [
            {"status": "ok", "diagnostics": []},
            {
                "status": "ok",
                "diagnostics": [
                    {
                        "path": "created.py",
                        "line": 1,
                        "column": 9,
                        "severity": "error",
                        "message": '"missing" is not defined',
                    }
                ],
            },
            {"status": "ok", "diagnostics": []},
        ]
    )
    agent = build_agent(tmp_path, [], ui=ui)

    broken = agent.run_tool(
        "write_file",
        {"path": "created.py", "content": "value = missing\n"},
    )
    fixed = agent.run_tool(
        "write_file",
        {"path": "created.py", "content": "value = 1\n"},
    )

    assert "New editor errors detected after this edit:" in broken
    assert fixed == "wrote created.py (10 chars)"
    assert [wait for _path, wait in ui.calls] == [False, True, True]
    assert agent._last_tool_result_metadata["editor_diagnostics_new_errors"] == 0

def test_write_file_requires_fresh_read_for_existing_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"

def test_write_file_append_requires_read_for_existing_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    rejected = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n", "mode": "append"})
    agent.run_tool("read_file", {"path": "target.txt", "read_all": True})
    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n", "mode": "append"})

    assert "must be read with read_file before editing" in rejected
    assert result == "appended target.txt (5 chars)"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"

def test_write_file_append_creates_missing_file(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "logs/daily.md", "content": "- remembered\n", "mode": "append"})

    assert result == "appended logs/daily.md (13 chars)"
    assert (tmp_path / "logs" / "daily.md").read_text(encoding="utf-8") == "- remembered\n"

def test_write_file_rejects_unknown_mode(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n", "mode": "merge"})

    assert "mode must be one of: overwrite, append" in result

def test_successful_edit_refreshes_file_state_for_follow_up_edit(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    rejected = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})
    assert "must be read with read_file before editing" in rejected

    read_args = {"path": "target.txt", "start": 1, "end": 10}
    agent.run_tool("read_file", read_args)
    first = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})
    second = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "beta", "new_text": "gamma"})

    assert first == "patched target.txt"
    assert second == "patched target.txt"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "gamma\n"
