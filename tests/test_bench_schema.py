import pytest

from agent_go.bench_schema import BENCH_SCHEMA_VERSION, validate_record, validate_results_file


def _record():
    return {
        "bench_schema_version": BENCH_SCHEMA_VERSION,
        "task_id": "task-1", "task_version": "abc", "suite": "smoke",
        "source_batch": "batch-1", "model": "m1", "planner_model": "p1",
        "judge_model": "j1", "repeat": 1, "difficulty": "easy",
        "failure_class": None, "accepted_delivery": False,
        "delivery_branch_created": False, "pr_created": False,
        "spec_compliance": None, "architecture_compliance": None,
        "total_cost_usd": 0.1, "elapsed_sec": 1.2,
    }


def test_valid_record_passes():
    assert validate_record(_record()) == []


def test_missing_required_field_is_rejected():
    record = _record()
    del record["source_batch"]
    assert any("source_batch" in error for error in validate_record(record))


def test_type_and_enum_errors_are_rejected():
    record = _record()
    record["repeat"] = True
    record["difficulty"] = "unknown"
    record["accepted_delivery"] = "true"
    assert len(validate_record(record)) == 3


def test_jsonl_validator_rejects_mixed_invalid_batch(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text('{"task_id": "missing-fields"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        validate_results_file(path)
