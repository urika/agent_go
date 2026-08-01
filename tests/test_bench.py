"""bench 目录匹配逻辑测试（S8 目录错配 bug 修复）。"""

import json
from pathlib import Path

from agent_go.bench import _dir_matches_task, _collect_result


def _write_meta(td: Path, task: str) -> None:
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({"task": task}), encoding="utf-8")


def test_dir_matches_task_prefix(tmp_path):
    td = tmp_path / "task-1"
    _write_meta(td, "Add conditional branching to the data pipeline: 1. run_branch")
    assert _dir_matches_task(td, "Add conditional branching to the data pipeline: 1. run_branch")


def test_dir_matches_task_expected_trimmed(tmp_path):
    td = tmp_path / "task-1"
    _write_meta(td, "Add conditional branching")
    assert _dir_matches_task(td, "Add conditional branching to the data pipeline: 1. run_branch")


def test_dir_not_match_different_task(tmp_path):
    td = tmp_path / "task-1"
    _write_meta(td, "Implement a simple cache decorator in src/utils.py")
    assert not _dir_matches_task(td, "Build a comprehensive integration test suite")


def test_dir_matches_task_empty_expected(tmp_path):
    td = tmp_path / "task-1"
    _write_meta(td, "Anything here")
    assert _dir_matches_task(td, "")


def test_dir_matches_task_missing_meta(tmp_path):
    td = tmp_path / "task-1"
    td.mkdir(parents=True, exist_ok=True)
    assert not _dir_matches_task(td, "Some task")


def test_dir_matches_task_missing_dir(tmp_path):
    assert not _dir_matches_task(tmp_path / "nope", "Some task")


def test_dir_matches_task_bad_meta(tmp_path):
    td = tmp_path / "task-1"
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text("{not valid json", encoding="utf-8")
    assert not _dir_matches_task(td, "Some task")


def test_collect_result_prefers_exact_td(tmp_path):
    good = tmp_path / "task-good"
    _write_meta(good, "Integration test suite build")
    bad = tmp_path / "task-bad"
    _write_meta(bad, "Cache decorator")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 10.0, 0, "",
        new_dirs={bad}, exact_td=good, expected_task="Integration test suite build",
    )
    assert str(good) == result["task_dir"]


def test_collect_result_rejects_mismatched_exact_td(tmp_path):
    bad = tmp_path / "task-bad"
    _write_meta(bad, "Cache decorator")
    good = tmp_path / "task-good"
    _write_meta(good, "Integration test suite build")
    # exact_td 与期望任务不匹配 → 应丢弃，从 new_dirs 中找匹配的
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 10.0, 0, "",
        new_dirs={bad, good}, exact_td=bad, expected_task="Integration test suite build",
    )
    assert str(good) == result["task_dir"]


def test_collect_result_filters_new_dirs_by_task(tmp_path):
    bad = tmp_path / "task-bad"
    _write_meta(bad, "Cache decorator")
    good = tmp_path / "task-good"
    _write_meta(good, "Integration test suite build")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 10.0, 0, "",
        new_dirs={bad, good}, exact_td=None, expected_task="Integration test suite build",
    )
    assert str(good) == result["task_dir"]


def test_collect_result_no_match_returns_empty(tmp_path):
    bad = tmp_path / "task-bad"
    _write_meta(bad, "Cache decorator")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 10.0, 0, "",
        new_dirs={bad}, exact_td=None, expected_task="Integration test suite build",
    )
    assert result["task_dir"] == ""
    assert result["completed"] == 0


def test_collect_result_fallback_scan(tmp_path, monkeypatch):
    """差集无匹配 → 回退全盘扫描 meta.task 匹配的最近目录。"""
    from agent_go import bench
    monkeypatch.setattr(bench, "AGENT_GO_DIR", tmp_path)
    stale = tmp_path / "task-20260801-000001-000-abcd"
    _write_meta(stale, "Cache decorator")
    good = tmp_path / "task-20260801-000002-000-abcd"
    _write_meta(good, "Integration test suite build")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 10.0, 0, "",
        new_dirs=set(), exact_td=None, expected_task="Integration test suite build",
    )
    assert str(good) == result["task_dir"]


def _write_full_meta(td: Path, task: str, status: str, results: list) -> None:
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task": task,
        "status": status,
        "results": results,
    }), encoding="utf-8")


def test_collect_result_stale_aborted_not_counted_as_pass(tmp_path):
    """被 SIGKILL 的任务（exit=-9 / meta.status=stale_aborted）不应按完整通过计。

    即使子任务标记 completed+verify_ok，进程被中断说明任务未自然完成，
    pass_rate 应反映未完成，而不是 1.0。
    """
    td = tmp_path / "task-aborted"
    _write_full_meta(td, "Some integration task", "stale_aborted", [
        {"id": "sub-1", "status": "completed", "verify_ok": True},
        {"id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some integration task",
    )
    assert result["subprocess_exit"] == -9
    assert result["all_verify_ok"] is False
    assert result["completed"] == 0
    assert result["pass_rate"] == 0.0


def test_collect_result_exit0_completed_counts_pass(tmp_path):
    """正常退出（exit=0, meta.status=completed）时通过率正常计算。"""
    td = tmp_path / "task-ok"
    _write_full_meta(td, "Some integration task", "completed", [
        {"id": "sub-1", "status": "completed", "verify_ok": True},
        {"id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some integration task",
    )
    assert result["completed"] == 2
    assert result["pass_rate"] == 1.0
    assert result["all_verify_ok"] is True


def test_collect_result_stale_aborted_verify_unknown(tmp_path):
    """stale_aborted 任务的所有 subtask 不计通过，仅统计明确失败/阻塞。"""
    td = tmp_path / "task-aborted2"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"id": "sub-1", "status": "completed", "verify_ok": True},
        {"id": "sub-2", "status": "completed", "verify_ok": False},
        {"id": "sub-3", "status": "failed"},
    ])
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some task",
    )
    assert result["completed"] == 0
    assert result["failed"] == 1
    assert result["pass_rate"] == 0.0


# ═══════════════════════════════════════════════════════════════
# S10-P1 schema 扩展
# ═══════════════════════════════════════════════════════════════

def _write_meta_metering(td: Path, task: str, metering_events: list) -> None:
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task": task, "status": "completed",
        "results": [{"id": "sub-1", "status": "completed", "verify_ok": True}],
    }), encoding="utf-8")
    with open(td / "metering.jsonl", "w", encoding="utf-8") as f:
        for ev in metering_events:
            f.write(json.dumps(ev) + "\n")


def test_collect_result_has_s10_p1_schema_fields(tmp_path):
    """S10-P1：新字段 timed_out/source_batch 从参数透传，judge/planner 从 metering 提取。"""
    td = tmp_path / "task-s10"
    _write_meta_metering(td, "Some task", [
        {"role": "planner", "actual_model": "deepseek-v4-flash"},
        {"role": "worker", "actual_model": "claude-haiku-4-5"},
        {"role": "evaluator", "actual_model": "gpt-5"},
    ])
    result = _collect_result(
        "s10-task", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task",
        timed_out=True, source_batch="smoke-20260801",
    )
    assert result["timed_out"] is True
    assert result["source_batch"] == "smoke-20260801"
    assert result["planner_model"] == "deepseek-v4-flash"
    assert result["judge_model"] == "gpt-5"
    # 不传参时默认值（向后兼容）
    result2 = _collect_result("s10-task", "claude-haiku-4-5", 1.0, 0, "",
                              exact_td=td, expected_task="Some task")
    assert result2["timed_out"] is False
    assert result2["source_batch"] == ""
    assert result2["planner_model"] == "deepseek-v4-flash"
    assert result2["judge_model"] == "gpt-5"


def test_collect_result_no_metering_models_empty(tmp_path):
    """无 metering 或无对应 role 事件 → judge/planner_model 为空串。"""
    td = tmp_path / "task-nometer"
    _write_meta_metering(td, "Some task", [
        {"role": "worker", "actual_model": "claude-haiku-4-5"},
    ])
    result = _collect_result("s10-task", "claude-haiku-4-5", 1.0, 0, "",
                             exact_td=td, expected_task="Some task")
    assert result["planner_model"] == ""
    assert result["judge_model"] == ""


# ═══════════════════════════════════════════════════════════════
# S10-P1 $/pass 统一口径 + K8 修订
# ═══════════════════════════════════════════════════════════════

def test_analyze_model_productivity_s10_metrics(tmp_path):
    """$/pass = sum(cost)/sum(pass_rate)；K8 = 通过 record 中 zero-retry 占比。"""
    from agent_go.bench import analyze_model_productivity
    results = [
        {"model": "m1", "total_cost_usd": 1.0, "pass_rate": 0.5, "total_retries": 0,
         "completed": 1, "total_subtasks": 2},
        {"model": "m1", "total_cost_usd": 2.0, "pass_rate": 1.0, "total_retries": 2,
         "completed": 2, "total_subtasks": 2},
        {"model": "m1", "total_cost_usd": 3.0, "pass_rate": 0.0, "total_retries": 0,
         "completed": 0, "total_subtasks": 2},
    ]
    rp = tmp_path / "r.jsonl"
    with open(rp, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    data = analyze_model_productivity(rp)
    m1 = data["models"]["m1"]
    # $/pass = (1+2+3) / (0.5+1.0+0.0) = 6/1.5 = 4.0
    assert m1["dollar_per_pass"] == 4.0
    # K8：通过 record（pass_rate>0）2 个，其中 zero-retry 1 个 → 0.5
    assert m1["k8_zero_retry_pass_rate"] == 0.5
    # legacy 口径保留
    assert "dollar_per_pass_legacy" in m1


def test_analyze_model_productivity_k8_empty_denominator(tmp_path):
    """无通过 record → K8 = None（分母为 0）。"""
    from agent_go.bench import analyze_model_productivity
    rp = tmp_path / "r.jsonl"
    with open(rp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"model": "m1", "total_cost_usd": 1.0,
                            "pass_rate": 0.0, "total_retries": 0,
                            "completed": 0, "total_subtasks": 2}) + "\n")
    data = analyze_model_productivity(rp)
    assert data["models"]["m1"]["k8_zero_retry_pass_rate"] is None
