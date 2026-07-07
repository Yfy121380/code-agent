# 工具 schema 与描述：定义模型可见的工具参数结构、风险标记和使用说明。

BASE_TOOL_SPECS = {
    "list_files": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory path to list.",
                    "default": ".",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "risky": False,
        "description": (
            "List direct children of a workspace directory. Output format: one entry per line, "
            "with [D] path for directories and [F] path for files. The output is not recursive. "
            "Use this to inspect directory structure before reading specific files."
        ),
    },
    "read_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to read."},
                "start": {"type": "integer", "description": "1-based starting line number.", "default": 1},
                "end": {"type": "integer", "description": "1-based ending line number, inclusive.", "default": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": (
            "Read a UTF-8 text file by line range before reasoning about or editing it. "
            "Output format: the first line is a tool header like '# path/to/file' and is not file content; "
            "file content lines follow as '<line_number>: <content>'. "
            "For example, a one-line file returns '# README.md\n   1: hello'. "
            "An empty file returns only the '# path' header, meaning there is no file content to patch."
        ),
    },
    "grep": {
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression pattern to search for."},
                "path": {"type": "string", "description": "Workspace-relative file or directory path to search.", "default": "."},
                "mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "count", "content"],
                    "description": (
                        "Output mode. files_with_matches returns only matching file paths; "
                        "count returns per-file match counts plus total_matches; "
                        "content returns matching lines with paths and line numbers."
                    ),
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
        "description": (
            "Search workspace files by regular expression. Output depends on mode: "
            "files_with_matches returns one matching file path per line; "
            "count returns one line per file with its match count plus a final total_matches line; "
            "content returns matching lines with file path and line number, and includes surrounding context lines "
            "when before/after/context are set. Use files_with_matches to locate files, count to estimate scope, "
            "and content to inspect exact matches. before/after override context on their respective sides."
        ),
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
        "description": (
            "Run a shell command from the repository root. Prefer dedicated tools for common workspace operations: "
            "use read_file for reading files, grep for searching, and write_file/patch_file for edits. "
            "Use run_shell for tests, syntax checks, git status/log, package scripts, and other shell-only operations. "
            "Read-only commands with workspace-safe paths may run directly; write-like commands are risky; "
            "dangerous commands require approval. Keep commands focused and avoid unnecessary destructive operations."
        ),
    },
    "write_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to write."},
                "content": {"type": "string", "description": "UTF-8 text content to write or append."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": (
                        "Write mode. overwrite replaces the complete file content; "
                        "append adds content to the end of the file, creating it if needed."
                    ),
                    "default": "overwrite",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": (
            "Create or update a UTF-8 text file. mode='overwrite' creates a new file or replaces the complete "
            "contents of an existing file; for existing files, read the exact file first so you do not overwrite "
            "unknown content. mode='append' appends content to the end of the file, creating the file if needed; "
            "use patch_file for small targeted edits to existing files."
        ),
    },
    "patch_file": {
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path to patch."},
                "old_text": {"type": "string", "description": "Exact text block to replace. Must occur exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "risky": True,
        "description": (
            "Replace one exact text block in an existing UTF-8 text file. Read the exact file first, "
            "then provide old_text copied exactly from the current file. old_text must occur exactly once; "
            "otherwise the patch is rejected. Use patch_file for targeted edits and keep the replacement as small as practical."
        ),
    },
    "todo_write": {
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Complete replacement todo list for the current coding session.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Specific actionable task description."},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Current task status.",
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": (
            "Create or update the active todo list for the current coding session. "
            "The todo list is the active work plan; when current_todos appears in Working memory, "
            "continue following those items until they are completed or no longer relevant. "
            "Use todo_write for complex multi-step tasks with 3 or more meaningful steps, non-trivial tasks "
            "that require planning/investigation/multiple file edits/verification, explicit task tracking requests, "
            "multi-part user requests, and new instructions that change the active work. "
            "Do not use todo_write for a single straightforward task, trivial work where tracking adds no value, "
            "tasks with fewer than 3 simple steps, or purely conversational/informational requests. "
            "Positive example: for an e-commerce request covering user registration, product catalog, shopping cart, "
            "and checkout flow, create todos for each feature and verification. "
            "Negative examples: answering how to print Hello World in Python, explaining git status, adding one comment "
            "to one function, or running npm install and reporting the result. "
            "Task rules: todos must be specific and actionable; statuses are pending, in_progress, or completed; "
            "at most one todo may be in_progress, but one is not required; mark work in_progress before starting it; "
            "mark completed immediately after finishing; do not mark completed if tests fail, implementation is partial, "
            "errors remain unresolved, or required files/dependencies are missing; remove todos that are no longer relevant."
        ),
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
