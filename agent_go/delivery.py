"""Task-level delivery contract."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any


def _git_ref_exists(repo: str | Path, ref: str) -> bool:
    if not repo or not ref:
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def evaluate_accepted_delivery(meta: dict[str, Any], repo: str | Path | None = None) -> dict[str, Any]:
    """Evaluate the M0-1 Accepted Delivery predicate."""
    reasons: list[str] = []
    results = meta.get("results") or []
    excluded = bool(meta.get("excluded") or meta.get("valid_task") is False)
    if excluded:
        reasons.append("invalid_task")
    if meta.get("status") in {
        "failed", "blocked", "cancelled", "interrupted", "stale_aborted",
        "VERIFICATION_FAILED", "BLOCKED", "CANCELLED", "EXECUTING",
    }:
        reasons.append("task_not_successful")
    if not results:
        reasons.append("no_subtasks")
    if any(r.get("status") not in {"completed", "no_changes"} for r in results):
        reasons.append("incomplete_subtask")
    if any(r.get("verify_ok") is not True for r in results):
        reasons.append("verification_not_passed")
    if meta.get("high_risk_warnings"):
        reasons.append("high_risk_warning")

    # CR-#4：交付产物（commit/分支/PR）只对"实际尝试交付"的生产 run 强制。
    # harness/bench run 从不 push 分支/建 PR（delivery_attempted 缺省），若强制检查会
    # 结构性 accepted_delivery=False——应改由代码正确性（子任务全通过+已验证）判定交付。
    delivery_attempted = bool(meta.get("delivery_attempted"))
    if delivery_attempted:
        commit_hashes = meta.get("commit_hashes") or []
        if not commit_hashes:
            commit_hashes = [r.get("commit_hash") for r in results if r.get("commit_hash")]
        if meta.get("commit_hash"):
            commit_hashes.append(meta["commit_hash"])
        commit_hashes = list(dict.fromkeys(h for h in commit_hashes if h))
        if not commit_hashes:
            reasons.append("missing_commit")
        elif repo and any(not _git_ref_exists(repo, commit_hash) for commit_hash in commit_hashes):
            reasons.append("commit_not_found")

        delivery_branch = meta.get("delivery_branch") or ""
        if not delivery_branch:
            reasons.append("missing_delivery_branch")
        elif repo and not _git_ref_exists(repo, delivery_branch):
            reasons.append("delivery_branch_not_found")

        explicit_merge = meta.get("explicit_merge_commit") or ""
        if explicit_merge and repo and not _git_ref_exists(repo, explicit_merge):
            reasons.append("explicit_merge_not_found")
        if not (meta.get("pr_url") or explicit_merge):
            reasons.append("missing_pr_or_explicit_merge")

        # When GitHub/PR metadata is available, enforce head/base consistency.
        if meta.get("pr_url"):
            target_branch = meta.get("target_branch") or ""
            pr_base = meta.get("pr_base") or ""
            pr_head = meta.get("pr_head") or ""
            if target_branch and pr_base and target_branch != pr_base:
                reasons.append("pr_base_mismatch")
            if delivery_branch and pr_head and delivery_branch != pr_head:
                reasons.append("pr_head_mismatch")

    return {
        "accepted_delivery": not reasons,
        "delivery_failed": bool(meta.get("delivery_attempted")) and bool(reasons) and not excluded,
        "accepted_delivery_reasons": reasons,
    }


def apply_delivery_result(meta: dict[str, Any], repo: str | Path | None = None) -> dict[str, Any]:
    """Persist the delivery decision into task metadata and return it."""
    result = evaluate_accepted_delivery(meta, repo)
    meta.update(result)
    return result
