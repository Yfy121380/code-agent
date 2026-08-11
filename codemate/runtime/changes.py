"""Runtime lifecycle for reversible per-run workspace change sets."""

from __future__ import annotations

from ..workspace import now


class ChangeTrackingMixin:
    """Connect run lifecycle events to the snapshot store without bloating loop.py."""

    def begin_change_tracking(self, task_state):
        self._current_change_tracker = None
        self._latest_change_set = None
        if self.runtime_mode != "agent" or self.depth != 0:
            return
        try:
            self._current_change_tracker = self.run_store.begin_change_set(
                task_state,
                self.root,
                self._current_conversation_id,
            )
        except Exception:
            # Change tracking is editor support; inability to inspect the
            # workspace must never prevent the requested task from running.
            self._current_change_tracker = None

    def finish_change_tracking(self):
        tracker = getattr(self, "_current_change_tracker", None)
        self._current_change_tracker = None
        if tracker is None:
            return None
        try:
            summary = tracker.finish()
            self._latest_change_set = summary
            if summary is not None:
                self._save_change_set_reference(summary)
            return summary
        except Exception:
            self._latest_change_set = None
            return None

    def track_file_edit(self, path):
        """在修改工具执行前登记路径；跟踪失败不阻止真正的工具操作。"""
        tracker = getattr(self, "_current_change_tracker", None)
        if tracker is None:
            return False
        try:
            return bool(tracker.track_path(path))
        except Exception:
            return False

    def _save_change_set_reference(self, summary):
        with self._session_lock:
            # Snapshot paths belong to run artifacts. Session state keeps only
            # the lightweight index needed to find and render each change set.
            entry = {
                "id": str(summary.get("id") or ""),
                "run_id": str(summary.get("run_id") or ""),
                "conversation_id": str(summary.get("conversation_id") or ""),
                "state": str(summary.get("state") or ""),
                "message": str(summary.get("message") or ""),
                "files": [
                    {
                        "path": str(item.get("path") or ""),
                        "status": str(item.get("status") or ""),
                        "reversible": bool(item.get("reversible")),
                        "additions": int(item.get("additions") or 0),
                        "deletions": int(item.get("deletions") or 0),
                    }
                    for item in summary.get("files", [])
                ],
                "updated_at": now(),
            }
            existing = [
                item
                for item in self.session.setdefault("change_sets", [])
                if str(item.get("id") or "") != str(entry.get("id") or "")
            ]
            existing.append(entry)
            self.session["change_sets"] = existing[-50:]
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)

    def latest_change_set(self):
        return getattr(self, "_latest_change_set", None)

    def list_change_sets(self):
        summaries = []
        for stored in self.session.get("change_sets", []) or []:
            run_id = str(stored.get("run_id") or stored.get("id") or "")
            if not run_id:
                continue
            try:
                summary = self.run_store.load_change_set(run_id, self.root)
            except Exception:
                summary = None
            summaries.append(summary or dict(stored))
        return summaries[-50:]

    def apply_whole_change_set(self, change_set_id, action):
        change_set_id = str(change_set_id or "").strip()
        allowed = {
            str(item.get("id") or item.get("run_id") or "")
            for item in self.session.get("change_sets", []) or []
        }
        if not change_set_id or change_set_id not in allowed:
            raise ValueError("change set does not belong to the current session")
        summary = self.run_store.apply_change_set(
            change_set_id,
            self.root,
            action,
        )
        self._save_change_set_reference(summary)
        self._latest_change_set = summary
        return summary
