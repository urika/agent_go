"""Canonical task state machine for M0-2.

Subtask result statuses remain intentionally separate.  This module only
normalizes the task-level ``meta.json.status`` field.
"""

from __future__ import annotations

from typing import Any


TASK_STATES = frozenset({
    "DRAFT", "SPEC_REVIEW", "ARCHITECTURE_REVIEW", "PLAN_REVIEW",
    "EXECUTING", "VERIFYING", "COMMITTED_UNVERIFIED", "DELIVERY_READY",
    "ACCEPTED_DELIVERY", "VERIFICATION_FAILED", "DELIVERY_FAILED",
    "BLOCKED", "CANCELLED",
})
STATUS_SCHEMA_VERSION = 1

LEGACY_STATUS_MAP = {
    "draft": "DRAFT",
    "spec_review": "SPEC_REVIEW",
    "architecture_review": "ARCHITECTURE_REVIEW",
    "plan_review": "PLAN_REVIEW",
    "running": "EXECUTING",
    "executing": "EXECUTING",
    "verifying": "VERIFYING",
    "committed_unverified": "COMMITTED_UNVERIFIED",
    "completed": "DELIVERY_READY",
    "failed": "VERIFICATION_FAILED",
    "verification_failed": "VERIFICATION_FAILED",
    "delivery_failed": "DELIVERY_FAILED",
    "blocked": "BLOCKED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "paused": "PLAN_REVIEW",
    "interrupted": "EXECUTING",
    "stale_aborted": "EXECUTING",
}


def normalize_task_status(status: str | None, meta: dict[str, Any] | None = None) -> str:
    """Return a canonical task state, using delivery metadata when available."""
    value = (status or "DRAFT").strip()
    if value in TASK_STATES:
        return value
    meta = meta or {}
    if meta.get("accepted_delivery") is True:
        return "ACCEPTED_DELIVERY"
    if meta.get("delivery_failed") is True:
        return "DELIVERY_FAILED"
    return LEGACY_STATUS_MAP.get(value.lower(), "DRAFT")


def set_task_status(meta: dict[str, Any], status: str) -> str:
    """Set a canonical status and retain a legacy value only when migrating."""
    canonical = status if status in TASK_STATES else LEGACY_STATUS_MAP.get(status.lower())
    if canonical is None:
        raise ValueError(f"invalid task status: {status}")
    if canonical not in TASK_STATES:
        raise ValueError(f"invalid task status: {status}")
    previous = meta.get("status")
    if previous and previous not in TASK_STATES and previous != canonical:
        meta.setdefault("legacy_status", previous)
    meta["status"] = canonical
    meta["status_schema_version"] = STATUS_SCHEMA_VERSION
    return canonical


def migrate_meta_status(meta: dict[str, Any]) -> str:
    """Normalize an old meta dictionary in place and return its state."""
    return set_task_status(meta, normalize_task_status(meta.get("status"), meta))


def task_status(meta: dict[str, Any]) -> str:
    """Read a task state without mutating metadata."""
    raw = meta.get("status")
    # Old task directories remain readable until an explicit migration writes
    # status_schema_version; new tasks always use canonical states.
    if not meta.get("status_schema_version"):
        return raw if raw else "unknown"
    return normalize_task_status(raw, meta)
