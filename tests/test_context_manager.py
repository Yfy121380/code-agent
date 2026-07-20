from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.context import ContextManager


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
    agent.relevant_long_term_memory = [
        {"source": "project_context", "text": note, "kind": "long_term"}
        for note in notes
    ]
    agent.long_term_memory_status = "ok"


def write_skill(tmp_path, name, description, body="Follow this skill."):
    skill_dir = tmp_path / ".codemate" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    add_durable_notes(agent, ["deploy key is red"])
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are codemate") < prompt.index("Working memory:")
    assert prompt.index("You are codemate") < prompt.index("Available skills:")
    assert prompt.index("Available skills:") < prompt.index("Working memory:")
    assert prompt.index("Working memory:") < prompt.index("Relevant memory:")
    assert prompt.index("Relevant memory:") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert metadata["section_order"] == ["prefix", "skills", "memory", "relevant_memory", "history", "current_request"]


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
            "skills": 80,
            "memory": 120,
            "relevant_memory": 120,
            "history": 400,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "skills", "memory", "relevant_memory", "history"):
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
        total_budget=700,
        section_budgets={
            "prefix": 80,
            "skills": 80,
            "memory": 160,
            "relevant_memory": 360,
            "history": 80,
        },
    ).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert metadata["relevant_memory"]["limit"] == 3
    assert all("durable recall note" in note for note in metadata["relevant_memory"]["selected_notes"])
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert metadata["relevant_memory"]["rendered_count"] == 3
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert "user_profile:" in relevant_section
    assert "feedback_workflow:" in relevant_section
    assert "project_context:" in relevant_section
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ") and line != "- none"]) == 3
    assert "alpha durab" in relevant_section
    assert "beta durable" in relevant_section
    assert "gamma durab" in relevant_section
    assert "older unmatched note" not in relevant_section


def test_context_manager_renders_and_reduces_available_skills_by_entry(tmp_path):
    write_skill(tmp_path, "backend", "Backend workflow " + ("A" * 300))
    write_skill(tmp_path, "paper", "Paper summary workflow " + ("B" * 120))
    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(
        agent,
        total_budget=800,
        section_budgets={
            "prefix": 120,
            "skills": 90,
            "memory": 120,
            "relevant_memory": 120,
            "history": 120,
        },
    ).build("inspect skills")

    skills_section = prompt.split("Available skills:\n", 1)[1].split("\n\nWorking memory:", 1)[0]
    assert "- backend:" in skills_section or "- backend" in skills_section
    assert "- paper:" in skills_section or "- paper" in skills_section
    assert metadata["skills"]["selected_count"] == 2
    assert metadata["skills"]["rendered_count"] == 2
    assert len(agent.available_skills()[0]["description"]) <= 250


def test_available_skills_require_frontmatter_name_matching_directory(tmp_path):
    skill_dir = tmp_path / ".codemate" / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: Should not be listed.\n---\n",
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [])

    assert agent.available_skills() == []
    assert agent.available_skills_text() == "Available skills:\n- none"


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.memory.render_memory_text = lambda: "MEMORY " + ("B" * 600)
    agent.relevant_long_term_memory = [
        {"source": "project_context", "text": f"{i} " + ("C" * 220), "kind": "long_term"}
        for i in range(5)
    ]
    agent.long_term_memory_status = "ok"
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


def test_context_manager_renders_current_todos_in_working_memory(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["todos"] = [
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
        {"phase": "Run tests", "status": "pending", "tasks": []},
    ]

    prompt, _metadata = ContextManager(agent).build("continue")
    message_build = ContextManager(agent).build_messages("continue")

    assert "- current_todos: follow these phases and tasks until completed" in prompt
    assert "  1. [completed] Inspect current implementation" in prompt
    assert "     - [completed] Read tool files" in prompt
    assert "  2. [in_progress] Add todo_write tool" in prompt
    assert "     - [in_progress] Update handler" in prompt
    assert "  3. [pending] Run tests" in prompt
    assert "current_todos: follow these phases and tasks until completed" in message_build.messages[0]["content"]


def test_context_manager_renders_empty_current_todos(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt, _metadata = ContextManager(agent).build("continue")

    assert "- current_todos: -" in prompt


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


def test_context_manager_renders_selected_long_term_memory(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.relevant_long_term_memory = [
        {
            "source": "project_context",
            "text": "Use constrained tools instead of guessing.",
            "kind": "long_term",
        }
    ]
    agent.long_term_memory_status = "ok"

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_sources"] == ["project_context"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["long_term"]
    assert metadata["relevant_memory"]["retrieval_status"] == "ok"

def test_ask_retrieves_long_term_memory_once_and_injects_selected_note(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '{"selected": [{"source": "feedback_workflow", "text": "回答时使用中文。", "reason": "用户偏好"}]}',
            "完成。",
        ],
    )
    memory_path = agent.paths.memory_root / "feedback_workflow.md"
    memory_path.write_text("# Feedback Workflow\n\n- 回答时使用中文。\n", encoding="utf-8")

    result = agent.ask("介绍一下项目")

    assert result == "完成。"
    assert agent.long_term_memory_status == "ok"
    assert len(agent.model_client.prompts) == 2
    assert "回答时使用中文。" in agent.model_client.prompts[1]
    assert agent.model_client.prompts[1].count("Relevant memory:") == 1
    assert "user_profile:" in agent.model_client.prompts[1]
    assert "feedback_workflow:" in agent.model_client.prompts[1]
    assert "project_context:" in agent.model_client.prompts[1]
    assert "Runtime context:" in agent.model_client.prompts[1]
    assert "current_local_datetime:" in agent.model_client.prompts[1]
    assert "today_daily_log_path: " in agent.model_client.prompts[1]
    assert "/memory/daily_logs/" in agent.model_client.prompts[1]
    assert agent.model_client.structured_outputs[0]["name"] == "long_term_memory_retrieval"
    assert agent.model_client.structured_outputs[1] is None
