from agent_go.delivery import evaluate_accepted_delivery


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

def test_harness_run_accepted_from_code_correctness():
    """无 delivery_attempted（harness/bench，从不 push 分支/建 PR）→ accepted 由代码正确性
    （全部子任务通过+已验证）判定，不再因缺 delivery 分支/PR 结构性为 False。"""
    meta = {
        "status": "DELIVERY_READY",
        "results": [
            {"status": "completed", "verify_ok": True},
            {"status": "completed", "verify_ok": True},
        ],
    }
    result = evaluate_accepted_delivery(meta)
    assert result["accepted_delivery"] is True
    assert result["accepted_delivery_reasons"] == []


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
