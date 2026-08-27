"""Model-visible tool contracts for progressive memory."""

PROGRESSIVE_MEMORY_TOOL_SPECS = {
    "memory_index": {
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional case-insensitive title substring.",
                },
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Search or browse all Ordinary Memory IDs and titles with pagination. Use it when the visible index lacks a relevant topic.",
    },
    "memory_read": {
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Ordinary Memory ID, such as M001.",
                }
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Read one complete Ordinary Memory topic by ID.",
    },
    "core_memory_update": {
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Stable identity.*, preference.*, safety.*, or privacy.* key.",
                },
                "value": {
                    "type": "string",
                    "description": "Concise durable cross-project user fact or rule.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this information belongs in Core memory.",
                },
                "explicit_user_statement": {
                    "type": "string",
                    "description": "Exact supporting quote from the current user request.",
                },
            },
            "required": ["key", "value", "reason", "explicit_user_statement"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Upsert explicit cross-project user memory. Evidence must be verbatim and value may faithfully summarize it. A capacity_exceeded result leaves existing memory unchanged and may be retried with a shorter value.",
    },
    "core_memory_remove": {
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Existing Core memory key."},
                "reason": {
                    "type": "string",
                    "description": "Why the entry is being revoked.",
                },
                "explicit_user_statement": {
                    "type": "string",
                    "description": "Exact revocation quote from the current user request.",
                },
            },
            "required": ["key", "reason", "explicit_user_statement"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Remove a Core entry only when the current user request explicitly revokes it.",
    },
    "memory_create": {
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Stable project-topic title."},
                "content": {
                    "type": "string",
                    "description": "Complete self-contained topic body.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this durable topic is being created.",
                },
            },
            "required": ["title", "content", "reason"],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Create a durable Ordinary Memory topic after checking that no existing topic owns the information.",
    },
    "memory_update": {
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "title": {"type": "string"},
                "content": {
                    "type": "string",
                    "description": "Complete replacement body preserving valid existing details.",
                },
                "reason": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 1},
            },
            "required": [
                "memory_id",
                "title",
                "content",
                "reason",
                "expected_revision",
            ],
            "additionalProperties": False,
        },
        "risky": False,
        "description": "Replace an Ordinary Memory topic read during this consolidation run, guarded by its expected revision.",
    },
}

MAIN_PROGRESSIVE_MEMORY_TOOLS = {
    "memory_index",
    "memory_read",
    "core_memory_update",
    "core_memory_remove",
}
PLAN_PROGRESSIVE_MEMORY_TOOLS = {"memory_index", "memory_read"}
CONSOLIDATION_MEMORY_TOOLS = {
    "memory_index",
    "memory_read",
    "memory_create",
    "memory_update",
}
