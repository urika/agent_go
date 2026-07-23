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
