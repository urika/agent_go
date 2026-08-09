from agent_go.delivery import check_mergeability, create_delivery_branch, evaluate_accepted_delivery


def _meta(**overrides):
    meta = {
        "status": "completed",
        "commit_hash": "abc123",
        "delivery_branch": "agent_go/task/delivery",
        "pr_url": "https://example.test/pr/1",
        "results": [{"status": "completed", "verify_ok": True}],
    }
    meta.update(overrides)
    return meta


def test_accepted_delivery_requires_all_delivery_gates():
    result = evaluate_accepted_delivery(_meta())
    assert result["accepted_delivery"] is True
    assert result["delivery_failed"] is False
    assert result["accepted_delivery_reasons"] == []


def test_completed_without_delivery_branch_is_not_accepted():
    # 生产 run（delivery_attempted=True）仍强制 delivery 分支。
    result = evaluate_accepted_delivery(_meta(delivery_attempted=True, delivery_branch=""))
    assert result["accepted_delivery"] is False
    assert "missing_delivery_branch" in result["accepted_delivery_reasons"]


def test_verification_failure_is_not_accepted():
    result = evaluate_accepted_delivery(
        _meta(results=[{"status": "failed", "verify_ok": False}])
    )
    assert result["accepted_delivery"] is False
    assert "verification_not_passed" in result["accepted_delivery_reasons"]


def test_pr_failure_is_delivery_failure():
    result = evaluate_accepted_delivery(_meta(pr_url="", delivery_attempted=True))
    assert result["accepted_delivery"] is False
    assert result["delivery_failed"] is True
    assert "missing_pr_or_explicit_merge" in result["accepted_delivery_reasons"]


def test_excluded_task_is_not_delivery_failure():
    result = evaluate_accepted_delivery(_meta(excluded=True))
    assert result["accepted_delivery"] is False
    assert result["delivery_failed"] is False
    assert "invalid_task" in result["accepted_delivery_reasons"]


def test_all_subtask_commits_are_required_when_repo_is_not_checked():
    result = evaluate_accepted_delivery(
        _meta(commit_hashes=["commit-a", "commit-b"], commit_hash="")
    )
    assert result["accepted_delivery"] is True


def test_pr_head_and_base_must_match_delivery_relationship():
    # 生产 run（delivery_attempted=True + pr_url）→ PR head/base 一致性仍强制。
    result = evaluate_accepted_delivery(
        _meta(delivery_attempted=True, target_branch="main", pr_base="develop", pr_head="wrong")
    )
    assert result["accepted_delivery"] is False
    assert "pr_base_mismatch" in result["accepted_delivery_reasons"]
    assert "pr_head_mismatch" in result["accepted_delivery_reasons"]


def test_cancelled_task_cannot_be_accepted():
    result = evaluate_accepted_delivery(_meta(status="CANCELLED"))
    assert result["accepted_delivery"] is False
    assert "task_not_successful" in result["accepted_delivery_reasons"]


# ═══════════════════════════════════════════════════════════════
# CR-#4：harness/bench run（无 delivery_attempted）→ 代码正确性判交付
# ═══════════════════════════════════════════════════════════════

def test_harness_run_without_delivery_artifacts_is_not_accepted():
    """Bench/harness 也不能绕过 delivery branch/PR 门禁。"""
    meta = {
        "status": "DELIVERY_READY",
        "results": [
            {"status": "completed", "verify_ok": True},
            {"status": "completed", "verify_ok": True},
        ],
    }
    result = evaluate_accepted_delivery(meta)
    assert result["accepted_delivery"] is False
    assert "missing_delivery_branch" in result["accepted_delivery_reasons"]


def test_harness_run_with_failure_not_accepted():
    """harness run 有未完成/未验证子任务 → 仍不 accepted（代码正确性不满足）。"""
    meta = {
        "status": "VERIFICATION_FAILED",
        "results": [
            {"status": "completed", "verify_ok": True},
            {"status": "failed", "verify_ok": False},
        ],
    }
    result = evaluate_accepted_delivery(meta)
    assert result["accepted_delivery"] is False
    reasons = result["accepted_delivery_reasons"]
    assert "task_not_successful" in reasons
    assert "incomplete_subtask" in reasons


def test_production_run_still_requires_delivery_artifacts():
    """delivery_attempted=True（生产 run）→ 仍强制分支/PR/commit 检查。"""
    meta = {
        "status": "DELIVERY_READY", "delivery_attempted": True,
        "results": [{"status": "completed", "verify_ok": True}],
    }
    result = evaluate_accepted_delivery(meta)
    assert result["accepted_delivery"] is False  # 缺 commit/分支/PR
    reasons = result["accepted_delivery_reasons"]
    assert "missing_commit" in reasons
    assert "missing_delivery_branch" in reasons
    assert "missing_pr_or_explicit_merge" in reasons


# ═══════════════════════════════════════════════════════════════
# M1.1: create_delivery_branch（真实 git 仓库）
# ═══════════════════════════════════════════════════════════════

import subprocess
from pathlib import Path


def _init_repo(path: Path, files=None):
    """git init + first commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), capture_output=True)
    for name, content in (files or {"file.txt": "base"}).items():
        (path / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


def _commit(path: Path, filename: str, content: str, msg: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(path), capture_output=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True)
    return out.stdout.strip()


def test_create_delivery_branch_aggregates_commits(tmp_path):
    """成功子任务 commit 汇总到 delivery branch，且主分支不受污染。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    # 用两个独立 worktree 分支模拟子任务 commit
    for sub_id, fname in [("sub-1", "a.txt"), ("sub-2", "b.txt")]:
        wt = tmp_path / f"wt_{sub_id}"
        subprocess.run(["git", "worktree", "add", "-b", f"agent_go/t/{sub_id}", str(wt)], cwd=str(repo), capture_output=True)
        _commit(wt, fname, f"{fname} content", f"{sub_id} commit")
    # 收集两个子任务的 commit
    commits = []
    for sub_id, fname in [("sub-1", "a.txt"), ("sub-2", "b.txt")]:
        wt = tmp_path / f"wt_{sub_id}"
        h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt), capture_output=True, text=True).stdout.strip()
        commits.append(h)
    results = [
        {"subtask_id": "sub-1", "status": "completed", "commit_hash": commits[0]},
        {"subtask_id": "sub-2", "status": "completed", "commit_hash": commits[1]},
    ]
    ok, branch, err = create_delivery_branch(repo, "t", base, results)
    assert ok, err
    assert branch == "agent_go/t/delivery"
    # delivery branch 应包含两个子任务文件
    check = subprocess.run(
        ["git", "show", f"{branch}:a.txt"], cwd=str(repo), capture_output=True, text=True)
    assert check.returncode == 0 and check.stdout.strip() == "a.txt content"
    check2 = subprocess.run(
        ["git", "show", f"{branch}:b.txt"], cwd=str(repo), capture_output=True, text=True)
    assert check2.returncode == 0 and check2.stdout.strip() == "b.txt content"
    # 主分支仍停留在 base（未被污染）
    main_head = subprocess.run(["git", "rev-parse", "main"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    assert main_head == base


def test_create_delivery_branch_with_no_commits(tmp_path):
    """没有成功子任务 commit → 创建空 delivery branch（锚定 base_commit）。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    ok, branch, err = create_delivery_branch(repo, "t2", base, [])
    assert ok, err
    assert branch == "agent_go/t2/delivery"


def test_create_delivery_branch_invalid_base(tmp_path):
    """无效 base_commit → 失败，不抛出异常。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    ok, branch, err = create_delivery_branch(repo, "t3", "deadbeef" * 5, [{"status": "completed", "commit_hash": "x"}])
    assert not ok
    assert branch == "agent_go/t3/delivery"


# ═══════════════════════════════════════════════════════════════
# M1.1: pipeline 集成 — 成功后自动创建 delivery branch
# ═══════════════════════════════════════════════════════════════

import threading
from unittest.mock import MagicMock, patch

from agent_go.pipeline import _run_pipeline


def _make_subtask(sub_id, difficulty="easy"):
    return {"id": sub_id, "title": f"task {sub_id}", "difficulty": difficulty,
            "prompt": f"do {sub_id}", "agent_type": "developer", "deps": []}


def test_pipeline_success_creates_delivery_branch(tmp_path):
    """pipeline 全部成功后自动创建 delivery branch 并写入 meta。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()
    confirmed = [_make_subtask("sub-1")]
    meta = {"task_id": "task-p1", "status": "EXECUTING", "status_schema_version": 1,
            "base_commit": base, "base_branch": "main", "target_branch": "main",
            "delivery_branch": "", "results": []}
    interrupt = threading.Event()

    def mock_run_subtask(*a, **k):
        # 返回带 commit_hash 的成功结果
        return {"subtask_id": "sub-1", "status": "completed", "exit_code": 0,
                "summary": "done", "worktree": "", "sandbox_type": "headless",
                "verify_ok": True, "duration_sec": 1.0,
                "commit_hash": base}

    # 让所有 git 子命令返回成功（空输出）
    def fake_git(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    try:
        with patch("agent_go.pipeline.run_subtask", side_effect=mock_run_subtask), \
             patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, "")), \
             patch("agent_go.pipeline._worktree_remove", return_value=(True, "")), \
             patch("agent_go.pipeline._worktree_prune", return_value=(True, "")), \
             patch("agent_go.pipeline.subprocess.run", side_effect=fake_git), \
             patch("agent_go.pipeline.signal.signal"), \
             patch("agent_go.pipeline._stop_heartbeat"):
            _run_pipeline(
                confirmed, repo, task_dir, MagicMock(),
                {"plan_api": {"provider": "test"}},
                headless=True, parallel=1, issue_ref="",
                meta=meta, remote_url="", interrupted=interrupt)
    except SystemExit:
        pass

    # delivery branch 应被创建（subprocess 被 mock，create_delivery_branch 内部也会走到 mock）
    # 这里验证 meta 中 delivery_branch 已被赋值（由 pipeline 写入）
    assert meta["delivery_branch"] == "agent_go/task-p1/delivery"
    assert meta["status"] == "DELIVERY_READY"


# ═══════════════════════════════════════════════════════════════
# M1: mergeability 预检（PR/merge 前的 dry-run merge）
# ═══════════════════════════════════════════════════════════════


def test_mergeability_clean_when_no_conflict(tmp_path):
    """delivery branch 相对 target 有新增且无冲突 → mergeable=True，无 conflicts。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    # 在 delivery 分支加一个新文件
    wt = tmp_path / "wt_delivery"
    subprocess.run(["git", "worktree", "add", "-b", "delivery/test", str(wt)], cwd=str(repo), capture_output=True)
    _commit(wt, "new.txt", "new", "add new file")
    result = check_mergeability(repo, "delivery/test", "main")
    assert result["mergeable"] is True
    assert result["conflicts"] == []
    assert result["ahead"] >= 1
    assert result["base_sha"]
    assert result["head_sha"]


def test_mergeability_detects_conflict(tmp_path):
    """delivery branch 修改了 target 也修改的文件 → mergeable=False，conflicts 非空。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    # target 分支（main）修改 file.txt
    _commit(repo, "file.txt", "main change", "main modify")
    # delivery 分支基于 base 修改同一文件
    base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    wt = tmp_path / "wt_conflict"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt), base], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "checkout", "-b", "delivery/conflict"], cwd=str(wt), capture_output=True)
    _commit(wt, "file.txt", "delivery change", "delivery modify")
    result = check_mergeability(repo, "delivery/conflict", "main")
    assert result["mergeable"] is False
    assert "file.txt" in result["conflicts"]


def test_mergeability_ahead_zero(tmp_path):
    """delivery branch 与 target 相同（无新增）→ mergeable=True，ahead=0。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = check_mergeability(repo, "main", "main")
    assert result["mergeable"] is True
    assert result["ahead"] == 0


def test_mergeability_missing_branch(tmp_path):
    """delivery branch 不存在 → error 字段，mergeable=False。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = check_mergeability(repo, "delivery/not-exist", "main")
    assert result["mergeable"] is False
    assert "不存在" in result["error"]


def test_mergeability_non_git_repo(tmp_path):
    """非 git 仓库 → error 字段。"""
    repo = tmp_path / "not-repo"
    repo.mkdir()
    result = check_mergeability(repo, "x", "main")
    assert result["mergeable"] is False
    assert "git 仓库" in result["error"]
