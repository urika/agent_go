import json

from agent_go.metadata_migration import repair_all_tasks, repair_task_metadata


def _write_task(tmp_path, results, status="VERIFICATION_FAILED"):
    task = tmp_path / "task-1"
    task.mkdir()
    (task / "meta.json").write_text(json.dumps({
        "task_id": "task-1", "status": status, "status_schema_version": 1,
        "results": results,
    }), encoding="utf-8")
    return task


def test_migration_dry_run_does_not_modify_metadata(tmp_path):
    task = _write_task(tmp_path, [{"subtask_id": "s1", "status": "failed", "verify_ok": False}])
    before = (task / "meta.json").read_text()
    report = repair_task_metadata(task)
    assert report["changed"] is True
    assert report["applied"] is False
    assert (task / "meta.json").read_text() == before


def test_migration_repairs_blocked_root_and_keeps_backup(tmp_path):
    task = _write_task(tmp_path, [
        {"subtask_id": "s1", "status": "failed", "verify_ok": False, "kill_reason": "hard_timeout"},
        {"subtask_id": "s2", "status": "blocked", "verify_ok": False, "blocked_by": ["s1"]},
    ])
    backup = tmp_path / "backup"
    report = repair_task_metadata(task, apply=True, backup_dir=backup)
    data = json.loads((task / "meta.json").read_text())
    assert report["applied"] is True
    assert data["failure_class"] == "timeout"
    assert data["results"][1]["failure_class"] == "timeout"
    assert (backup / "task-1" / "meta.json").exists()


def test_repair_all_reports_errors_and_counts(tmp_path):
    _write_task(tmp_path, [{"subtask_id": "s1", "status": "failed", "verify_ok": False}])
    report = repair_all_tasks(tmp_path)
    assert report["task_count"] == 1
    assert report["changed_task_count"] == 1


def test_migration_marks_mixed_completed_blocked_task_as_blocked(tmp_path):
    task = _write_task(tmp_path, [
        {"subtask_id": "s1", "status": "completed", "verify_ok": True, "commit_hash": "c1"},
        {"subtask_id": "s2", "status": "blocked", "verify_ok": False, "blocked_by": ["s1"]},
    ])
    repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    data = json.loads((task / "meta.json").read_text())
    assert data["status"] == "BLOCKED"


def test_migration_is_idempotent_after_apply(tmp_path):
    """apply 后再运行 dry-run 不应报告变更（修复幂等性 bug）。"""
    task = _write_task(tmp_path, [
        {"subtask_id": "s1", "status": "failed", "verify_ok": False, "kill_reason": "hard_timeout"},
        {"subtask_id": "s2", "status": "completed", "verify_ok": True, "commit_hash": "c1",
         "failure_class": "verification_failure"},
    ])
    first = repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    assert first["applied"] is True
    second = repair_task_metadata(task)
    assert second["changed"] is False, f"幂等性失败: {second['changes']}"


def test_migration_task_failure_class_idempotent(tmp_path):
    """任务级 failure_class 写入后不应每次运行重复报告变更。"""
    task = _write_task(tmp_path, [{"subtask_id": "s1", "status": "failed", "verify_ok": False}])
    repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    second = repair_task_metadata(task)
    assert second["changed"] is False


def _write_empty_blocked_task(tmp_path, name="task-1"):
    task = tmp_path / name
    task.mkdir()
    (task / "meta.json").write_text(json.dumps({
        "task_id": name, "status": "BLOCKED", "status_schema_version": 1,
        "results": [], "subtasks": [],
    }), encoding="utf-8")
    return task


def test_migration_repairs_blocked_without_result(tmp_path):
    """results 为空但 status=BLOCKED 的任务应保守补 system_error。"""
    task = _write_empty_blocked_task(tmp_path)
    report = repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    assert report["applied"] is True
    data = json.loads((task / "meta.json").read_text())
    assert data["status"] == "BLOCKED"  # 保留原状态
    assert data["failure_class"] == "system_error"
    assert data["blocked_without_result"] is True
    assert data["root_failure_class"] == "system_error"
    assert data["failure_reason"] == "blocked_without_result"


def test_migration_blocked_without_result_idempotent(tmp_path):
    """blocked_without_result 修复后再次运行不应报告变更。"""
    task = _write_empty_blocked_task(tmp_path)
    repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    second = repair_task_metadata(task)
    assert second["changed"] is False


def test_migration_skips_blocked_without_result_when_fc_exists(tmp_path):
    """已有 failure_class 的 blocked 空结果任务不应被覆盖。"""
    task = _write_empty_blocked_task(tmp_path)
    data = json.loads((task / "meta.json").read_text())
    data["failure_class"] = "budget_abort"
    (task / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    report = repair_task_metadata(task, apply=True, backup_dir=tmp_path / "backup")
    data2 = json.loads((task / "meta.json").read_text())
    assert data2["failure_class"] == "budget_abort"  # 不被覆盖
