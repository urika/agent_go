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


def test_aggregate_prefers_explicit_delivery_failure():
    assert aggregate_failure_class(["model_failure"], {"delivery_failed": True}) == "delivery_failure"
