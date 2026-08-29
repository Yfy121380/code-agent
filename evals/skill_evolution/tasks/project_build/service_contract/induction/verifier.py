import ast
import os
import subprocess
import sys


README = """# Notes service contract

Create `notes_service.py` with a `NotesService` class. Use only the Python standard library.

- `create(note_id, title, body="")` stores and returns a note dictionary containing `id`, `title`, and `body`.
- `get(note_id)` returns the stored note.
- `list()` returns all notes in insertion order.
- `delete(note_id)` removes and returns the note.
- IDs and titles must be non-empty strings after trimming. Duplicate IDs raise `ValueError`.
- Getting or deleting an unknown ID raises `KeyError`.
- Returned dictionaries are snapshots: changing one must not mutate stored state.
"""


def _run(workspace, script):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)
    return subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def _stdlib_only(path):
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {"collections", "copy", "dataclasses", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] not in allowed for alias in node.names):
                return False
        elif (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] not in allowed
        ):
            return False
    return True


def verify(workspace, final_answer, trace_events):
    functional = _run(
        workspace,
        """from notes_service import NotesService
s = NotesService()
assert s.create('n1', ' First ', 'body') == {'id': 'n1', 'title': 'First', 'body': 'body'}
assert s.create('n2', 'Second') == {'id': 'n2', 'title': 'Second', 'body': ''}
assert s.list() == [
    {'id': 'n1', 'title': 'First', 'body': 'body'},
    {'id': 'n2', 'title': 'Second', 'body': ''},
]
assert s.delete('n1')['id'] == 'n1'
assert [item['id'] for item in s.list()] == ['n2']
""",
    )
    regression = _run(
        workspace,
        """from notes_service import NotesService
s = NotesService()
for args in [('', 'title'), (' ', 'title'), ('n', ''), ('n', ' ')]:
    try: s.create(*args)
    except (TypeError, ValueError): pass
    else: raise AssertionError(args)
s.create('n', 'title')
try: s.create('n', 'again')
except ValueError: pass
else: raise AssertionError('duplicate')
for method in (s.get, s.delete):
    try: method('missing')
    except KeyError: pass
    else: raise AssertionError('missing')
copy = s.get('n'); copy['title'] = 'changed'
assert s.get('n')['title'] == 'title'
listed = s.list(); listed[0]['title'] = 'changed again'
assert s.get('n')['title'] == 'title'
""",
    )
    source = workspace / "notes_service.py"
    instruction = (workspace / "README.md").read_text(
        encoding="utf-8"
    ) == README and _stdlib_only(source)
    validation = any(
        word in final_answer.lower() for word in ("pytest", "unittest", "测试", "验证")
    )
    headings = all(word in final_answer for word in ("实现", "验证", "限制"))
    return {
        "FUNC-001": {
            "passed": functional.returncode == 0,
            "evidence": functional.stderr,
        },
        "REG-001": {
            "passed": regression.returncode == 0,
            "evidence": regression.stderr,
        },
        "INSTR-001": {
            "passed": instruction,
            "evidence": "README changed or non-stdlib import used",
        },
        "QUALITY-001": {
            "passed": validation,
            "evidence": "executable validation not reported",
        },
        "PREF-001": {"passed": headings, "evidence": "report headings missing"},
    }
