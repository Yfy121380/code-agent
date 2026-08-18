"""Thread-safe JSON Lines primitives used by the CodeMate bridge."""

from __future__ import annotations

import json
import queue
import threading
import uuid


class ProtocolError(ValueError):
    """Raised when an inbound bridge message is not a valid protocol object."""


class JsonLineWriter:
    """Write complete JSON objects atomically so concurrent events cannot mix."""

    def __init__(self, stream):
        self.stream = stream
        self._lock = threading.Lock()

    def emit(self, event_type, **payload):
        message = {"type": str(event_type), **payload}
        encoded = json.dumps(
            message, ensure_ascii=False, default=str, separators=(",", ":")
        )
        with self._lock:
            self.stream.write(encoded + "\n")
            self.stream.flush()
        return message


def parse_message(line):
    """Parse one inbound JSONL line and require an object with a string type."""
    try:
        message = json.loads(str(line))
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    if not isinstance(message.get("type"), str) or not message["type"].strip():
        raise ProtocolError("message.type must be a non-empty string")
    return message


class InteractionBroker:
    """Correlate blocking runtime UI requests with asynchronous client replies."""

    def __init__(self, writer, request_id_getter=lambda: ""):
        self.writer = writer
        self.request_id_getter = request_id_getter
        self._pending = {}
        self._lock = threading.Lock()

    def request(self, event_type, payload, timeout=None):
        """Emit an interaction and wait for its matching response.

        Interactive approvals intentionally wait without a deadline. Optional
        editor integrations can provide a timeout so an unavailable client
        cannot stall the agent's tool loop.
        """
        interaction_id = f"interaction_{uuid.uuid4().hex[:12]}"
        response_queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[interaction_id] = response_queue
        self.writer.emit(
            event_type,
            request_id=self.request_id_getter() or "",
            interaction_id=interaction_id,
            **dict(payload or {}),
        )
        try:
            try:
                return response_queue.get(timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self._lock:
                self._pending.pop(interaction_id, None)

    def deliver(self, message):
        interaction_id = str(message.get("interaction_id") or "")
        with self._lock:
            response_queue = self._pending.get(interaction_id)
        if response_queue is None:
            return False
        try:
            response_queue.put_nowait(message.get("value"))
        except queue.Full:
            return False
        return True

    def cancel_all(self):
        with self._lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(None)
            except queue.Full:
                pass
