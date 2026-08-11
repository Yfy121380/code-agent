"""Session checkpoint tests cover the persisted retry boundary."""

import copy

from codemate.storage import SessionStore


def test_request_checkpoint_preserves_pre_request_state(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = {
        "id": "session-1",
        "title": "Original",
        "history": [{"role": "user", "content": "earlier"}],
        "todos": [{"title": "Keep state"}],
    }
    expected = copy.deepcopy(session)

    store.save_request_checkpoint(session, "new request")
    session["history"].append({"role": "assistant", "content": "changed"})
    session["todos"].clear()

    checkpoint = store.load_request_checkpoint("session-1")
    assert checkpoint == {
        "version": 1,
        "user_request": "new request",
        "editor_context": "",
        "response_annotations": [],
        "transcript_size": 0,
        "session": expected,
    }
    assert store.request_checkpoint_info("session-1") == {
        "user_request": "new request",
        "response_annotations": [],
    }


def test_missing_request_checkpoint_is_not_retryable(tmp_path):
    store = SessionStore(tmp_path / "sessions")

    assert store.load_request_checkpoint("missing") is None
    assert store.request_checkpoint_info("missing") is None


def test_clear_request_checkpoint_removes_retry_state(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = {"id": "session-1", "history": []}
    store.save_request_checkpoint(session, "request")

    store.clear_request_checkpoint("session-1")

    assert store.request_checkpoint_info("session-1") is None


def test_transcript_is_append_only_and_can_restore_checkpoint_boundary(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    first = {"id": "one", "role": "user", "content": "before"}
    second = {"id": "two", "role": "assistant", "content": "after"}

    store.append_transcript("session-1", first)
    checkpoint_size = store.transcript_size("session-1")
    store.append_transcript("session-1", second)

    assert store.load_transcript("session-1") == [first, second]
    store.truncate_transcript("session-1", checkpoint_size)
    assert store.load_transcript("session-1") == [first]

    store.clear_transcript("session-1")
    assert store.load_transcript("session-1") == []
