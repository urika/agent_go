"""M0-11 minimum end-to-end contract gates."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from agent_go.bench import cmd_bench
from agent_go.bench_schema import REQUIRED_FIELDS, validate_results_file
from agent_go.delivery import apply_delivery_result
from agent_go.metrics import compute_frozen_metrics


def _meta(**overrides):
    meta = {
        "status": "DELIVERY_READY",
        "results": [{"subtask_id": "s1", "status": "completed", "verify_ok": True, "commit_hash": "c1"}],
        "commit_hash": "c1",
        "delivery_branch": "agent_go/t/delivery",
        "pr_url": "https://example.test/pr/1",
    }
    meta.update(overrides)
    return meta


def test_single_task_meta_has_explicit_delivery_decision():
    meta = _meta()
    result = apply_delivery_result(meta)
    assert set(result) == {"accepted_delivery", "delivery_failed", "accepted_delivery_reasons"}
    assert meta["accepted_delivery"] is True
    assert meta["delivery_failed"] is False


def test_commit_or_verification_failure_cannot_be_accepted():
    # 生产 run（delivery_attempted=True）缺 commit → 不 accepted（CR-#4：harness 不强制 commit）。
    missing_commit = apply_delivery_result(
        _meta(delivery_attempted=True, commit_hash="", commit_hashes=[],
              results=[{"subtask_id": "s1", "status": "completed", "verify_ok": True}])
    )
    verification_failed = apply_delivery_result(
        _meta(results=[{"subtask_id": "s1", "status": "failed", "verify_ok": False}], commit_hash="")
    )
    assert missing_commit["accepted_delivery"] is False
    assert verification_failed["accepted_delivery"] is False


def test_pr_failure_is_delivery_failure_not_model_failure():
    meta = _meta(pr_url="", delivery_attempted=True)
    result = apply_delivery_result(meta)
    assert result["accepted_delivery"] is False
    assert result["delivery_failed"] is True
    assert meta.get("failure_class") != "model_failure"


def test_infrastructure_failure_is_excluded_from_capability_denominator():
    metrics = compute_frozen_metrics([
        {"failure_class": "infrastructure_failure", "total_cost_usd": 1.0, "accepted_delivery": False},
        {"failure_class": "verification_failure", "total_cost_usd": 1.0, "accepted_delivery": False},
    ])
    assert metrics["valid_task_count"] == 1
    assert metrics["failure_class_summary"]["excluded_reasons"] == {"infrastructure_failure": 1}


def test_smoke_suite_executes_only_catalog_smoke_tasks_and_emits_complete_records(tmp_path: Path):
    tasks_dir = tmp_path / "eval_suite"
    (tasks_dir / "tasks").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (tasks_dir / "task_catalog.json").write_text(json.dumps({
        "smoke-task": {"suites": ["smoke"]},
        "core-task": {"suites": ["core"]},
    }), encoding="utf-8")
    for task_id in ("smoke-task", "core-task"):
        (tasks_dir / "tasks" / f"{task_id}.yaml").write_text(
            f"id: {task_id}\ndifficulty: easy\nrepo: {repo}\ntask: do {task_id}\nverification: ['true']\n",
            encoding="utf-8",
        )
    output = tmp_path / "results.jsonl"
    calls = []

    def fake_run(task, _repo, _model, task_id, **_kwargs):
        calls.append(task_id)
        return {"task_id": task_id, "model": _model, "binary_pass": True, "per_subtask": []}

    args = argparse.Namespace(
        tasks=str(tasks_dir), candidate_models="m1", repeat=1, output=str(output),
        source_batch="smoke-batch", no_skills=False, yes=True, eval_all=False,
        bench_suite="smoke", bench_parallel=1,
    )
    with patch("agent_go.bench._run_one_task", side_effect=fake_run), \
         patch("agent_go.bench._preflight_model_pricing", return_value=True):
        cmd_bench(args)

    assert calls == ["smoke-task"]
    records = validate_results_file(output)
    assert len(records) == 1
    assert REQUIRED_FIELDS <= records[0].keys()
    assert records[0]["suite"] == "smoke"
    assert records[0]["source_batch"] == "smoke-batch"
    assert records[0]["bench_schema_version"] == 1
