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
) -> tuple[bool, str, str]:
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


def check_mergeability(
    repo: str | Path,
    delivery_branch: str,
    target_branch: str,
) -> dict[str, Any]:
    """Pre-flight check whether ``delivery_branch`` can merge cleanly into ``target_branch``.

    Performs a dry-run merge in a temporary worktree (detached at target) so the
    main checkout and both branches are never mutated. Used before ``cmd_pr`` /
    ``cmd_merge`` to give the user a mergeability verdict before touching GitHub.

    Returns:
        {
          "mergeable": bool,       # True if merge would be clean
          "conflicts": [str],      # conflicting file paths (empty when mergeable)
          "ahead": int,            # commits delivery_branch is ahead of target
          "base_sha": str,         # target branch tip
          "head_sha": str,         # delivery branch tip
          "error": str,            # diagnostic when refs missing / repo invalid
        }
    """
    repo_path = Path(repo)
    base = {"mergeable": False, "conflicts": [], "ahead": 0,
            "base_sha": "", "head_sha": "", "error": ""}
    if not (repo_path / ".git").exists():
        base["error"] = "仓库不是 git 仓库"
        return base

    for ref in (target_branch, delivery_branch):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=str(repo_path), capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            base["error"] = f"分支不存在: {ref}"
            return base
    base["base_sha"] = subprocess.run(
        ["git", "rev-parse", target_branch], cwd=str(repo_path),
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    base["head_sha"] = subprocess.run(
        ["git", "rev-parse", delivery_branch], cwd=str(repo_path),
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()

    # Ahead count: commits in delivery_branch not reachable from target.
    ahead_r = subprocess.run(
        ["git", "rev-list", "--count", f"{target_branch}..{delivery_branch}"],
        cwd=str(repo_path), capture_output=True, text=True, timeout=10,
    )
    if ahead_r.returncode == 0 and ahead_r.stdout.strip().isdigit():
        base["ahead"] = int(ahead_r.stdout.strip())
    if base["ahead"] == 0:
        base["mergeable"] = True
        return base

    # Dry-run merge in a temp worktree detached at target.
    tmp = Path(tempfile.mkdtemp(prefix=f"agent_go_mergecheck_{Path(delivery_branch).name}_"))
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp), target_branch],
            cwd=str(repo_path), capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            base["error"] = f"无法创建 merge 检查 worktree: {add.stderr.strip()[:200]}"
            return base
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "--no-commit", delivery_branch],
            cwd=str(tmp), capture_output=True, text=True, timeout=60,
        )
        if merge.returncode == 0:
            base["mergeable"] = True
        else:
            # Enumerate conflicted paths (git status --porcelain shows "UU path").
            st = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(tmp),
                capture_output=True, text=True, timeout=10,
            )
            conflicts = []
            for line in (st.stdout or "").splitlines():
                parts = line.strip().split(None, 1)
                if parts and parts[0].startswith("U"):
                    conflicts.append(parts[1] if len(parts) > 1 else "")
            base["conflicts"] = conflicts
        # Abort the dry-run merge so the temp worktree stays clean for removal.
        subprocess.run(
            ["git", "merge", "--abort"], cwd=str(tmp),
            capture_output=True, text=True, timeout=30,
        )
        return base
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tmp)],
            cwd=str(repo_path), capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(tmp, ignore_errors=True)


def apply_local_delivery(
    repo: str | Path,
    meta: dict[str, Any],
    advance_target: bool = False,
) -> dict[str, Any]:
    """本地交付：把 delivery branch merge 进 target branch（不经 GitHub PR）。

    与 ``cmd_merge`` 同语义，但为可编程调用（bench / harness）抽取为纯函数：
    mergeability 预检 → 临时 worktree 执行 merge → 记录 ``explicit_merge_commit``。

    ``advance_target=False``（默认，bench fixture 模式）时只记录 merge commit，
    **不推进 target 分支引用**——fixture 仓库要在多次 repeat 间保持基线可复现，
    推进 target 会让后续 run 的 worktree 从「已交付」的 HEAD 建，污染任务难度。
    merge commit 对象留在共享 object db 中，``evaluate_accepted_delivery`` 的
    ref 存在性校验可通过。meta 写入 ``delivery_mode="bench_local"`` 以便审计区分。

    冲突/失败时不抛异常：写 ``delivery_attempted/delivery_failed/delivery_error``
    后返回，由 ``evaluate_accepted_delivery`` 与 failure class 归因为 delivery_failure。

    Returns:
        {"delivered": bool, "merge_commit": str, "conflicts": [str], "error": str}
    """
    result: dict[str, Any] = {"delivered": False, "merge_commit": "", "conflicts": [], "error": ""}
    repo_path = Path(repo)
    delivery_branch = meta.get("delivery_branch") or ""
    target = meta.get("target_branch") or meta.get("base_branch") or "main"
    task_id = meta.get("task_id", "")

    def _fail(error: str, conflicts: list[str] | None = None) -> dict[str, Any]:
        result["error"] = error
        result["conflicts"] = conflicts or []
        meta["delivery_attempted"] = True
        meta["delivery_failed"] = True
        meta["delivery_error"] = error
        return result

    if not delivery_branch:
        return _fail("无 delivery_branch，无法本地交付")
    if not _git_ref_exists(repo_path, delivery_branch):
        return _fail(f"delivery branch 不存在: {delivery_branch}")
    if not _git_ref_exists(repo_path, target):
        return _fail(f"target branch 不存在: {target}")

    mc = check_mergeability(repo_path, delivery_branch, target)
    if mc.get("error"):
        return _fail(f"mergeability 检查失败: {mc['error']}")
    if not mc.get("mergeable"):
        return _fail(
            "merge 冲突: " + ", ".join(mc.get("conflicts") or ["<未知>"]),
            conflicts=mc.get("conflicts") or [],
        )

    # ahead == 0：delivery 相对 target 无新增 commit（如全部 no_changes）。
    # 以 target tip 作为显式交付点，merge 为空操作。
    if mc.get("ahead") == 0:
        merge_commit = mc.get("base_sha") or ""
        if not merge_commit:
            return _fail("target branch tip 解析失败")
        result["delivered"] = True
        result["merge_commit"] = merge_commit
    else:
        tmp = Path(tempfile.mkdtemp(prefix=f"agent_go_localdelivery_{task_id}_"))
        try:
            add = subprocess.run(
                ["git", "worktree", "add", "--detach", str(tmp), target],
                cwd=str(repo_path), capture_output=True, text=True, timeout=30,
            )
            if add.returncode != 0:
                return _fail(f"无法创建 merge worktree: {add.stderr.strip()[:200]}")
            merge = subprocess.run(
                ["git", "merge", "--no-ff", "-m",
                 f"agent_go: local delivery of {task_id}", delivery_branch],
                cwd=str(tmp), capture_output=True, text=True, timeout=60,
            )
            if merge.returncode != 0:
                return _fail(f"merge 失败: {merge.stderr.strip()[:200]}")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(tmp),
                capture_output=True, text=True, timeout=10,
            )
            if head.returncode != 0 or not head.stdout.strip():
                return _fail("merge 后 rev-parse HEAD 失败")
            merge_commit = head.stdout.strip()
            if advance_target:
                update_ref = subprocess.run(
                    ["git", "update-ref", f"refs/heads/{target}", merge_commit],
                    cwd=str(repo_path), capture_output=True, text=True, timeout=10,
                )
                if update_ref.returncode != 0:
                    return _fail(f"更新 {target} 分支失败: {update_ref.stderr.strip()[:200]}")
            result["delivered"] = True
            result["merge_commit"] = merge_commit
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tmp)],
                cwd=str(repo_path), capture_output=True, text=True, timeout=30,
            )
            shutil.rmtree(tmp, ignore_errors=True)

    meta["explicit_merge_commit"] = result["merge_commit"]
    meta["delivery_attempted"] = True
    meta["delivery_failed"] = False
    meta["delivery_error"] = ""
    meta["delivery_mode"] = "local_advance" if advance_target else "bench_local"
    meta.pop("accepted_delivery_reasons", None)
    if not any(r.get("status") in ("failed", "blocked") for r in meta.get("results", [])):
        meta["accepted_delivery"] = True
        if meta.get("status_schema_version"):
            meta["status"] = "ACCEPTED_DELIVERY"
    return result
