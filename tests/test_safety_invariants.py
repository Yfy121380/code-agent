import os
import shlex
import sys
from unittest.mock import patch

from prompt_toolkit.document import Document

from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate import cli as mini_cli
from codemate.storage import TaskState


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

# 工作区外且 home 外路径拒绝
def test_outside_workspace_and_home_read_is_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "../outside.txt"})

    assert "outside the current workspace and outside home" in result

# 符号链接解析后指向 home 外路径时拒绝
def test_symlink_to_outside_home_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "outside the current workspace and outside home" in result

def test_grep_outside_workspace_and_home_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "abc", "path": "../outside"})

    assert "outside the current workspace and outside home" in result


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
    memory_listing = agent.run_tool("list_files", {"path": ".codemate/memory"})
    ignored_listing = agent.run_tool("list_files", {"path": ".codemate"})

    assert ".codemate" not in root_listing
    assert "user_profile.md" in memory_listing
    assert "path is ignored" in ignored_listing


def test_grep_can_search_memory_directory_explicitly(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "User Profile", "path": ".codemate/memory", "mode": "content"})

    assert ".codemate/memory/user_profile.md" in result


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
    assert "Root: .codemate/skills/backend" in memory_text
    assert "Use references/guide.md" in memory_text
    assert "skill unloaded: backend" in unloaded
    assert "skill is not active" in missing


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
    agent = build_agent(tmp_path, [], approval_policy="never")

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


def test_read_shell_command_allows_valid_workspace_paths_even_with_never_policy(tmp_path):
    (tmp_path / "notes.txt").write_text("safe read\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "cat notes.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "safe read" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"
    assert agent._last_tool_result_metadata["risk_level"] == "low"


def test_read_shell_command_allows_globs_when_paths_stay_in_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "cat *.txt", "timeout": 20})

    assert "exit_code: 0" in result
    assert "alpha" in result
    assert "beta" in result
    assert agent._last_tool_result_metadata["shell_has_glob"] is True


def test_shell_read_outside_workspace_and_home_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-shell.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": f"cat ../{outside.name}", "timeout": 20})

    assert "outside the current workspace and outside home" in result
    assert agent._last_tool_result_metadata["shell_kind"] == "read"


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


def test_dangerous_shell_command_full_policy_allows_without_prompt(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="full")

    result = agent.run_tool("run_shell", {"command": "rm README.md", "timeout": 20})

    assert "exit_code: 0" in result
    assert not (tmp_path / "README.md").exists()
    assert agent._last_tool_result_metadata["shell_kind"] == "dangerous"


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


# 危险工具审批拒绝
def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: write operation requires approval"

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
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
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
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
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
    with patch.dict(os.environ, {}, clear=True), patch("codemate.cli.AnthropicCompatibleModelClient", DummyModelClient):
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
        {
            "MCA_CUSTOM_SECRET": "custom-secret-value",
            "MINI_CODING_AGENT_SECRET_ENV_NAMES": "MCA_CUSTOM_SECRET",
        },
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

    assert any(str(item.display_text) == "/help" and str(item.display_meta_text) for item in root_items)
    assert any(str(item.display_text) == "/provider openai" for item in provider_items)
    assert any(str(item.display_text) == "/provider anthropic" for item in provider_items)
    assert any(str(item.display_text) == "/model gpt-5.5" for item in model_items)
    assert any(str(item.display_text) == "/model claude-opus-4-8" for item in model_items)
    remember = next(item for item in remember_items if str(item.display_text) == "/remember <text>")
    assert remember.text == "/remember "
    assert str(remember.display_meta_text) == "Append a memory entry to today's daily log."


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
    agent = build_agent(tmp_path, [], approval_policy="auto")
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
    assert agent.tool_run_shell.__func__.__module__ == "codemate.runtime"

    with patch("codemate.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"task": "inspect README.md", "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()

# delegate 超过 max_depth 会被拒绝
def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"task": "inspect README.md", "max_steps": 2})
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
            ModelResponse.tool_call("delegate", {"task": "write a file", "max_steps": 2}),
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

# 构造包含 secret 值的 payload，然后写 trace。
def test_configured_secret_env_names_are_redacted_in_trace(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}, clear=True):
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
