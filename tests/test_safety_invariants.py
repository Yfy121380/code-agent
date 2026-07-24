import json
import os
import shlex
import sys
from io import StringIO
from unittest.mock import patch

from prompt_toolkit.document import Document
from rich.console import Console

from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate import cli as mini_cli
from codemate.storage import TaskState
from codemate.tools.constants import LIST_FILE_LINE_COUNT_MAX_BYTES, MAX_TOOL_RESULT_CHARS
from codemate.ui import TerminalUI


class RememberingApprovalUI:
    def __init__(self):
        self.calls = []

    def approval_request(self, name, args, metadata=None):
        metadata = dict(metadata or {})
        self.calls.append({"name": name, "args": dict(args or {}), "metadata": metadata})
        return {
            "allowed": True,
            "remember": {
                "access": metadata["approval_access"],
                "path": metadata["suggested_allow_dir"],
            },
        }

    def tool_start(self, name, args, risk_level=""):
        pass

    def tool_result(self, name, args, result, metadata=None):
        pass

    def model_start(self):
        pass

    def model_end(self, kind="", metadata=None):
        pass

    def final_answer(self, text):
        pass


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def write_skill(tmp_path, name="backend", description="Backend workflow", body="Follow backend rules."):
    skill_dir = tmp_path / ".codemate" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def isolated_env(tmp_path, extra=None):
    # 有些测试需要 clear=True 验证环境变量收集逻辑。
    # 这里把测试隔离用 HOME/CODEMATE_HOME 补回去，避免状态写进真实 ~/.codemate。
    values = {
        "HOME": str(tmp_path.parent),
        "CODEMATE_HOME": str(tmp_path.parent / f"{tmp_path.name}-home" / ".codemate"),
    }
    values.update(extra or {})
    return values

# 工作区外路径不再由 home 边界硬拒绝，读权限交给规则和 approval policy 判断。
def test_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": f"../../{outside.name}"})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"


def test_symlink_to_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"


def test_grep_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-grep"
    outside.mkdir(exist_ok=True)
    (outside / "note.txt").write_text("abc\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "abc", "path": f"../../{outside.name}", "mode": "content"})

    assert "abc" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"


def test_outside_workspace_home_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-home.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        result = agent.run_tool("read_file", {"path": f"../{outside.name}", "start": 1, "end": 1})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"


def test_outside_workspace_home_write_auto_policy_asks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-write.txt"
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        result = agent.run_tool("write_file", {"path": f"../{outside.name}", "content": "outside\n"})

    assert result == "error: approval denied for write_file"
    assert not outside.exists()
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "ask"


def test_outside_workspace_write_uses_rules_and_approval_instead_of_path_denial(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-write.txt"
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("write_file", {"path": f"../../{outside.name}", "content": "outside\n"})

    assert result == "error: approval denied for write_file"
    assert not outside.exists()
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "ask"
    assert agent._last_tool_result_metadata["tool_error_code"] == "approval_denied"


def test_approval_can_add_temporary_write_allow_for_session(tmp_path):
    ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=ui)

    first = agent.run_tool("write_file", {"path": "first.txt", "content": "one\n"})
    second = agent.run_tool("write_file", {"path": "second.txt", "content": "two\n"})

    assert first == "wrote first.txt (4 chars)"
    assert second == "wrote second.txt (4 chars)"
    assert len(ui.calls) == 1
    assert ui.calls[0]["metadata"]["approval_access"] == "write"
    assert ui.calls[0]["metadata"]["suggested_allow_dir"] == str(tmp_path.resolve())


def test_temporary_permission_persists_when_session_resumes(tmp_path):
    first_ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=first_ui)

    first = agent.run_tool("write_file", {"path": "first.txt", "content": "one\n"})
    session_id = agent.session["id"]
    saved = json.loads(agent.session_store.path(session_id).read_text(encoding="utf-8"))

    second_ui = RememberingApprovalUI()
    resumed = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session=agent.session_store.load(session_id),
        approval_policy="ask",
        ui=second_ui,
    )
    second = resumed.run_tool("write_file", {"path": "second.txt", "content": "two\n"})

    assert first == "wrote first.txt (4 chars)"
    assert second == "wrote second.txt (4 chars)"
    assert saved["temporary_permissions"]["permissions"]["write"]["allow"] == [str(tmp_path.resolve())]
    assert len(first_ui.calls) == 1
    assert second_ui.calls == []


def test_approval_can_add_temporary_read_allow_for_session(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-read-dir"
    outside_dir.mkdir()
    (outside_dir / "one.txt").write_text("one\n", encoding="utf-8")
    (outside_dir / "two.txt").write_text("two\n", encoding="utf-8")
    ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=ui)

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        first = agent.run_tool("read_file", {"path": f"../{outside_dir.name}/one.txt", "start": 1, "end": 1})
        second = agent.run_tool("read_file", {"path": f"../{outside_dir.name}/two.txt", "start": 1, "end": 1})

    assert "one" in first
    assert "two" in second
    assert len(ui.calls) == 1
    assert ui.calls[0]["metadata"]["approval_access"] == "read"
    assert ui.calls[0]["metadata"]["suggested_allow_dir"] == str(outside_dir.resolve())


def test_permission_deny_overrides_temporary_allow(tmp_path):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "note.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".codemate").mkdir(exist_ok=True)
    (tmp_path / ".codemate" / "settings.json").write_text(
        '{"mcp":{"servers":{}},"permissions":{"read":{"allow":[],"deny":["secret"]},"write":{"allow":[],"deny":[]}}}\n',
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [], approval_policy="full")
    agent.add_temporary_permission("read", secret_dir)

    result = agent.run_tool("read_file", {"path": "secret/note.txt", "start": 1, "end": 1})

    assert "read denied by permission rules" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "read_permission_denied"


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


def test_memory_directory_is_explicitly_accessible_but_not_default_listed(tmp_path):
    agent = build_agent(tmp_path, [])

    root_listing = agent.run_tool("list_files", {"path": "."})
    memory_listing = agent.run_tool("list_files", {"path": str(agent.paths.memory_root)})
    ignored_listing = agent.run_tool("list_files", {"path": ".codemate"})

    assert ".codemate" not in root_listing
    assert "user_profile.md" in memory_listing
    assert "path is ignored" in ignored_listing


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

def test_patch_file_requires_fresh_read_first(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert "must be read with read_file before editing" in result
    notes = agent.session["memory"]["process_notes"]
    assert notes[0]["kind"] == "invalid_arguments"
    assert notes[0]["tool"] == "patch_file"
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


def test_write_file_requires_fresh_read_for_existing_file(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n"})

    assert "must be read with read_file before editing" in result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "alpha\n"


def test_write_file_append_adds_to_existing_file_without_prior_read(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("write_file", {"path": "target.txt", "content": "beta\n", "mode": "append"})

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


def test_todo_write_updates_session_without_workspace_change(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Inspect current implementation",
                    "status": "completed",
                    "tasks": [{"description": "Read tool files", "status": "completed"}],
                },
                {
                    "phase": "Add todo_write tool",
                    "status": "in_progress",
                    "tasks": [{"description": "Update handler", "status": "in_progress"}],
                },
                {"phase": "Add tests", "status": "pending", "tasks": []},
            ]
        },
    )

    assert "todos updated: 3 phases, 2 tasks, 1 phase in_progress, 1 task in_progress" in result
    assert agent.session["todos"] == [
        {
            "phase": "Inspect current implementation",
            "status": "completed",
            "tasks": [{"description": "Read tool files", "status": "completed"}],
        },
        {
            "phase": "Add todo_write tool",
            "status": "in_progress",
            "tasks": [{"description": "Update handler", "status": "in_progress"}],
        },
        {"phase": "Add tests", "status": "pending", "tasks": []},
    ]
    assert agent._last_tool_result_metadata["read_only"] is True
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_todo_write_rejects_invalid_status_and_multiple_in_progress(tmp_path):
    agent = build_agent(tmp_path, [])

    invalid_status = agent.run_tool("todo_write", {"todos": [{"phase": "Do work", "status": "blocked", "tasks": []}]})
    multiple_active = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {"phase": "Do first thing", "status": "in_progress", "tasks": []},
                {"phase": "Do second thing", "status": "in_progress", "tasks": []},
            ]
        },
    )
    multiple_active_tasks = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Do work",
                    "status": "in_progress",
                    "tasks": [
                        {"description": "Do first thing", "status": "in_progress"},
                        {"description": "Do second thing", "status": "in_progress"},
                    ],
                }
            ]
        },
    )

    assert "status must be one of" in invalid_status
    assert "at most one phase may be in_progress" in multiple_active
    assert "at most one task may be in_progress within the same phase" in multiple_active_tasks
    assert agent.session["todos"] == []


def test_todo_write_rejects_empty_content(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("todo_write", {"todos": [{"phase": "  ", "status": "pending", "tasks": []}]})

    assert "phase must not be empty" in result
    assert agent.session["todos"] == []


def test_read_only_policy_allows_todo_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("todo_write", {"todos": [{"phase": "Inspect files", "status": "pending", "tasks": []}]})

    assert "todos updated: 1 phases" in result
    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    assert agent.session["todos"] == [{"phase": "Inspect files", "status": "pending", "tasks": []}]


def test_todo_write_rejects_inconsistent_phase_and_task_status(tmp_path):
    agent = build_agent(tmp_path, [])

    pending_with_done_task = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Implement changes",
                    "status": "pending",
                    "tasks": [{"description": "Fix bug", "status": "completed"}],
                }
            ]
        },
    )
    completed_with_pending_task = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Implement changes",
                    "status": "completed",
                    "tasks": [{"description": "Fix bug", "status": "pending"}],
                }
            ]
        },
    )

    assert "pending phase cannot contain completed or in_progress tasks" in pending_with_done_task
    assert "completed phase cannot contain pending or in_progress tasks" in completed_with_pending_task
    assert agent.session["todos"] == []


def test_todo_write_empty_or_all_completed_clears_session(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["todos"] = [{"phase": "Old task", "status": "pending", "tasks": []}]

    cleared = agent.run_tool("todo_write", {"todos": []})
    agent.session["todos"] = [{"phase": "Old task", "status": "pending", "tasks": []}]
    completed = agent.run_tool(
        "todo_write",
        {
            "todos": [
                {
                    "phase": "Inspect implementation",
                    "status": "completed",
                    "tasks": [{"description": "Read files", "status": "completed"}],
                },
                {"phase": "Run tests", "status": "completed", "tasks": []},
            ]
        },
    )

    assert cleared == "todos updated: todo list cleared."
    assert "all phases completed; todo list cleared" in completed
    assert agent.session["todos"] == []

def test_repeated_tool_call_uses_assistant_tool_calls_and_excludes_current_call(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"path": "README.md", "start": 1, "end": 1}

    first = agent.run_tool("read_file", args)
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "read_file", "args": args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": first, "created_at": "2026-04-09T00:00:00+00:00"})

    second = agent.run_tool("read_file", args, current_tool_call_id="call_2")
    assert "# README.md" in second
    agent.record({"role": "assistant", "content": "", "tool_calls": [{"id": "call_2", "name": "read_file", "args": args}], "created_at": "2026-04-09T00:00:00+00:00"})
    agent.record({"role": "tool", "tool_call_id": "call_2", "name": "read_file", "content": second, "created_at": "2026-04-09T00:00:00+00:00"})

    third = agent.run_tool("read_file", args, current_tool_call_id="call_3")

    assert "repeated identical tool call" in third
    notes = agent.session["memory"]["process_notes"]
    assert notes[0]["kind"] == "repeated_call"


def test_runtime_records_one_assistant_message_for_multiple_tool_calls(tmp_path):
    call_id = "toolu_read"
    second_call_id = "toolu_list"
    response = ModelResponse.from_tool_calls(
        [
            {"id": call_id, "name": "read_file", "args": {"path": "README.md", "start": 1, "end": 1}},
            {"id": second_call_id, "name": "list_files", "args": {"path": "."}},
        ],
        text="我先检查 README 和目录结构。",
    )
    agent = build_agent(tmp_path, [response, ModelResponse.final("done")])

    result = agent.ask("inspect README")

    assistant_calls = [item for item in agent.session["history"] if item.get("role") == "assistant" and item.get("tool_calls")]
    assert result == "done"
    assert len(assistant_calls) == 1
    assert assistant_calls[0]["kind"] == "tool_calls"
    assert assistant_calls[0]["content"] == "我先检查 README 和目录结构。"
    assert [call["id"] for call in assistant_calls[0]["tool_calls"]] == [call_id, second_call_id]


def test_runtime_records_commentary_response_and_continues(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.commentary("我先确认当前任务。"), ModelResponse.final("done")])

    result = agent.ask("say hello")

    assert result == "done"
    assert any(
        item.get("role") == "assistant" and item.get("kind") == "commentary" and item.get("content") == "我先确认当前任务。"
        for item in agent.session["history"]
    )


def test_main_agent_does_not_stop_at_max_steps(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": "README.md", "start": 1, "end": 1}, call_id="call_read"),
            ModelResponse.tool_call("list_files", {"path": "."}, call_id="call_list"),
            ModelResponse.final("done"),
        ],
        max_steps=1,
    )

    result = agent.ask("inspect more than one thing")

    assert result == "done"
    assert agent.current_task_state.tool_steps == 2
    assert agent.current_task_state.stop_reason == "final_answer_returned"


def test_repeated_call_process_note_clears_after_any_successful_tool(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"path": "README.md", "start": 1, "end": 1}

    for index in range(2):
        result = agent.run_tool("read_file", args)
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_{index}", "name": "read_file", "args": args}],
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "read_file",
                "content": result,
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )

    rejected = agent.run_tool("read_file", args, current_tool_call_id="call_2")
    assert "repeated identical tool call" in rejected
    assert agent.session["memory"]["process_notes"][0]["kind"] == "repeated_call"

    result = agent.run_tool("list_files", {"path": "."})

    assert "README.md" in result
    assert agent.session["memory"]["process_notes"] == []


def test_list_files_shows_text_line_count_binary_and_large_file(tmp_path):
    (tmp_path / "small.txt").write_text("one\ntwo\nthree", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    (tmp_path / "large.txt").write_bytes(b"a" * (LIST_FILE_LINE_COUNT_MAX_BYTES + 1))
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("list_files", {"path": "."})

    assert "[F] small.txt  3 lines" in result
    assert "[F] binary.bin  binary file" in result
    assert "[F] large.txt  large file" in result


def test_read_file_read_all_ignores_range(tmp_path):
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "notes.txt", "start": 2, "end": 2, "read_all": True})

    assert "   1: one" in result
    assert "   2: two" in result
    assert "   3: three" in result


def test_tool_result_is_truncated_before_history_and_trace_use(tmp_path):
    (tmp_path / "huge.txt").write_text("x" * (MAX_TOOL_RESULT_CHARS + 10_000), encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "huge.txt", "read_all": True})

    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert result.startswith("Tool result truncated from ")
    assert agent._last_tool_result_metadata["tool_result_truncated"] is True
    assert agent._last_tool_result_metadata["tool_result_max_chars"] == MAX_TOOL_RESULT_CHARS


def test_invalid_argument_process_note_clears_after_same_tool_success(tmp_path):
    (tmp_path / "target.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    rejected = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})
    assert "must be read with read_file before editing" in rejected

    read_args = {"path": "target.txt", "start": 1, "end": 10}
    agent.run_tool("read_file", read_args)
    result = agent.run_tool("patch_file", {"path": "target.txt", "old_text": "alpha", "new_text": "beta"})

    assert result == "patched target.txt"
    assert agent.session["memory"]["process_notes"] == []


def test_read_shell_command_allows_valid_workspace_paths_in_read_only_policy(tmp_path):
    (tmp_path / "notes.txt").write_text("safe read\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "cat notes.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "safe read" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"
    assert agent._last_tool_result_metadata["risk_level"] == "low"


def test_read_shell_command_allows_globs_when_paths_stay_in_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "cat *.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "alpha" in result
    assert "beta" in result
    assert agent._last_tool_result_metadata["shell_has_glob"] is True


def test_shell_read_outside_workspace_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-shell.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": f"cat ../../{outside.name}", "timeout": 20})

    assert "exit_code: 0" in result
    assert "outside" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"


def test_risky_shell_command_auto_policy_allows_workspace_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "mkdir logs", "timeout": 20})

    assert "exit_code: 0" in result
    assert (tmp_path / "logs").is_dir()
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"


def test_shell_redirection_is_risky_and_auto_policy_allows_workspace_write(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "echo hi > out.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "hi"
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"
    assert agent._last_tool_result_metadata["shell_has_redirection"] is True


def test_risky_shell_command_rejects_glob_write(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "backup").mkdir()
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "mv *.py backup/", "timeout": 20})

    assert "wildcards are not allowed for risky shell commands" in result
    assert (tmp_path / "a.py").exists()


def test_dangerous_shell_command_auto_policy_still_requires_approval(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("builtins.input", return_value="n"):
        result = agent.run_tool("run_shell", {"command": "rm README.md", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert (tmp_path / "README.md").exists()
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"


def test_python_script_shell_command_is_dangerous(tmp_path):
    (tmp_path / "test.py").write_text("print('hi')\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "python test.py", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"


def test_python_inline_code_is_dangerous_without_path_glob_rejection(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "python -c 'print(route.dependant.body_params[0].type_)'", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"
    assert agent._last_tool_result_metadata["shell_paths"] == []
    assert agent._last_tool_result_metadata["shell_has_glob"] is False


def test_python_py_compile_shell_command_stays_read(tmp_path):
    (tmp_path / "test.py").write_text("print('hi')\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "python -m py_compile test.py", "timeout": 20})

    assert "exit_code: 0" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"


def test_pytest_shell_command_is_risky_not_read(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="ask")

    result = agent.run_tool("run_shell", {"command": "pytest", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_kind"] == "risky"


def test_dangerous_shell_command_full_policy_allows_without_prompt(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="full")

    result = agent.run_tool("run_shell", {"command": "rm README.md", "timeout": 20})

    assert "exit_code: 0" in result
    assert not (tmp_path / "README.md").exists()
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"


def test_hard_blocked_shell_command_full_policy_still_rejects(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="full")

    result = agent.run_tool("run_shell", {"command": "reboot", "timeout": 20})

    assert "shell command is blocked even in full approval mode: reboot" in result
    assert agent._last_tool_result_metadata["shell_blocked"] is True
    assert "hard_blocked_shell_command" in agent._last_tool_result_metadata["shell_reasons"]


def test_dangerous_shell_command_rejects_glob_without_approval(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "rm *.md", "timeout": 20})

    assert "wildcards are not allowed for dangerous shell commands" in result
    assert (tmp_path / "README.md").exists()


def test_dangerous_shell_command_rejects_root_targets_without_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "rm -rf /", "timeout": 20})

    assert "dangerous shell target is blocked: /" in result


# read_only 模式拒绝非读取 shell 命令
def test_read_only_policy_blocks_write_like_shell(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="read_only")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: write operations are blocked in read-only mode"

"""
secret来源
命令行 --secret-env-name
默认根据环境中敏感名称推断
.env 中的 provider key
MINI_CODING_AGENT_SECRET_ENV_NAMES 配置
"""
def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}), clear=True), patch(
        "codemate.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--approval",
                "auto",
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = mini_cli.build_agent(args)
        assert {"GITHUB_PAT", "GH_PAT"} <= set(agent.secret_env_summary()["secret_env_names"])


def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path, {"GH_PAT": "ghp-default-1"}), clear=True), patch(
        "codemate.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert "GH_PAT" in agent.secret_env_summary()["secret_env_names"]


def test_cli_build_agent_loads_project_env_secrets_before_redaction_setup(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CODEMATE_DEEPSEEK_API_KEY=sk-project-secret\n", encoding="utf-8")
    with patch.dict(os.environ, isolated_env(tmp_path), clear=True), patch("codemate.cli.AnthropicCompatibleModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek"])
        agent = mini_cli.build_agent(args)
        assert "CODEMATE_DEEPSEEK_API_KEY" in agent.secret_env_summary()["secret_env_names"]


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, messages, max_new_tokens, **kwargs):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        isolated_env(
            tmp_path,
            {
                "MCA_CUSTOM_SECRET": "custom-secret-value",
                "MINI_CODING_AGENT_SECRET_ENV_NAMES": "MCA_CUSTOM_SECRET",
            },
        ),
        clear=True,
    ), patch("codemate.cli.OllamaModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert "MCA_CUSTOM_SECRET" in agent.secret_env_summary()["secret_env_names"]


def test_slash_command_completer_shows_descriptions_and_inserts_template():
    completer = mini_cli.SlashCommandCompleter()

    root_items = list(completer.get_completions(Document("/"), None))
    remember_items = list(completer.get_completions(Document("/rem"), None))
    provider_items = list(completer.get_completions(Document("/provider "), None))
    model_items = list(completer.get_completions(Document("/model "), None))
    approval_items = list(completer.get_completions(Document("/approval "), None))

    assert any(str(item.display_text) == "/help" and str(item.display_meta_text) for item in root_items)
    assert any(str(item.display_text) == "/approval read_only" for item in approval_items)
    assert any(str(item.display_text) == "/provider openai" for item in provider_items)
    assert any(str(item.display_text) == "/provider anthropic" for item in provider_items)
    assert any(str(item.display_text) == "/model gpt-5.5" for item in model_items)
    assert any(str(item.display_text) == "/model claude-opus-4-8" for item in model_items)
    remember = next(item for item in remember_items if str(item.display_text) == "/remember <text>")
    assert remember.text == "/remember "
    assert str(remember.display_meta_text) == "Append a memory entry to today's daily log."


def test_terminal_approval_can_return_session_allow_choice():
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    captured_choices = []

    def fake_menu(choices):
        captured_choices.extend(choices)
        return choices[1][1]

    with patch.object(ui, "approval_menu", fake_menu):
        decision = ui.approval_request(
            "read_file",
            {"path": "/home/user/data/a.txt"},
            metadata={
                "risk_level": "low",
                "approval_access": "read",
                "suggested_allow_dir": "/home/user/data",
            },
        )

    assert decision == {
        "allowed": True,
        "remember": {
            "access": "read",
            "path": "/home/user/data",
        },
    }
    assert [label for label, _decision in captured_choices] == [
        "Allow once",
        "Allow read for /home/user/data this session",
        "Deny",
    ]


def test_cli_build_switched_model_client_overrides_provider_and_model(tmp_path):
    class DummyOpenAIClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.model = kwargs["model"]
            self.base_url = kwargs["base_url"]

    args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "deepseek", "--model", "deepseek-v4-pro"])

    with patch.dict(os.environ, {}, clear=True), patch("codemate.cli.OpenAICompatibleModelClient", DummyOpenAIClient):
        client = mini_cli._build_switched_model_client(args, "openai", "gpt-5.5")

    assert client.model == "gpt-5.5"
    assert client.kwargs["temperature"] == args.temperature
    assert client.base_url == mini_cli.DEFAULT_OPENAI_BASE_URL

# run_shell只传allow_list，读不到MCA_ALLOWLIST_SECRET
def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="full")
    script = 'import os; print(os.getenv("MCA_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_bound_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.subprocess.run") as fake_run:
        fake_run.return_value = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "toolkit-shell\n", "stderr": ""},
        )()
        shell_result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_run.assert_called_once()
    assert agent.tool_run_shell.__func__.__module__ == "codemate.runtime.tool_execution"

    with patch("codemate.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"tasks": [{"task": "inspect README.md"}], "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()

# delegate 超过 max_depth 会被拒绝
def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"tasks": [{"task": "inspect README.md"}], "max_steps": 2})
    except ValueError as exc:
        assert "delegate depth exceeded" in str(exc)
    else:
        raise AssertionError("delegate depth validation did not fail")

# delegate 创建的 child agent 是 read_only
def test_delegate_child_is_read_only(tmp_path):
    target = tmp_path / "child-was-not-allowed.txt"
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("delegate", {"tasks": [{"task": "write a file"}], "max_steps": 2}),
            ModelResponse.tool_call("write_file", {"path": "child-was-not-allowed.txt", "content": "nope"}),
            ModelResponse.final("child done"),
            ModelResponse.final("parent done"),
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


def test_delegate_runs_multiple_read_only_investigations(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("first report"), ModelResponse.final("second report")], approval_policy="auto")

    result = agent.run_tool(
        "delegate",
        {
            "tasks": [
                {"task": "inspect README", "focus": "README.md"},
                {"task": "inspect tests", "focus": "tests"},
            ],
            "max_steps": 2,
        },
    )

    assert "Task 1: inspect README" in result
    assert "Task 2: inspect tests" in result
    assert "Status: ok" in result
    assert "first report" in result
    assert "second report" in result
    assert agent._last_tool_result_metadata["delegate_task_count"] == 2
    assert [item["status"] for item in agent._last_tool_result_metadata["delegate_tasks"]] == ["ok", "ok"]


def test_delegate_child_inherits_temporary_read_permissions(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-delegate-outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "note.txt"
    outside_file.write_text("delegated evidence\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call("read_file", {"path": str(outside_file), "start": 1, "end": 1}),
            ModelResponse.final("read delegated evidence"),
        ],
        approval_policy="ask",
    )
    agent.add_temporary_permission("read", outside_dir)

    result = agent.run_tool("delegate", {"tasks": [{"task": "read outside note", "focus": str(outside_file)}], "max_steps": 3})

    assert "Status: ok" in result
    assert "read delegated evidence" in result
    child_sessions = [item for item in agent.session_store.root.iterdir() if item.name.startswith("delegate-")]
    assert child_sessions
    child_session = json.loads((child_sessions[0] / "session.json").read_text(encoding="utf-8"))
    assert child_session["temporary_permissions"]["permissions"]["read"]["allow"] == [str(outside_dir.resolve())]
    child_tool_results = [item for item in child_session["history"] if item.get("role") == "tool"]
    assert child_tool_results
    assert "delegated evidence" in child_tool_results[0]["content"]

# 构造包含 secret 值的 payload，然后写 trace。
def test_configured_secret_env_names_are_redacted_in_trace(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, isolated_env(tmp_path, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}), clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert trace_text.count("<redacted>") >= 4
