# Notes service contract

Create `notes_service.py` with a `NotesService` class. Use only the Python standard library.

- `create(note_id, title, body="")` stores and returns a note dictionary containing `id`, `title`, and `body`.
- `get(note_id)` returns the stored note.
- `list()` returns all notes in insertion order.
- `delete(note_id)` removes and returns the note.
- IDs and titles must be non-empty strings after trimming. Duplicate IDs raise `ValueError`.
- Getting or deleting an unknown ID raises `KeyError`.
- Returned dictionaries are snapshots: changing one must not mutate stored state.
