# 工具 schema 与描述：定义模型可见的工具参数结构、风险标记和使用说明。

BASE_TOOL_SPECS = {
    "list_files": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Relative paths resolve from the workspace root; home paths may require approval.",
                    "default": ".",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
List direct children of a directory.

Output format: one entry per line, with [D] path for directories and [F] path for files.
The output is not recursive. Use this to inspect directory structure before reading specific files.
Relative paths are resolved from the workspace root. Paths outside the workspace may require approval and sensitive paths are blocked.
""".strip(),
    },
    "read_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read. Relative paths resolve from the workspace root; home paths may require approval."},
                "start": {"type": "integer", "description": "1-based starting line number.", "default": 1},
                "end": {"type": "integer", "description": "1-based ending line number, inclusive.", "default": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
Read a UTF-8 text file by line range before reasoning about or editing it.

Relative paths are resolved from the workspace root. Paths outside the workspace may require approval and sensitive paths are blocked.

Output format:
- The first line is a tool header like "# path/to/file" and is not file content.
- File content lines follow as "<line_number>: <content>".
- For example, a one-line file returns "# README.md\n   1: hello".
- An empty file returns only the "# path" header, meaning there is no file content to patch.
""".strip(),
    },
    "grep": {
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression pattern to search for."},
                "path": {"type": "string", "description": "File or directory path to search. Relative paths resolve from the workspace root; home paths may require approval.", "default": "."},
                "mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "count", "content"],
                    "description": """
Output mode. files_with_matches returns only matching file paths; count returns per-file match counts plus total_matches; content returns matching lines with paths and line numbers.
""".strip(),
                    "default": "content",
                },
                "after": {"type": "integer", "description": "Context lines after each match in content mode.", "default": 0},
                "before": {"type": "integer", "description": "Context lines before each match in content mode.", "default": 0},
                "context": {"type": "integer", "description": "Symmetric context lines like rg -C; overridden by before/after when set.", "default": 0},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
Search files by regular expression.

Output depends on mode:
- files_with_matches: one matching file path per line.
- count: one line per file with its match count plus a final total_matches line.
- content: matching lines with file path and line number, including surrounding context lines when before/after/context are set.

Use files_with_matches to locate files, count to estimate scope, and content to inspect exact matches.
before/after override context on their respective sides.
Relative paths are resolved from the workspace root. Paths outside the workspace may require approval and sensitive paths are blocked.
""".strip(),
    },
    "run_shell": {
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run in the repository root."},
                "timeout": {"type": "integer", "description": "Timeout in seconds, from 1 to 120.", "default": 20},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": """
Run a shell command from the repository root.

Prefer dedicated tools for common workspace operations: use read_file for reading files, grep for searching, and write_file/patch_file for edits.
Use run_shell for tests, syntax checks, git status/log, package scripts, and other shell-only operations.

Read-only commands with allowed paths may run directly. Write-like commands are risky. Dangerous commands require approval.
Paths are resolved like file tools: workspace paths are preferred, home paths may require approval, and sensitive paths are blocked.
Keep commands focused and avoid unnecessary destructive operations.
""".strip(),
    },
    "write_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write. Relative paths resolve from the workspace root; home paths may require approval."},
                "content": {"type": "string", "description": "UTF-8 text content to write or append."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": """
Write mode. overwrite replaces the complete file content; append adds content to the end of the file, creating it if needed.
""".strip(),
                    "default": "overwrite",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": """
Create or update a UTF-8 text file.

mode="overwrite" creates a new file or replaces the complete contents of an existing file.
For existing files, read the exact file first so you do not overwrite unknown content.

mode="append" appends content to the end of the file, creating the file if needed.
Use patch_file for small targeted edits to existing files.
Writes outside the workspace require approval unless full approval is enabled. Sensitive paths are blocked.
""".strip(),
    },
    "patch_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to patch. Relative paths resolve from the workspace root; home paths may require approval."},
                "old_text": {"type": "string", "description": "Exact text block to replace. Must occur exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": """
Replace one exact text block in an existing UTF-8 text file.

Read the exact file first, then provide old_text copied exactly from the current file.
old_text must occur exactly once; otherwise the patch is rejected.
Use patch_file for targeted edits and keep the replacement as small as practical.
Writes outside the workspace require approval unless full approval is enabled. Sensitive paths are blocked.
""".strip(),
    },
    "todo_write": {
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Complete replacement todo plan for the current coding session.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase": {"type": "string", "description": "High-level stage goal for the current work."},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Current phase status.",
                            },
                            "tasks": {
                                "type": "array",
                                "description": "Concrete steps inside this phase. Use an empty array when the phase does not need finer-grained tracking.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string", "description": "Concrete actionable task inside the phase."},
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "in_progress", "completed"],
                                            "description": "Current task status.",
                                        },
                                    },
                                    "required": ["description", "status"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["phase", "status", "tasks"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
Create or update the active todo plan for the current coding session.

The todo plan is shown in Working memory as current_todos. When it appears, follow it until the work is completed or the plan is no longer relevant.

When to use:
- Use todo_write for complex multi-step work, non-trivial tasks that require planning, investigation, multiple edits, or verification.
- Use todo_write when the user explicitly asks for task tracking.
- Use todo_write for multi-part user requests.
- Do not use todo_write for a single straightforward edit, trivial work where tracking adds no value, tasks with fewer than 3 simple steps, or purely conversational/informational requests.

Structure:
- Todos are organized as phases.
- A phase is a high-level stage of the current work, such as understanding requirements, implementing changes, or verifying results.
- A phase should describe the stage goal, not just a short label. Prefer "Implement the concrete changes identified from the review" over "Implement".
- Each phase may contain tasks.
- Tasks are concrete steps inside that phase.
- Do not expand every phase by default.
- Use tasks only when a phase contains multiple concrete steps that are useful to track separately.
- Keep tasks empty when the phase is simple, obvious, or can be completed as one action.
- A phase with empty tasks is still a valid todo item.

Progressive planning:
- When later work depends on information you have not gathered yet, start with high-level phases instead of guessing detailed tasks.
- After completing an investigation, review, or analysis phase, update the todo plan before implementation.
- Expand the current complex phase into concrete tasks based on what you learned.
- Do not keep broad items like "modify code/tests/docs" once you know the specific changes to make.

Plan maintenance:
- Phase and task items are not fixed forever. Maintain the plan when necessary so it reflects the real remaining work.
- Add a phase or task when you discover new required work during execution.
- Modify a phase or task when the required work changes or a better description is needed.
- Delete a phase or task when it is obsolete, wrong, duplicated, or no longer required.
- Expanding a broad phase into concrete tasks after investigation is a normal plan modification.
- Do not change the plan for tiny implementation details, transient observations, or every tool action.
- todo_write replaces the entire current todo plan, so include all still-relevant phases and tasks when making any change.

Status rules:
- Phase and task statuses are pending, in_progress, or completed.
- Maintain statuses accurately.
- At most one phase may be in_progress at a time.
- Within the same phase, at most one task may be in_progress at a time.
- If a phase has tasks, its task statuses must be consistent with the phase status.
- A completed phase cannot contain pending or in_progress tasks.
- A pending phase cannot contain completed or in_progress tasks.
- An in_progress phase may contain pending, in_progress, or completed tasks.
- Mark a phase or task completed only when it is fully done.
- Do not mark completed if tests fail, implementation is partial, errors remain unresolved, or required files/dependencies are missing.
- Remove phases or tasks that are no longer relevant.

Positive example: progressive planning before reading review_notes.md:
{
  "todos": [
    {
      "phase": "Understand the requested changes from review_notes.md",
      "status": "in_progress",
      "tasks": [
        {"description": "Read review_notes.md", "status": "in_progress"},
        {"description": "Extract concrete action items from the review", "status": "pending"}
      ]
    },
    {
      "phase": "Implement the concrete changes identified from the review",
      "status": "pending",
      "tasks": []
    },
    {
      "phase": "Verify the modified behavior and summarize the result",
      "status": "pending",
      "tasks": []
    }
  ]
}

Positive example: after the review notes are understood:
{
  "todos": [
    {
      "phase": "Understand the requested changes from review_notes.md",
      "status": "completed",
      "tasks": [
        {"description": "Read review_notes.md", "status": "completed"},
        {"description": "Extract concrete action items from the review", "status": "completed"}
      ]
    },
    {
      "phase": "Implement the concrete changes identified from the review",
      "status": "in_progress",
      "tasks": [
        {"description": "Fix empty-input validation", "status": "in_progress"},
        {"description": "Add regression test for empty input", "status": "pending"},
        {"description": "Update README usage example", "status": "pending"}
      ]
    },
    {
      "phase": "Verify the modified behavior and summarize the result",
      "status": "pending",
      "tasks": []
    }
  ]
}

Positive example: simple phases inside a complex task can keep tasks empty:
{
  "todos": [
    {
      "phase": "Inspect the authentication flow and identify why login fails",
      "status": "completed",
      "tasks": [
        {"description": "Read the login handler", "status": "completed"},
        {"description": "Trace the token validation path", "status": "completed"}
      ]
    },
    {
      "phase": "Apply the one-line token expiry comparison fix",
      "status": "in_progress",
      "tasks": []
    },
    {
      "phase": "Run the focused authentication regression test",
      "status": "pending",
      "tasks": []
    }
  ]
}

Positive example: multi-feature e-commerce request:
Create phases such as "Implement user registration", "Implement product catalog", "Implement shopping cart", "Implement checkout flow", and "Verify the complete purchase workflow". Expand a phase into tasks only when it needs finer-grained tracking.

Negative examples:
- Do not use todo_write to answer how to print Hello World in Python.
- Do not use todo_write to explain git status.
- Do not use todo_write to add one comment to one function.
- Do not use todo_write to run one command and report the result.
- After you already know the specific changes, do not create a vague task like:
{
  "todos": [
    {
      "phase": "Implement changes",
      "status": "in_progress",
      "tasks": [
        {"description": "Modify project code/tests/docs", "status": "in_progress"}
      ]
    }
  ]
}
""".strip(),
    },
    "skill_load": {
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of an available skill under .codemate/skills/<name>/SKILL.md."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
Load a skill into Working memory when it is clearly useful for the current task.

Use this only for skills listed in Available skills. Do not load a skill that is already active.
After loading, follow the skill instructions while they remain relevant.

Skill files live at .codemate/skills/<name>/SKILL.md.
Relative resources mentioned by the skill, such as scripts/, references/, examples/, and templates/, are located under .codemate/skills/<name>/.
""".strip(),
    },
    "skill_unload": {
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the active skill to unload."},
                "reason": {"type": "string", "description": "Brief reason why the skill no longer applies.", "default": ""},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": """
Unload an active skill from Working memory when it no longer applies to the current user task.

Use skill_unload when:
- The user switches to an unrelated task and the active skill is no longer useful.
- The active skill was loaded by mistake.
- The current task direction changed and the skill instructions no longer apply.

Do not unload a skill only because one request has been completed. Keep it active if the next user request may reasonably continue the same task, project, or workflow.
Do not unload a skill if its instructions are still relevant.
""".strip(),
    },
}

DELEGATE_TOOL_SPEC = {
    "input_schema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Bounded read-only investigation task for the child agent."},
            "max_steps": {"type": "integer", "description": "Maximum child-agent tool steps.", "default": 3},
        },
        "required": ["task"],
        "additionalProperties": False,
    },
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}
