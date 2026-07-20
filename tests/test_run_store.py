import json

from codemate.storage import RunStore, SessionStore
from codemate.storage import TaskState


def test_session_store_groups_session_json_and_runs_dir(tmp_path):
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    session = {"id": "session_001", "history": [], "memory": {}, "todos": []}
    dream_session = {"id": "dream-001", "history": [], "memory": {}, "todos": []}

    session_path = store.save(session)
    store.save(dream_session)

    assert session_path == tmp_path / ".codemate" / "sessions" / "session_001" / "session.json"
    assert store.runs_dir("session_001") == tmp_path / ".codemate" / "sessions" / "session_001" / "runs"
    assert store.load("session_001")["id"] == "session_001"
    assert store.latest() == "session_001"
    assert store.count() == 1


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".codemate" / "sessions" / "session_001" / "runs")
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".codemate" / "sessions" / "session_001" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_trace(state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "prompt_metadata": {"prompt_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(state.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_preserves_unicode_text_on_disk(tmp_path):
    store = RunStore(tmp_path / ".codemate" / "sessions" / "session_001" / "runs")
    state = TaskState.create(run_id="run_unicode", task_id="task_unicode", user_request="检查中文")
    store.start_run(state)
    state.finish_success("完成。")

    store.write_task_state(state)
    store.append_trace(state, {"event": "run_finished", "final_answer": "完成。"})

    task_state_text = store.task_state_path(state).read_text(encoding="utf-8")
    trace_text = store.trace_path(state).read_text(encoding="utf-8")

    assert "检查中文" in task_state_text
    assert "完成。" in trace_text
    assert "\\u" not in task_state_text
    assert "\\u" not in trace_text


def test_run_store_tolerates_missing_final_trace_only_run(tmp_path):
    store = RunStore(tmp_path / ".codemate" / "sessions" / "session_001" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
