"""Validation and formatting helpers for the structured todo plan."""

from .constants import TODO_STATUSES


def normalize_todos(raw_todos):
    """Validate a complete todo replacement and return canonical fields."""
    if not isinstance(raw_todos, list):
        raise ValueError("todos must be a list")
    todos = []
    in_progress_phases = 0
    for phase_index, item in enumerate(raw_todos):
        if not isinstance(item, dict):
            raise ValueError(f"todo phase at index {phase_index} must be an object")
        phase = str(item.get("phase", "")).strip()
        if not phase:
            raise ValueError(f"todo phase at index {phase_index} phase must not be empty")
        status = str(item.get("status", "")).strip()
        if status not in TODO_STATUSES:
            raise ValueError(
                f"todo phase at index {phase_index} status must be one of: pending, in_progress, completed"
            )
        if status == "in_progress":
            in_progress_phases += 1
        raw_tasks = item.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ValueError(f"todo phase at index {phase_index} tasks must be a list")

        tasks = []
        in_progress_tasks = 0
        for task_index, task in enumerate(raw_tasks):
            if not isinstance(task, dict):
                raise ValueError(f"todo task at phase {phase_index}, index {task_index} must be an object")
            description = str(task.get("description", "")).strip()
            if not description:
                raise ValueError(
                    f"todo task at phase {phase_index}, index {task_index} description must not be empty"
                )
            task_status = str(task.get("status", "")).strip()
            if task_status not in TODO_STATUSES:
                raise ValueError(
                    f"todo task at phase {phase_index}, index {task_index} "
                    "status must be one of: pending, in_progress, completed"
                )
            if task_status == "in_progress":
                in_progress_tasks += 1
            if status == "pending" and task_status != "pending":
                raise ValueError("pending phase cannot contain completed or in_progress tasks")
            if status == "completed" and task_status != "completed":
                raise ValueError("completed phase cannot contain pending or in_progress tasks")
            if task_status == "in_progress" and status != "in_progress":
                raise ValueError("phase must be in_progress when one of its tasks is in_progress")
            tasks.append({"description": description, "status": task_status})
        if in_progress_tasks > 1:
            raise ValueError("at most one task may be in_progress within the same phase")
        todos.append({"phase": phase, "status": status, "tasks": tasks})
    if in_progress_phases > 1:
        raise ValueError("at most one phase may be in_progress")
    return todos


def format_todo_plan(todos, heading="Active todo plan:"):
    """Render the complete plan for model-visible tool and compact context."""
    todos = list(todos or [])
    if not todos:
        return "No active todo plan."
    lines = [heading]
    for index, phase in enumerate(todos, 1):
        lines.append(f"{index}. [{phase['status']}] {phase['phase']}")
        for task in phase.get("tasks") or []:
            lines.append(f"   - [{task['status']}] {task['description']}")
    return "\n".join(lines)
