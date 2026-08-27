"""Progressive Core/Ordinary memory backend tests."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from codemate import (
    FakeModelClient,
    MiniAgent,
    ModelResponse,
    SessionStore,
    WorkspaceContext,
)
from codemate.config import codemate_paths
from codemate.memory.progressive.store import CoreMemoryStore, ProjectMemoryStore
from codemate.storage.atomic import atomic_write_text


def build_agent(tmp_path, outputs=None, *, backend="progressive"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".sessions"),
        approval_policy="auto",
        memory_backend=backend,
        feature_flags={"session_title": False},
    )


def add_completed_conversation(agent, index, text="request"):
    conversation_id = f"turn_{index}"
    agent._current_conversation_id = conversation_id
    agent.record({"role": "user", "content": f"{text} {index}"})
    agent.record({"role": "assistant", "kind": "final", "content": "done"})
    return conversation_id


def test_progressive_storage_is_isolated_from_legacy_memory(tmp_path):
    agent = build_agent(tmp_path)
    paths = codemate_paths(tmp_path)

    assert agent.memory_backend_name == "progressive"
    assert paths.progressive_memory_root.is_dir()
    assert paths.progressive_core_memory.is_file()
    assert not (paths.memory_root / "user_profile.md").exists()
    assert not (paths.memory_root / "candidates").exists()


def test_resumed_session_keeps_legacy_backend_when_no_backend_was_recorded(tmp_path):
    agent = build_agent(tmp_path, backend="legacy")
    saved = agent.session_store.load(agent.session["id"])
    saved.pop("memory_backend", None)
    agent.session_store.save(saved)

    resumed = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session=saved,
        approval_policy="auto",
        memory_backend="progressive",
        feature_flags={"session_title": False},
    )

    assert resumed.memory_backend_name == "legacy"


def test_long_term_feature_flag_disables_backend_without_rewriting_session_choice(
    tmp_path,
):
    agent = build_agent(tmp_path)
    saved = agent.session_store.load(agent.session["id"])

    disabled = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session=saved,
        approval_policy="auto",
        feature_flags={"long_term_memory": False, "session_title": False},
    )

    assert disabled.memory_backend_name == "disabled"
    assert disabled.session["memory_backend"] == "progressive"
    assert not any(name.startswith("memory_") for name in disabled.tools)


def test_core_memory_requires_exact_current_user_statement(tmp_path):
    agent = build_agent(tmp_path)
    agent._current_source_user_request = "以后回答时先说结论。"

    result = agent.run_tool(
        "core_memory_update",
        {
            "key": "preference.response_order",
            "value": "先说结论，再解释原因。",
            "reason": "用户明确说明跨项目回复偏好",
            "explicit_user_statement": "先说结论",
        },
    )

    assert '"status": "updated"' in result
    rendered = agent.memory_backend.core_store.render()
    assert "# Core Memory" in rendered
    assert "## preference 记忆" in rendered
    assert "preference.response_order" in rendered
    rejected = agent.run_tool(
        "core_memory_update",
        {
            "key": "preference.response_length",
            "value": "简洁",
            "reason": "推测",
            "explicit_user_statement": "用户没有说过这句话",
        },
    )
    assert "exact substring" in rejected


def test_core_memory_capacity_returns_actionable_result_without_writing(
    tmp_path, monkeypatch
):
    store = CoreMemoryStore(tmp_path / "core.json")
    store.upsert("identity.role", "student", "explicit user fact")
    monkeypatch.setattr(
        "codemate.memory.progressive.store.MAX_CORE_RENDERED_CHARS",
        len(store.render()) + 20,
    )

    result = store.upsert(
        "preference.explanation",
        "Provide a detailed explanation while preserving the conclusion.",
        "explicit user preference",
    )

    assert result["status"] == "capacity_exceeded"
    assert result["excess_chars"] > 0
    assert result["suggested_max_value_chars"] > 0
    assert "preference.explanation" not in store.load()["entries"]


def test_progressive_memory_uses_runtime_memory_section_and_refreshes_next_turn(
    tmp_path,
):
    agent = build_agent(tmp_path)
    agent.memory_backend.core_store.upsert(
        "identity.role", "student", "explicit user fact"
    )
    agent.memory_backend.project_store.create(
        "Streaming protocol", "durable project details", "test"
    )
    agent.memory_backend.prepare_request("inspect", None)

    message_build = agent.context_manager.build_messages("inspect")

    assert "# Core Memory" not in message_build.system
    assert "# Core Memory" in message_build.messages[0]["content"]
    assert "# Ordinary Memory" in message_build.messages[0]["content"]
    frozen_core = agent.memory_backend.context().core

    agent._current_source_user_request = "以后回答时先说结论。"
    result = agent.run_tool(
        "core_memory_update",
        {
            "key": "preference.response_order",
            "value": "先说结论，再解释原因。",
            "reason": "用户明确说明跨项目回复偏好",
            "explicit_user_statement": "先说结论",
        },
    )

    assert '"status": "updated"' in result
    assert agent.memory_backend.context().core == frozen_core
    agent.memory_backend.prepare_request("next turn", None)
    assert "preference.response_order" in agent.memory_backend.context().core


def test_core_memory_accepts_explicit_response_annotation_comment(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call(
                "core_memory_update",
                {
                    "key": "preference.response_order",
                    "value": "先说结论，再解释原因。",
                    "reason": "用户在批注中明确说明跨项目偏好",
                    "explicit_user_statement": "以后回答先说结论",
                },
            ),
            ModelResponse.final("已记录。"),
        ],
    )

    agent.ask(
        "Rendered annotation request",
        source_user_request="",
        response_annotations=[{"comment": "以后回答先说结论"}],
    )

    assert "preference.response_order" in agent.memory_backend.core_store.render()


def test_project_store_create_read_update_and_index(tmp_path):
    store = ProjectMemoryStore(tmp_path / "memory")
    created = store.create(
        "Streaming completion", "Initial behavior.", "new durable topic"
    )
    read = store.read(created.id)
    updated = store.update(
        created.id, created.title, "Complete behavior.", "confirmed fix", read.revision
    )

    assert updated.revision == 2
    assert store.read(created.id, track_access=False).content == "Complete behavior."
    assert store.index()["items"] == [
        {"id": created.id, "title": "Streaming completion"}
    ]
    assert (store.records_dir / f"{created.id}.md").is_file()
    assert store.index_path.is_file()


def test_visibility_score_has_thirty_day_half_life_and_log_access_gain(tmp_path):
    del tmp_path
    current_time = datetime.now().astimezone()
    recent = {
        "updated_at": current_time.isoformat(),
        "last_accessed_at": current_time.isoformat(),
        "access_count": 0,
    }
    old = {
        **recent,
        "updated_at": (current_time - timedelta(days=30)).isoformat(),
        "last_accessed_at": (current_time - timedelta(days=30)).isoformat(),
    }
    frequently_used = {**old, "access_count": 10}

    recent_score = ProjectMemoryStore.visibility_score(recent, current_time)
    old_score = ProjectMemoryStore.visibility_score(old, current_time)

    assert recent_score == 1.0
    assert old_score == 0.5
    assert (
        ProjectMemoryStore.visibility_score(frequently_used, current_time) > old_score
    )


def test_prompt_index_limits_to_twenty_five_and_read_promotes_old_memory(tmp_path):
    store = ProjectMemoryStore(tmp_path / "memory")
    records = [
        store.create(f"Topic {index:02d}", f"content {index}", "test")
        for index in range(27)
    ]
    first = records[0]
    old_time = (datetime.now().astimezone() - timedelta(days=90)).isoformat()
    old_record = store.read(first.id, track_access=False)

    atomic_write_text(
        store._path(first.id),
        replace(old_record, last_accessed_at=old_time, updated_at=old_time).render(),
    )
    store.rebuild_index()

    prompt, before = store.prompt_index(25)
    assert len(before["items"]) == 25
    assert first.id not in {item["id"] for item in before["items"]}
    assert prompt.count("\n- M") == 25

    store.read(first.id)
    _, after = store.prompt_index(25)
    assert first.id in {item["id"] for item in after["items"]}


def test_memory_index_supports_search_pagination_and_repairs_corruption(tmp_path):
    store = ProjectMemoryStore(tmp_path / "memory")
    store.create("Shell sandbox", "sandbox", "test")
    store.create("Shell timeout", "timeout", "test")
    store.create("Context compact", "compact", "test")

    first_page = store.index(query="shell", offset=0, limit=1)
    second_page = store.index(query="shell", offset=1, limit=1)
    assert first_page["total"] == 2
    assert len(first_page["items"]) == len(second_page["items"]) == 1
    assert first_page["items"] != second_page["items"]

    store.index_path.write_text("not json", encoding="utf-8")
    repaired = store.index()
    assert repaired["total"] == 3
    assert store.index_path.read_text(encoding="utf-8").startswith("{")


def test_create_never_reuses_id_of_a_corrupt_record(tmp_path):
    store = ProjectMemoryStore(tmp_path / "memory")
    corrupt_path = store.records_dir / "M001.md"
    corrupt_path.write_text("corrupt memory", encoding="utf-8")

    created = store.create("Valid topic", "durable content", "test")

    assert created.id == "M002"
    assert corrupt_path.read_text(encoding="utf-8") == "corrupt memory"


def test_consolidation_reads_do_not_increase_access_count(tmp_path):
    agent = build_agent(tmp_path)
    created = agent.memory_backend.project_store.create("Memory access", "body", "test")
    agent.runtime_mode = "memory_consolidation"

    agent.memory_backend.read(created.id)

    assert (
        agent.memory_backend.project_store.read(
            created.id, track_access=False
        ).access_count
        == 0
    )


def test_consolidation_update_requires_reading_the_current_record(tmp_path):
    agent = build_agent(tmp_path)
    created = agent.memory_backend.project_store.create(
        "Memory update", "old body", "test"
    )
    agent.runtime_mode = "memory_consolidation"

    with pytest.raises(ValueError, match="requires memory_read"):
        agent.memory_backend.update(
            created.id,
            created.title,
            "new body",
            "new durable information",
            created.revision,
        )

    current = agent.memory_backend.read(created.id)
    updated = agent.memory_backend.update(
        current.id,
        current.title,
        "new body",
        "new durable information",
        current.revision,
    )

    assert updated.revision == 2
    assert updated.content == "new body"


def test_progressive_threshold_uses_twenty_complete_user_turns(tmp_path):
    agent = build_agent(tmp_path)
    for index in range(19):
        add_completed_conversation(agent, index)
    assert agent.memory_backend.pending()["due"] is False

    add_completed_conversation(agent, 19)
    pending = agent.memory_backend.pending()
    assert pending["due"] is True
    assert pending["user_turns"] == 20


def test_consolidation_child_creates_memory_and_advances_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            ModelResponse.tool_call(
                "memory_create",
                {
                    "title": "Streaming completion",
                    "content": "A completion event is required before accepting a stream.",
                    "reason": "confirmed project behavior",
                },
            ),
            ModelResponse.final("Consolidated one durable topic."),
        ],
    )
    conversation_id = add_completed_conversation(
        agent, 0, "Require explicit stream completion"
    )

    result = agent.memory_backend.maybe_consolidate(
        reason="test", background=False, force=True
    )

    assert result["status"] == "ok"
    assert agent.session["memory_consolidation_checkpoint"] == conversation_id
    records = agent.memory_backend.project_store.list_records()
    assert [(item.id, item.title) for item in records] == [
        ("M001", "Streaming completion")
    ]
    assert set(
        agent.model_client.tool_specs[0][index]["name"]
        for index in range(len(agent.model_client.tool_specs[0]))
    ) == {
        "memory_index",
        "memory_read",
        "memory_create",
        "memory_update",
    }


def test_failed_consolidation_does_not_advance_checkpoint(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse.final("No tool changes.")])
    add_completed_conversation(agent, 0)

    # A normal final is a successful no-op consolidation, so use a model error
    # to cover the retryable failure boundary.
    agent.model_client.outputs.clear()
    result = agent.memory_backend.maybe_consolidate(
        reason="test", background=False, force=True
    )

    assert result["status"] == "error"
    assert agent.session["memory_consolidation_checkpoint"] == ""


def test_plan_mode_exposes_only_progressive_read_tools(tmp_path):
    agent = build_agent(tmp_path)
    agent.enter_plan_mode()
    names = {item["name"] for item in agent.model_tools()}

    assert {"memory_index", "memory_read"} <= names
    assert "core_memory_update" not in names
    assert "core_memory_remove" not in names
    assert "memory_create" not in names
    assert "memory_update" not in names
