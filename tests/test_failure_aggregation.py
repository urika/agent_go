from agent_go.metrics import aggregate_failure_classes, compute_frozen_metrics


def _record(failure_class=None, **kwargs):
    record = {
        "failure_class": failure_class,
        "total_cost_usd": 1.0,
        "timed_out": False,
        "kill_reason": None,
        "accepted_delivery": failure_class is None,
    }
    record.update(kwargs)
    return record


def test_failure_classes_are_grouped_without_merging_operational_failures():
    summary = aggregate_failure_classes([
        _record("model_failure"),
        _record("verification_failure"),
        _record("timeout", timed_out=True),
        _record("budget_abort"),
        _record("infrastructure_failure"),
        _record("delivery_failure"),
    ])
    assert summary["failure_class_counts"]["model_failure"] == 1
    assert summary["failure_class_counts"]["infrastructure_failure"] == 1
    assert summary["failure_class_counts"]["budget_abort"] == 1
    assert summary["capability_failure_count"] == 3
    assert summary["excluded_reasons"] == {"budget_abort": 1, "infrastructure_failure": 1}


def test_timeout_cleanup_race_and_cost_censoring_are_separate():
    summary = aggregate_failure_classes([
        _record("timeout", timed_out=True),
        _record(None, timed_out=True, kill_reason="cleanup_race"),
        _record("infrastructure_failure", timed_out=True),
    ])
    disposition = summary["timeout_disposition"]
    assert disposition["product_semantics"] == "failure"
    assert disposition["timeout_failure_count"] == 1
    assert disposition["right_censored_for_cost_baseline_count"] == 2
    assert disposition["cleanup_race_count"] == 1


def test_frozen_metrics_exposes_failure_summary():
    metrics = compute_frozen_metrics([_record("system_error")])
    assert metrics["failure_class_summary"]["excluded_task_count"] == 1
    assert metrics["failure_class_summary"]["failure_class_counts"]["system_error"] == 1
