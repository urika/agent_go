"""Stable failure classes and policy for M0-3."""

from __future__ import annotations

from typing import Any


FAILURE_CLASSES = frozenset({
    "model_failure",
    "verification_failure",
    "timeout",
    "budget_abort",
    "infrastructure_failure",
    "delivery_failure",
    "user_cancelled",
    "system_error",
})

# These are intentionally not renamed in persisted data.  They remain useful
# low-level evidence while ``failure_class`` is the stable aggregation key.
KILL_REASON_CLASS = {
    "over_budget_l1": "budget_abort",
    "over_budget_l2": "budget_abort",
    "over_budget_l3": "budget_abort",
    "stuck": "timeout",
    "hard_timeout": "timeout",
    "goal_timeout": "timeout",
    "goal_turns_exceeded": "timeout",
    "stuck_or_hardtimeout": "timeout",
    "infra": "infrastructure_failure",
    "metering_unavailable": "infrastructure_failure",
    "cleanup_failure": "infrastructure_failure",
    "system_error": "system_error",
    "user_cancelled": "user_cancelled",
    "cancelled": "user_cancelled",
    "delivery_failed": "delivery_failure",
    "interrupted_or_unknown": "system_error",
}

# Capability denominator excludes failures where the harness or user, rather
# than the model, prevented a valid capability observation.
FAILURE_POLICY = {
    "model_failure": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "verification_failure": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "timeout": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "budget_abort": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "infrastructure_failure": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "delivery_failure": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "user_cancelled": {"capability_failure": False, "cost_included": True, "resume_allowed": False, "preserve_worktree": True},
    "system_error": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
}

_CLASS_PRIORITY = (
    "user_cancelled", "budget_abort", "timeout", "infrastructure_failure",
    "system_error", "delivery_failure", "verification_failure", "model_failure",
)


def failure_policy(failure_class: str | None) -> dict[str, Any]:
    """Return policy flags for a class, or safe defaults for no failure."""
    if failure_class in FAILURE_POLICY:
        return dict(FAILURE_POLICY[failure_class])
    return {"capability_failure": False, "cost_included": True, "resume_allowed": False, "preserve_worktree": False}


def classify_failure(
    result: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    *,
    timed_out: bool = False,
) -> str | None:
    """Map runtime evidence to one stable failure class."""
    result = result or {}
    meta = meta or {}
    explicit = result.get("failure_class") or meta.get("failure_class")
    if explicit in FAILURE_CLASSES:
        return explicit
    if meta.get("delivery_failed"):
        return "delivery_failure"
    kill_reason = result.get("kill_reason") or meta.get("kill_reason")
    if kill_reason in KILL_REASON_CLASS:
        return KILL_REASON_CLASS[kill_reason]
    if timed_out:
        return "timeout"
    if kill_reason == "none" or result.get("status") in {"completed", "no_changes"}:
        return None
    if result.get("status") == "blocked":
        # A blocked task is an orchestration failure unless a more specific
        # budget/infrastructure reason was already supplied above.
        return "system_error"
    if result.get("status") == "failed":
        if result.get("verify_ok") is False:
            return "verification_failure"
        if result.get("exit_code") not in (None, 0):
            return "model_failure"
        return "system_error"
    return None


def aggregate_failure_class(classes: list[str | None], meta: dict[str, Any] | None = None) -> str | None:
    """Choose a deterministic task-level class from subtask evidence."""
    meta = meta or {}
    explicit = meta.get("failure_class")
    if explicit in FAILURE_CLASSES:
        return explicit
    if meta.get("delivery_failed"):
        return "delivery_failure"
    present = set(c for c in classes if c in FAILURE_CLASSES)
    for failure_class in _CLASS_PRIORITY:
        if failure_class in present:
            return failure_class
    return None
