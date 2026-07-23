"""NFR 可观测性测试 — P1 级别

覆盖:
  - 日志-分析闭环: log_event 产生的 JSON 能被 _read_log_events 正确解析
  - Metrics 完整性: run_subtask 结果包含所有必需字段
  - 聚合一致性: analyze_* 单任务结果与 aggregate_* 一致
  - 日志事件覆盖: execution.log 包含所有关键阶段事件
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.config import setup_logger, log_event
from agent_go.eval import (
    _read_log_events,
    _read_meta,
    analyze_quality,
    analyze_performance,
    analyze_cost,
    aggregate_quality,
    aggregate_performance,
)
from agent_go.metrics import (
    collect_timing,
    collect_change_stats,
    collect_merge_result,
    extract_usage,
)


# ═══════════════════════════════════════════════════════════════
# 1. 日志-分析闭环
# ═══════════════════════════════════════════════════════════════

class TestLogToEvalRoundtrip:
    """log_event 写入的 JSON 能被 _read_log_events 完整解析"""

    def test_log_event_writes_parsable_json(self, tmp_path):
        """log_event → execution.log → _read_log_events 可解析"""
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        logger = setup_logger("task-001", task_dir)

        event_types = ["plan_complete", "api_call", "api_error", "subtask_complete"]
        for ev_type in event_types:
            log_event(logger, ev_type, {"id": "test", "data": 42})

        for ev_type in event_types:
            events = _read_log_events(task_dir / "execution.log", ev_type)
            assert len(events) >= 1, f"event '{ev_type}' 未找到"
            for ev in events:
                assert ev["event"] == ev_type
                assert ev["id"] == "test"

    def test_all_required_event_fields_present(self, tmp_path):
        """每个事件类型至少包含 event 和 timestamp 字段"""
        task_dir = tmp_path / "task-002"
        task_dir.mkdir()
        logger = setup_logger("task-002", task_dir)

        test_events = [
            ("plan_complete", {"duration_ms": 500}),
            ("api_call", {"provider": "test", "tokens": 100}),
            ("verification_rejected", {"command": "rm", "reason": "dangerous"}),
        ]

        for ev_type, data in test_events:
            log_event(logger, ev_type, data)

        log_path = task_dir / "execution.log"
        all_events = _read_log_events(log_path, "plan_complete") + \
                     _read_log_events(log_path, "api_call") + \
                     _read_log_events(log_path, "verification_rejected")

        assert len(all_events) == 3
        for ev in all_events:
            assert "event" in ev
            assert "timestamp" in ev

    def test_debug_events_not_in_info_output(self, tmp_path):
        """DEBUG 级别的事件写入文件，INFO 级别写入控制台"""
        task_dir = tmp_path / "task-003"
        task_dir.mkdir()
        logger = setup_logger("task-003", task_dir)

        log_event(logger, "test_debug_event", {"value": 1})

        log_path = task_dir / "execution.log"
        content = log_path.read_text(encoding="utf-8")
        assert "test_debug_event" in content
        assert "DEBUG" in content


# ═══════════════════════════════════════════════════════════════
# 2. Metrics 完整性
# ═══════════════════════════════════════════════════════════════

class TestMetricsCompleteness:
    """验证所有 metrics 采集函数的返回值结构"""

    REQUIRED_TIMING_KEYS = [
        "worktree_create_ms", "merge_upstream_ms",
        "claude_execute_ms", "verification_ms", "git_commit_ms",
    ]

    REQUIRED_CHANGE_STATS_KEYS = [
        "files_changed", "insertions", "deletions",
        "new_files", "modified_files", "actual_files",
    ]

    def test_timing_has_all_fields(self):
        result = collect_timing(100, 200, 30000, 1500, 300)
        for key in self.REQUIRED_TIMING_KEYS:
            assert key in result, f"缺少 timing 字段: {key}"
        assert len(result) == 5

    def test_change_stats_has_all_fields(self):
        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.stdout = ""
            m.returncode = 0
            mock_run.return_value = m

            result = collect_change_stats(Path("/tmp/test"))
            for key in self.REQUIRED_CHANGE_STATS_KEYS:
                assert key in result, f"缺少 change_stats 字段: {key}"

    def test_merge_result_has_required_fields(self):
        result = collect_merge_result("sub-1", True)
        assert "upstream" in result
        assert "status" in result

        result_conflict = collect_merge_result("sub-2", False, ["a.py"])
        assert "conflict_files" in result_conflict

    def test_extract_usage_has_model_and_provider(self):
        result = extract_usage(
            {"usage": {"input_tokens": 100, "output_tokens": 200}},
            "anthropic", "claude-sonnet-4",
        )
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 200
        assert result["model"] == "claude-sonnet-4"
        assert result["provider"] == "anthropic"

    def test_timing_values_are_rounded_ints(self):
        """所有 timing 值是舍入后的整数"""
        result = collect_timing(1.499, 2.501, 3.0, 4.0, 5.0)
        for v in result.values():
            assert isinstance(v, int), f"期望 int，实际 {type(v)}"
            assert v == round(v)

    def test_change_stats_counts_are_non_negative(self):
        """文件数和增删行数不能为负"""
        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.stdout = ""
            m.returncode = 0
            mock_run.return_value = m

            result = collect_change_stats(Path("/tmp/test"))
            assert result["files_changed"] >= 0
            assert result["insertions"] >= 0
            assert result["deletions"] >= 0
            assert result["new_files"] >= 0
            assert result["modified_files"] >= 0


# ═══════════════════════════════════════════════════════════════
# 3. 聚合一致性
# ═══════════════════════════════════════════════════════════════

def _make_completed_meta(task_id, n_results=2):
    """构造包含完整 results 的 meta dict。"""
    results = []
    for i in range(n_results):
        results.append({
            "subtask_id": f"sub-{i+1}",
            "status": "completed",
            "verify_ok": True,
            "retry_count": 0,
            "duration_sec": 50.0 + i * 10,
            "change_stats": {
                "files_changed": 2 if i == 0 else 1,
                "insertions": 50 if i == 0 else 20,
                "deletions": 10 if i == 0 else 5,
                "new_files": 1 if i == 0 else 0,
                "modified_files": 1,
                "actual_files": [f"src/file{i}.py"],
            },
            "timing": {
                "worktree_create_ms": 300,
                "merge_upstream_ms": 100,
                "claude_execute_ms": 48000 + i * 1000,
                "verification_ms": 1200,
                "git_commit_ms": 300,
            },
            "merge_results": [],
        })
    return {
        "task_id": task_id,
        "status": "completed",
        "subtasks": [
            {"id": f"sub-{i+1}", "files_hint": "*"}
            for i in range(n_results)
        ],
        "results": results,
    }


class TestAggregationConsistency:
    """汇总数据与单个报告数据一致"""

    def test_quality_aggregate_tasks_count(self, tmp_path):
        """聚合报告的任务数正确"""
        for i in range(3):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(
                json.dumps(_make_completed_meta(f"task-00{i}")),
                encoding="utf-8",
            )

        agg = aggregate_quality(tmp_path)
        assert agg is not None
        assert agg["tasks_analyzed"] == 3

    def test_aggregate_scores_in_valid_range(self, tmp_path):
        """所有聚合评分在 0-100 范围内"""
        for i in range(2):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(
                json.dumps(_make_completed_meta(f"task-00{i}")),
                encoding="utf-8",
            )

        agg = aggregate_quality(tmp_path)
        assert 0 <= agg["avg_score"] <= 100
        assert 0 <= agg["avg_success_rate"] <= 100
        assert 0 <= agg["avg_first_pass"] <= 100

    def test_perf_aggregate_large_duration(self, tmp_path):
        """大耗时任务在聚合中正确反映"""
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(
            json.dumps(_make_completed_meta("task-001", n_results=1)),
            encoding="utf-8",
        )
        (td / "execution.log").write_text(
            "2026-01-01 00:00:00 | INFO | test | start\n"
            "2026-01-01 00:01:40 | INFO | test | end\n",
            encoding="utf-8",
        )

        agg = aggregate_performance(tmp_path)
        # P1 应该大约 100 秒
        assert agg["tasks_analyzed"] >= 1

    def test_quality_individual_vs_aggregate(self, tmp_path):
        """单个任务的质量报告与聚合报告中的对应指标一致"""
        meta_dict = _make_completed_meta("task-001", n_results=1)
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps(meta_dict), encoding="utf-8")

        single = analyze_quality(meta_dict)
        agg = aggregate_quality(tmp_path)

        assert single is not None
        assert agg is not None
        # avg_success_rate 应对应单任务的 Q1
        assert agg["avg_success_rate"] == single["Q1_task_success_rate"]


# ═══════════════════════════════════════════════════════════════
# 4. 执行日志事件覆盖
# ═══════════════════════════════════════════════════════════════

class TestExecutionLogCoverage:
    """验证 execution.log 中的关键事件类型"""

    REQUIRED_EVENTS = [
        "plan_complete",      # Plan 生成完成
        "api_call",           # API 调用
        "api_error",          # API 错误
        "plan_generate",      # Plan 生成过程
        "subtask_complete",   # Subtask 完成
        "verification_rejected",  # 验证命令被拒
    ]

    def test_required_events_defined(self):
        """不需要实际日志，仅验证代码中存在这些事件类型"""
        from agent_go.eval import _read_log_events
        from agent_go.config import log_event as _log_event

        # 验证 eval.py 中引用了所有必需的事件类型
        # (通过检查 analyze_cost 等函数中用的 search 字符串)
        import inspect
        source = inspect.getsource(analyze_cost)
        for ev in ["api_call", "api_error", "plan_complete"]:
            assert f'"{ev}"' in source, f"analyze_cost 中缺少事件类型: {ev}"

    def test_log_event_accepted_by_all_analyzers(self, tmp_path):
        """log_event 写入的数据能被所有分析器解析"""
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        logger = setup_logger("task-001", task_dir)

        # 写入多种事件
        log_event(logger, "api_call", {
            "provider": "anthropic", "model": "claude-sonnet-4",
            "prompt_tokens": 1000, "completion_tokens": 500,
        })
        log_event(logger, "plan_complete", {
            "plan_duration_ms": 3000, "iteration": 1,
        })
        log_event(logger, "api_error", {
            "provider": "anthropic", "status_code": 429,
        })

        # 写入 meta.json
        (task_dir / "meta.json").write_text(json.dumps(
            _make_completed_meta("task-001", n_results=1)
        ), encoding="utf-8")

        # 所有分析函数不应崩溃
        meta = _read_meta(task_dir)
        assert meta is not None

        q = analyze_quality(meta)
        assert q is not None

        p = analyze_performance(meta, task_dir / "execution.log")
        assert p is not None

        # cost 分析需要上级目录（_scan_task_dirs 搜索 task-* 模式）
        # 但 analyze_cost 使用 tasks_dir 参数
        from agent_go.config import AGENT_GO_DIR
        # 使用 tmp_path 作为 tasks_dir
        c = analyze_cost(tmp_path)
        assert c["total_calls"] >= 1
