from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.context_manager import ContextManager


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


def add_durable_notes(agent, notes):
    promotions = [("project-conventions", note) for note in notes]
    agent.memory.promote_durable(promotions)
    agent.session["memory"] = agent.memory.to_dict()


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    add_durable_notes(agent, ["deploy key is red"])
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are codemate") < prompt.index("Working memory:")
    assert prompt.index("Working memory:") < prompt.index("Relevant memory:")
    assert prompt.index("Relevant memory:") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "memory", "relevant_memory", "history", "current_request"]


def test_context_manager_reduces_relevant_memory_before_history_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    add_durable_notes(
        agent,
        [
            "keep durable note one " + ("C" * 220),
            "keep durable note two " + ("D" * 220),
            "keep durable note three " + ("E" * 220),
        ],
    )
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=700,
        section_budgets={
            "prefix": 120,
            "memory": 120,
            "relevant_memory": 120,
            "history": 400,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "memory", "relevant_memory", "history"):
        assert metadata["sections"][section]["rendered_chars"] <= metadata["sections"][section]["budget_chars"]

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[0] == "relevant_memory"
    assert reduction_sections
    assert "RECENT-CONTEXT" in prompt
    assert "OLD-CONTEXT" not in prompt
    assert "keep this request verbatim" in prompt


def test_context_manager_renders_top_three_durable_notes_per_note_under_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    add_durable_notes(
        agent,
        [
            "alpha durable recall note " + ("A" * 120),
            "beta durable recall note " + ("B" * 120),
            "gamma durable recall note " + ("C" * 120),
            "older unmatched note",
            "Unrelated note",
        ],
    )

    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 60,
            "memory": 60,
            "relevant_memory": 80,
            "history": 60,
        },
    ).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert metadata["relevant_memory"]["limit"] == 3
    assert all("durable recall note" in note for note in metadata["relevant_memory"]["selected_notes"])
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert metadata["relevant_memory"]["rendered_count"] == 3
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha durab" in relevant_section
    assert "beta durable" in relevant_section
    assert "gamma durab" in relevant_section
    assert "older unmatched note" not in relevant_section


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.memory.retrieval_view = lambda query, limit=3: "Relevant memory:\n" + "\n".join(f"- {i} " + ("C" * 220) for i in range(5))
    agent.history_text = lambda: "Transcript:\n" + "\n".join(f"[user] {i} " + ("D" * 220) for i in range(5))

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "memory": 80,
            "relevant_memory": 80,
            "history": 80,
        },
    ).build(request)

    assert prompt.split("Current user request:\n", 1)[1] == request
    assert metadata["current_request"]["text"] == request
    assert metadata["current_request"]["rendered_chars"] == len(request)


def test_build_messages_does_not_append_current_user_after_tool_result(tmp_path):
    agent = build_agent(tmp_path, [])
    request = "append hello to README"
    agent.record({"role": "user", "content": request, "created_at": "2026-04-07T09:00:00+00:00"})
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_write", "name": "write_file", "args": {"path": "README.md", "content": "hello"}}],
            "created_at": "2026-04-07T09:00:01+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": "call_write",
            "name": "write_file",
            "content": "wrote README.md (5 chars)",
            "created_at": "2026-04-07T09:00:02+00:00",
        }
    )

    message_build = ContextManager(agent).build_messages(request)

    assert message_build.messages[-1]["role"] == "tool"
    assert message_build.messages[-1]["content"] == "wrote README.md (5 chars)"
    assert [item.get("content") for item in message_build.messages if item.get("role") == "user"].count(request) == 1


def test_context_manager_collapses_older_duplicate_reads_to_latest_structured_group(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.memory.set_file_summary("sample.txt", "alpha | beta")
    agent.memory.remember_file("sample.txt")

    for index, created_at in enumerate(("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00")):
        call_id = f"call_read_{index}"
        args = {"path": "sample.txt", "start": 1, "end": 2}
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "read_file", "args": args}],
                "created_at": created_at,
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "read_file",
                "content": "# sample.txt\nalpha\nbeta\n",
                "created_at": created_at,
            }
        )

    for minute in range(2, 8):
        role = "user" if minute % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check the file")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 1
    assert "# sample.txt" in transcript
    assert metadata["history"]["collapsed_duplicate_tool_results"] == 1


def test_context_manager_keeps_read_file_calls_with_different_ranges(tmp_path):
    (tmp_path / "sample.txt").write_text("\n".join(str(i) for i in range(20)), encoding="utf-8")
    agent = build_agent(tmp_path, [])

    for index, args in enumerate(({"path": "sample.txt", "start": 1, "end": 5}, {"path": "sample.txt", "start": 6, "end": 10})):
        call_id = f"call_read_range_{index}"
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "read_file", "args": args}],
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "read_file",
                "content": f"# sample.txt\nRANGE-{index}",
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check ranges")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 2
    assert "RANGE-0" in transcript
    assert "RANGE-1" in transcript
    assert metadata["history"]["collapsed_duplicate_tool_results"] == 0


def test_context_manager_collapses_duplicate_grep_calls(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"pattern": "run_shell", "path": "codemate", "mode": "content", "before": 1, "after": 1, "context": 0}

    for index in range(2):
        call_id = f"call_grep_{index}"
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "grep", "args": args}],
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "grep",
                "content": f"grep-result-{index}",
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("find shell")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:grep]") == 1
    assert "grep-result-1" in transcript
    assert "grep-result-0" not in transcript
    assert metadata["history"]["collapsed_duplicate_tool_results"] == 1


def test_context_manager_microcompacts_old_read_only_tool_results(tmp_path):
    agent = build_agent(tmp_path, [])
    shell_call_id = "call_shell_old"
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": shell_call_id, "name": "run_shell", "args": {"command": "ls codemate"}}],
            "created_at": "2026-04-07T08:59:00+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": shell_call_id,
            "name": "run_shell",
            "content": "exit_code: 0\nstdout:\nOLD-SHELL-OBSERVATION\nstderr:\n(empty)",
            "created_at": "2026-04-07T08:59:00+00:00",
        }
    )

    for index in range(21):
        call_id = f"call_read_micro_{index}"
        args = {"path": "sample.txt", "start": index + 1, "end": index + 1}
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "read_file", "args": args}],
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "read_file",
                "content": f"# sample.txt\nOBSERVATION-{index}",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("summarize observations")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "OBSERVATION-20" in transcript
    assert "OBSERVATION-0" not in transcript
    assert "OLD-SHELL-OBSERVATION" not in transcript
    assert transcript.count("Old tool result content cleared.") == 2
    assert metadata["history"]["cleared_old_tool_results"] == 2


def test_context_manager_clips_tool_output_without_breaking_tool_structure(tmp_path):
    agent = build_agent(tmp_path, [])
    call_id = "call_shell"
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": "run_shell", "args": {"command": "pytest -q"}}],
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "run_shell",
            "content": "FAIL test_one\n" + ("very long output\n" * 80),
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    prompt, metadata = ContextManager(
        agent,
        total_budget=900,
        section_budgets={"prefix": 120, "memory": 120, "relevant_memory": 120, "history": 360},
    ).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "[assistant:tool_calls]" in transcript
    assert "[tool:run_shell]" in transcript
    assert "very long output" in transcript
    assert transcript.count("very long output") < 80


def test_context_manager_relevant_memory_can_mix_durable_notes(tmp_path):
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
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_durable_count"] == 1
    assert metadata["relevant_memory"]["selected_sources"] == ["project-conventions"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["durable"]
