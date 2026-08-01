"""Independent code-review child runtime.

The review tool starts one fresh CodeMate instance with a review-specific
prompt and tool set. It inherits the parent's approval policy and temporary
permissions, but not the parent's conversation history or memory features.
"""

from __future__ import annotations

import copy
import textwrap
import uuid

from ..tools.constants import SUBAGENT_MAX_STEPS
from ..ui import NullUI
from ..workspace import now
from .errors import ModelRequestError


REVIEW_ALLOWED_TOOLS = {
    "list_files",
    "read_file",
    "grep",
    "run_shell",
    "todo_write",
    "todo_list",
}
REVIEW_EVIDENCE_TOOLS = {"list_files", "read_file", "grep", "run_shell"}

MANUAL_REVIEW_REQUEST = textwrap.dedent(
    """\
    Review the current code changes.

    Use the review tool as the only tool call. If the user supplied a concrete
    review focus, pass it as the optional target. Otherwise omit the target
    instead of inventing one.

    After the review tool returns, independently verify each reported finding
    against the user's requirements, deliberate design decisions, and the actual
    repository code. Use read-only tools when needed to confirm the affected
    path and failure scenario. Present only findings that remain actionable
    after this verification. Treat unsupported, intent-dependent, or
    contradicted findings as unconfirmed rather than established defects. Do
    not modify files unless the user separately asks you to address them. Use
    the language of the surrounding conversation for the target and answer.
    """
).strip()


REVIEW_FINALIZATION_RECOVERY_REQUEST = textwrap.dedent(
    """\
    The previous response was interrupted.

    Do not continue investigating and do not call tools. Using only the
    evidence already collected in this review, return the final review report
    now. Follow the required finding format. If no actionable finding was
    established, say so clearly and mention any remaining validation
    uncertainty.
    """
).strip()


def manual_review_request(focus=""):
    """Build the main-agent request for a manual review command."""
    focus = str(focus or "").strip()
    if not focus:
        return MANUAL_REVIEW_REQUEST
    return (
        f"{MANUAL_REVIEW_REQUEST}\n\n"
        "User-requested review target:\n"
        f"{focus}"
    )


REVIEW_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are a code reviewer working inside a local repository.

    Review the current code changes, investigate the affected code paths,
    validate concrete risks when practical, and return an actionable review.
    Do not modify project source files or implement fixes.

    The current changes are the primary review scope. If you discover a concrete
    pre-existing issue while investigating related code, report it and mark it
    as Pre-existing. Do not expand into unrelated repository areas merely to
    search for additional issues.

    ## Review process

    Follow these phases in order. Adapt the specific files, searches, and
    validation commands to the changes being reviewed.

    ### Phase 1: Inspect the change scope

    - Inspect Git status.
    - Inspect all relevant staged, unstaged, and untracked project changes.
    - Ignore Codemate-generated runtime metadata such as `.codemate/` unless
      the optional review target explicitly concerns Codemate configuration,
      skills, or other `.codemate` contents.
    - Determine which files, interfaces, state transitions, and behavior paths
      are affected.
    - If there are no changes, inspect the optional review target when it
      identifies a concrete scope.
    - If neither changes nor a concrete target are available, report that there
      is nothing to review.

    ### Phase 2: Understand the surrounding behavior

    - Read the complete context around changed code rather than judging isolated
      diff hunks.
    - Inspect relevant callers, callees, shared abstractions, tests,
      configuration, documentation, and similar implementations.
    - Identify existing repository conventions and behavior contracts.
    - Determine whether the change is implemented at the correct ownership
      boundary or merely works around a visible symptom.

    Use repository evidence to determine:

    - What behavior the change appears intended to alter.
    - What related behavior should remain stable.
    - Which defaults, optional parameters, alternate branches, error paths,
      compatibility paths, and state transitions may be affected.
    - Whether the implementation handles the underlying case or only one visible
      example.

    You are not given the original user request. Do not invent missing
    requirements or assume that every previous behavior must remain unchanged.
    When repository evidence is insufficient to determine intent, describe the
    uncertainty instead of reporting a confirmed defect.

    ### Phase 3: Investigate and validate concrete risks

    Check for:

    - Incorrect logic or behavior.
    - Regressions and compatibility problems.
    - Missing boundary handling and broken error paths.
    - Inconsistent state, lifecycle, concurrency, or persistence behavior.
    - Security vulnerabilities.
    - Meaningful performance regressions.
    - Concrete maintainability problems such as duplicated behavior, broken
      abstraction boundaries, or inconsistent use of established project
      patterns.
    - Risks related to the optional review target.

    The optional review target is an investigation focus, not an established
    fact, requirement, or defect. Verify it against the diff and repository
    evidence. Do not restrict the review to the target when other concrete
    issues are found.

    Before reporting a finding:

    - Confirm that the affected execution path is reachable.
    - Check whether upstream validation, nearby code, or downstream recovery
      already handles the case.
    - Check whether tests, documentation, comments, or project conventions
      identify the behavior as intentional.
    - Establish a concrete trigger scenario and practical impact.
    - Do not report an interpretation as a defect when the intended behavior is
      ambiguous and repository evidence does not establish it. Continue
      investigating when practical; if the ambiguity cannot be resolved,
      describe it as an uncertainty rather than an actionable finding.

    Run focused tests, builds, static checks, or minimal behavior reproductions
    when they materially improve confidence. Use available tools and follow the
    active permission policy. Do not intentionally modify project source files.

    ### Phase 4: Filter and report findings

    Do not report:

    - Subjective style preferences.
    - Unsupported speculation.
    - Theoretical concerns without a concrete trigger or impact.
    - Vague requests for additional tests without identifying missing behavior.
    - Issues unrelated to the reviewed changes and their surrounding code.

    A pre-existing issue may still be reported when it is concrete, reachable,
    actionable, and directly related to the code path being investigated.

    Return normal Markdown. Put findings first and order them by severity.

    For every finding:

    - Include a concise priority-bearing title.
    - Identify the file and line or function.
    - Explain the concrete trigger scenario.
    - Explain the resulting behavior and impact.
    - Give a concise correction direction when useful.
    - Use the `Pre-existing` label when the issue was not introduced by the
      current changes.

    Good finding:

    ### [P1] Restoring saved state overwrites a newer policy
    `path/to/runtime.py:120`

    Resuming the operation restores a policy captured before the user changed
    the current setting. The stale value therefore replaces the newer policy.
    Restore from the current authoritative state or avoid persisting this value.

    Good pre-existing finding:

    ### [Pre-existing][P2] Cancelled operations retain active task state
    `path/to/tasks.py:85`

    The cancellation path clears the operation but leaves its active task record
    available to subsequent requests. Clear both pieces of state in the same
    lifecycle transition.

    Do not produce findings such as:

    - This function is too long.
    - There may be a race condition.
    - More tests should be added.
    - The naming could be clearer.

    If no actionable issues are found, say so directly. Then briefly identify
    any meaningful validation gaps or remaining uncertainty.
    """
).strip()


class _ReviewChildUI(NullUI):
    """Forward review progress and compact tool events to the parent UI."""

    def __init__(self, parent_agent):
        self.parent_agent = parent_agent
        self.parent_ui = parent_agent.ui
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

    def approval_request(self, name, args, metadata=None):
        """Bubble child approvals and persist session grants in the parent."""
        callback = getattr(self.parent_ui, "approval_request", None)
        if not callable(callback):
            return {"allowed": False}
        decision = callback(name, args, metadata=metadata)
        if (
            isinstance(decision, dict)
            and decision.get("allowed")
            and decision.get("remember")
        ):
            self.parent_agent.add_temporary_approval(decision["remember"])
        return decision


def review_system_prompt():
    """Return the stable system prompt used by every review child."""
    return REVIEW_SYSTEM_PROMPT


def _review_request(target=""):
    target = str(target or "").strip()
    target_text = target or (
        "No specific target was provided. Perform a general review of the current changes."
    )
    return textwrap.dedent(
        f"""\
        Optional review target:

        {target_text}

        Review all relevant current staged, unstaged, and untracked project
        changes. Follow the required review phases and return the final review.
        """
    ).strip()


def _review_child_session(agent, target):
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
        "review_target": target,
    }


def _has_collected_review_evidence(child):
    """Return whether a failed review already recorded investigation results."""
    for item in child.session.get("history", []):
        if item.get("role") != "tool" or item.get("name") not in REVIEW_EVIDENCE_TOOLS:
            continue
        content = str(item.get("content", "") or "").lstrip().lower()
        if not content.startswith("status: rejected"):
            return True
    return False


def run_review(agent, target=""):
    """Run one serial review under the parent's effective approval policy."""
    from .agent import CodeMate

    target = str(target or "").strip()
    start = getattr(agent.ui, "review_start", None)
    if callable(start):
        start()

    child = None
    status = "error"
    report = ""
    error = ""
    recovery_attempted = False
    recovery_succeeded = False
    try:
        fork_model_client = getattr(agent.model_client, "fork", None)
        model_client = fork_model_client() if callable(fork_model_client) else agent.model_client
        child_depth = agent.depth + 1
        child = CodeMate(
            model_client=model_client,
            workspace=agent.workspace,
            session_store=agent.session_store,
            session=_review_child_session(agent, target),
            approval_policy=agent.approval_policy,
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
            ui=_ReviewChildUI(agent),
            allowed_tools=REVIEW_ALLOWED_TOOLS,
            runtime_mode="review",
            stream=False,
            timezone_name=getattr(agent, "timezone_name", "Asia/Shanghai"),
        )
        try:
            report = child.ask(_review_request(target))
        except ModelRequestError:
            if not _has_collected_review_evidence(child):
                raise
            recovery_attempted = True
            # The retry is a report-only pass over the existing child history.
            # Removing the registry enforces the no-tool recovery boundary.
            child.allowed_tools = set()
            child.tools = {}
            report = child.ask(REVIEW_FINALIZATION_RECOVERY_REQUEST)
            recovery_succeeded = True
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
            "review_recovery_attempted": recovery_attempted,
            "review_recovery_succeeded": recovery_succeeded,
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
