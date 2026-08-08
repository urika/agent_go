import json

import pytest

from agent_go.bench_schema import BENCH_SCHEMA_VERSION
from agent_go.metric_report import build_metric_freeze_report, write_metric_freeze_report


def _record(**overrides):
    record = {
        "bench_schema_version": BENCH_SCHEMA_VERSION,
        "task_id": "task-1", "task_version": "v1", "suite": "core",
        "source_batch": "batch-1", "model": "m1", "planner_model": "p1",
        "judge_model": "j1", "repeat": 1, "difficulty": "easy",
        "failure_class": None, "accepted_delivery": True,
        "delivery_branch_created": True, "pr_created": True,
        "spec_compliance": None, "architecture_compliance": None,
        "total_cost_usd": 1.0, "elapsed_sec": 12.0,
        "binary_pass": True, "pass_rate": 1.0, "total_retries": 0,
    }
    record.update(overrides)
    return record


def test_metric_freeze_report_contains_hashes_metrics_and_window(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    catalog = tmp_path / "task_catalog.json"
    catalog.write_text("{}\n", encoding="utf-8")
    report = build_metric_freeze_report(results, catalog_path=catalog, config_path=results)
    assert report["bench_schema_version"] == 1
    assert report["source_batch"] == "batch-1"
    assert report["source_batch_immutable"] is True
    assert report["metrics"]["accepted_delivery_rate"] == 1.0
    assert report["metrics"]["pr_creation_rate"] == 1.0
    assert report["task_catalog_hash"]
    assert report["config_hash"]
    output = write_metric_freeze_report(report, tmp_path / "report.json")
    assert json.loads(output.read_text(encoding="utf-8"))["suite"] == "core"


def test_metric_freeze_rejects_mixed_source_batches(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps(_record(task_id="a")) + "\n" + json.dumps(_record(task_id="b", source_batch="batch-2")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source_batch"):
        build_metric_freeze_report(results, catalog_path=tmp_path / "catalog", config_path=results)


def test_metric_freeze_rejects_mixed_suites(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps(_record(task_id="a")) + "\n" + json.dumps(_record(task_id="b", suite="stress")) + "\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog"
    catalog.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="suite"):
        build_metric_freeze_report(results, catalog_path=catalog)


def test_metric_freeze_uses_default_config_hash_when_file_is_absent(tmp_path, monkeypatch):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(_record()) + "\n", encoding="utf-8")
    catalog = tmp_path / "catalog"
    catalog.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("agent_go.metric_report.CONFIG_PATH", tmp_path / "missing-config.json")
    report = build_metric_freeze_report(results, catalog_path=catalog)
    assert report["config_hash"]
    with pytest.raises(ValueError, match="config file not found"):
        build_metric_freeze_report(results, catalog_path=catalog, config_path=tmp_path / "missing.json")
