"""任务状态测试。

覆盖模块：storage.task_state。
重点边界：running/success/step_limit/retry_limit 状态记录和 snapshot 输出。
"""

from codemate.storage import (
    STOP_REASON_FINAL_ANSWER_RETURNED,
    STOP_REASON_RETRY_LIMIT_REACHED,
    STOP_REASON_STEP_LIMIT_REACHED,
    TaskState,
)


def test_task_state_starts_running_with_empty_progress():
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    assert state.task_id == "task_001"
    assert state.run_id == "run_001"
    assert state.user_request == "Inspect the repo."
    assert state.source_user_request == "Inspect the repo."
    assert state.status == "running"
    assert state.tool_steps == 0
    assert state.attempts == 0
    assert state.last_tool == ""
    assert state.stop_reason == ""
    assert state.final_answer == ""


def test_task_state_records_success_and_final_answer():
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Fix the bug.")
    state.record_attempt()
    state.record_tool("read_file")
    state.finish_success("Done.")

    assert state.attempts == 1
    assert state.tool_steps == 1
    assert state.last_tool == "read_file"
    assert state.status == "completed"
    assert state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED
    assert state.final_answer == "Done."


def test_task_state_records_step_limit_stop_reason():
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Try again.")

    state.stop_step_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_STEP_LIMIT_REACHED


def test_task_state_records_retry_limit_stop_reason():
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Try again.")

    state.stop_retry_limit()

    assert state.status == "stopped"
    assert state.stop_reason == STOP_REASON_RETRY_LIMIT_REACHED


def test_task_state_snapshot_keeps_final_answer():
    state = TaskState.create(run_id="run_005", task_id="task_005", user_request="Return the answer.")
    state.finish_success("Final answer.")

    snapshot = state.to_dict()

    assert snapshot["final_answer"] == "Final answer."
    assert snapshot["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED


def test_task_state_keeps_runtime_and_source_requests_separate():
    state = TaskState.create(
        run_id="run_review",
        task_id="task_review",
        user_request="Internal review instruction.",
        source_user_request="",
    )

    restored = TaskState.from_dict(state.to_dict())

    assert restored.user_request == "Internal review instruction."
    assert restored.source_user_request == ""


def test_old_task_state_uses_user_request_as_source_request():
    restored = TaskState.from_dict(
        {
            "run_id": "run_old",
            "task_id": "task_old",
            "user_request": "Fix the old bug.",
        }
    )

    assert restored.source_user_request == "Fix the old bug."


def test_null_source_request_uses_persisted_user_request():
    restored = TaskState.from_dict(
        {
            "run_id": "run_null",
            "task_id": "task_null",
            "user_request": "Review this change.",
            "source_user_request": None,
        }
    )

    assert restored.source_user_request == "Review this change."
