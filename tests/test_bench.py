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
