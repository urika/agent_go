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


# ═══════════════════════════════════════════════════════════════
# CR-G6: cmd_bench 编排器（修复 ImportError + 编排正确性）
# ═══════════════════════════════════════════════════════════════

def test_cmd_bench_orchestrates_tasks_models_repeat(tmp_path):
    """CR-G6：cmd_bench 按 tasks × models × repeat 笛卡尔积编排 _run_one_task，
    每次结果写一行、model/repeat 注入。修复前 `from .bench import cmd_bench` ImportError。"""
    import argparse
    from agent_go.bench import cmd_bench

    tasks_dir = tmp_path / "eval_suite"
    (tasks_dir / "tasks").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in (1, 2):
        (tasks_dir / "tasks" / f"t{i}.yaml").write_text(
            f"id: t{i}\nrepo: {repo}\ntask: do task {i}\nverification: ['true']\n",
            encoding="utf-8")

    out = tmp_path / "results.jsonl"
    calls = {"n": 0}

    def _fake_run_one_task(task, _repo, model, task_id, **kw):
        calls["n"] += 1
        return {"task_id": task_id, "model": model, "binary_pass": True, "per_subtask": []}

    args = argparse.Namespace(
        tasks=str(tasks_dir), candidate_models="m1,m2", repeat=2,
        output=str(out), source_batch="bench", no_skills=False,
        yes=True, eval_all=False)

    with patch("agent_go.bench._run_one_task", side_effect=_fake_run_one_task), \
         patch("agent_go.bench._preflight_model_pricing", return_value=True):
        cmd_bench(args)

    assert calls["n"] == 8  # 2 tasks × 2 models × 2 repeat
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 8
    for ln in lines:
        rec = json.loads(ln)
        assert rec["model"] in ("m1", "m2")
        assert "repeat" in rec


def test_cmd_bench_no_models_errors(tmp_path):
    """CR-G6：未指定 --candidate-models → 报错 sys.exit。"""
    import argparse
    from agent_go.bench import cmd_bench
    args = argparse.Namespace(
        tasks=str(tmp_path), candidate_models="", repeat=1,
        output=str(tmp_path / "o.jsonl"), source_batch="bench", no_skills=False,
        yes=True, eval_all=False)
    with patch("agent_go.bench._preflight_model_pricing", return_value=True):
        try:
            cmd_bench(args)
            assert False, "应 sys.exit"
        except SystemExit:
            pass


def test_cmd_bench_handles_list_return(tmp_path):
    """CR-G6：_run_one_task 历史 list[dict] 签名也逐条写入（防御）。"""
    import argparse
    from agent_go.bench import cmd_bench
    tasks_dir = tmp_path / "es"
    (tasks_dir / "tasks").mkdir(parents=True)
    (tasks_dir / "tasks" / "t1.yaml").write_text(
        f"id: t1\nrepo: {tmp_path}\ntask: x\nverification: ['true']\n", encoding="utf-8")
    out = tmp_path / "r.jsonl"

    def _list_ret(task, _repo, model, task_id, **kw):
        return [{"task_id": task_id, "model": model, "sub": "a"},
                {"task_id": task_id, "model": model, "sub": "b"}]

    args = argparse.Namespace(tasks=str(tasks_dir), candidate_models="m1", repeat=1,
                              output=str(out), source_batch="bench", no_skills=False,
                              yes=True, eval_all=False)
    with patch("agent_go.bench._run_one_task", side_effect=_list_ret), \
         patch("agent_go.bench._preflight_model_pricing", return_value=True):
        cmd_bench(args)
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # 1 task × 1 model × 1 repeat，但 _run_one_task 返回 2 条


# ═══════════════════════════════════════════════════════════════
# CR-G1: _recommend 成本维度 + best_value
# ═══════════════════════════════════════════════════════════════

def test_recommend_cost_downgrade_recommended_to_conditional():
    """CR-G1：≥80% 通过但 $/pass > 2× 中位数 → recommended 降 conditional，roles 不变。"""
    from agent_go.bench import _recommend
    cat, roles, reason = _recommend("expensive-opus", 0.85, 1.0, 5,
                                    dollar_per_pass=0.10, dpp_median=0.03)
    assert cat == "conditional"
    assert roles == ["worker_easy", "worker_medium", "worker_hard"]  # 能力不变
    assert "成本过高" in reason


def test_recommend_no_downgrade_when_cost_within_median():
    """CR-G1：$/pass ≤ 2× 中位数 → 保持 recommended。"""
    from agent_go.bench import _recommend
    cat, _roles, reason = _recommend("cheap-sonnet", 0.85, 0.2, 5,
                                     dollar_per_pass=0.03, dpp_median=0.03)
    assert cat == "recommended"
    assert "成本过高" not in reason


def test_recommend_no_downgrade_for_conditional():
    """CR-G1：成本降档只影响 recommended，conditional 不再往下压成 discouraged。"""
    from agent_go.bench import _recommend
    cat, roles, _reason = _recommend("m", 0.72, 1.0, 5,
                                     dollar_per_pass=9.99, dpp_median=0.01)
    assert cat == "conditional"
    assert roles == ["worker_easy", "worker_medium"]


def test_recommend_backward_compat_no_cost_args():
    """CR-G1：不传成本参数（旧调用）行为不变。"""
    from agent_go.bench import _recommend
    assert _recommend("m", 0.85, 0.2, 5)[0] == "recommended"
    assert _recommend("m", 0.55, 0.2, 5)[0] == "discouraged"
    assert _recommend("m", 0.85, 0.2, 2)[0] == "insufficient_data"


def test_median_helper():
    from agent_go.bench import _median
    assert _median([]) is None
    assert _median([1.0]) == 1.0
    assert _median([1.0, 2.0]) == 1.5
    assert _median([3.0, 1.0, 2.0]) == 2.0


def _g1_rec(model, pass_rate, cost, n=3):
    """构造 n 条同模型 record：平均通过率 pass_rate、平均成本 cost。"""
    return [dict(model=model, completed=int(pass_rate > 0), total_subtasks=1,
                 total_cost_usd=cost, pass_rate=pass_rate, per_subtask=[],
                 total_retries=0, lint_errors=0, tests_broken=0) for _ in range(n)]


def test_analyze_best_value_and_cost_downgrade(tmp_path):
    """CR-G1：analyze 算 best_value（≥70% 中 efficiency 最高）+ 贵模型 $/pass>2× 中位数 降档。"""
    from agent_go.bench import analyze_model_productivity
    out = tmp_path / "r.jsonl"
    recs = (_g1_rec("cheap-A", 0.85, 0.05)        # dpp≈0.059, eff≈17
            + _g1_rec("expensive-B", 0.85, 0.50)  # dpp≈0.59 > 2× 中位数 → 降档
            + _g1_rec("low-C", 0.50, 0.05))       # <60% → discouraged
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    data = analyze_model_productivity(out)
    mA = data["models"]["cheap-A"]
    mB = data["models"]["expensive-B"]
    mC = data["models"]["low-C"]
    assert mA.get("best_value") is True      # ≥70% 且 efficiency 最高
    assert mB.get("best_value") is False
    assert mA["recommendation"] == "recommended"
    assert mB["recommendation"] == "conditional"  # 贵 → 降档
    assert mC["recommendation"] == "discouraged"


# ═══════════════════════════════════════════════════════════════
# CR-G5: bench → worker_models 自动衔接（recommend）
# ═══════════════════════════════════════════════════════════════

import argparse


def _g5_model(name, pass_rate, dpp, roles, best_value=False, rec="recommended"):
    return {name: {"avg_pass_rate": pass_rate, "dollar_per_pass": dpp,
                   "recommendation": rec, "recommended_roles": roles,
                   "best_value": best_value}}


def test_recommend_worker_models_slot_assignment():
    """CR-G5：hard=通过率最高，medium/easy=$/pass 最低（差异化分配）。"""
    from agent_go.bench import _recommend_worker_models
    models = {
        **_g5_model("A", 0.85, 0.06, ["worker_easy", "worker_medium", "worker_hard"], best_value=True),
        **_g5_model("B", 0.75, 0.04, ["worker_easy", "worker_medium", "worker_hard"]),
        **_g5_model("C", 0.65, 0.02, ["worker_easy"], rec="conditional"),
    }
    p = _recommend_worker_models(models)
    assert p["hard"]["model"] == "A"      # 通过率最高
    assert p["medium"]["model"] == "B"    # medium-qualified 中 $/pass 最低（0.04 < 0.06）
    assert p["easy"]["model"] == "C"      # easy-qualified 中 $/pass 最低（0.02）


def test_recommend_worker_models_no_qualified():
    """CR-G5：discouraged 模型（roles=[]）→ 所有槽 None。"""
    from agent_go.bench import _recommend_worker_models
    models = _g5_model("D", 0.50, 0.01, [], rec="discouraged")
    p = _recommend_worker_models(models)
    assert p["hard"] is None and p["medium"] is None and p["easy"] is None


def _g5_analyze_return(models):
    return {"models": models, "total_runs": 99}


def _g5_args(apply=False, force=False, results="eval_suite/results.jsonl"):
    return argparse.Namespace(apply=apply, force=force, results=results)


def test_cmd_recommend_dry_run_no_write(tmp_path):
    """CR-G5：默认 dry-run 不碰 config。"""
    from agent_go.bench import cmd_recommend
    cfg = tmp_path / "config.json"
    models = {**_g5_model("claude-sonnet-5", 0.85, 0.06, ["worker_easy", "worker_medium", "worker_hard"], True),
              **_g5_model("claude-haiku-4-5", 0.70, 0.02, ["worker_easy", "worker_medium"], rec="conditional")}
    with patch("agent_go.bench.analyze_model_productivity", return_value=_g5_analyze_return(models)), \
         patch("agent_go.bench.CONFIG_PATH", cfg):
        cmd_recommend(_g5_args(apply=False))
    assert not cfg.exists(), "dry-run 不应写 config"


def test_cmd_recommend_apply_writes(tmp_path):
    """CR-G5：--apply 写入 worker_models（无 tier 错配时）。"""
    from agent_go.bench import cmd_recommend
    cfg = tmp_path / "config.json"
    models = {**_g5_model("claude-opus-4-8", 0.85, 0.06, ["worker_easy", "worker_medium", "worker_hard"], True),
              **_g5_model("claude-haiku-4-5", 0.70, 0.02, ["worker_easy", "worker_medium"], rec="conditional")}
    with patch("agent_go.bench.analyze_model_productivity", return_value=_g5_analyze_return(models)), \
         patch("agent_go.bench.CONFIG_PATH", cfg):
        cmd_recommend(_g5_args(apply=True))
    import json as _j
    saved = _j.loads(cfg.read_text(encoding="utf-8"))
    assert saved["worker_models"]["hard"] == "claude-opus-4-8"      # frontier, 通过率最高
    assert saved["worker_models"]["easy"] == "claude-haiku-4-5"     # lite, $/pass 最低


def test_cmd_recommend_apply_tier_mismatch_refuse(tmp_path):
    """CR-G5：推荐结果 tier 错配（hard=lite）+ --apply 无 --force → 拒绝写入。"""
    from agent_go.bench import cmd_recommend
    cfg = tmp_path / "config.json"
    # 让 claude-haiku-4-5（lite）成为唯一 hard-qualified → hard 槽 = lite（错配）
    models = {**_g5_model("claude-haiku-4-5", 0.82, 0.02, ["worker_easy", "worker_medium", "worker_hard"], True)}
    with patch("agent_go.bench.analyze_model_productivity", return_value=_g5_analyze_return(models)), \
         patch("agent_go.bench.CONFIG_PATH", cfg):
        try:
            cmd_recommend(_g5_args(apply=True, force=False))
            assert False, "tier 错配应 sys.exit"
        except SystemExit:
            pass
    assert not cfg.exists(), "拒绝写入时不应创建 config"


def test_cmd_recommend_apply_force_overrides_tier(tmp_path):
    """CR-G5：tier 错配 + --apply --force → 强制写入。"""
    from agent_go.bench import cmd_recommend
    cfg = tmp_path / "config.json"
    models = {**_g5_model("claude-haiku-4-5", 0.82, 0.02, ["worker_easy", "worker_medium", "worker_hard"], True)}
    with patch("agent_go.bench.analyze_model_productivity", return_value=_g5_analyze_return(models)), \
         patch("agent_go.bench.CONFIG_PATH", cfg):
        cmd_recommend(_g5_args(apply=True, force=True))
    import json as _j
    saved = _j.loads(cfg.read_text(encoding="utf-8"))
    assert saved["worker_models"]["hard"] == "claude-haiku-4-5"  # 强制写入


def test_kill_reason_runtime_over_budget_l3(tmp_path):
    """覆盖补强：子任务 kill_reason=over_budget_l3（pipeline 级熔断）→ 任务级分类正确。
    此前只测了 over_budget_l2 分支，L3 分支（bench.py 任务级 kill_reason 归因）无守护。"""
    td = tmp_path / "task-l3-budget"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "blocked", "verify_ok": False,
         "kill_reason": "over_budget_l3"},
    ])
    (td / "metering.jsonl").write_text(
        json.dumps({"cost_usd": 0.50, "latency_ms": 1000}) + "\n", encoding="utf-8")
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "over_budget_l3"
    assert result["per_subtask"][0]["kill_reason"] == "over_budget_l3"


def test_kill_reason_runtime_hard_timeout(tmp_path):
    """覆盖补强：子任务 kill_reason=hard_timeout → 任务级分类为 hard_timeout
    （而非笼统 stuck_or_hardtimeout）。"""
    td = tmp_path / "task-hardtimeout"
    _write_full_meta(td, "Some task", "stale_aborted", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False,
         "kill_reason": "hard_timeout"},
    ])
    (td / "metering.jsonl").write_text(
        json.dumps({"cost_usd": 0.10, "latency_ms": 1000}) + "\n", encoding="utf-8")
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 0, "",
        exact_td=td, expected_task="Some task", timed_out=True,
    )
    assert result["kill_reason"] == "hard_timeout"
    assert result["per_subtask"][0]["kill_reason"] == "hard_timeout"


# ─────────────────────────────────────────────────────────────
# S12 bench 并行：--bench-parallel 线程池调度
# ─────────────────────────────────────────────────────────────

class TestBenchParallel:
    def test_cmd_bench_parallel_collects_all(self, tmp_path, monkeypatch, capsys):
        """并发模式下所有 (task×model×repeat) 组合都被执行并收集。"""
        import yaml as _yaml
        from agent_go import bench as _bench

        # 构造 2 任务 × 1 模型 × 2 重复 = 4 组合
        tasks_dir = tmp_path / "eval_suite"
        (tasks_dir / "tasks").mkdir(parents=True)
        for i in (1, 2):
            (tasks_dir / "tasks" / f"task-{i}.yaml").write_text(
                _yaml.safe_dump({
                    "id": f"t{i}", "difficulty": "easy", "repo": "eval_suite/fixtures/task-mgr",
                    "task": f"do {i}", "verification": ["echo ok"], "timeout": 30,
                }), encoding="utf-8")

        out = tmp_path / "results.jsonl"

        # mock _run_one_task 返回固定结果，避免真实 claude 调用
        _calls = []
        def _fake_run_one_task(task, repo, model, task_id, **kw):
            _calls.append((task_id, model))
            return {"task_id": task_id, "model": model, "pass_rate": 1.0}
        monkeypatch.setattr(_bench, "_run_one_task", _fake_run_one_task)
        monkeypatch.setattr(_bench, "_preflight_model_pricing", lambda *a, **k: True)

        class _Args:
            tasks = str(tasks_dir)
            candidate_models = "m1"
            repeat = 2
            output = str(out)
            source_batch = "par-test"
            no_skills = False
            bench_parallel = 2
            yes = False
            eval_all = False

        # 让 cmd_bench 相对路径解析正常：monkeypatch 工作区基准
        # （tasks_dir 已绝对路径，cmd_bench 直接用）
        _bench.cmd_bench(_Args())

        # 4 个组合全部执行且结果收集
        assert len(_calls) == 4
        lines = [l for l in out.read_text().splitlines() if l.strip()]
        assert len(lines) == 4
        assert all("t" in json.loads(l)["task_id"] for l in lines)

    def test_cmd_bench_serial_when_parallel_1(self, tmp_path, monkeypatch):
        """--bench-parallel 1 → 串行执行（同样收集全部）。"""
        import yaml as _yaml
        from agent_go import bench as _bench

        tasks_dir = tmp_path / "eval_suite2"
        (tasks_dir / "tasks").mkdir(parents=True)
        (tasks_dir / "tasks" / "t.yaml").write_text(
            _yaml.safe_dump({"id": "tx", "difficulty": "easy", "repo": "eval_suite/fixtures/task-mgr",
                             "task": "x", "verification": ["echo ok"], "timeout": 30}), encoding="utf-8")
        out = tmp_path / "r2.jsonl"

        _calls = []
        def _fake_run_one_task(task, repo, model, task_id, **kw):
            _calls.append(task_id)
            return {"task_id": task_id, "model": model, "pass_rate": 1.0}
        monkeypatch.setattr(_bench, "_run_one_task", _fake_run_one_task)
        monkeypatch.setattr(_bench, "_preflight_model_pricing", lambda *a, **k: True)

        class _Args:
            tasks = str(tasks_dir)
            candidate_models = "m1,m2"
            repeat = 1
            output = str(out)
            source_batch = ""
            no_skills = False
            bench_parallel = 1
            yes = False
            eval_all = False

        _bench.cmd_bench(_Args())
        assert sorted(_calls) == ["tx", "tx"]


# ═══════════════════════════════════════════════════════════════
# CR-P1-1：low_confidence（样本<5 不决策，不参与自动路由）
# ═══════════════════════════════════════════════════════════════

def test_low_confidence_flag_for_small_samples(tmp_path):
    """n<5 → low_confidence=True，reason 含 low_confidence 注记。"""
    from agent_go.bench import analyze_model_productivity
    out = tmp_path / "r.jsonl"
    recs = _g1_rec("small-model", 0.85, 0.05, n=4)  # n=4 < 5
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    m = analyze_model_productivity(out)["models"]["small-model"]
    assert m["low_confidence"] is True
    assert "low_confidence" in m["reason"]


def test_low_confidence_cleared_at_five_samples(tmp_path):
    """n≥5 → low_confidence=False。"""
    from agent_go.bench import analyze_model_productivity
    out = tmp_path / "r.jsonl"
    recs = _g1_rec("ok-model", 0.85, 0.05, n=5)
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    m = analyze_model_productivity(out)["models"]["ok-model"]
    assert m["low_confidence"] is False


def test_recommend_excludes_low_confidence_models():
    """_recommend_worker_models 排除 low_confidence 模型（PRD：不参与自动路由）。"""
    from agent_go.bench import _recommend_worker_models
    models = {
        "small-hard": {"avg_pass_rate": 0.85, "dollar_per_pass": 0.06, "recommendation": "recommended",
                       "recommended_roles": ["worker_easy", "worker_medium", "worker_hard"],
                       "best_value": True, "low_confidence": True},  # n<5 → 排除
    }
    p = _recommend_worker_models(models)
    assert p["hard"] is None  # 唯一候选是 low_confidence → 排除 → 留空


def test_recommend_includes_non_low_confidence():
    """low_confidence=False 的合格候选正常入选。"""
    from agent_go.bench import _recommend_worker_models
    models = {
        "solid-model": {"avg_pass_rate": 0.85, "dollar_per_pass": 0.06, "recommendation": "recommended",
                        "recommended_roles": ["worker_easy", "worker_medium", "worker_hard"],
                        "best_value": True, "low_confidence": False},
    }
    p = _recommend_worker_models(models)
    assert p["hard"]["model"] == "solid-model"


# ─────────────────────────────────────────────────────────────
# system_error kill_reason 识别（内部 bug 崩溃归因）
# ─────────────────────────────────────────────────────────────

def test_kill_reason_system_error_detected(tmp_path):
    """运行时 kill_reason=system_error（内部 bug 崩溃）→ 任务级 kill_reason 归为 system_error。"""
    td = tmp_path / "task-syserr"
    _write_full_meta(td, "Some task", "failed", [
        {"subtask_id": "sub-1", "status": "failed", "verify_ok": False,
         "kill_reason": "system_error"},
    ])
    result = _collect_result(
        "task-x", "claude-haiku-4-5", 100.0, 1, "",
        exact_td=td, expected_task="Some task", timed_out=False,
    )
    assert result["kill_reason"] == "system_error"
    assert not (result["pass_rate"] or 0) > 0


# ═══════════════════════════════════════════════════════════════
# CR-P1-2：任务级 $/pass（PRD 分母缺陷修正，all-or-nothing）
# ═══════════════════════════════════════════════════════════════

def test_task_delivered_all_or_nothing():
    """_task_delivered：全部子任务通过才算交付；部分通过/全失败不算；cleanup_race 算。"""
    from agent_go.bench import _task_delivered
    # 全通过
    assert _task_delivered({"pass_rate": 1.0, "per_subtask": [
        {"status": "completed", "verify_ok": True}, {"status": "completed", "verify_ok": True}]})
    # 部分通过（pass_rate 0.5）→ 不交付
    assert not _task_delivered({"pass_rate": 0.5, "per_subtask": [
        {"status": "completed", "verify_ok": True}, {"status": "failed", "verify_ok": False}]})
    # 全失败
    assert not _task_delivered({"pass_rate": 0.0, "per_subtask": [
        {"status": "failed", "verify_ok": False}]})
    # cleanup_race：headline 0 但全部子任务完成已验证 → 交付
    assert _task_delivered({"pass_rate": 0.0, "per_subtask": [
        {"status": "completed", "verify_ok": True}, {"status": "completed", "verify_ok": True}]})


def _p12_rec(pass_rate, subtask_oks):
    return {"pass_rate": pass_rate,
            "per_subtask": [{"status": "completed" if ok else "failed", "verify_ok": ok} for ok in subtask_oks]}


def test_task_level_dollar_per_pass_vs_legacy(tmp_path):
    """任务级 $/pass = sum(cost)/交付数（全部通过才算 1），显著高于 legacy sum(cost)/sum(pass_rate)
    ——证明旧口径系统性低估真实每交付成本（K4 偏乐观）。"""
    from agent_go.bench import analyze_model_productivity
    out = tmp_path / "r.jsonl"
    # 3 个任务各跑 1 次：A 全交付($1)，B 部分($1)，C 全失败($1)
    recs = [
        {"model": "m", "completed": 2, "total_subtasks": 2, "total_cost_usd": 1.0, **_p12_rec(1.0, [True, True])},
        {"model": "m", "completed": 1, "total_subtasks": 2, "total_cost_usd": 1.0, **_p12_rec(0.5, [True, False])},
        {"model": "m", "completed": 0, "total_subtasks": 2, "total_cost_usd": 1.0, **_p12_rec(0.0, [False, False])},
    ]
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    m = analyze_model_productivity(out)["models"]["m"]
    # 交付数=1（仅 A）→ 任务级 $/pass = 3/1 = $3.00
    assert m["task_level_dollar_per_pass"] == 3.0
    # legacy sum(cost)/sum(pass_rate) = 3/1.5 = $2.00（把 B 的部分通过也当分母 → 低估）
    assert m["dollar_per_pass"] == 2.0
    assert m["task_level_dollar_per_pass"] > m["dollar_per_pass"]


# ═══════════════════════════════════════════════════════════════
# CR-#1：no_changes（成功态）计为通过
# ═══════════════════════════════════════════════════════════════

def test_collect_result_no_changes_counts_as_pass(tmp_path):
    """no_changes 子任务（任务本不需改动、验证通过）计为 completed → pass_rate 不因成功态被拉低。"""
    td = tmp_path / "task-nochange"
    _write_full_meta(td, "Some task", "completed", [
        {"subtask_id": "sub-1", "status": "no_changes", "verify_ok": True},
        {"subtask_id": "sub-2", "status": "completed", "verify_ok": True},
    ])
    result = _collect_result("t", "m", 10.0, 0, "", exact_td=td, expected_task="Some task")
    assert result["completed"] == 2
    assert result["pass_rate"] == 1.0
    assert result["all_verify_ok"] is True


# ═══════════════════════════════════════════════════════════════
# ISSUE-38：fixture 仓库 worktree 泄漏清理
# ═══════════════════════════════════════════════════════════════

def _init_git(tmp_path):
    """初始化一个真实 git 仓库。"""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess_run = __import__("subprocess").run
    subprocess_run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess_run(["git", "config", "user.email", "t@t.com"], cwd=repo)
    subprocess_run(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess_run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess_run(["git", "commit", "-qm", "init"], cwd=repo)
    return repo


def test_prune_fixture_worktrees_removes_stale(tmp_path):
    """_prune_fixture_worktrees：清理指向已删除目录的失效 worktree 注册。"""
    import subprocess as sp
    from agent_go.bench import _prune_fixture_worktrees

    repo = _init_git(tmp_path)
    wt_dir = tmp_path / "work"
    # 注册一个 worktree，然后删除其目录（模拟 bench 打断后残留注册）
    sp.run(["git", "worktree", "add", str(wt_dir), "-b", "agent_go/x/y"],
           cwd=repo, capture_output=True, check=True)
    sp.run(["rm", "-rf", str(wt_dir)], check=True)

    # 清理前：worktree list 有残留
    before = sp.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert len([l for l in before.stdout.strip().split("\n") if l.strip()]) >= 2

    _prune_fixture_worktrees(repo)

    after = sp.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert len([l for l in after.stdout.strip().split("\n") if l.strip()]) == 1, \
        "prune 后应只剩主仓库 worktree"


def test_prune_fixture_worktrees_non_git_noop(tmp_path):
    """非 git 仓库 → 无异常（幂等 no-op）。"""
    from agent_go.bench import _prune_fixture_worktrees
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    _prune_fixture_worktrees(repo)  # 不应抛异常


def test_prune_fixture_worktrees_active_kept(tmp_path):
    """活跃 worktree（目录仍存在）→ 保留。"""
    import subprocess as sp
    from agent_go.bench import _prune_fixture_worktrees

    repo = _init_git(tmp_path)
    wt_dir = tmp_path / "active-work"
    sp.run(["git", "worktree", "add", str(wt_dir), "-b", "agent_go/x/y"],
           cwd=repo, capture_output=True, check=True)

    _prune_fixture_worktrees(repo)

    after = sp.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert len([l for l in after.stdout.strip().split("\n") if l.strip()]) == 2, \
        "活跃 worktree 不应被 prune"


# ═══════════════════════════════════════════════════════════════
# P1 router recommend：完整角色路由推荐（planner/worker/reviewer + fallback）
# ═══════════════════════════════════════════════════════════════

def _role_model(name, pass_rate, dpp, best_value=False, rec="recommended", low=False):
    """构造 _recommend_roles 消费的 models 项（与 analyze_model_productivity 输出对齐）。"""
    return {name: {"avg_pass_rate": pass_rate, "dollar_per_pass": dpp,
                   "sample_size": 10, "recommendation": rec,
                   "best_value": best_value, "low_confidence": low}}


def test_recommend_roles_ironclad_rules():
    """P1：planner 不降级（无 fallback）；reviewer 与 worker 不同 provider。"""
    from agent_go.bench import _recommend_roles
    models = {
        **_role_model("deepseek-chat", 0.75, 0.008, best_value=True, rec="conditional"),
        **_role_model("claude-opus-4-8", 0.88, 0.25),
        **_role_model("gemini-3.1-pro", 0.90, 0.30),
        **_role_model("kimi-k2", 0.82, 0.05),
    }
    p = _recommend_roles(models)
    # planner = 通过率最高（gemini）且无 fallback
    assert p["planner"]["model"] == "gemini-3.1-pro"
    assert "fallback" not in p["planner"]
    # worker = 最佳性价比（deepseek best_value）→ fallback 不同 provider
    assert p["worker"]["model"] == "deepseek-chat"
    fb = p["worker"]["fallback"]
    assert fb is not None and fb["provider"] != "deepseek"
    # reviewer 与 worker 不同源
    assert p["reviewer"]["provider"] != p["worker"]["provider"]


def test_recommend_roles_cheapest_worker_when_no_best_value():
    """P1：无 best_value 时 worker 取 $/pass 最低（≥70% 能力门槛）。"""
    from agent_go.bench import _recommend_roles
    models = {
        **_role_model("claude-opus-4-8", 0.88, 0.25),
        **_role_model("gpt-4.1", 0.80, 0.12),
    }
    p = _recommend_roles(models)
    assert p["worker"]["model"] == "gpt-4.1"       # $/pass 最低且 ≥70%
    assert p["planner"]["model"] == "claude-opus-4-8"
    # reviewer：与 worker(openai) 不同源的最高通过率者
    assert p["reviewer"]["model"] == "claude-opus-4-8"
    assert p["reviewer"]["provider"] == "anthropic"


def test_recommend_roles_no_qualified_returns_none():
    """P1：无通过率≥60% 模型 → 三角色 None + note。"""
    from agent_go.bench import _recommend_roles
    models = _role_model("weak", 0.50, 0.001, rec="discouraged")
    p = _recommend_roles(models)
    assert p["planner"] is None and p["worker"] is None and p["reviewer"] is None
    assert "note" in p


def test_recommend_roles_reviewer_none_when_all_same_provider():
    """P1：合格模型均与 worker 同 provider → reviewer None 或不同源（不同源铁律）。"""
    from agent_go.bench import _recommend_roles
    models = {
        **_role_model("deepseek-chat", 0.78, 0.008, best_value=True),
        **_role_model("deepseek-v4-pro", 0.60, 0.43),
    }
    p = _recommend_roles(models)
    assert p["worker"]["model"] == "deepseek-chat"
    if p["reviewer"] is not None:
        assert p["reviewer"]["provider"] != "deepseek"


def test_apply_roles_writes_and_skips(tmp_path):
    """P1：_apply_roles 写 router.roles，保留其余 config，provider None 的角色跳过。"""
    from agent_go.bench import _apply_roles
    import json as _j
    cfg = tmp_path / "config.json"
    cfg.write_text(_j.dumps({"worker_models": {"easy": ""}}, ensure_ascii=False))
    proposal = {
        "planner": {"provider": "google", "model": "gemini-3.1-pro"},
        "worker": {"provider": "deepseek", "model": "deepseek-chat",
                   "fallback": {"provider": "google", "model": "gemini-3.1-pro"}},
        "reviewer": None,
        "unknown": {"provider": None, "model": "x"},
    }
    from unittest.mock import patch
    with patch("agent_go.bench.CONFIG_PATH", cfg):
        skipped = _apply_roles(proposal)
    saved = _j.loads(cfg.read_text(encoding="utf-8"))
    assert skipped == ["reviewer"]
    assert saved["router"]["roles"]["planner"] == {"provider": "google", "model": "gemini-3.1-pro"}
    assert saved["router"]["roles"]["worker"] == {
        "provider": "deepseek", "model": "deepseek-chat",
        "fallback": {"provider": "google", "model": "gemini-3.1-pro"}}
    assert "unknown" not in saved["router"]["roles"]      # 非三角色键不处理
    assert saved["worker_models"]["easy"] == ""      # 保留原配置


def test_apply_roles_atomic_preserves_unknown_keys(tmp_path):
    """P1：_apply_roles 原子写不破坏未知顶层键。"""
    from agent_go.bench import _apply_roles
    import json as _j
    cfg = tmp_path / "config.json"
    cfg.write_text(_j.dumps({"api_key": "k", "router": {"enabled": False}}, ensure_ascii=False))
    proposal = {"planner": {"provider": "anthropic", "model": "claude-opus-4-8"},
                "worker": None, "reviewer": None}
    from unittest.mock import patch
    with patch("agent_go.bench.CONFIG_PATH", cfg):
        _apply_roles(proposal)
    saved = _j.loads(cfg.read_text(encoding="utf-8"))
    assert saved["api_key"] == "k"
    assert saved["router"]["enabled"] is False
    assert saved["router"]["roles"]["planner"] == {"provider": "anthropic", "model": "claude-opus-4-8"}
