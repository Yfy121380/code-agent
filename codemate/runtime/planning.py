"""Plan Mode state transitions and model instructions.

Plan Mode is a workflow state layered on top of the existing approval system.
It temporarily forces read-only execution, exposes a planning-specific tool
set, and restores the previous approval policy after approval or cancellation.
"""

from __future__ import annotations

import uuid

from ..tools.constants import PLAN_INTERACTION_TOOLS
from ..workspace import now


AGENT_MODE = "agent"
PLAN_MODE = "plan"
PLAN_STATUSES = {"drafting", "awaiting_approval", "approved"}
PLAN_VISIBLE_TOOLS = {
    "list_files",
    "read_file",
    "grep",
    "run_shell",
    "web_search",
    "web_extract",
    "web_research",
    "todo_write",
    "todo_list",
    "skill_load",
    "delegate",
    *PLAN_INTERACTION_TOOLS,
}

PLAN_TOOL_DESCRIPTION_OVERRIDES = {
    "run_shell": """Run a read-only shell command to inspect the repository.

Use this for repository information that dedicated read tools do not provide conveniently, such as git status, git log, tracked-file metadata, or read-only project configuration queries.

Do not use this tool for tests, builds, formatters, package installation, code generation, file writes, or commands that may modify the repository or environment. Prefer list_files, read_file, and grep for ordinary file inspection.""",
    "todo_write": """Create or update a Todo list for a complex planning investigation.

Use Todo only to track meaningful investigation phases such as locating the responsible subsystem, comparing related behavior, resolving an interface contract, and preparing the proposal. It is not the implementation plan submitted to the user.

The call replaces the complete Todo list. Include every still-relevant phase and task, keep statuses accurate, allow at most one in-progress phase and one in-progress task within it, and mark the full Todo complete when the investigation is finished. Do not use Todo for simple planning requests.""",
    "todo_list": """Read the active Todo list for the planning investigation.

Use this only when the current investigation phases and progress are no longer visible. It does not return or submit the formal implementation plan, and it does not modify the Todo list.""",
}


PLAN_MODE_PROMPT = """## Tool use

- Use list_files, read_file, and grep to inspect the local repository.
- Use web tools only when current external information or official documentation is needed.
- Use skill_load when an available skill clearly applies to the planning task.
- Use delegate only when a focused read-only investigation would materially reduce broad or noisy exploration.
- Use run_shell only for commands whose purpose and effects are read-only.
- Do not use run_shell for tests, builds, formatters, package installation, code generation, or commands that may modify the repository or environment.
- Use todo_write only when the planning investigation has multiple meaningful phases. The Todo list tracks investigation work; it is not the plan submitted for approval.
- Use todo_list only when the current investigation Todo is no longer visible.
- request_user_input and submit_plan are interactive boundary tools and must each be the only tool call in a response; never call either in parallel with another tool, and wait for its result before choosing the next action.
- Do not repeat reads, searches, or tool calls merely to reconfirm information already available.

## Planning workflow

- Start by grounding the request in the actual repository.
- Inspect the relevant implementation, callers, tests, configuration, documentation, and similar patterns needed to understand the behavior.
- Scale investigation to risk. Narrow mechanical changes require limited exploration; shared behavior, public APIs, persistence, permissions, compatibility, and unclear bugs require broader investigation.
- Treat examples, error messages, and reported symptoms as evidence, not necessarily as the complete specification.
- Determine the intended behavior, current behavior, affected ownership boundaries, related behavior that must remain unchanged, and repository conventions that constrain the solution.
- Resolve repository facts with tools. Separate discoverable facts from product preferences and implementation tradeoffs.
- Continue until the goal, success criteria, scope, implementation approach, interfaces, data flow, failure behavior, compatibility expectations, and validation strategy are sufficiently decided.

## User decisions

- Use request_user_input only when the answer cannot be established from the repository and would materially change behavior, scope, compatibility, or architecture.
- Do not ask the user for file locations, existing behavior, configuration, or other facts that can be discovered with tools.
- Prefer one focused question. Ask two or three together only when they are closely related and answering them together avoids unnecessary back-and-forth.
- Provide two or three mutually exclusive options, put the recommended option first, and explain the practical impact of each option briefly.
- Do not guess when the user cancels a decision that is required to complete the plan.

## Progress updates

- Use commentary to report useful planning progress during substantial work.
- After a meaningful investigation phase, summarize the important finding, concrete repository evidence, why it affects the design, and what remains to be resolved.
- Preserve important findings in commentary during broad investigations because older read and search results may later be cleared from context.
- Do not narrate every trivial read, search, or command.

## Plan quality and submission

A good plan is a decision-complete implementation guide grounded in repository evidence and confirmed user requirements. It explains the intended behavior, where each change belongs, how related components interact, what must remain unchanged, and how correctness will be verified.

- Resolve significant design choices before submission. Do not leave vague instructions such as "choose an appropriate approach", "handle as needed", or "update related code".
- Identify the subsystem responsible for each behavior and describe important control flow or data flow between related components.
- Describe changed interfaces and contracts precisely when relevant, including tool schemas, commands, session fields, persisted data, return values, state transitions, and failure behavior.
- Preserve existing defaults, alternate branches, compatibility paths, permission boundaries, and error behavior unless the request changes them.
- Order dependent changes coherently.
- Define validation through observable behavior, covering both the intended change and important adjacent behavior that must remain stable.
- Include necessary supporting work, but exclude unrelated refactors, speculative improvements, exploration logs, and obvious mechanical steps.
- Mention concrete files, functions, or types when they remove implementation ambiguity, not merely to create a file inventory.
- Begin the plan with a clear Markdown level-one title.
- Organize the body under meaningful Markdown level-two headings chosen for the task. Headings should represent behaviors or subsystems rather than fixed generic fields.

Before calling submit_plan, verify that another coding agent could implement the plan without making a significant unresolved decision, that important interfaces and preserved behavior are explicit, that validation demonstrates the requested behavior, that required user decisions have been resolved, and that every included change is necessary.

Call submit_plan only when these conditions are satisfied. Do not return the formal implementation plan as an ordinary final answer.

## Answer rules

- Never invent repository facts, tool results, test results, or user decisions.
- Respond in the same language as the user's request unless the user explicitly asks for another language. Use Chinese for Chinese requests and English for English requests.
- Ordinary final answers in Plan Mode are reserved for direct explanations, genuine blockers, cancelled planning, or questions that cannot be represented by request_user_input.
- Keep explanations focused on the current planning discussion."""


class PlanModeMixin:
    """Maintain the single active plan and its approval-policy boundary."""

    def is_plan_mode(self):
        return self.session.get("workflow_mode") == PLAN_MODE

    def _activate_workflow_context(self):
        mode = PLAN_MODE if self.is_plan_mode() else AGENT_MODE
        self.prefix_state = self.prefix_states[mode]
        self.prefix = self.prefix_state.text

    def enter_plan_mode(self):
        if self.is_plan_mode():
            return False
        # Starting a new planning workflow explicitly replaces any previously
        # approved plan retained for continuation.
        with self._session_lock:
            self.session["workflow_mode"] = PLAN_MODE
            self.session["plan"] = {
                "status": "drafting",
                "title": "",
                "content": "",
                "revision_feedback": "",
                "previous_approval_policy": self.approval_policy,
                "updated_at": now(),
            }
            self.approval_policy = "read_only"
            self._activate_workflow_context()
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)
        return True

    def _invalidate_planning_todos(self, *, defer_context=False):
        """Clear active planning Todos while preserving an auditable history."""
        if not self.session.get("todos"):
            return
        self.session["todos"] = []
        context_id = f"context_{uuid.uuid4().hex[:12]}"
        message = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "conversation_id": context_id,
            "role": "user",
            "kind": "todo_invalidated_context",
            "content": (
                "The active planning Todo became invalid because the plan was cancelled or exited. "
                "Do not continue those Todo items."
            ),
            "created_at": now(),
        }
        if defer_context:
            pending = getattr(self, "_pending_internal_context_messages", None)
            if pending is None:
                pending = []
                self._pending_internal_context_messages = pending
            pending.append(message)
        else:
            self.session["history"].append(message)

    def flush_pending_internal_context(self):
        """Persist deferred context only after the current tool result is paired."""
        pending = list(getattr(self, "_pending_internal_context_messages", []) or [])
        self._pending_internal_context_messages = []
        for message in pending:
            self.record(message)

    def begin_plan_submission(self, title, content):
        with self._session_lock:
            if not self.is_plan_mode():
                raise ValueError("submit_plan is only available in Plan Mode")
            plan = self.session.get("plan")
            if not isinstance(plan, dict):
                raise ValueError("Plan Mode has no active plan state")
            plan.update(
                {
                    "status": "awaiting_approval",
                    "title": str(title).strip(),
                    "content": str(content).strip(),
                    "revision_feedback": "",
                    "updated_at": now(),
                }
            )
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)

    def approve_plan(self):
        with self._session_lock:
            plan = self.session.get("plan")
            if not self.is_plan_mode() or not isinstance(plan, dict):
                raise ValueError("there is no plan awaiting approval")
            if plan.get("status") != "awaiting_approval":
                raise ValueError("the current plan is not awaiting approval")
            plan["status"] = "approved"
            plan["updated_at"] = now()
            self.session["workflow_mode"] = AGENT_MODE
            self.approval_policy = str(plan.get("previous_approval_policy") or "ask")
            self._activate_workflow_context()
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)

    def revise_plan(self, feedback=""):
        with self._session_lock:
            plan = self.session.get("plan")
            if not self.is_plan_mode() or not isinstance(plan, dict):
                raise ValueError("there is no plan to revise")
            if plan.get("status") != "awaiting_approval":
                raise ValueError("the current plan is not awaiting revision")
            plan["status"] = "drafting"
            plan["revision_feedback"] = str(feedback or "").strip()
            plan["updated_at"] = now()
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)

    def exit_plan_mode(self, *, defer_context=False):
        with self._session_lock:
            plan = self.session.get("plan")
            was_plan_mode = self.is_plan_mode()
            has_retained_plan = isinstance(plan, dict) and plan.get("status") == "approved"
            if not was_plan_mode and not has_retained_plan:
                return False
            previous = plan.get("previous_approval_policy", "ask") if isinstance(plan, dict) else "ask"
            self._invalidate_planning_todos(defer_context=defer_context)
            self.session["workflow_mode"] = AGENT_MODE
            self.session["plan"] = None
            if was_plan_mode:
                self.approval_policy = str(previous or "ask")
            self._activate_workflow_context()
            self.session["updated_at"] = now()
            self.session_path = self.session_store.save(self.session)
        return True
