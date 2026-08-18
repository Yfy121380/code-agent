"""Runtime UI adapter that emits structured events over JSON Lines."""

from __future__ import annotations

import uuid
from collections import defaultdict, deque

from ..ui import NullUI


def _visible_tool_args(name, args):
    """Keep large edit payloads out of the editor UI transport."""
    values = dict(args or {})
    if str(name or "") in {"write_file", "patch_file"}:
        return {"path": str(values.get("path") or "")}
    return values


class JsonUI(NullUI):
    """Translate runtime UI callbacks into provider-independent bridge events."""

    def __init__(self, writer, interactions, request_id_getter=lambda: ""):
        self.writer = writer
        self.interactions = interactions
        self.request_id_getter = request_id_getter
        self._stream_id = ""
        self._pending_tools = defaultdict(deque)

    def _emit(self, event_type, **payload):
        return self.writer.emit(
            event_type,
            request_id=self.request_id_getter() or "",
            **payload,
        )

    def model_start(self):
        self._emit("model_status", status="started")

    def model_end(self, kind="", metadata=None):
        self._emit(
            "model_status", status="finished", kind=kind, metadata=dict(metadata or {})
        )

    def stream_start(self, phase=""):
        self._stream_id = f"stream_{uuid.uuid4().hex[:12]}"
        self._emit("stream_start", stream_id=self._stream_id, phase=str(phase or ""))

    def stream_delta(self, text, phase=""):
        text = str(text or "")
        if text:
            self._emit(
                "text_delta",
                stream_id=self._stream_id,
                phase=str(phase or ""),
                text=text,
            )

    def stream_end(self, kind="", metadata=None):
        self._emit(
            "stream_end",
            stream_id=self._stream_id,
            kind=str(kind or ""),
            metadata=dict(metadata or {}),
        )
        self._stream_id = ""

    def commentary(self, text):
        text = str(text or "").strip()
        if text:
            self._emit("commentary", text=text)

    def final_answer(self, text):
        text = str(text or "").strip()
        if text:
            self._emit("final", text=text)

    def tool_start(self, name, args, risk_level=""):
        tool_id = f"tool_{uuid.uuid4().hex[:12]}"
        self._pending_tools[str(name or "")].append(tool_id)
        self._emit(
            "tool_start",
            tool_id=tool_id,
            name=str(name or ""),
            args=_visible_tool_args(name, args),
            risk_level=str(risk_level or ""),
        )

    def tool_result(self, name, args, result, metadata=None):
        pending = self._pending_tools[str(name or "")]
        tool_id = pending.popleft() if pending else f"tool_{uuid.uuid4().hex[:12]}"
        self._emit(
            "tool_result",
            tool_id=tool_id,
            name=str(name or ""),
            args=_visible_tool_args(name, args),
            result=str(result or ""),
            metadata=dict(metadata or {}),
        )

    def compact_start(self, reason=""):
        self._emit("compact_status", status="started", reason=str(reason or ""))

    def compact_end(self, status="", metadata=None):
        self._emit(
            "compact_status", status=str(status or ""), metadata=dict(metadata or {})
        )

    def review_start(self):
        self._emit("review_status", status="started")

    def review_end(self, status="", metadata=None):
        self._emit(
            "review_status", status=str(status or ""), metadata=dict(metadata or {})
        )

    def approval_request(self, name, args, metadata=None):
        metadata = dict(metadata or {})
        tool_id = f"tool_{uuid.uuid4().hex[:12]}"
        self._pending_tools[str(name or "")].append(tool_id)
        options = [{"label": "Allow once", "value": {"allowed": True}}]
        access = str(metadata.get("approval_access") or "").strip()
        allow_dir = str(metadata.get("suggested_allow_dir") or "").strip()
        shell_subject = str(metadata.get("suggested_shell_subject") or "").strip()
        if shell_subject:
            options.append(
                {
                    "label": f"Allow all `{shell_subject}` commands this session",
                    "value": {
                        "allowed": True,
                        "remember": {"shell_subject": shell_subject},
                    },
                }
            )
        if access in {"read", "write"} and allow_dir:
            options.append(
                {
                    "label": f"Allow {access} for {allow_dir} this session",
                    "value": {
                        "allowed": True,
                        "remember": {"access": access, "path": allow_dir},
                    },
                }
            )
        if shell_subject and access in {"read", "write"} and allow_dir:
            options.append(
                {
                    "label": f"Allow `{shell_subject}` and {access} for {allow_dir} this session",
                    "value": {
                        "allowed": True,
                        "remember": {
                            "shell_subject": shell_subject,
                            "access": access,
                            "path": allow_dir,
                        },
                    },
                }
            )
        options.append({"label": "Deny", "value": {"allowed": False}})
        result = self.interactions.request(
            "approval_request",
            {
                "name": str(name or ""),
                "tool_id": tool_id,
                "args": _visible_tool_args(name, args),
                "metadata": metadata,
                "options": options,
            },
        )
        return result if isinstance(result, dict) else {"allowed": False}

    def request_user_input(self, questions):
        result = self.interactions.request(
            "user_input_request",
            {"questions": list(questions or [])},
        )
        if not isinstance(result, dict):
            return {"status": "cancelled", "answers": {}}
        return result

    def editor_diagnostics(self, path, *, wait_for_update=False):
        """Ask the editor host for current errors without involving Webview UI."""
        result = self.interactions.request(
            "editor_diagnostics_request",
            {
                "path": str(path or ""),
                "wait_for_update": bool(wait_for_update),
            },
            timeout=4.0,
        )
        if not isinstance(result, dict):
            return {"status": "unavailable", "diagnostics": []}
        return result

    def plan_review(self, title, plan):
        result = self.interactions.request(
            "plan_review_request",
            {"title": str(title or ""), "plan": str(plan or "")},
        )
        if not isinstance(result, dict):
            return {"decision": "cancelled"}
        return result

    def session_menu(self, sessions, current_id=""):
        result = self.interactions.request(
            "session_select_request",
            {"sessions": list(sessions or []), "current_id": str(current_id or "")},
        )
        return result if isinstance(result, dict) else None
