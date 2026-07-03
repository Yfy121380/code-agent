# 记忆系统常量：控制工作记忆容量、过程笔记 TTL 和长期记忆默认主题。

WORKING_FILE_LIMIT = 12
FILE_SUMMARY_LIMIT = 6
PROCESS_NOTE_LIMIT = 6
PROCESS_NOTE_TTL_TURNS = 3

DURABLE_TOPIC_DEFAULTS = {
    "project-conventions": {
        "title": "Project Conventions",
        "summary": "Stable repository conventions.",
        "tags": ["convention"],
    },
    "key-decisions": {
        "title": "Key Decisions",
        "summary": "Long-lived decisions and rationale anchors.",
        "tags": ["decision"],
    },
    "dependency-facts": {
        "title": "Dependency Facts",
        "summary": "Stable dependency and environment facts.",
        "tags": ["dependency"],
    },
    "user-preferences": {
        "title": "User Preferences",
        "summary": "Stable user preferences.",
        "tags": ["preference"],
    },
}

PROCESS_NOTE_KIND_BY_ERROR_CODE = {
    "invalid_arguments": "invalid_arguments",
    "repeated_identical_call": "repeated_call",
    "approval_denied": "approval_denied",
    "unknown_tool": "rejected",
    "tool_failed": "error",
    "tool_partial_success": "partial_success",
}
