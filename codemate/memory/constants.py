# 记忆系统常量：控制工作记忆容量、过程笔记 TTL 和长期记忆候选提取。

WORKING_FILE_LIMIT = 12
FILE_SUMMARY_LIMIT = 6
PROCESS_NOTE_LIMIT = 6
PROCESS_NOTE_TTL_TURNS = 3

MEMORY_CANDIDATE_EXTRACT_INTERVAL_TURNS = 5
MEMORY_CANDIDATE_EXTRACT_MIN_CHARS = 50_000
MEMORY_CANDIDATE_EXTRACT_MAX_RETRIES = 3
MEMORY_CANDIDATE_MAX_ITEMS = 20
MEMORY_CANDIDATE_MAX_TEXT_CHARS = 500
MEMORY_CANDIDATE_MAX_EVIDENCE_CHARS = 300


PROCESS_NOTE_KIND_BY_ERROR_CODE = {
    "invalid_arguments": "invalid_arguments",
    "repeated_identical_call": "repeated_call",
    "approval_denied": "approval_denied",
    "unknown_tool": "rejected",
    "tool_failed": "error",
}
