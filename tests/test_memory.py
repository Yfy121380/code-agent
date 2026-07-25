import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codemate.memory import LayeredMemory
from codemate import memory as memorylib
from codemate.memory import dream as dreamlib
from codemate.memory import long_term as longterm
from codemate.models import FakeModelClient
from codemate.runtime import CodeMate
from codemate.storage import SessionStore
from codemate.workspace import WorkspaceContext


def test_working_memory_tracks_summary_and_recent_files():
    memory = LayeredMemory()

    memory.set_task_summary("Investigate flaky tests")
    memory.remember_file("README.md")
    memory.remember_file("src/app.py")
    memory.remember_file("README.md")

    snapshot = memory.to_dict()

    assert snapshot["working"]["task_summary"] == "Investigate flaky tests"
    assert snapshot["working"]["recent_files"] == ["src/app.py", "README.md"]
    assert "task" not in snapshot
    assert "files" not in snapshot
    assert "notes" not in snapshot
    assert "episodic_notes" not in snapshot


def test_file_summaries_use_canonical_paths_and_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    memory.remember_file("./sample.txt")
    snapshot = memory.to_dict()["file_summaries"]["sample.txt"]

    assert snapshot["summary"] == "sample.txt: alpha"
    assert snapshot["freshness"]

    assert "sample.txt: alpha" in memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")
    assert "sample.txt: alpha" not in memory.render_memory_text()

    memory.invalidate_file_summary("sample.txt")

    assert "sample.txt" not in memory.to_dict()["file_summaries"]


def test_has_fresh_file_summary_requires_recent_file_and_matching_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("sample.txt", "sample.txt: alpha")

    assert not memory.has_fresh_file_summary("sample.txt")

    memory.remember_file("sample.txt")

    assert memory.has_fresh_file_summary("sample.txt")

    file_path.write_text("beta\n", encoding="utf-8")

    assert not memory.has_fresh_file_summary("sample.txt")


def test_file_summary_freshness_supports_paths_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=workspace)

    memory.set_file_summary(str(outside), "outside: alpha")
    memory.remember_file(str(outside))

    assert memory.has_fresh_file_summary(str(outside))

    outside.write_text("beta\n", encoding="utf-8")

    assert not memory.has_fresh_file_summary(str(outside))


def test_process_notes_merge_duplicate_abnormal_tool_calls():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "rejected",
        "tool_error_code": "invalid_arguments",
    }

    memory.record_process_note("patch_file", {"path": "README.md"}, metadata, "error: invalid arguments", current_turn=1)
    memory.record_process_note("patch_file", {"path": "README.md"}, metadata, "error: invalid arguments again", current_turn=2)

    notes = memory.to_dict()["process_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "invalid_arguments"
    assert notes[0]["tool"] == "patch_file"
    assert notes[0]["count"] == 2
    assert notes[0]["updated_turn"] == 2
    assert notes[0]["message"] == "error: invalid arguments again"


def test_process_notes_expire_by_turn_ttl():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "rejected",
        "tool_error_code": "repeated_identical_call",
    }

    memory.record_process_note("read_file", {"path": "README.md"}, metadata, "error: repeated", current_turn=1)
    memory.expire_process_notes(current_turn=3)

    assert len(memory.to_dict()["process_notes"]) == 1

    memory.expire_process_notes(current_turn=4)

    assert memory.to_dict()["process_notes"] == []


def test_process_notes_clear_after_success_rules():
    memory = LayeredMemory()

    memory.record_process_note(
        "patch_file",
        {"path": "README.md"},
        {"tool_status": "rejected", "tool_error_code": "invalid_arguments"},
        "error: invalid arguments",
        current_turn=1,
    )
    memory.record_process_note(
        "read_file",
        {"path": "README.md"},
        {"tool_status": "rejected", "tool_error_code": "repeated_identical_call"},
        "error: repeated",
        current_turn=1,
    )

    memory.resolve_process_notes_after_success("list_files", {}, current_turn=1)
    notes = memory.to_dict()["process_notes"]

    assert [note["kind"] for note in notes] == ["invalid_arguments"]

    memory.resolve_process_notes_after_success("patch_file", {"path": "README.md"}, current_turn=1)

    assert memory.to_dict()["process_notes"] == []


def test_render_memory_text_includes_process_notes_near_file_summaries():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "error",
        "tool_error_code": "tool_failed",
    }

    memory.record_process_note("run_shell", {"command": "pytest"}, metadata, "exit_code: 1", current_turn=1)

    text = memory.render_memory_text()

    assert "- file_summaries:" in text
    assert "- process_notes:" in text
    assert "run_shell error, count=1" in text
    assert "exit_code: 1" in text


def test_long_term_memory_files_are_initialized(tmp_path):
    memory = LayeredMemory(workspace_root=tmp_path)

    memory_root = longterm.memory_root(tmp_path)

    assert (memory_root / "user_profile.md").is_file()
    assert (memory_root / "feedback_workflow.md").is_file()
    assert (memory_root / "project_context.md").is_file()
    assert (memory_root / "candidates").is_dir()
    assert set(memory.read_long_term_memory()) == {"user_profile", "feedback_workflow", "project_context"}


def test_long_term_memory_migrates_legacy_user_preferences_file(tmp_path):
    memory_root = longterm.memory_root(tmp_path)
    memory_root.mkdir(parents=True)
    (memory_root / "user_preferences.md").write_text("# User Preferences\n\n- old preference\n", encoding="utf-8")

    memory = LayeredMemory(workspace_root=tmp_path)

    assert not (memory_root / "user_preferences.md").exists()
    assert (memory_root / "user_profile.md").read_text(encoding="utf-8") == "# User Preferences\n\n- old preference\n"
    assert "user_profile" in memory.read_long_term_memory()


def test_retrieve_long_term_memory_keeps_created_at_and_limits_results(tmp_path):
    longterm.ensure_long_term_memory(tmp_path)
    longterm.long_term_file_path(tmp_path, "feedback_workflow").write_text(
        "# Feedback Workflow\n\n- [2026-07-25T10:12:00+08:00] 用户希望代码修改前先讨论方案。\n",
        encoding="utf-8",
    )
    selected = [
        {
            "source": "feedback_workflow",
            "created_at": "" if index == 0 else f"2026-07-25T10:{index:02d}:00+08:00",
            "text": "- [2026-07-25T10:12:00+08:00] 用户希望代码修改前先讨论方案。" if index == 0 else f"memory {index}",
            "reason": f"reason {index}",
        }
        for index in range(25)
    ]
    client = FakeModelClient([json.dumps({"selected": selected}, ensure_ascii=False)])

    result = memorylib.retrieve_long_term_memory(
        client,
        tmp_path,
        "现在开始修改代码",
        recent_messages=[{"role": "user", "content": "上一轮讨论了实现方案。"}],
    )

    assert result["status"] == "ok"
    assert len(result["selected"]) == 20
    assert result["selected"][0]["created_at"] == "2026-07-25T10:12:00+08:00"
    assert result["selected"][0]["text"] == "用户希望代码修改前先讨论方案。"
    assert "上一轮讨论了实现方案。" in client.prompts[0]
    assert client.tool_specs[0] == []


def test_remember_long_term_appends_candidate_memory(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    agent = CodeMate(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    result = agent.remember_long_term("用户希望以后先说明修改范围")

    log_path = Path(result["path"])
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "/.codemate/projects/" in result["path"]
    assert "/memory/candidates/" in result["path"]
    assert result["entry"] == "用户希望以后先说明修改范围"
    assert entries[-1]["type"] == "unspecified"
    assert entries[-1]["memory"] == "用户希望以后先说明修改范围"
    assert entries[-1]["evidence"] == "用户通过 /remember 要求记住这条信息。"
    assert entries[-1]["confidence"] == "high"


def test_dream_trigger_uses_unprocessed_candidate_count_and_time(tmp_path):
    log_path = longterm.candidate_log_path(tmp_path, date="2026-07-09")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "created_at": "2026-07-09T10:00:00+08:00",
        "type": "feedback_workflow",
        "memory": "用户希望回答先给结论。",
        "evidence": "用户明确说明。",
        "confidence": "high",
    }
    log_path.write_text("\n".join(json.dumps(candidate, ensure_ascii=False) for _ in range(9)) + "\n", encoding="utf-8")

    assert dreamlib.should_run_dream(tmp_path) == (False, "not_enough_candidates")

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")

    assert dreamlib.should_run_dream(tmp_path) == (True, "candidate_threshold")

    longterm.save_dream_state(tmp_path, {"last_processed_candidate": {"file": "2026-07-09.jsonl", "line": 10}})
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")
    old_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    longterm.save_dream_state(
        tmp_path,
        {
            "last_dream_at": old_at,
            "last_processed_candidate": {"file": "2026-07-09.jsonl", "line": 10},
            "last_status": "ok",
        },
    )

    assert dreamlib.should_run_dream(tmp_path) == (True, "time_and_candidate_interval")


def test_dream_prompt_contains_candidate_batch():
    batch = "Source: candidates/2026-07-09.jsonl\n\n1. line: 12\n   type: unspecified\n   memory: 以后先说结论"
    prompt = dreamlib.dream_prompt(batch)

    assert "Consolidate the candidate memories below" in prompt
    assert "Candidate memories to process:" in prompt
    assert "Source: candidates/2026-07-09.jsonl" in prompt
    assert "type: unspecified" in prompt


def test_run_dream_once_updates_cursor_without_counting_dream_session(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    store.save({"id": "session_001", "history": [], "memory": {}, "todos": []})
    agent = CodeMate(
        model_client=FakeModelClient(["done"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    log_path = longterm.candidate_log_path(tmp_path, date="2026-07-09")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    first = {
        "created_at": "2026-07-09T10:00:00+08:00",
        "type": "feedback_workflow",
        "memory": "first",
        "evidence": "test",
        "confidence": "high",
    }
    second = {**first, "memory": "second"}
    log_path.write_text(
        json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(second, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = agent.run_dream_once(reason="manual", foreground=False)
    state = longterm.load_dream_state(tmp_path)

    assert result == "dream completed: processed through 2026-07-09.jsonl line 2"
    assert state["last_processed_candidate"] == {"file": "2026-07-09.jsonl", "line": 2}
    assert store.count() == 2
    assert any(path.name.startswith("dream-") for path in store.root.iterdir() if path.is_dir())


def test_run_dream_once_returns_error_message_without_raising(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    agent = CodeMate(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    log_path = longterm.candidate_log_path(tmp_path, date="2026-07-09")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = {
        "created_at": "2026-07-09T10:00:00+08:00",
        "type": "feedback_workflow",
        "memory": "should stay pending",
        "evidence": "test",
        "confidence": "high",
    }
    log_path.write_text(json.dumps(candidate, ensure_ascii=False) + "\n", encoding="utf-8")

    result = agent.run_dream_once(reason="manual", foreground=True)
    state = longterm.load_dream_state(tmp_path)

    assert result.startswith("dream failed: ")
    assert "fake model ran out of outputs" in result
    assert state["last_status"] == "error"
    assert state.get("last_processed_candidate", {}) == {}
