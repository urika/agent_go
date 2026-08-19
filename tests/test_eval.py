"""测试 eval.py — 质量/性能/成本/可靠性/UX 分析引擎

全覆盖:
  - _percentiles, _perf_score（基础工具函数）
  - analyze_quality（8 个 Q 指标 + 综合评分）
  - analyze_performance（6 个 P 指标 + 评分）
  - analyze_cost（API 调用统计 + 费用估算）
  - analyze_reliability（任务完成率 + sandbox 分布）
  - analyze_ux（文档使用/Agent 多样性/Skill 使用率）
  - aggregate_quality / aggregate_performance（聚合指标）
  - _read_meta, _read_log_events（内部辅助）
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.eval import (
    _percentiles, _perf_score,
    analyze_quality, analyze_performance,
    analyze_cost, analyze_reliability, analyze_ux,
    aggregate_quality, aggregate_performance,
    gate_cost,
    MODEL_PRICES,
)


# ═══════════════════════════════════════════════════════════════
# Helper 工具
# ═══════════════════════════════════════════════════════════════

def _make_meta(task_id="test-001", status="completed", n_subtasks=2, n_results=2):
    """构造一个标准的 meta dict。"""
    subtasks = [{"id": f"sub-{i+1}", "files_hint": "*",
                  "skills": [] if i > 0 else ["security-review"],
                  "agent_type": "developer"}
                for i in range(n_subtasks)]
    results = []
    for i in range(min(n_results, n_subtasks)):
        r = {
            "subtask_id": f"sub-{i+1}",
            "status": "completed",
            "exit_code": 0,
            "summary": "1 file changed" if i == 0 else "无文件变更",
            "verify_ok": True,
            "retry_count": 0,
            "duration_sec": 45.0 + i * 10,
            "sandbox_type": "headless",
            "change_stats": {
                "files_changed": 2 if i == 0 else 0,
                "insertions": 50 if i == 0 else 0,
                "deletions": 10 if i == 0 else 0,
                "new_files": 1 if i == 0 else 0,
                "modified_files": 1 if i == 0 else 0,
                "actual_files": ["src/main.py"] if i == 0 else [],
            },
            "timing": {
                "worktree_create_ms": 300, "merge_upstream_ms": 0,
                "claude_execute_ms": 44000, "verification_ms": 1000,
                "git_commit_ms": 200,
            },
            "merge_results": [{"upstream": "sub-0", "status": "success"}],
            "verification_results": [{"command": "pytest", "exit_code": 0,
                                       "duration_ms": 500, "attempt": 1}],
        }
        results.append(r)
    return {
        "task_id": task_id,
        "task": "测试任务",
        "status": status,
        "subtasks": subtasks,
        "results": results,
    }


def _make_log_file(log_path, events):
    """写入 execution.log 并插入结构化事件。

    注意两点，否则 _read_log_events 解析不到：
    1. 事件 JSON 必须用紧凑分隔符 — 解析按 '"event":"<name>"' 无空格形式匹配
    2. 必须先创建父目录
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for ev_type, ev_data in events:
        ev_json = json.dumps({'event': ev_type, **ev_data}, ensure_ascii=False,
                             separators=(",", ":"))
        lines.append(f"{now} | INFO | test | {ev_json}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# _percentiles / _perf_score
# ═══════════════════════════════════════════════════════════════

class TestPercentiles:
    """百分位计算"""

    def test_basic_percentiles(self):
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        result = _percentiles(data, [50, 95])
        assert result[50] == 5.5  # P50 of 1..10
        assert result[95] == 9.5  # P95 (banker's rounding: round(9.55, 1) = 9.5)

    def test_empty_data(self):
        result = _percentiles([], [50, 95])
        assert result == {50: 0, 95: 0}

    def test_single_element(self):
        result = _percentiles([7], [50, 95])
        assert result[50] == 7
        assert result[95] == 7

    def test_all_same(self):
        result = _percentiles([5, 5, 5, 5], [50, 99])
        assert result[50] == 5
        assert result[99] == 5


class TestPerfScore:
    """性能综合评分"""

    def test_perfect_score(self):
        # p1<=0 时 _perf_score 返回 50（实现中的特例短路）
        score = _perf_score(0, 0, 100)
        assert score == 50

    def test_good_score(self):
        score = _perf_score(60, 30, 80)
        # p1_score = 100-60/3 = 80, p95_score = 100-30/6 = 95, p6_score = 80
        # weighted = 80*0.3 + 95*0.3 + 80*0.4 = 24 + 28.5 + 32 = 84.5
        assert score == 84

    def test_worst_score(self):
        score = _perf_score(300, 600, 0)
        assert score < 30

    def test_zero_duration_default(self):
        score = _perf_score(0, 0, 0)
        assert score == 50

    def test_mid_range(self):
        score = _perf_score(60, 120, 50)
        assert 20 <= score <= 80


# ═══════════════════════════════════════════════════════════════
# analyze_quality
# ═══════════════════════════════════════════════════════════════

class TestAnalyzeQuality:
    """质量分析"""

    def test_basic_quality(self):
        meta = _make_meta()
        result = analyze_quality(meta)
        assert result is not None
        assert result["task_id"] == "test-001"
        assert "Q1_task_success_rate" in result
        assert "Q3_first_pass_rate" in result
        assert "score" in result

    def test_none_meta(self):
        assert analyze_quality(None) is None

    def test_no_results(self):
        meta = _make_meta(n_results=0)
        # results 为空时会返回 None 或者全 0
        result = analyze_quality(meta)
        # 如果结果列表为空，analyze_quality 返回 None
        if result is not None:
            assert result["Q1_task_success_rate"] == 0

    def test_plan_accuracy(self):
        """Q7 计划准确性在有 files_hint 时计算"""
        meta = _make_meta()
        meta["subtasks"][0]["files_hint"] = "src/main.py"
        meta["subtasks"][1]["files_hint"] = "src/*"
        result = analyze_quality(meta)
        assert result is not None
        assert "Q7_plan_accuracy_precision" in result
        assert "Q7_plan_accuracy_recall" in result

    def test_verify_pass_rate(self):
        """Q4 验证通过率"""
        meta = _make_meta()
        # 所有结果 verify_ok=True
        result = analyze_quality(meta)
        assert result["Q4_verify_pass_rate"] == 100

    def test_no_changes_counted(self):
        """no_changes 计入 Q2"""
        meta = _make_meta()
        meta["results"][0]["status"] = "no_changes"
        result = analyze_quality(meta)
        assert result["Q2_subtask_success_rate"] == 100

    def test_q11_not_present_without_task_dir(self):
        """不传 task_dir → Q11 字段不存在"""
        meta = _make_meta()
        result = analyze_quality(meta)
        assert result is not None
        assert "Q11_false_positive_rate" not in result

    def test_q11_computed_from_assessment(self, tmp_path):
        """有 assessment.jsonl → Q11 字段出现"""
        # 写 assessment.jsonl
        from agent_go.assessment import AssessmentEvent, write
        td = tmp_path / "task-001"
        td.mkdir()
        path = td / "assessment.jsonl"
        for p in [True, False, True]:
            write(path, AssessmentEvent(
                task_id="task-001", subtask_id="s1",
                trigger_source="auto", passed=p, confidence=0.8 if p else 0.2,
                evaluator_model="haiku",
            ))
        # 写 meta.json
        meta = _make_meta()
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        result = analyze_quality(meta, task_dir=td)
        assert result is not None
        assert result.get("Q11_evaluated_count") == 3
        assert result.get("Q11_flagged_count") == 1
        assert result.get("Q11_false_positive_rate") == pytest.approx(33.3, rel=1)
        assert result.get("Q11_avg_confidence") == 0.8


class TestAnalyzePerformance:
    """性能分析"""

    def test_basic_performance(self):
        meta = _make_meta()
        result = analyze_performance(meta)
        assert result is not None
        assert "P1_total_duration_sec" in result
        assert "P3_avg_subtask_sec" in result
        assert "score" in result

    def test_with_log_path(self, tmp_path):
        meta = _make_meta()
        log_path = tmp_path / "execution.log"
        _make_log_file(log_path, [
            ("plan_complete", {"plan_duration_ms": 5000, "iteration": 1}),
        ])
        result = analyze_performance(meta, log_path)
        assert result is not None
        assert result["P2_plan_duration_ms"] == 5000

    def test_phase_breakdown(self, tmp_path):
        """P5 阶段占比"""
        meta = _make_meta()
        log_path = tmp_path / "execution.log"
        _make_log_file(log_path, [("plan_complete", {"plan_duration_ms": 3000})])
        result = analyze_performance(meta, log_path)
        p5 = result.get("P5_phase_breakdown_pct", {})
        assert "claude_execute_ms" in p5 or p5 == {}

    def test_none_meta(self):
        assert analyze_performance(None) is None


class TestAnalyzeCost:
    """成本分析"""

    def test_basic_cost(self, tmp_path):
        result = analyze_cost(tmp_path)
        assert "total_calls" in result
        assert "estimated_cost_usd" in result

    def test_with_log_data(self, tmp_path):
        task_dir = tmp_path / "task-001"
        task_dir.mkdir(parents=True)
        _make_log_file(task_dir / "execution.log", [
            ("api_call", {"provider": "anthropic", "model": "claude-sonnet-4",
                          "prompt_tokens": 1000, "completion_tokens": 500}),
            ("api_call", {"provider": "deepseek", "model": "deepseek-chat",
                          "prompt_tokens": 2000, "completion_tokens": 1000}),
            ("api_error", {"provider": "anthropic", "status_code": 429}),
            ("plan_complete", {"iteration": 1, "cache_hit": True}),
        ])
        result = analyze_cost(tmp_path)
        assert result["total_calls"] == 2
        assert result["errors"] == 1
        assert result["cache_hits"] == 1
        assert result["cache_checks"] == 1

    def test_zero_calls(self, tmp_path):
        result = analyze_cost(tmp_path)
        assert result["total_calls"] == 0
        assert result["estimated_cost_usd"] == 0
        assert result["cache_hit_rate"] == 0

    def test_by_role_aggregation(self, tmp_path):
        """metering.jsonl 的 role 字段被聚合为按角色成本拆分"""
        task_dir = tmp_path / "task-001"
        task_dir.mkdir(parents=True)
        events = [
            {"role": "planner", "actual_provider": "anthropic", "actual_model": "claude-sonnet-4",
             "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.01, "result": "success"},
            {"role": "worker", "actual_provider": "claude-code", "actual_model": "claude-code-executor",
             "prompt_tokens": 5000, "completion_tokens": 2000, "cost_usd": 0.05, "result": "success"},
            {"role": "worker", "actual_provider": "claude-code", "actual_model": "claude-code-executor",
             "prompt_tokens": 3000, "completion_tokens": 1000, "cost_usd": 0.03, "result": "success"},
        ]
        (task_dir / "metering.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8")
        result = analyze_cost(tmp_path)
        assert result["total_calls"] == 3
        assert result["by_role"]["planner"] == {"calls": 1, "cost_usd": 0.01}
        assert result["by_role"]["worker"] == {"calls": 2, "cost_usd": 0.08}


# ═══════════════════════════════════════════════════════════════
# 发布门禁：$/pass rate（gate_cost 纯函数）
# ═══════════════════════════════════════════════════════════════

class TestGateCost:
    """gate_cost: PRD 北极星指标的发布门禁判定。

    语义：
      - actual is None（无完成任务/metering）→ passed=True（不阻挡新项目）
      - actual > baseline → passed=False（劣化）
      - actual <= baseline → passed=True

    注意：analyze_cost 的 cost 由 MODEL_PRICES × token 数重算，不读 metering 的 cost_usd。
    因此 fixture 通过控制 completion_tokens 精确产生目标 cost。
    sonnet-4 价格：prompt $3/M、completion $15/M。
    """

    # sonnet-4 completion: 15 USD / 1M tokens → 每 token $0.000015
    # 想要 cost=X，用 completion_tokens = X / 0.000015
    SONNET4_COMPLETION_PER_TOKEN = 15.0 / 1_000_000

    def _cost_to_tokens(self, cost_usd: float) -> dict:
        """把目标 cost 换算成 sonnet-4 的 completion_tokens（prompt 设 0 简化）。"""
        ct = round(cost_usd / self.SONNET4_COMPLETION_PER_TOKEN)
        return {"prompt_tokens": 0, "completion_tokens": ct}

    def _mk_task(self, base: Path, name: str, cost_usd: float, completed: int, failed: int = 0):
        """构造一个任务目录：metering.jsonl（token，由 analyze_cost 按价重算 cost）+ meta.json（results）。"""
        td = base / name
        td.mkdir(parents=True)
        tokens = self._cost_to_tokens(cost_usd)
        (td / "metering.jsonl").write_text(
            json.dumps({"role": "worker",
                        "prompt_tokens": tokens["prompt_tokens"],
                        "completion_tokens": tokens["completion_tokens"],
                        "actual_model": "claude-sonnet-4",
                        "actual_provider": "anthropic",
                        "cost_usd": cost_usd, "result": "success"}),
            encoding="utf-8")
        results = [{"subtask_id": f"s{i}", "status": "completed"} for i in range(completed)]
        results += [{"subtask_id": f"f{i}", "status": "failed"} for i in range(failed)]
        (td / "meta.json").write_text(json.dumps({
            "task_id": name, "status": "completed", "results": results,
        }), encoding="utf-8")

    def test_no_data_passes(self, tmp_path):
        """空目录（无任务）→ dollar_per_pass_rate=None → 通过，门禁未生效"""
        result = gate_cost(0.05, tmp_path)
        assert result["passed"] is True
        assert result["actual"] is None
        assert "门禁未生效" in result["reason"]

    def test_under_baseline_passes(self, tmp_path):
        """rate=0.02 < baseline=0.05 → 通过"""
        self._mk_task(tmp_path, "task-001", cost_usd=0.02, completed=1)
        result = gate_cost(0.05, tmp_path)
        assert result["passed"] is True
        assert result["actual"] == 0.02

    def test_over_baseline_fails(self, tmp_path):
        """rate=0.10 > baseline=0.05 → 不通过"""
        self._mk_task(tmp_path, "task-001", cost_usd=0.10, completed=1)
        result = gate_cost(0.05, tmp_path)
        assert result["passed"] is False
        assert result["actual"] == 0.10
        assert "超过基线" in result["reason"]

    def test_exact_boundary_passes(self, tmp_path):
        """rate==baseline → 通过（用 > 而非 >=，边界值放行）"""
        self._mk_task(tmp_path, "task-001", cost_usd=0.05, completed=1)
        result = gate_cost(0.05, tmp_path)
        assert result["passed"] is True
        assert result["actual"] == 0.05

    def test_aggregates_across_tasks(self, tmp_path):
        """多任务汇总：总 cost / 总 completed"""
        self._mk_task(tmp_path, "task-001", cost_usd=0.06, completed=2)
        self._mk_task(tmp_path, "task-002", cost_usd=0.04, completed=1)
        # 汇总：0.10 / 3 completed = 0.0333
        result = gate_cost(0.05, tmp_path)
        assert result["passed"] is True
        assert result["completed_subtasks"] == 3
        assert result["actual"] == round(0.10 / 3, 4)

    def test_failed_subtasks_excluded_from_denominator(self, tmp_path):
        """failed 子任务不计入 completed 分母（拉高 rate）"""
        self._mk_task(tmp_path, "task-001", cost_usd=0.10, completed=1, failed=2)
        # rate = 0.10 / 1 completed（failed 不算分母）
        result = gate_cost(0.05, tmp_path)
        assert result["actual"] == 0.10
        assert result["passed"] is False


class TestGateCostFromRecords:
    """ISSUE-37：gate_cost 支持 --results batch 隔离（不用全库扫描）。"""

    def _record(self, task_id="t1", total_cost=0.02, pass_rate=1.0,
                binary_pass=True, failure_class=None, timed_out=False):
        return {
            "task_id": task_id, "task_version": 1, "suite": "decision",
            "source_batch": "decision-20260809", "model": "deepseek-v4-flash",
            "binary_pass": binary_pass, "pass_rate": pass_rate,
            "failure_class": failure_class, "total_cost_usd": total_cost,
            "timed_out": timed_out, "bench_schema_version": 1,
        }

    def test_records_under_baseline_passes(self, tmp_path):
        """batch 数据 $/pass 低于基线 → 通过（不受全库高成本任务干扰）。"""
        records = [
            self._record("t1", total_cost=0.02, pass_rate=1.0),
            self._record("t2", total_cost=0.02, pass_rate=1.0),
        ]
        result = gate_cost(0.05, tmp_path, records=records)
        assert result["passed"] is True
        # $/pass = 0.04 / 2 = 0.02
        assert result["actual"] == round(0.04 / 2, 6)
        assert result["completed_subtasks"] == 2

    def test_records_over_baseline_fails(self, tmp_path):
        """batch 数据 $/pass 高于基线 → 不通过。"""
        records = [
            self._record("t1", total_cost=0.08, pass_rate=1.0),
        ]
        result = gate_cost(0.05, tmp_path, records=records)
        assert result["passed"] is False
        assert result["actual"] == 0.08

    def test_records_timed_out_counts_as_failure(self, tmp_path):
        """timed_out 计为失败（产品语义 timeout_disposition=failure）：pass_rate=0
        不贡献分母 → $/pass 上升，与 metric-freeze dollar_per_pass_diagnostic_usd 一致。"""
        records = [
            self._record("t1", total_cost=0.04, pass_rate=1.0),
            self._record("t2", total_cost=0.06, pass_rate=0.0, binary_pass=False,
                         failure_class="timeout", timed_out=True),
        ]
        result = gate_cost(0.05, tmp_path, records=records)
        # valid_cost=0.10, diagnostic_pass=1.0 → $/pass=0.10 > 0.05
        assert result["actual"] == round(0.10 / 1.0, 6)
        assert result["passed"] is False, "timeout 计为失败 → $/pass 应劣化"

    def test_records_empty_falls_back_to_dir(self, tmp_path):
        """records 为空列表 → 回退 tasks_dir 扫描（向后兼容）。"""
        # 空 records + 无任务目录 → actual=None 通过（门禁未生效）
        result = gate_cost(0.05, tmp_path, records=[])
        assert result["actual"] is None
        assert result["passed"] is True


class TestAnalyzeReliability:
    """可靠性分析"""

    def test_basic_reliability(self, tmp_path):
        result = analyze_reliability(tmp_path)
        assert "tasks_total" in result
        assert "success_rate" in result

    def test_mixed_status(self, tmp_path):
        td1 = tmp_path / "task-001"
        td1.mkdir()
        (td1 / "meta.json").write_text(json.dumps({
            "task_id": "task-001", "status": "completed",
            "results": [
                {"subtask_id": "sub-1", "status": "completed",
                 "sandbox_type": "greywall", "retry_count": 0},
                {"subtask_id": "sub-2", "status": "no_changes",
                 "sandbox_type": "greywall", "retry_count": 1},
            ]
        }), encoding="utf-8")

        td2 = tmp_path / "task-002"
        td2.mkdir()
        (td2 / "meta.json").write_text(json.dumps({
            "task_id": "task-002", "status": "failed",
            "results": [
                {"subtask_id": "sub-1", "status": "failed",
                 "sandbox_type": "native", "retry_count": 2},
            ]
        }), encoding="utf-8")

        result = analyze_reliability(tmp_path)
        assert result["tasks_total"] == 2
        assert result["completed"] == 1
        assert result["failed"] == 1
        assert result["success_rate"] == 50
        assert result["retries_total"] == 3

    def test_empty_dir(self, tmp_path):
        result = analyze_reliability(tmp_path)
        assert result["tasks_total"] == 0


class TestAnalyzeUX:
    """使用习惯分析"""

    def test_basic_ux(self, tmp_path):
        result = analyze_ux(tmp_path)
        assert "tasks_total" in result
        assert "docs_usage_pct" in result

    def test_with_data(self, tmp_path):
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": "task-001", "status": "completed",
            "reference_docs": ["README.md"],
            "results": [
                {"subtask_id": "sub-1", "agent_type_source": "llm"},
                {"subtask_id": "sub-2", "agent_type_source": "rule"},
            ],
            "subtasks": [
                {"id": "sub-1", "skills": ["security"]},
                {"id": "sub-2", "skills": []},
            ],
        }), encoding="utf-8")
        (td / "execution.log").write_text(
            '2026-01-01 | INFO | test | {"event":"plan_generate","iteration":2}\n',
            encoding="utf-8",
        )

        result = analyze_ux(tmp_path)
        assert result["tasks_total"] == 1
        assert result["docs_usage_pct"] == 100
        assert result["avg_plan_iterations"] == 2


class TestAggregateQuality:
    """质量聚合"""

    def test_empty(self):
        result = aggregate_quality(Path("/nonexistent"))
        assert result is None

    def test_aggregate(self, tmp_path):
        for i in range(3):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(json.dumps(
                _make_meta(f"task-00{i}")
            ), encoding="utf-8")

        result = aggregate_quality(tmp_path)
        assert result is not None
        assert result["tasks_analyzed"] == 3
        assert "avg_score" in result


class TestAggregatePerformance:
    """性能聚合"""

    def test_empty(self):
        result = aggregate_performance(Path("/nonexistent"))
        # 空路径返回带 tasks_analyzed=0 的 dict（非 None）
        assert result is not None
        assert result["tasks_analyzed"] == 0

    def test_aggregate(self, tmp_path):
        for i in range(2):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(json.dumps(
                _make_meta(f"task-00{i}")
            ), encoding="utf-8")
            (td / "execution.log").write_text(
                "2026-01-01 | INFO | test | something\n",
                encoding="utf-8",
            )

        result = aggregate_performance(tmp_path)
        if result:
            assert "tasks_analyzed" in result


# ═══════════════════════════════════════════════════════════════
# C3：诊断维度聚合（route 分布 / hit_ratio 分档 / 注入分布）
# ═══════════════════════════════════════════════════════════════

class TestAnalyzeCostDiagnostics:
    """analyze_cost 消费 R8 route_target / R13 hit_ratio / feedback_injected"""

    def _write_metering(self, tmp_path, events):
        task_dir = tmp_path / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "metering.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8")

    def test_route_distribution_and_cloud_warning(self, tmp_path):
        events = [
            {"role": "worker", "actual_model": "m1", "prompt_tokens": 10,
             "completion_tokens": 5, "cost_usd": 0.0, "route_target": "cloud"},
            {"role": "worker", "actual_model": "m1", "prompt_tokens": 10,
             "completion_tokens": 5, "cost_usd": 0.0, "route_target": "local"},
            {"role": "worker", "actual_model": "m1", "prompt_tokens": 10,
             "completion_tokens": 5, "cost_usd": 0.0, "route_target": "local"},
        ]
        self._write_metering(tmp_path, events)
        result = analyze_cost(tmp_path)
        assert result["route_distribution"]["cloud"] == {"count": 1, "pct": 33.3}
        assert result["route_distribution"]["local"]["count"] == 2
        assert result["route_cloud_warning"] is True  # 33.3% > 30%

    def test_hit_ratio_stats(self, tmp_path):
        events = [
            {"role": "planner", "actual_model": "m1", "prompt_tokens": 100,
             "completion_tokens": 5, "cost_usd": 0.0, "hit_ratio": hr}
            for hr in (0.90, 0.95, 0.99)
        ]
        self._write_metering(tmp_path, events)
        stats = analyze_cost(tmp_path)["hit_ratio_by_model"]["m1"]
        assert stats["n"] == 3
        assert stats["p50"] == 0.95
        assert "note" not in stats

    def test_hit_ratio_small_sample_noted(self, tmp_path):
        events = [{"role": "planner", "actual_model": "m1", "prompt_tokens": 100,
                   "completion_tokens": 5, "cost_usd": 0.0, "hit_ratio": 0.9}]
        self._write_metering(tmp_path, events)
        stats = analyze_cost(tmp_path)["hit_ratio_by_model"]["m1"]
        assert stats["n"] == 1
        assert stats["note"] == "样本<3，不参评"

    def test_feedback_injected_distribution(self, tmp_path):
        events = [
            {"role": "planner", "actual_model": "m1", "prompt_tokens": 10,
             "completion_tokens": 5, "cost_usd": 0.0,
             "feedback_injected": ["loop_l1", "blocker"]},
            {"role": "worker_diag", "injection_counts": {"loop_l1": 3},
             "session_key": "k", "result": "success"},
        ]
        self._write_metering(tmp_path, events)
        diag = analyze_cost(tmp_path)["diagnostics"]
        assert diag["feedback_injected_events"] == 1
        assert diag["injection_counts"] == {"blocker": 1, "loop_l1": 4}

    def test_old_records_without_diag_fields(self, tmp_path):
        """旧批次（无任何新字段）→ 空结构，不报错"""
        events = [{"role": "planner", "actual_model": "m1", "prompt_tokens": 10,
                   "completion_tokens": 5, "cost_usd": 0.01, "result": "success"}]
        self._write_metering(tmp_path, events)
        result = analyze_cost(tmp_path)
        assert result["route_distribution"] == {}
        assert result["route_cloud_warning"] is False
        assert result["hit_ratio_by_model"] == {}
        assert result["diagnostics"]["injection_counts"] == {}
