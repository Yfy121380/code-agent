"""上下文组装测试。

覆盖模块：context.manager、context.history、长期记忆注入。
重点边界：section 顺序、history summary 位置、skill/working memory 渲染、旧读类工具结果清理、tool group 保持完整。
"""

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
    assert metadata["section_order"] == ["prefix", "skills", "memory", "relevant_memory", "history_summary", "history", "current_request"]
    assert metadata["sections"]["history_summary"]["rendered_chars"] == 0


def test_context_manager_injects_history_summary_before_recent_history(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["history_summary"] = "## Working Directory\n- `/tmp/project`."
    agent.record({"role": "user", "content": "recent request", "created_at": "2026-04-07T10:00:00+00:00"})

    prompt, metadata = ContextManager(agent).build("continue")
    message_build = ContextManager(agent).build_messages("continue")

    assert prompt.index("This session is being continued") < prompt.index("Transcript:")
    assert prompt.index("Summary:\n## Working Directory") < prompt.index("Transcript:")
    assert metadata["history_summary"]["has_summary"] is True
    assert message_build.messages[1]["content"].startswith("This session is being continued")
    assert message_build.messages[2]["content"] == "recent request"


def test_context_manager_no_longer_reduces_sections_by_legacy_budget(tmp_path):
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

    manager = ContextManager(agent)

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "skills", "memory", "relevant_memory", "history"):
        assert metadata["sections"][section]["budget_chars"] is None

    assert "RECENT-CONTEXT" in prompt
    assert "OLD-CONTEXT" in prompt
    assert "keep this request verbatim" in prompt


def test_context_manager_renders_up_to_twenty_durable_notes(tmp_path):
    agent = build_agent(tmp_path, [])
    add_durable_notes(
        agent,
        [
            "alpha durable recall note " + ("A" * 120),
            "beta durable recall note " + ("B" * 120),
            "gamma durable recall note " + ("C" * 120),
            "delta durable recall note",
            "epsilon durable recall note",
        ],
    )

    prompt, metadata = ContextManager(agent).build("recall")

    assert metadata["relevant_memory"]["selected_count"] == 5
    assert metadata["relevant_memory"]["limit"] == 20
    assert all("durable recall note" in note for note in metadata["relevant_memory"]["selected_notes"])
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 5
    assert metadata["relevant_memory"]["rendered_count"] == 5
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]
    assert "user_profile:" in relevant_section
    assert "feedback_workflow:" in relevant_section
    assert "project_context:" in relevant_section
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ") and line != "- none"]) == 5
    assert "alpha durab" in relevant_section
    assert "beta durable" in relevant_section
    assert "gamma durab" in relevant_section
    assert "delta durable recall note" in relevant_section
    assert "epsilon durable recall note" in relevant_section


def test_context_manager_renders_available_skills(tmp_path):
    write_skill(tmp_path, "backend", "Backend workflow " + ("A" * 300))
    write_skill(tmp_path, "paper", "Paper summary workflow " + ("B" * 120))
    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("inspect skills")

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


def test_context_manager_preserves_current_request_in_text_prompt(tmp_path):
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
    prompt, metadata = ContextManager(agent).build(request)

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


def test_context_manager_keeps_read_all_separate_from_ranged_read(tmp_path):
    (tmp_path / "sample.txt").write_text("\n".join(str(i) for i in range(20)), encoding="utf-8")
    agent = build_agent(tmp_path, [])

    for index, args in enumerate(({"path": "sample.txt", "read_all": True}, {"path": "sample.txt", "start": 1, "end": 20})):
        call_id = f"call_read_all_{index}"
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "read_file", "args": args}],
                "created_at": f"2026-04-07T09:1{index}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "read_file",
                "content": f"# sample.txt\nREAD-{index}",
                "created_at": f"2026-04-07T09:1{index}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check reads")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 2
    assert "READ-0" in transcript
    assert "READ-1" in transcript
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


def test_context_manager_collapses_duplicate_web_search_calls(tmp_path):
    agent = build_agent(tmp_path, [])
    args = {"query": "Python 3.13 release notes", "max_results": 5}

    for index in range(2):
        call_id = f"call_web_search_{index}"
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "web_search", "args": args}],
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "web_search",
                "content": f"web-result-{index}",
                "created_at": f"2026-04-07T09:0{index}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check web results")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:web_search]") == 1
    assert "web-result-1" in transcript
    assert "web-result-0" not in transcript
    assert metadata["history"]["collapsed_duplicate_tool_results"] == 1


def test_context_manager_microcompacts_old_read_only_tool_results(tmp_path):
    agent = build_agent(tmp_path, [])
    web_call_id = "call_web_old"
    agent.record(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": web_call_id, "name": "web_search", "args": {"query": "old context search"}}],
            "created_at": "2026-04-07T08:59:00+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": web_call_id,
            "name": "web_search",
            "content": "OLD-WEB-OBSERVATION\nhttps://example.com/old",
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
    assert "OLD-WEB-OBSERVATION" not in transcript
    assert transcript.count("Old tool result content cleared.") == 2
    assert metadata["history"]["cleared_old_tool_results"] == 2


def test_context_manager_removes_image_blocks_when_old_read_result_is_cleared(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(25):
        call_id = f"call_image_{index}"
        args = {"path": f"shot_{index}.png"}
        agent.record(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": "read_file", "args": args}],
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": "read_file",
                "content": f"Image file: shot_{index}.png",
                "content_blocks": [{"type": "image", "path": f"/tmp/shot_{index}.png", "media_type": "image/png"}],
                "created_at": "2026-04-09T00:00:00+00:00",
            }
        )

    _system, messages, metadata = agent._build_messages_and_metadata("")
    cleared = [
        message
        for message in messages
        if message.get("role") == "tool" and message.get("content") == "Old tool result content cleared."
    ]

    assert metadata["history"]["cleared_old_tool_results"] > 0
    assert cleared
    assert all("content_blocks" not in message for message in cleared)


def test_context_manager_keeps_tool_output_structure_without_budget_clipping(tmp_path):
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

    prompt, metadata = ContextManager(agent).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert "[assistant:tool_calls]" in transcript
    assert "[tool:run_shell]" in transcript
    assert "very long output" in transcript
    assert transcript.count("very long output") == 80


def test_context_manager_preserves_assistant_tool_call_kind(tmp_path):
    agent = build_agent(tmp_path, [])
    call_id = "toolu_1"
    agent.record(
        {
            "role": "assistant",
            "kind": "tool_calls",
            "content": "",
            "tool_calls": [{"id": call_id, "name": "read_file", "args": {"path": "README.md", "start": 1, "end": 1}}],
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": "# README.md\n   1: demo",
            "created_at": "2026-04-07T09:00:01+00:00",
        }
    )

    message_build = ContextManager(agent).build_messages("continue")
    assistant_messages = [message for message in message_build.messages if message.get("role") == "assistant"]

    assert assistant_messages
    assert assistant_messages[0]["kind"] == "tool_calls"
    assert assistant_messages[0]["tool_calls"][0]["id"] == call_id


def test_context_manager_renders_selected_long_term_memory(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.relevant_long_term_memory = [
        {
            "source": "project_context",
            "created_at": "2026-07-25T10:12:00+08:00",
            "text": "Use constrained tools instead of guessing.",
            "reason": "debug only",
            "kind": "long_term",
        }
    ]
    agent.long_term_memory_status = "ok"

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    relevant_section = prompt.split("Relevant memory:\n", 1)[1].split("\n\nTranscript:", 1)[0]

    assert "- [2026-07-25T10:12:00+08:00] Use constrained tools instead of guessing." in relevant_section
    assert "debug only" not in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_created_at"] == ["2026-07-25T10:12:00+08:00"]
    assert metadata["relevant_memory"]["selected_reasons"] == ["debug only"]
    assert metadata["relevant_memory"]["selected_sources"] == ["project_context"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["long_term"]
    assert metadata["relevant_memory"]["retrieval_status"] == "ok"

def test_ask_retrieves_long_term_memory_once_and_injects_selected_note(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "完成。",
        ],
    )
    memory_path = agent.paths.memory_root / "feedback_workflow.md"
    memory_path.write_text("# Feedback Workflow\n\n- [2026-07-25T10:12:00+08:00] 回答时使用中文。\n", encoding="utf-8")
    agent.record({"role": "user", "content": "上一轮讨论了文档。", "created_at": "2026-07-25T10:00:00+08:00"})

    result = agent.ask("介绍一下项目")

    assert result == "完成。"
    assert agent.long_term_memory_status == "direct_small"
    assert len(agent.model_client.prompts) == 1
    assert "[2026-07-25T10:12:00+08:00] 回答时使用中文。" in agent.model_client.prompts[0]
    assert "direct small-memory load" not in agent.model_client.prompts[0]
    assert agent.model_client.prompts[0].count("Relevant memory:") == 1
    assert "user_profile:" in agent.model_client.prompts[0]
    assert "feedback_workflow:" in agent.model_client.prompts[0]
    assert "project_context:" in agent.model_client.prompts[0]
    assert "Runtime context:" in agent.model_client.prompts[0]
    assert "current_local_datetime:" in agent.model_client.prompts[0]
    assert "memory_root:" in agent.model_client.prompts[0]
    assert agent.model_client.structured_outputs[0] is None


def test_history_recent_messages_for_retrieval_keeps_tool_group_and_truncates_result(tmp_path):
    agent = build_agent(tmp_path, [])
    call_id = "call_read"
    agent.record(
        {
            "role": "assistant",
            "kind": "tool_calls",
            "content": "I will read the file.",
            "tool_calls": [{"id": call_id, "name": "read_file", "args": {"path": "README.md"}}],
            "created_at": "2026-07-25T10:00:00+08:00",
        }
    )
    agent.record(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": "X" * 500,
            "created_at": "2026-07-25T10:00:01+08:00",
        }
    )

    messages = ContextManager(agent).history_renderer.recent_messages_for_retrieval(max_messages=1, tool_result_chars=300)

    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["id"] == call_id
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == call_id
    assert len(messages[1]["content"]) < 340
    assert "truncated" in messages[1]["content"]
