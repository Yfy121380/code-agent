"""编辑器静态诊断的运行时比较与工具结果格式化。"""

from collections import Counter


MAX_EDITOR_DIAGNOSTICS = 20
MAX_EDITOR_DIAGNOSTIC_SNAPSHOT = 100


def _normalize_editor_diagnostics(response):
    """将不可信的编辑器响应收敛为稳定的 Error 记录。"""
    if not isinstance(response, dict) or response.get("status") != "ok":
        return None
    normalized = []
    for raw in list(response.get("diagnostics", []) or [])[
        :MAX_EDITOR_DIAGNOSTIC_SNAPSHOT
    ]:
        if not isinstance(raw, dict) or str(raw.get("severity") or "") != "error":
            continue
        message = " ".join(str(raw.get("message") or "").split())[:2000]
        if not message:
            continue
        normalized.append(
            {
                "path": str(raw.get("path") or "")[:1000],
                "line": max(1, int(raw.get("line") or 1)),
                "column": max(1, int(raw.get("column") or raw.get("character") or 1)),
                "severity": "error",
                "message": message,
                "source": str(raw.get("source") or "").strip()[:200],
                "code": str(raw.get("code") or "").strip()[:200],
            }
        )
    return normalized


def _diagnostic_identity(item):
    """忽略编辑导致的行号漂移，按错误本身识别同一条诊断。"""
    return (item["message"], item["source"], item["code"])


def _new_editor_diagnostics(before, after):
    """使用多重集合排除修改前已经存在的同类错误。"""
    remaining = Counter(_diagnostic_identity(item) for item in before)
    introduced = []
    for item in after:
        identity = _diagnostic_identity(item)
        if remaining[identity] > 0:
            remaining[identity] -= 1
        else:
            introduced.append(item)
    return introduced[:MAX_EDITOR_DIAGNOSTICS]


def format_editor_diagnostics(items):
    """生成可直接附加到修改工具结果中的简短错误列表。"""
    if not items:
        return ""
    lines = ["New editor errors detected after this edit:"]
    for item in items:
        location = f"{item['path']}:{item['line']}:{item['column']}"
        origin = "/".join(value for value in (item["source"], item["code"]) if value)
        suffix = f" ({origin})" if origin else ""
        lines.append(f"- {location} [error] {item['message']}{suffix}")
    return "\n".join(lines)


class EditorDiagnosticsMixin:
    """为修改工具维护每轮基线，并查询修改后的新增 Error。"""

    def _capture_editor_diagnostic_baseline(self, edit_path):
        baselines = getattr(self, "_editor_diagnostic_baselines", None)
        if baselines is None:
            baselines = {}
            self._editor_diagnostic_baselines = baselines
        key = str(edit_path)
        if key in baselines:
            return
        query = getattr(self.ui, "editor_diagnostics", None)
        if not callable(query):
            baselines[key] = None
            return
        try:
            baselines[key] = _normalize_editor_diagnostics(
                query(key, wait_for_update=False)
            )
        except Exception:
            baselines[key] = None

    def _introduced_editor_diagnostics(self, edit_path):
        baselines = getattr(self, "_editor_diagnostic_baselines", {})
        before = baselines.get(str(edit_path))
        query = getattr(self.ui, "editor_diagnostics", None)
        if before is None or not callable(query):
            return None
        try:
            after = _normalize_editor_diagnostics(
                query(str(edit_path), wait_for_update=True)
            )
        except Exception:
            return None
        if after is None:
            return None
        return _new_editor_diagnostics(before, after)
