"""从 worktree 状态重建 meta.json（应对 SIGKILL / 异常中断场景）。

核心规则（commit 是边界）：
- 有 commit + verify pass → completed
- 有 commit + verify fail → failed
- 无 commit + 有 orphan → reset orphan（视为未完成，resume 会重新跑）
- 无 commit + 无 orphan → no_changes（从未跑过，resume 会重新跑）

设计原则：
- recover 只清理 worktree 临时状态，**不 commit orphan**（保持 commit = 完成边界）
- atomic 写入 meta.json（write tmp + rename）
- 后续可用 `agent_go resume <task-id>` 重新跑未完成的 subtask
"""
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from .config import AGENT_GO_DIR

__all__ = ["recover_task", "scan_subtask_state", "reset_orphan_changes"]


def _run_git(cwd: Path, *args: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run git command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, "", str(e)


def _save_meta_atomic(meta: dict, task_dir: Path) -> None:
    """原子写 meta.json：先写 .tmp，再 rename（POSIX 保证原子性）。"""
    meta_path = task_dir / "meta.json"
    tmp_path = task_dir / "meta.json.tmp"
    tmp_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(tmp_path, meta_path)


def _branch_has_commits(worktree: Path, expected_branch_prefix: str) -> tuple[bool, int, str]:
    """检查 worktree 分支是否在 expected_branch_prefix（agent_go/task-XXX/sub-Y）上 + 是否有 commits。"""
    rc, stdout, _ = _run_git(worktree, "branch", "--show-current")
    if rc != 0:
        return False, 0, ""
    current_branch = stdout.strip()

    on_correct_branch = current_branch.startswith(expected_branch_prefix)

    rc, stdout, _ = _run_git(worktree, "rev-list", "--count", "main..HEAD")
    if rc != 0:
        rc2, stdout2, _ = _run_git(worktree, "rev-list", "--count", "HEAD")
        if rc2 != 0:
            return on_correct_branch, 0, current_branch
        try:
            count = max(0, int(stdout2.strip()) - 1)
        except ValueError:
            count = 0
    else:
        try:
            count = int(stdout.strip())
        except ValueError:
            count = 0

    return on_correct_branch, count, current_branch


def _has_verify_pass(log_path: Path, sub_id: str) -> Optional[bool]:
    """从 execution.log 检查 subtask 的 verify 结果。

    Returns:
        True if verify passed, False if failed, None if not found
    """
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    sub_lines = [ln for ln in text.splitlines() if f"[{sub_id}]" in ln]

    has_success_marker = any(
        "subtask_complete" in ln and '"status": "completed"' in ln
        for ln in sub_lines
    )
    has_fail_marker = any(
        "subtask_complete" in ln and ('"status": "failed"' in ln or '"status": "no_changes"' in ln)
        for ln in sub_lines
    )

    if has_success_marker and not has_fail_marker:
        return True
    if has_fail_marker:
        return False
    return None


def reset_orphan_changes(worktree: Path) -> bool:
    """丢弃 worktree 中的所有未提交改动（reset 到 HEAD）。

    用于 recover 时：原子性要求，要么提交要么丢弃。
    不能让 dirty 状态污染后续 resume。

    Returns:
        True if successful (or already clean), False otherwise
    """
    rc, stdout, _ = _run_git(worktree, "status", "--porcelain")
    if rc != 0 or not stdout.strip():
        return True  # 已经干净

    # Remove untracked files first, then reset tracked files.
    # Order matters: git checkout -- . fails when only untracked files exist
    # ("pathspec '.' did not match any file(s) known to git"), so clean first.
    rc, _, _ = _run_git(worktree, "clean", "-fd")
    if rc != 0:
        return False

    # checkout is best-effort: if the index has tracked files that need
    # resetting, they get restored. If the index is empty (edge case),
    # git fails with "pathspec '.' did not match any file(s)" — ignore.
    _run_git(worktree, "checkout", "--", ".")

    return True


def scan_subtask_state(
    task_id: str,
    task_dir: Path,
    sub_id: str,
) -> dict[str, Any]:
    """扫描单个 subtask 的 worktree 状态，推断其结果。

    原子完成规则（commit 是边界）：
    - 有 commit + verify pass → completed
    - 有 commit + verify fail → failed
    - 无 commit + 有 orphan → reset orphan（视为未完成，resume 会重新跑）
    - 无 commit + 无 orphan → no_changes（从未跑过，resume 会重新跑）

    注意：recover 永远 reset orphan，从不 commit orphan。
    这是为了保持"commit = 完成边界"的语义清晰。
    """
    worktree = task_dir / sub_id / "work"
    branch_prefix = f"agent_go/{task_id}/{sub_id}"

    result = {
        "subtask_id": sub_id,
        "status": "unknown",
        "verify_ok": None,
        "branch": None,
        "commits": 0,
        "orphan_reset": False,
        "recovered": True,
        "recovered_at": datetime.now().isoformat(),
    }

    if not worktree.exists():
        result["status"] = "no_worktree"
        return result

    if not (worktree / ".git").exists():
        result["status"] = "no_git_link"
        return result

    on_branch, commit_count, current_branch = _branch_has_commits(worktree, branch_prefix)
    result["branch"] = current_branch
    result["commits"] = commit_count

    if not on_branch:
        result["status"] = "wrong_branch"
        return result

    # 检查 orphan（未提交改动）
    rc, status_out, _ = _run_git(worktree, "status", "--porcelain")
    has_orphan = bool(rc == 0 and status_out.strip())

    # 处理 orphan：永远 reset（不 commit），无论有没有 commit 都清掉中间产物
    # 这保证 worktree 处于 atomic 状态：要么完整 commit 要么干净
    if has_orphan:
        result["orphan_reset"] = reset_orphan_changes(worktree)

    # 从 execution.log 推断 verify 结果
    log_path = task_dir / "execution.log"
    result["verify_ok"] = _has_verify_pass(log_path, sub_id)

    # 综合判断 status（核心规则：commit 是完成边界）
    if commit_count == 0:
        # 无 commit = 未完成（不论有没有 orphan 都被 reset 掉了）
        result["status"] = "no_changes"
    elif result["verify_ok"] is False:
        result["status"] = "failed"
    elif result["verify_ok"] is True:
        result["status"] = "completed"
    else:
        # 有 commit + verify 未知（execution.log 没记录明确 success/failure）
        result["status"] = "completed"

    return result


def recover_task(
    task_id: str,
    update_meta: bool = True,
) -> dict[str, Any]:
    """恢复被异常中断的任务：从 worktree 状态重建 meta.json。

    核心规则：commit 是完成的边界。
    - 有 commit → 标记为 completed/failed（atomic 状态）
    - 无 commit → reset orphan → 标记为 no_changes（resume 会重新跑）

    recover 不会"恢复 claude 在 SIGKILL 前的工作"——那是数据，不是状态。
    状态只承认 commit（atomic boundary）。
    """
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        return {"error": f"task {task_id} not found at {task_dir}"}

    meta_path = task_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {"status": "running", "task_id": task_id, "results": []}
    else:
        meta = {"status": "running", "task_id": task_id, "results": []}

    # 找 subtasks
    subtasks = meta.get("subtasks", [])
    if not subtasks:
        for sub_dir in sorted(task_dir.glob("sub-*")):
            sub_id = sub_dir.name
            subtasks.append({"id": sub_id, "title": f"(recovered {sub_id})"})
        meta["subtasks"] = subtasks

    existing_results = {r.get("subtask_id"): r for r in meta.get("results", [])}
    recovered_results = []
    for st in subtasks:
        sub_id = st["id"]
        existing = existing_results.get(sub_id, {})
        # 已有真实（不是 recovered 标记的）结果 → 保留
        if existing.get("status") in ("completed", "failed", "blocked", "no_changes") and not existing.get("recovered"):
            recovered_results.append(existing)
            continue
        # 否则扫描 worktree
        sub_state = scan_subtask_state(task_id, task_dir, sub_id)
        recovered_results.append(sub_state)

    # 原子完成规则：commit 是边界
    # - 所有 subtask 都有 commit 且至少一个 completed → completed
    # - 至少一个 subtask 是 no_changes（没 commit）→ interrupted（resume 会重新跑）
    # - 至少一个 subtask 是 failed → failed（但可 resume 重新尝试）
    # - 任何 dirty/error 状态 → interrupted
    statuses = [r.get("status") for r in recovered_results]
    if not statuses:
        overall_status = "no_subtasks"
    elif any(s in ("dirty", "wrong_branch", "no_git_link", "no_worktree", "unknown") for s in statuses):
        overall_status = "interrupted"
    elif any(s == "no_changes" for s in statuses):
        # 至少一个 subtask 没 commit → 任务未完成，resume 接力
        overall_status = "interrupted"
    elif all(s == "completed" for s in statuses):
        overall_status = "completed"
    elif any(s == "failed" for s in statuses):
        overall_status = "failed"
    else:
        overall_status = "interrupted"

    meta_updated = False
    if update_meta:
        meta["results"] = recovered_results
        meta["status"] = overall_status
        meta["recovered_at"] = datetime.now().isoformat()
        _save_meta_atomic(meta, task_dir)
        meta_updated = True

    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "previous_status": meta.get("status", "unknown") if not update_meta else None,
        "recovered": recovered_results,
        "overall_status": overall_status,
        "meta_updated": meta_updated,
    }