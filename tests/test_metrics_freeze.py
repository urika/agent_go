import pytest

from agent_go.metrics import compute_frozen_metrics, is_valid_metric_task


def _record(**overrides):
    record = {
        "accepted_delivery": False,
        "binary_pass": False,
        "elapsed_sec": 10,
        "failure_class": "model_failure",
        "pass_rate": 0,
        "total_cost_usd": 1.0,
        "total_retries": 0,
    }
    record.update(overrides)
    return record


def test_accepted_delivery_rate_and_cost_use_valid_task_denominator():
    records = [
        _record(accepted_delivery=True, binary_pass=True, pass_rate=1.0),
        _record(failure_class="infrastructure_failure", total_cost_usd=5.0),
        _record(failure_class="budget_abort", total_cost_usd=2.0),
    ]
    metrics = compute_frozen_metrics(records)
    assert metrics["valid_task_count"] == 1
    assert metrics["accepted_delivery_rate"] == 1.0
    assert metrics["valid_cost_usd"] == 1.0
    assert metrics["cost_per_accepted_delivery_usd"] == 1.0
    assert metrics["pr_creation_rate"] == 0.0
    assert metrics["excluded_reasons"] == {"infrastructure_failure": 1, "budget_abort": 1}


def test_failure_rates_and_first_pass_are_repeatable():
    records = [
        _record(accepted_delivery=True, binary_pass=True, pass_rate=1.0, total_retries=0),
        _record(failure_class="timeout", total_retries=1),
        _record(failure_class="delivery_failure"),
    ]
    first = compute_frozen_metrics(records)
    second = compute_frozen_metrics(records)
    assert first == second
    assert first["first_pass_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert first["timeout_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert first["retry_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert first["delivery_failure_rate"] == pytest.approx(1 / 3, abs=1e-6)


def test_empty_denominators_are_none():
    metrics = compute_frozen_metrics([_record(failure_class="system_error")])
    assert metrics["accepted_delivery_rate"] is None
    assert metrics["cost_per_accepted_delivery_usd"] is None
    assert metrics["pass_rate_diagnostic"] is None
    assert not is_valid_metric_task(_record(failure_class="user_cancelled"))
