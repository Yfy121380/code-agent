"""Independent code-review child runtime.

The review tool starts one fresh read-only CodeMate instance. The child sees a
review-specific system prompt and the explicit review objective, but none of
the parent's conversation history or memory-maintenance features.
"""

from __future__ import annotations

import copy
import textwrap
import uuid

from ..tools.constants import SUBAGENT_MAX_STEPS
from ..ui import NullUI
from ..workspace import now


REVIEW_ALLOWED_TOOLS = {
    "list_files",
    "read_file",
    "grep",
    "run_shell",
    "todo_write",
    "todo_list",
}

MANUAL_REVIEW_REQUEST = textwrap.dedent(
    """\
    Review the current code changes.

    Use the review tool as the only tool call. Build a concrete review task from
    the requirements, approved plan, and implementation context already present
    in this conversation. Include the intended behavior, important constraints,
    deliberate interface or behavior changes, behavior explicitly required to
    remain stable, and relevant concerns. Do not invent preservation
    requirements merely because an old behavior exists. If the original
    implementation intent is unavailable, say so in the review task.

    After the review tool returns, independently verify each reported finding
    against the user's requirements, deliberate design decisions, and the actual
    repository code. Use read-only tools when needed to confirm the affected
    path and failure scenario. Present only findings that remain actionable
    after this verification. Treat unsupported, intent-dependent, or
    contradicted findings as unconfirmed rather than established defects. Do
    not modify files unless the user separately asks you to address them. Use
    the language of the surrounding conversation for the review task and answer.
    """
).strip()


def manual_review_request(focus=""):
    """Build the main-agent request for a manual review command."""
    focus = str(focus or "").strip()
    if not focus:
        return MANUAL_REVIEW_REQUEST
    return (
        f"{MANUAL_REVIEW_REQUEST}\n\n"
        "User-requested review focus:\n"
        f"{focus}"
    )


REVIEW_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an independent code reviewer working inside a local repository.

    Review the current code changes against the objective provided in the user
    request. Investigate the repository thoroughly enough to understand the
    affected subsystem, identify concrete issues, and return an actionable
    review. Do not modify files or implement fixes.

    ## Investigation

    - Begin by inspecting Git status and all staged, unstaged, and untracked changes.
    - Use run_shell only for read-only repository inspection such as Git status,
      diff, log, and metadata queries.
    - Do not draw conclusions from an isolated diff hunk.
    - Understand the relevant subsystem and behavior contract before judging the implementation.
    - Inspect related classes, functions, callers, callees, shared abstractions,
      state transitions, tests, configuration, documentation, and similar implementations.
    - Determine what should change and what existing behavior must remain stable.
    - Check whether the change belongs at the chosen ownership boundary or merely
      works around a visible symptom.
    - Scale investigation to the reviewed change, but do not stop at the first
      plausible explanation or issue.
    - Before reporting a finding, confirm that the execution path is reachable
      and nearby code does not already handle it.

    ## Progress updates

    - Use commentary to report meaningful review progress.
    - After substantial investigation phases, summarize the evidence, its
      implications, and what will be checked next.
    - Do not emit commentary before every simple read or search.
    - Preserve important findings in commentary because old tool results may
      later be cleared.

    ## Findings

    Report concrete, actionable issues affecting correctness, security,
    performance, or maintainability.

    Current changes are the primary scope. You may also report pre-existing
    issues when they are concrete and directly related to the reviewed
    subsystem. Mark them as `Pre-existing`.

    Do not report unsupported speculation, subjective style preferences,
    ordinary lint findings, vague test requests, or unrelated repository
    problems. Prefer no finding over low-signal feedback.

    Do not assume every previous interface or behavior must remain unchanged.
    A compatibility finding must be grounded in the review objective, repository
    contract, or a concrete caller that still depends on the old behavior. If a
    concern depends on whether a change was deliberate and the objective does
    not establish that intent, report it as uncertainty rather than a confirmed
    actionable finding.

    ## Output

    Return normal Markdown in the language used by the review objective. Put
    findings first and order them by severity.

    For each finding, identify the file and line or function, explain the
    concrete failure scenario and impact, and provide a concise correction
    direction when useful.

    If no actionable issues are found, say so directly and briefly, then mention
    meaningful validation gaps or remaining uncertainty.

    Good finding:

    ### [P1] Approval restoration can overwrite a newer policy
    `codemate/runtime/planning.py:...`

    Resuming an approved plan restores the stale policy saved when Plan Mode
    began. Use one authoritative source during restoration.

    Good pre-existing finding:

    ### [Pre-existing][P2] Cancelled plan todos remain active
    `codemate/tools/handlers.py:...`

    This predates the current patch but directly affects the reviewed plan
    lifecycle. Cancelling clears the plan while leaving its active todo state
    available to later requests.

    Bad findings:

    - This function is too long.
    - There may be a race condition.
    - More tests should be added.
    - The naming could be clearer.
    """
).strip()


class _ReviewChildUI(NullUI):
    """Forward review progress and compact tool events to the parent UI."""

    def __init__(self, parent_ui):
        self.parent_ui = parent_ui
        self._started_tools = []

    def commentary(self, text):
        callback = getattr(self.parent_ui, "commentary", None)
        if callable(callback):
            callback(text)

    def tool_start(self, name, args, risk_level=""):
        self._started_tools.append((name, args))
        callback = getattr(self.parent_ui, "tool_start", None)
        if callable(callback):
            callback(name, args, risk_level=risk_level)

    def tool_result(self, name, args, result, metadata=None):
        started = next(
            (index for index, item in enumerate(self._started_tools) if item == (name, args)),
            None,
        )
        if started is None:
            # Validation and policy rejection happen before the normal start
            # event. Show the attempted call so the following rejection has
            # enough context to be useful.
            start = getattr(self.parent_ui, "tool_start", None)
            if callable(start):
                start(name, args, risk_level=str((metadata or {}).get("risk_level", "")))
        else:
            self._started_tools.pop(started)
        callback = getattr(self.parent_ui, "tool_result", None)
        if callable(callback):
            callback(name, args, result, metadata=metadata)


def review_system_prompt():
    """Return the stable system prompt used by every review child."""
    return REVIEW_SYSTEM_PROMPT


def _review_request(task):
    return textwrap.dedent(
        f"""\
        Review objective:
        {task}

        Review the current staged, unstaged, and untracked changes against this
        objective. Investigate the relevant surrounding code and return the
        final review.
        """
    ).strip()


def _review_child_session(agent, task):
    timestamp = now()
    return {
        "id": f"review-{agent.session['id']}-{uuid.uuid4().hex[:6]}",
        "created_at": timestamp,
        "updated_at": timestamp,
        "title": "code review",
        "title_slug": "code-review",
        "workspace_root": agent.workspace.repo_root,
        "history": [],
        "history_summary": "",
        "read_files": {},
        "todos": [],
        "invoked_skills": [],
        "temporary_permissions": copy.deepcopy(agent.session.get("temporary_permissions", {})),
        "review_parent_session": agent.session.get("id", ""),
        "review_task": task,
    }


def run_review(agent, task):
    """Run one serial read-only review and return a report for the parent agent."""
    from .agent import CodeMate

    task = str(task or "").strip()
    start = getattr(agent.ui, "review_start", None)
    if callable(start):
        start()

    child = None
    status = "error"
    report = ""
    error = ""
    try:
        fork_model_client = getattr(agent.model_client, "fork", None)
        model_client = fork_model_client() if callable(fork_model_client) else agent.model_client
        child_depth = agent.depth + 1
        child = CodeMate(
            model_client=model_client,
            workspace=agent.workspace,
            session_store=agent.session_store,
            session=_review_child_session(agent, task),
            approval_policy="read_only",
            max_steps=SUBAGENT_MAX_STEPS,
            max_new_tokens=agent.max_new_tokens,
            depth=child_depth,
            max_depth=child_depth,
            secret_env_names=agent.secret_env_names,
            shell_env_allowlist=agent.shell_env_allowlist,
            feature_flags={
                **agent.feature_flags,
                "long_term_memory": False,
                "relevant_memory": False,
                "memory_candidates": False,
                "memory_dream": False,
                "session_title": False,
            },
            ui=_ReviewChildUI(agent.ui),
            allowed_tools=REVIEW_ALLOWED_TOOLS,
            runtime_mode="review",
            stream=False,
            timezone_name=getattr(agent, "timezone_name", "Asia/Shanghai"),
        )
        report = child.ask(_review_request(task))
        stop_reason = str(getattr(child.current_task_state, "stop_reason", "") or "")
        if stop_reason == "step_limit_reached":
            status = "step_limit"
            error = f"review reached the {SUBAGENT_MAX_STEPS}-step limit without a final report"
            report = ""
        elif stop_reason == "retry_limit_reached":
            status = "error"
            error = "review stopped after too many malformed model responses"
            report = ""
        else:
            status = "ok"
    except Exception as exc:
        error = str(exc)
    finally:
        if child is not None:
            child.close()
        metadata = {
            "review_status": status,
            "review_report_chars": len(report),
            "review_session_id": child.session.get("id", "") if child is not None else "",
            "review_run_dir": str(getattr(child, "current_run_dir", "") or "") if child is not None else "",
        }
        agent._last_review_metadata = metadata
        end = getattr(agent.ui, "review_end", None)
        if callable(end):
            end(status=status, metadata=metadata)

    lines = [f"review_status: {status}"]
    if status == "ok":
        lines.extend(["review_report:", report.strip() or "(empty)"])
    else:
        lines.extend(["error:", error or "unknown review error"])
    return "\n".join(lines)
