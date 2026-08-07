"""bench 目录匹配逻辑测试（S8 目录错配 bug 修复）。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_collect_result_stale_aborted_all_subtasks_done(tmp_path):
    """被 SIGKILL 但全部计划子任务已完成+验证通过 → 视为完工（收尾阶段被杀）。

    与 test_collect_result_stale_aborted_not_counted_as_pass 的区别：
    此处 meta 含 subtasks 元数据，results 覆盖全部计划子任务且均 completed+verify_ok，
    说明任务实际已跑完，仅收尾被中断 → 应计通过。
    """
    td = tmp_path / "task-done"
    _write_full_meta(td, "Some integration task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
        {"subtask_id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    # 补充 subtasks 元数据
    meta_path = td / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["subtasks"] = [
        {"id": "sub-1"}, {"id": "sub-2"},
    ]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some integration task",
    )
    assert result["subprocess_exit"] == -9
    assert result["completed"] == 2
    assert result["pass_rate"] == 1.0


def test_collect_result_stale_aborted_partial_subtasks(tmp_path):
    """被 SIGKILL 且部分子任务未完成（results 未覆盖全部计划）→ 计失败。"""
    td = tmp_path / "task-partial"
    _write_full_meta(td, "Some integration task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
    ])
    meta_path = td / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["subtasks"] = [{"id": "sub-1"}, {"id": "sub-2"}]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some integration task",
    )
    assert result["completed"] == 0
    assert result["pass_rate"] == 0.0


def test_collect_result_completed_status_with_nonzero_exit(tmp_path):
    """meta.status=completed 但 exit_code 非零 → 仍计通过。

    任务明确成功（meta.completed），exit_code 非零可能来自收尾命令
    （cleanup/push）的退出码，不应把成功任务误判为失败。
    """
    td = tmp_path / "task-ok-exit1"
    _write_full_meta(td, "Some integration task", "completed", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
        {"subtask_id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    meta_path = td / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["subtasks"] = [{"id": "sub-1"}, {"id": "sub-2"}]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    result = _collect_result(
        "integration-tests", "claude-haiku-4-5", 200.0, 1, "",
        exact_td=td, expected_task="Some integration task",
    )
    assert result["subprocess_exit"] == 1
    assert result["completed"] == 2
    assert result["pass_rate"] == 1.0


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


# ═══════════════════════════════════════════════════════════════
# S10-P2 P1 字段（per_subtask / binary_pass / semantic_pass / plan_step_count）


def _write_meta_with_semantic(td: Path, task: str, results: list) -> None:
    """写 meta.json（含 verification_results 的 semantic 评估 + subtasks 列表）。"""
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task": task, "status": "completed",
        "results": results,
        "subtasks": [{"id": f"sub-{i}", "title": f"t{i}"} for i in range(1, len(results) + 1)],
    }), encoding="utf-8")
    (td / "metering.jsonl").write_text("", encoding="utf-8")


def test_collect_result_semantic_pass_true(tmp_path):
    """全部子任务语义评估通过 → semantic_pass=True。"""
    td = tmp_path / "task-sem-ok"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "semantic", "passed": True}]},
        {"id": "sub-2", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "semantic", "passed": True}]},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["semantic_pass"] is True
    assert r["binary_pass"] is True


def test_collect_result_semantic_pass_false(tmp_path):
    """任一子任务语义评估失败 → semantic_pass=False，binary_pass=False。"""
    td = tmp_path / "task-sem-fail"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "semantic", "passed": True}]},
        {"id": "sub-2", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "semantic", "passed": False, "reason": "语义不符"}]},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["semantic_pass"] is False
    assert r["binary_pass"] is False  # 语义失败 → 即使 verify_ok 也非 binary pass


def test_collect_result_semantic_disabled_none(tmp_path):
    """未启用语义评估（无 semantic 结果）→ semantic_pass=None，binary_pass 退化为 verify_ok。"""
    td = tmp_path / "task-sem-none"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "shell", "exit_code": 0}]},
        {"id": "sub-2", "status": "completed", "verify_ok": True,
         "verification_results": [{"type": "shell", "exit_code": 0}]},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["semantic_pass"] is None
    assert r["binary_pass"] is True  # 无语义时退化为 all_verify_ok


def test_collect_result_per_subtask_structure(tmp_path):
    """per_subtask 提取每个子任务的 sub_id/status/retries/verify_ok/semantic_ok。"""
    td = tmp_path / "task-per-sub"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True, "retry_count": 2,
         "verification_results": [{"type": "semantic", "passed": True}]},
        {"id": "sub-2", "status": "failed", "verify_ok": False, "retry_count": 0,
         "verification_results": []},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["per_subtask"][0] == {
        "sub_id": "sub-1", "status": "completed", "retries": 2,
        "verify_ok": True, "semantic_ok": True, "kill_reason": None,
    }
    assert r["per_subtask"][1]["semantic_ok"] is None  # 无 semantic 结果


def test_collect_result_plan_step_count(tmp_path):
    """plan_step_count = subtasks 列表长度。"""
    td = tmp_path / "task-plan-count"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True},
        {"id": "sub-2", "status": "completed", "verify_ok": True},
        {"id": "sub-3", "status": "completed", "verify_ok": True},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["plan_step_count"] == 3


def test_collect_result_semantic_api_failure_skipped(tmp_path):
    """语义评估 API 调用失败（403）→ 视为未执行（None），binary_pass 退化为 verify_ok。

    复现 S10-P2 smoke test 发现的真实场景：evaluator 因 API 故障返回
    passed=False + reason 含「API 调用失败」——不应误判为语义失败。
    """
    td = tmp_path / "task-sem-403"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True,
         "verification_results": [
             {"type": "shell", "exit_code": 0},
             {"type": "semantic", "passed": False,
              "reason": "语义评估 API 调用失败无法执行: API 请求失败 (anthropic, HTTP 403)"},
         ]},
        {"id": "sub-2", "status": "completed", "verify_ok": True,
         "verification_results": [
             {"type": "semantic", "passed": False,
              "reason": "语义评估 API 调用失败无法执行: API 请求失败 (anthropic, HTTP 403)"},
         ]},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["semantic_pass"] is None  # API 故障跳过 → 不参与判定
    assert r["binary_pass"] is True    # 退化为 all_verify_ok
    assert r["per_subtask"][0]["semantic_ok"] is None


def test_collect_result_semantic_real_failure_not_skipped(tmp_path):
    """真实语义失败（非 API 故障）→ 不被误判为跳过，semantic_pass=False。"""
    td = tmp_path / "task-sem-real"
    _write_meta_with_semantic(td, "Task", [
        {"id": "sub-1", "status": "completed", "verify_ok": True,
         "verification_results": [
             {"type": "semantic", "passed": False,
              "reason": "代码未实现要求的业务逻辑，缺少校验分支"},
         ]},
    ])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["semantic_pass"] is False  # 真实语义失败保留
    assert r["binary_pass"] is False
    assert r["per_subtask"][0]["semantic_ok"] is False


def test_collect_result_p1_fields_backward_compat(tmp_path):
    """旧 meta（无 semantic/subtasks）→ 新字段不崩溃且为默认值。"""
    td = tmp_path / "task-p1-compat"
    _write_meta_metering(td, "Old task", [])  # 复用旧 helper，含 1 个无 verification_results 的 result
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Old task")
    assert r["semantic_pass"] is None
    assert r["binary_pass"] is True  # all_verify_ok 且无语义
    assert r["per_subtask"] == [{"sub_id": "sub-1", "status": "completed",
                                 "retries": 0, "verify_ok": True, "semantic_ok": None,
                                 "kill_reason": None}]
    assert r["plan_step_count"] == 0  # 旧 meta 无 subtasks


# ═══════════════════════════════════════════════════════════════
# 动态 timeout
# ═══════════════════════════════════════════════════════════════

from agent_go.bench import _dynamic_timeout, _estimate_subtasks_from_history


def _write_result(tmp_path, task_id, total_subtasks):
    path = tmp_path / "results.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"task_id": task_id, "total_subtasks": total_subtasks}) + "\n")
    return path


def test_estimate_subtasks_max(tmp_path):
    """历史子任务数取最大值（多行时取最大，避免低估）。"""
    p = _write_result(tmp_path, "task-x", 2)
    _write_result(tmp_path, "task-x", 3)
    assert _estimate_subtasks_from_history("task-x", p) == 3


def test_estimate_subtasks_empty(tmp_path):
    p = _write_result(tmp_path, "task-x", 3)
    assert _estimate_subtasks_from_history("task-y", p) == 0


def test_estimate_subtasks_missing_file(tmp_path):
    assert _estimate_subtasks_from_history("task-x", tmp_path / "nope.jsonl") == 0


def test_dynamic_timeout_expands_for_hard_difficulty(tmp_path):
    """hard 任务按难度扩展 timeout（mult=2.5 → 150*2.5+120=495），不低于配置值。"""
    # hard + 5 子任务历史 → max(495, 5*150+120=870) = 870s > 配置 300s
    p = _write_result(tmp_path, "task-x", 5)
    assert _dynamic_timeout({"timeout": 300, "difficulty": "hard"}, "task-x", p) == 870


def test_dynamic_timeout_uses_difficulty_when_no_history(tmp_path):
    """无历史子任务数时按难度计算（G6：耗时由难度驱动，非子任务数）。"""
    # hard → 150*2.5+120 = 495s；medium → 150*1.5+120 = 345s
    assert _dynamic_timeout({"timeout": 300, "difficulty": "hard"}, "task-x", None) == 495
    assert _dynamic_timeout({"timeout": 300, "difficulty": "medium"}, "task-x", None) == 345


def test_dynamic_timeout_keeps_config_when_larger(tmp_path):
    """配置值更大时保持配置（不缩短既有 timeout）。"""
    p = _write_result(tmp_path, "task-x", 2)
    # easy → 150*1+120 = 270s；2 子任务 → 2*150+120 = 420s；均 < 配置 1200s
    assert _dynamic_timeout({"timeout": 1200, "difficulty": "easy"}, "task-x", p) == 1200


def test_dynamic_timeout_no_history_uses_config(tmp_path):
    """无历史数据且 easy 难度动态值低于配置时用配置值。"""
    # easy → 150*1+120 = 270s < 配置 900s
    assert _dynamic_timeout({"timeout": 900, "difficulty": "easy"}, "task-x", None) == 900


# ═══════════════════════════════════════════════════════════════
# S10-P2：--parallel 1 顺序执行 + 代码质量维度 + 对照基线
# ═══════════════════════════════════════════════════════════════

from agent_go.bench import (
    _collect_quality, _git_diff_files, _lint_errors_for_worktree,
    _tests_broken_for_worktree, _run_baseline_one,
)


def _write_quality_meta(td: Path, task: str) -> None:
    """写 meta.json + 两个保留 worktree（sub-1/sub-2 的 work 目录）。"""
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task": task, "status": "completed",
        "results": [
            {"id": "sub-1", "status": "completed", "verify_ok": True},
            {"id": "sub-2", "status": "completed", "verify_ok": True},
        ],
    }), encoding="utf-8")
    (td / "metering.jsonl").write_text("", encoding="utf-8")
    for sub in ("sub-1", "sub-2"):
        (td / sub / "work").mkdir(parents=True, exist_ok=True)


def test_collect_result_quality_fields_default_zero(tmp_path):
    """无 worktree（无质量数据）→ lint_errors/tests_broken 为 0。"""
    td = tmp_path / "task-noquality"
    _write_meta_metering(td, "Some task", [])
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Some task")
    assert r["lint_errors"] == 0
    assert r["tests_broken"] == 0


def test_collect_quality_empty_dir(tmp_path):
    """目录不存在 / 空 → 全 0。"""
    assert _collect_quality(tmp_path / "nope") == {"lint_errors": 0, "tests_broken": 0}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _collect_quality(empty) == {"lint_errors": 0, "tests_broken": 0}


def test_collect_quality_no_worktrees(tmp_path):
    """有 task_dir 但无 worktree → 全 0（不 crash）。"""
    td = tmp_path / "task-no-wt"
    _write_quality_meta(td, "Task")
    (td / "sub-1" / "work").rmdir()  # 移除 work
    (td / "sub-2" / "work").rmdir()
    assert _collect_quality(td) == {"lint_errors": 0, "tests_broken": 0}


def test_git_diff_files_no_git(tmp_path):
    """非 git 目录 → 空列表（容错）。"""
    d = tmp_path / "plain"
    d.mkdir()
    (d / "x.py").write_text("x = 1\n", encoding="utf-8")
    assert _git_diff_files(d) == []


def test_lint_errors_tool_missing(tmp_path):
    """ruff/mypy 不可用（FileNotFoundError）→ 0（容错）。"""
    d = tmp_path / "repo"
    d.mkdir()
    (d / ".git").mkdir()
    (d / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    # 模拟 git diff 返回该文件、但 ruff 不存在 → 返回 0
    assert _lint_errors_for_worktree(d) == 0


def test_tests_broken_pytest_missing(tmp_path):
    """pytest 不可用 → 0（容错）。"""
    d = tmp_path / "repo"
    d.mkdir()
    assert _tests_broken_for_worktree(d) == 0


def test_collect_quality_aggregates_mock(tmp_path, monkeypatch):
    """聚合：两个 worktree 的 lint/tests 之和。"""
    td = tmp_path / "task-agg"
    _write_quality_meta(td, "Task")
    monkeypatch.setattr("agent_go.bench._lint_errors_for_worktree", lambda wt: 3)
    monkeypatch.setattr("agent_go.bench._tests_broken_for_worktree", lambda wt: 2)
    assert _collect_quality(td) == {"lint_errors": 6, "tests_broken": 4}


def test_collect_result_quality_aggregated(tmp_path, monkeypatch):
    """_collect_result 聚合质量字段（mock 底层检查）。"""
    td = tmp_path / "task-qagg"
    _write_quality_meta(td, "Task")
    monkeypatch.setattr("agent_go.bench._collect_quality", lambda td: {"lint_errors": 5, "tests_broken": 1})
    r = _collect_result("t", "m", 1.0, 0, "", exact_td=td, expected_task="Task")
    assert r["lint_errors"] == 5
    assert r["tests_broken"] == 1


def test_run_baseline_one_passes(tmp_path, monkeypatch):
    """裸跑通过：verification 全绿 → pass_rate=1.0，source_batch=baseline。"""
    repo = tmp_path / "fixture"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()
    task = {
        "task": "do something",
        "verification": ["python -c 'print(1)'"],
        "timeout": 60,
    }

    import subprocess as _sp
    real_run = _sp.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            return type("CP", (), {"returncode": 0, "stdout": json.dumps({"type": "result", "total_cost_usd": 0.123}) + "\n"})()
        if "verification" in str(cmd) or ("print(1)" in str(cmd)):
            return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if cmd and cmd[0] == "python" and "-m" in cmd:
            return type("CP", (), {"returncode": 0, "stdout": "1 passed\n", "stderr": ""})()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("agent_go.bench._lint_errors_for_worktree", lambda wt: 0)
    monkeypatch.setattr("agent_go.bench._tests_broken_for_worktree", lambda wt: 0)

    r = _run_baseline_one(task, repo, "claude-haiku-4-5", "baseline-task", source_batch="baseline")
    assert r["pass_rate"] == 1.0
    assert r["binary_pass"] is True
    assert r["completed"] == 1
    assert r["source_batch"] == "baseline"
    assert r["baseline"] is True
    assert r["total_subtasks"] == 1


def test_run_baseline_one_verification_fails(tmp_path, monkeypatch):
    """裸跑 verification 失败 → pass_rate=0.0。"""
    repo = tmp_path / "fixture2"
    repo.mkdir()
    task = {
        "task": "do something",
        "verification": ["python -c 'import sys; sys.exit(1)'"],
        "timeout": 60,
    }

    import subprocess as _sp
    real_run = _sp.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            return type("CP", (), {"returncode": 0, "stdout": json.dumps({"type": "result", "total_cost_usd": 0.1}) + "\n"})()
        if "sys.exit(1)" in str(cmd):
            return type("CP", (), {"returncode": 1, "stdout": "", "stderr": "fail"})()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_sp, "run", fake_run)
    r = _run_baseline_one(task, repo, "claude-haiku-4-5", "baseline-task", source_batch="baseline")
    assert r["pass_rate"] == 0.0
    assert r["binary_pass"] is False
    assert r["failed"] == 1


# ─────────────────────────── S12-P0：度量修正（kill_reason / cleanup_race / all([])）───────────────────────────

def test_cleanup_race_credits_pass_on_timeout_all_done(tmp_path):
    """S12-P0：超时被杀但全部子任务已 completed+verify_ok → cleanup_race，计为通过。

    v3 65 条假失败的根因场景：旧逻辑要求 results 覆盖全部计划子任务 id
    （_all_resulted），但 SIGKILL 常导致 meta.subtasks 未完整落盘，于是把已完工
    任务误判失败。新逻辑只要求 timed_out + 所有已落盘 result 都完成已验证。
    """
    td = tmp_path / "task-timeout-done"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
        {"subtask_id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    # 故意不写 subtasks 元数据（模拟 SIGKILL 前 meta.subtasks 未落盘）+ 不写 metering
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=True,
    )
    assert result["kill_reason"] == "cleanup_race"
    assert result["completed"] == 2
    assert result["pass_rate"] == 1.0
    assert result["binary_pass"] is True
    assert result["all_verify_ok"] is True


def test_cleanup_race_partial_meta_coverage(tmp_path):
    """超时 + 全部已落盘 result 完成已验证，但未覆盖计划全集 → 仍计 cleanup_race 通过。

    与旧 test_collect_result_stale_aborted_partial_subtasks 的关键差异：
    旧逻辑（依赖 _all_resulted）会判失败；S12-P0（只看 _all_results_done + timed_out）判通过。
    """
    td = tmp_path / "task-partial-done"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
    ])
    meta_path = td / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["subtasks"] = [{"id": "sub-1"}, {"id": "sub-2"}]  # 计划 2 个，只落盘 1 个
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=True,
    )
    assert result["kill_reason"] == "cleanup_race"
    assert result["completed"] == 1
    assert result["pass_rate"] == 1.0


def test_kill_reason_none_on_normal_pass(tmp_path):
    """正常通过（非超时、全部完成）→ kill_reason=none。"""
    td = tmp_path / "task-ok"
    _write_full_meta(td, "Some task", "completed", [
        {"id": "sub-1", "status": "completed", "verify_ok": True},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task",
    )
    assert result["kill_reason"] == "none"
    assert result["binary_pass"] is True


def test_kill_reason_stuck_on_timeout_incomplete(tmp_path):
    """超时被杀且子任务未全部完成 → stuck_or_hardtimeout，计失败。"""
    td = tmp_path / "task-timeout-stuck"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
        {"subtask_id": "sub-2", "status": "failed", "verify_ok": False},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 960.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=True,
    )
    assert result["kill_reason"] == "stuck_or_hardtimeout"
    assert result["completed"] == 0
    assert result["pass_rate"] == 0.0


def test_all_empty_not_vacuously_true(tmp_path):
    """S12-P0 修 all([])：无 completed 子任务时 all_verify_ok / binary_pass 不应为 True。

    旧 `all(r.verify_ok for r in results if status=='completed')` 对空集返回 True，
    会把"全失败"误判为通过（v2 41 条 / v3 binary_pass 55% 矛盾的根因之一）。
    """
    td = tmp_path / "task-all-failed"
    _write_full_meta(td, "Some task", "completed", [
        {"id": "sub-1", "status": "failed", "verify_ok": False},
        {"id": "sub-2", "status": "blocked", "verify_ok": False},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task",
    )
    assert result["all_verify_ok"] is False  # 旧 all([]) 会得 True
    assert result["binary_pass"] is False
    assert result["completed"] == 0
    assert result["kill_reason"] != "none"


def test_kill_reason_infra_on_zero_cost(tmp_path):
    """未通过 + 非超时 + cost=0（无 metering）→ infra（基础设施故障，非能力失败）。"""
    td = tmp_path / "task-infra"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 50.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "infra"


def test_kill_reason_interrupted_with_cost(tmp_path):
    """未通过 + 非超时 + cost>0 → interrupted_or_unknown（花钱了但没成，非 infra）。"""
    td = tmp_path / "task-interrupted"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False},
    ])
    (td / "metering.jsonl").write_text(
        json.dumps({"cost_usd": 0.05, "latency_ms": 1000}) + "\n", encoding="utf-8")
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 50.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "interrupted_or_unknown"


def test_kill_reason_runtime_over_budget_l2_priority(tmp_path):
    """S12-P0 G1：子任务结果携带运行时 kill_reason=over_budget_l2 →
    任务级 kill_reason 采用 over_budget_l2（预算熔断优先于 stuck/infra 反推）。"""
    td = tmp_path / "task-l2-budget"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False,
         "kill_reason": "over_budget_l2"},
    ])
    # 有成本 + 非超时，反推本应是 interrupted_or_unknown；但运行时 kill_reason 优先
    (td / "metering.jsonl").write_text(
        json.dumps({"cost_usd": 0.30, "latency_ms": 1000}) + "\n", encoding="utf-8")
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "over_budget_l2"
    assert result["per_subtask"][0]["kill_reason"] == "over_budget_l2"


def test_kill_reason_runtime_stuck_beats_infra(tmp_path):
    """S12-P0 G1：子任务 kill_reason=stuck（运行时 IDLE 杀）→ 任务级 stuck，
    即使 cost=0 本会反推 infra。运行时分类优先。"""
    td = tmp_path / "task-stuck-runtime"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False,
         "kill_reason": "stuck"},
    ])
    # 无 metering（cost=0）→ 反推 infra；但运行时 stuck 优先
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 700.0, -9, "",
        exact_td=td, expected_task="Some task", timed_out=True,
    )
    assert result["kill_reason"] == "stuck"


def test_kill_reason_runtime_none_on_pass(tmp_path):
    """S12-P0 G1：通过任务（all_passed）+ 无运行时 kill_reason → none。"""
    td = tmp_path / "task-pass-runtime"
    _write_full_meta(td, "Some task", "completed", [
        {"subtask_id": "sub-1", "status": "completed", "verify_ok": True},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 60.0, 0, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "none"



# ─────────────────────────────────────────────────────────────
# S12 运行前预检：_probe_actual_model / _preflight_model_pricing
# ─────────────────────────────────────────────────────────────

class TestPreflightModelPricing:
    @patch("agent_go.bench._probe_actual_model", return_value="glm-4.7")
    def test_all_known_price_returns_true(self, mock_probe, capsys):
        from agent_go.bench import _preflight_model_pricing
        assert _preflight_model_pricing(["claude-haiku-4-5"], interactive=False) is True
        out = capsys.readouterr().out
        assert "✅ 全部模型有定价" in out

    @patch("agent_go.bench._probe_actual_model", return_value="mystery-backend")
    def test_missing_actual_price_aborts_when_no(self, mock_probe, capsys):
        """探测到无定价的实际模型 → interactive 回答 n → 返回 False（中止）。"""
        from agent_go.bench import _preflight_model_pricing
        with patch("builtins.input", return_value="n"):
            assert _preflight_model_pricing(["mystery-model"], interactive=True) is False
        out = capsys.readouterr().out
        assert "⚠️" in out

    @patch("agent_go.bench._probe_actual_model", return_value="mystery-backend")
    def test_missing_price_continue_when_y(self, mock_probe, capsys):
        """缺定价但用户确认继续 → 返回 True。"""
        from agent_go.bench import _preflight_model_pricing
        with patch("builtins.input", return_value="y"):
            assert _preflight_model_pricing(["mystery-model"], interactive=True) is True

    @patch("agent_go.bench._probe_actual_model", return_value="")
    def test_probe_failure_uses_route_price(self, mock_probe, capsys):
        """探测失败但路由名有定价 → 沿用路由名定价，视为通过。"""
        from agent_go.bench import _preflight_model_pricing
        assert _preflight_model_pricing(["claude-haiku-4-5"], interactive=False) is True
        out = capsys.readouterr().out
        assert "探测失败" in out
        assert "✅" in out

    def test_probe_actual_model_parses_response(self):
        """_probe_actual_model 从 stream-json 响应解析 message.model。"""
        from agent_go.bench import _probe_actual_model
        fake_cp = MagicMock(stdout='{"type":"stream_event","event":{"message":{"model":"glm-4.7"}}}\n')
        with patch("agent_go.bench.subprocess.run", return_value=fake_cp):
            assert _probe_actual_model("claude-haiku-4-5") == "glm-4.7"

    def test_probe_actual_model_timeout_returns_empty(self):
        from agent_go.bench import _probe_actual_model
        with patch("agent_go.bench.subprocess.run", side_effect=TimeoutError("x")):
            assert _probe_actual_model("claude-haiku-4-5") == ""
