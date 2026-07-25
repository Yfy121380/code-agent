import json

from codemate import FakeModelClient, MiniAgent, ModelResponse, SessionStore, WorkspaceContext
from codemate import memory as memorylib
from codemate.runtime.compaction import HISTORY_SUMMARY_SECTIONS


SUMMARY = "\n\n".join(f"## {section}\n- None" for section in HISTORY_SUMMARY_SECTIONS)


def build_agent(tmp_path, outputs=None, **feature_flags):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    flags = {"memory_dream": False, "session_title": False}
    flags.update(feature_flags)
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags=flags,
    )


def candidate_json(memory="用户希望代码修改前先讨论方案，确认后再动手实现。"):
    return json.dumps(
        {
            "candidates": [
                {
                    "type": "feedback_workflow",
                    "memory": memory,
                    "evidence": "用户明确要求未来修改代码前先给方案。",
                    "confidence": "high",
                }
            ]
        },
        ensure_ascii=False,
    )


def add_completed_conversation(agent, user_text, final_text="done"):
    conversation_id = f"turn_test_{len(agent.session['history'])}"
    agent._current_conversation_id = conversation_id
    agent.record({"role": "user", "content": user_text, "created_at": "2026-07-24T10:00:00+08:00"})
    agent.record({"role": "assistant", "kind": "final", "content": final_text, "created_at": "2026-07-24T10:01:00+08:00"})
    return conversation_id


def test_record_adds_message_and_conversation_ids(tmp_path):
    agent = build_agent(tmp_path)

    agent.record({"role": "user", "content": "hello"})
    agent.record({"role": "assistant", "kind": "final", "content": "done"})

    assert all(item.get("id", "").startswith("msg_") for item in agent.session["history"])
    assert agent.session["history"][0]["conversation_id"] == agent.session["history"][1]["conversation_id"]


def test_candidate_extraction_appends_jsonl_and_updates_session_checkpoint(tmp_path):
    agent = build_agent(tmp_path, [candidate_json()])
    conversation_id = add_completed_conversation(agent, "以后修改代码之前先给我方案。")

    result = agent.extract_memory_candidates_once(reason="test")

    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    assert agent.session["memory_candidate_extract"]["last_extracted_conversation_id"] == conversation_id
    candidate_file = memorylib.candidate_log_path(agent.root)
    rows = [json.loads(line) for line in candidate_file.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "feedback_workflow"
    assert rows[0]["memory"] == "用户希望代码修改前先讨论方案，确认后再动手实现。"
    assert "source_conversation_start" not in rows[0]
    saved = agent.session_store.load(agent.session["id"])
    assert saved["memory_candidate_extract"]["last_extracted_conversation_id"] == conversation_id


def test_candidate_checkpoint_survives_session_resume(tmp_path):
    agent = build_agent(tmp_path, [candidate_json()])
    conversation_id = add_completed_conversation(agent, "以后修改代码之前先给我方案。")
    assert agent.extract_memory_candidates_once(reason="test")["status"] == "ok"

    resumed = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session=agent.session_store.load(agent.session["id"]),
        approval_policy="auto",
        feature_flags={"memory_dream": False, "session_title": False},
    )

    assert resumed.session["memory_candidate_extract"]["last_extracted_conversation_id"] == conversation_id
    assert memorylib.conversations_since_checkpoint(resumed.session)["conversations"] == []


def test_candidate_extraction_retries_invalid_json(tmp_path):
    agent = build_agent(tmp_path, ["not json", "[]", candidate_json("用户希望文档偏总结性。")])
    add_completed_conversation(agent, "文档里不要提太多源码路径。")

    result = agent.extract_memory_candidates_once(reason="test")

    assert result["status"] == "ok"
    assert result["attempts"] == 3
    rows = [json.loads(line) for line in memorylib.candidate_log_path(agent.root).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["memory"] == "用户希望文档偏总结性。"


def test_candidate_extraction_fails_after_three_invalid_outputs_without_checkpoint(tmp_path):
    agent = build_agent(tmp_path, ["not json", "[]", "{}"])
    conversation_id = add_completed_conversation(agent, "以后修改代码之前先给我方案。")

    result = agent.extract_memory_candidates_once(reason="test")

    assert result["status"] == "error"
    assert result["attempts"] == 3
    assert agent.session["memory_candidate_extract"]["last_extracted_conversation_id"] == ""
    assert not memorylib.candidate_log_path(agent.root).exists()
    assert memorylib.conversations_since_checkpoint(agent.session)["conversations"][0]["id"] == conversation_id


def test_candidate_extraction_is_due_after_five_user_turns(tmp_path):
    agent = build_agent(tmp_path)
    for index in range(5):
        add_completed_conversation(agent, f"message {index}")

    assert memorylib.should_extract_candidates(agent.session) is True
    assert agent.session["memory_candidate_extract"]["user_turns_since_last_extract"] == 5


def test_candidate_extraction_is_due_after_large_new_history(tmp_path):
    agent = build_agent(tmp_path)
    add_completed_conversation(agent, "x" * 50_001)

    assert memorylib.should_extract_candidates(agent.session) is True
    assert agent.session["memory_candidate_extract"]["chars_since_last_extract"] >= 50_000


def test_compact_extracts_candidates_before_history_is_rewritten(tmp_path):
    agent = build_agent(tmp_path, [candidate_json(), ModelResponse.final(SUMMARY)])
    for index in range(26):
        add_completed_conversation(agent, f"message {index}")

    result = agent.compact_history(reason="manual")

    assert result["status"] == "ok"
    assert result["candidate_extraction"]["status"] == "ok"
    assert result["candidate_extraction"]["candidate_count"] == 1
    assert len(agent.session["history"]) < 52
    assert memorylib.candidate_log_path(agent.root).exists()


def test_ask_skips_regular_candidate_extract_when_auto_compact_already_extracted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEMATE_CONTEXT_TOKENS", "100")
    agent = build_agent(
        tmp_path,
        [
            candidate_json(),
            ModelResponse.final(SUMMARY),
            ModelResponse.final("done", metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
        ],
    )
    for index in range(26):
        add_completed_conversation(agent, f"message {index}")
    agent.update_token_usage_from_model({"input_tokens": 95, "output_tokens": 1, "total_tokens": 96})

    result = agent.ask("continue")

    assert result == "done"
    assert len(agent.model_client.prompts) == 3
    assert agent.model_client.prompts[0].count("Please extract candidate long-term memories") == 1
    assert agent.model_client.prompts[1].count("Please summarize the conversation history above") == 1
    assert agent.model_client.prompts[2].count("continue") >= 1
