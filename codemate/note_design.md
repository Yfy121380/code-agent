# Process Notes Design

Process notes are short-lived working-memory reminders for abnormal tool calls.
They do not store ordinary facts, do not duplicate file summaries, and do not
participate in relevant-memory retrieval.

## Memory Shape

Working memory keeps only:

```json
{
  "working": {
    "task_summary": "",
    "recent_files": []
  },
  "file_summaries": {},
  "process_notes": []
}
```

Long-term durable memory is stored separately under `.codemate/memory/` and is
the only source for the `Relevant memory:` section.

## Process Note Shape

Each note records one abnormal tool-call pattern:

```json
{
  "id": "stable short hash",
  "kind": "invalid_arguments | repeated_call | approval_denied | rejected | error | partial_success",
  "tool": "patch_file",
  "tool_error_code": "invalid_arguments",
  "args_digest": "stable short hash",
  "args_preview": {"path": "README.md"},
  "affected_paths": ["README.md"],
  "inspected_paths": [],
  "message": "error: invalid arguments for patch_file: ...",
  "count": 1,
  "created_turn": 1,
  "updated_turn": 1,
  "created_at": "timestamp",
  "updated_at": "timestamp"
}
```

`message` is the original runtime error text returned to the model. The prompt
does not invent a separate explanation layer for these notes.

## Rendering

Process notes render inside `Memory:`, next to file summaries:

```text
Memory:
- task: ...
- recent_files: ...
- file_summaries:
  - README.md: ...
- process_notes:
  - patch_file invalid_arguments on README.md, count=2
    error: invalid arguments for patch_file: ...
- durable_topics: ...
```

This keeps them in working memory without creating another prompt section.

## Recording

Only abnormal tool results create process notes:

```text
invalid_arguments              -> invalid_arguments
repeated_identical_call         -> repeated_call
approval_denied                 -> approval_denied
unknown_tool / other rejection  -> rejected
tool_failed                     -> error
tool_partial_success            -> partial_success
```

The merge key is `kind + tool + args_digest + affected_paths`. A repeated
abnormal call updates `count`, `message`, and update timestamps instead of
appending another note.

## Clearing

All process notes expire after three `ask()` turns.

Additional clearing rules:

```text
invalid_arguments:
- clear when the same tool later succeeds

repeated_call:
- clear after any successful tool call

approval_denied / rejected / error:
- clear when the same tool later succeeds

partial_success:
- clear after every affected path has been successfully read with read_file
```

`partial_success` tracks `inspected_paths` so multi-file side effects are only
cleared after every affected file has been inspected.
