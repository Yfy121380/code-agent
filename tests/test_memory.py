from codemate.memory import LayeredMemory


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


def test_process_notes_merge_duplicate_abnormal_tool_calls():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "rejected",
        "tool_error_code": "invalid_arguments",
        "affected_paths": [],
    }

    memory.record_process_note("patch_file", {"path": "README.md"}, metadata, "error: invalid arguments", current_turn=1)
    memory.record_process_note("patch_file", {"path": "README.md"}, metadata, "error: invalid arguments again", current_turn=2)

    notes = memory.to_dict()["process_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "invalid_arguments"
    assert notes[0]["tool"] == "patch_file"
    assert notes[0]["affected_paths"] == ["README.md"]
    assert notes[0]["count"] == 2
    assert notes[0]["updated_turn"] == 2
    assert notes[0]["message"] == "error: invalid arguments again"


def test_process_notes_expire_by_turn_ttl():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "rejected",
        "tool_error_code": "repeated_identical_call",
        "affected_paths": [],
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
        {"tool_status": "rejected", "tool_error_code": "invalid_arguments", "affected_paths": []},
        "error: invalid arguments",
        current_turn=1,
    )
    memory.record_process_note(
        "read_file",
        {"path": "README.md"},
        {"tool_status": "rejected", "tool_error_code": "repeated_identical_call", "affected_paths": []},
        "error: repeated",
        current_turn=1,
    )

    memory.resolve_process_notes_after_success("list_files", {}, current_turn=1)
    notes = memory.to_dict()["process_notes"]

    assert [note["kind"] for note in notes] == ["invalid_arguments"]

    memory.resolve_process_notes_after_success("patch_file", {"path": "README.md"}, current_turn=1)

    assert memory.to_dict()["process_notes"] == []


def test_partial_success_clears_after_all_affected_files_are_read():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "partial_success",
        "tool_error_code": "tool_partial_success",
        "affected_paths": ["a.txt", "b.txt"],
    }

    memory.record_process_note("run_shell", {"command": "make edit"}, metadata, "error: command failed", current_turn=1)
    memory.resolve_process_notes_after_success("read_file", {"path": "a.txt"}, current_turn=1)

    notes = memory.to_dict()["process_notes"]
    assert len(notes) == 1
    assert notes[0]["inspected_paths"] == ["a.txt"]

    memory.resolve_process_notes_after_success("read_file", {"path": "b.txt"}, current_turn=1)

    assert memory.to_dict()["process_notes"] == []


def test_render_memory_text_includes_process_notes_near_file_summaries():
    memory = LayeredMemory()
    metadata = {
        "tool_status": "error",
        "tool_error_code": "tool_failed",
        "affected_paths": [],
    }

    memory.record_process_note("run_shell", {"command": "pytest"}, metadata, "exit_code: 1", current_turn=1)

    text = memory.render_memory_text()

    assert "- file_summaries:" in text
    assert "- process_notes:" in text
    assert "run_shell error on workspace, count=1" in text
    assert "exit_code: 1" in text


def test_durable_memory_index_and_topic_notes_are_loaded_and_retrieved(tmp_path):
    memory_root = tmp_path / ".codemate" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n"
        "- Preserve local agent state under .codemate/.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    assert "project-conventions" in memory.render_memory_text()
    lines = [line for line in memory.retrieval_view("constrained tools", limit=4).splitlines() if line.startswith("- ")]
    assert any("Use constrained tools instead of guessing." in line for line in lines)
