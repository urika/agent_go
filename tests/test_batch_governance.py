import json

import pytest

from agent_go.bench_schema import BENCH_SCHEMA_VERSION
from agent_go.batch_governance import archive_baseline, build_batch_manifest, validate_mergeable_batches


def _record(task_id="task-1", source_batch="batch-1", suite="core"):
    return {
        "bench_schema_version": BENCH_SCHEMA_VERSION,
        "task_id": task_id, "task_version": "v1", "suite": suite,
        "source_batch": source_batch, "model": "m1", "planner_model": "p1",
        "judge_model": "j1", "repeat": 1, "difficulty": "easy",
        "failure_class": None, "accepted_delivery": True,
        "delivery_branch_created": True, "pr_created": True,
        "spec_compliance": None, "architecture_compliance": None,
        "total_cost_usd": 1.0, "elapsed_sec": 10.0,
    }


def _write(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def test_manifest_contains_hash_and_batch_identity(tmp_path):
    results = tmp_path / "results.jsonl"
    _write(results, [_record()])
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    manifest = build_batch_manifest(results, catalog_path=catalog, config_path=results)
    assert manifest["immutable"] is True
    assert manifest["source_batch"] == "batch-1"
    assert manifest["results_sha256"]
    assert manifest["bench_schema_version"] == 1


def test_archive_preserves_source_and_creates_three_artifacts(tmp_path):
    source = tmp_path / "source.jsonl"
    _write(source, [_record()])
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    target = archive_baseline(source, tmp_path / "eval_suite", catalog_path=catalog, config_path=source)
    assert source.exists()
    assert {p.name for p in target.iterdir()} == {"manifest.json", "results.jsonl", "summary.json"}


def test_merge_rejects_different_source_batches(tmp_path):
    first = tmp_path / "one.jsonl"
    second = tmp_path / "two.jsonl"
    _write(first, [_record(source_batch="one")])
    _write(second, [_record(source_batch="two")])
    errors = validate_mergeable_batches([first, second])
    assert errors
    assert "source_batch" in errors[-1]["error"]


# ── C5：proxy_context 批次口径快照 ─────────────────────────────

def test_manifest_without_sidecar_marks_unavailable(tmp_path):
    results = tmp_path / "results.jsonl"
    _write(results, [_record()])
    manifest = build_batch_manifest(results)
    assert manifest["proxy_context"] == {"available": False}


def test_manifest_picks_up_sidecar_automatically(tmp_path):
    results = tmp_path / "results.jsonl"
    _write(results, [_record()])
    sidecar = tmp_path / "results.jsonl.proxy_context.json"
    sidecar.write_text(json.dumps({
        "available": True,
        "ctx_config": {"diag_enabled": True, "compression_mode": "semantic"},
        "route_config": {"route_enabled": True},
        "captured_at": "2026-08-19T00:00:00+00:00",
    }), encoding="utf-8")
    manifest = build_batch_manifest(results)
    assert manifest["proxy_context"]["available"] is True
    assert manifest["proxy_context"]["ctx_config"]["compression_mode"] == "semantic"


def test_manifest_explicit_proxy_context_path(tmp_path):
    results = tmp_path / "results.jsonl"
    _write(results, [_record()])
    explicit = tmp_path / "snapshot.json"
    explicit.write_text(json.dumps({"available": True, "ctx_config": {"diag_enabled": False}}),
                        encoding="utf-8")
    manifest = build_batch_manifest(results, proxy_context_path=explicit)
    assert manifest["proxy_context"]["ctx_config"]["diag_enabled"] is False


def test_manifest_unreadable_sidecar_fail_open(tmp_path):
    results = tmp_path / "results.jsonl"
    _write(results, [_record()])
    sidecar = tmp_path / "results.jsonl.proxy_context.json"
    sidecar.write_text("not json{", encoding="utf-8")
    manifest = build_batch_manifest(results)
    assert manifest["proxy_context"]["available"] is False
