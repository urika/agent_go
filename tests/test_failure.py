from agent_go.failure import FAILURE_CLASSES, aggregate_failure_class, classify_failure, failure_policy


def test_fixed_failure_classes_and_policy_matrix():
    assert len(FAILURE_CLASSES) == 8
    assert failure_policy("budget_abort")["capability_failure"] is False
    assert failure_policy("verification_failure")["capability_failure"] is True
    assert failure_policy("user_cancelled")["resume_allowed"] is False


def test_kill_reason_mapping_keeps_infra_and_budget_separate():
    assert classify_failure({"status": "failed", "kill_reason": "infra"}) == "infrastructure_failure"
    assert classify_failure({"status": "blocked", "kill_reason": "over_budget_l3"}) == "budget_abort"
    assert classify_failure({"status": "failed", "kill_reason": "hard_timeout"}) == "timeout"
    assert classify_failure(
        {"status": "blocked", "kill_reason": "over_budget_l3"}, timed_out=True
    ) == "budget_abort"


def test_verification_and_model_failures_are_distinct():
    assert classify_failure({"status": "failed", "verify_ok": False, "exit_code": 0}) == "verification_failure"
    assert classify_failure({"status": "failed", "verify_ok": True, "exit_code": 1}) == "model_failure"


def test_rejected_verification_is_verification_failure():
    """验证命令被安全门禁拒绝 = 生成质量问题，不是基础设施故障（2026-08-12 修复）。

    决策基线 decision-20260812 中 6 个任务（add-simple-caching/safe-file-reader/
    integration-tests-datapipeline/list-tools/add-stats-command 等）因 LLM 生成的
    验证命令不合规（python -c 含装饰器/换行/bash -c 包裹/未知前缀）被拒绝，原归类
    infrastructure_failure 使「infra 失败」统计失真。修正为 verification_failure。
    """
    assert classify_failure({
        "status": "failed",
        "verify_ok": False,
        "verification_results": [{"rejected": True}],
    }) == "verification_failure"


def test_aggregate_prefers_explicit_delivery_failure():
    assert aggregate_failure_class(["model_failure"], {"delivery_failed": True}) == "delivery_failure"


def test_semantic_failure_classified_when_status_completed():
    """阶段C review P0: status=completed 但语义评估失败 → verification_failure（不再返回 None）。

    implement-done-command r1 / conditional-branching r1 的 binary_pass=false 但 failure_class=null
    根因：classify_failure 在 status in {completed, no_changes} 时直接返回 None，未检查
    verification_results 中的 type=="semantic" 且 passed=False。修复后语义失败被正确分类。
    """
    result = {
        "status": "completed",
        "verify_ok": True,
        "exit_code": 0,
        "verification_results": [
            {"type": "semantic", "passed": False, "reason": "语义评估未通过"},
        ],
    }
    assert classify_failure(result) == "verification_failure"


def test_semantic_pass_returns_none_for_completed():
    """status=completed 且语义评估通过 → 无失败（返回 None）。"""
    result = {
        "status": "completed",
        "verify_ok": True,
        "exit_code": 0,
        "verification_results": [
            {"type": "semantic", "passed": True},
        ],
    }
    assert classify_failure(result) is None


def test_completed_no_semantic_results_returns_none():
    """status=completed 且无语义评估记录 → 无失败（返回 None），不破坏既有行为。"""
    result = {"status": "completed", "verify_ok": True, "exit_code": 0}
    assert classify_failure(result) is None
