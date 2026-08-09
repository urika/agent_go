"""Task-level delivery contract."""

from __future__ import annotations

import shutil
import tempfile
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


def create_delivery_branch(
    repo: str | Path,
    task_id: str,
    base_commit: str,
    results: list[dict[str, Any]],
) -> tuple[bool, str, list[str]]:
    """Create a delivery branch aggregating successful subtask commits.

    Returns (success: bool, delivery_branch_name: str, error: str).
    The delivery branch is named ``agent_go/{task_id}/delivery`` and anchored
    at ``base_commit``. Each successful subtask's commit is merged into it in
    topological order (as recorded in ``results``). A temporary worktree is
    used for the merges so the main checkout is never touched.
    """
    repo_path = Path(repo)
    branch = f"agent_go/{task_id}/delivery"

    # Collect successful subtask commit hashes in order.
    commits: list[str] = []
    for r in results:
        if r.get("status") in ("completed", "no_changes") and r.get("commit_hash"):
            commits.append(r["commit_hash"])
    commits = list(dict.fromkeys(commits))

    # Create the branch anchored at base_commit (force-refresh to be idempotent).
    branch_ok = _create_or_reset_branch(repo_path, branch, base_commit)
    if not branch_ok:
        return False, branch, "无法创建 delivery branch（rev-parse/branch 失败）"

    if not commits:
        return True, branch, ""

    # Merge each subtask commit into the delivery branch via a temp worktree.
    tmp = Path(tempfile.mkdtemp(prefix=f"agent_go_delivery_{task_id}_"))
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp), base_commit],
            cwd=str(repo_path), capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            return False, branch, f"无法创建 delivery worktree: {add.stderr.strip()[:200]}"

        for c in commits:
            # Reset the temp worktree to the delivery branch tip before merging.
            reset = subprocess.run(
                ["git", "reset", "--hard", branch],
                cwd=str(tmp), capture_output=True, text=True, timeout=30,
            )
            if reset.returncode != 0:
                return False, branch, f"delivery worktree reset 失败: {reset.stderr.strip()[:200]}"
            merge = subprocess.run(
                ["git", "merge", "--no-ff", "-m", f"agent_go: merge subtask {c[:12]}", c],
                cwd=str(tmp), capture_output=True, text=True, timeout=60,
            )
            if merge.returncode != 0:
                # Conflict: abort this merge and stop aggregating further.
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(tmp), capture_output=True, text=True, timeout=30,
                )
                return False, branch, (
                    f"delivery merge 冲突（{c[:12]}）: {merge.stderr.strip()[:200]}。"
                    "已停止汇总，可手动处理冲突后重试。"
                )
            # Advance the delivery branch to the merged commit.
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(tmp), capture_output=True, text=True, timeout=10,
            )
            if head.returncode == 0:
                subprocess.run(
                    ["git", "branch", "-f", branch, head.stdout.strip()],
                    cwd=str(repo_path), capture_output=True, text=True, timeout=10,
                )
        return True, branch, ""
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tmp)],
            cwd=str(repo_path), capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(tmp, ignore_errors=True)


def _create_or_reset_branch(repo: Path, branch: str, base: str) -> bool:
    """Create a branch at ``base`` or force-reset an existing one."""
    for args in (
        ["git", "rev-parse", "--verify", base],
        ["git", "branch", "-f", branch, base],
    ):
        r = subprocess.run(args, cwd=str(repo), capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False
    return True
