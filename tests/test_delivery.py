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
    result = evaluate_accepted_delivery(_meta(delivery_branch=""))
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
    result = evaluate_accepted_delivery(
        _meta(target_branch="main", pr_base="develop", pr_head="wrong")
    )
    assert result["accepted_delivery"] is False
    assert "pr_base_mismatch" in result["accepted_delivery_reasons"]
    assert "pr_head_mismatch" in result["accepted_delivery_reasons"]


def test_cancelled_task_cannot_be_accepted():
    result = evaluate_accepted_delivery(_meta(status="CANCELLED"))
    assert result["accepted_delivery"] is False
    assert "task_not_successful" in result["accepted_delivery_reasons"]
