# 记忆系统常量：控制工作记忆容量和过程笔记 TTL。

WORKING_FILE_LIMIT = 12
FILE_SUMMARY_LIMIT = 6
PROCESS_NOTE_LIMIT = 6
PROCESS_NOTE_TTL_TURNS = 3


PROCESS_NOTE_KIND_BY_ERROR_CODE = {
    "invalid_arguments": "invalid_arguments",
    "repeated_identical_call": "repeated_call",
    "approval_denied": "approval_denied",
    "unknown_tool": "rejected",
    "tool_failed": "error",
    "tool_partial_success": "partial_success",
}
