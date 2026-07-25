"""补强 tui.py 纯函数辅助函数的覆盖（不涉及 curses 渲染）。

现有 test_tui.py 已覆盖基本流程；本文件补：
  - _read_metering_cost：cost 汇总 / 损坏行容错 / 文件缺失
  - _get_task_status：running 超时降级为 failed / 子任务标题解析 / $/pass rate
  - _get_tail_lines：边界（空文件 / 无 | 行 / count 截断）
"""

import json
import sys
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════
# _read_metering_cost
# ═══════════════════════════════════════════════════════════════

class TestReadMeteringCost:
    def test_missing_file_returns_zero(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        assert _read_metering_cost(tmp_path) == 0.0

    def test_sums_cost_usd(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"cost_usd": 0.01}) + "\n"
            + json.dumps({"cost_usd": 0.02}) + "\n", encoding="utf-8")
        assert _read_metering_cost(tmp_path) == 0.03

    def test_skips_corrupt_lines(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text(
            "not json\n" + json.dumps({"cost_usd": 0.05}) + "\n", encoding="utf-8")
        assert _read_metering_cost(tmp_path) == 0.05

    def test_handles_missing_cost_field(self, tmp_path):
        """无 cost_usd 字段 → 视为 0"""
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"role": "planner"}) + "\n", encoding="utf-8")
        assert _read_metering_cost(tmp_path) == 0.0

    def test_null_cost_treated_as_zero(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"cost_usd": None}) + "\n", encoding="utf-8")
        assert _read_metering_cost(tmp_path) == 0.0

    def test_empty_file(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text("", encoding="utf-8")
        assert _read_metering_cost(tmp_path) == 0.0

    def test_rounded_to_6_decimal(self, tmp_path):
        from agent_go.tui import _read_metering_cost
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"cost_usd": 0.0000001}) + "\n", encoding="utf-8")
        # round(0.0000001, 6) == 0.0
        assert _read_metering_cost(tmp_path) == 0.0


# ═══════════════════════════════════════════════════════════════
# _get_task_status — running→failed 降级 / 子任务标题 / $/pass rate
# ═══════════════════════════════════════════════════════════════

class TestRunningTimeoutDegrade:
    def test_running_with_stale_log_degrades_to_failed(self, tmp_path):
        """status=running 但 log 超 600s 未更新 → 视为 failed（僵尸任务检测）"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "running",
                "subtasks": [], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        log = tmp_path / "execution.log"
        log.write_text("old log\n")
        # 把 mtime 设为 700s 前
        old_time = time.time() - 700
        import os
        os.utime(log, (old_time, old_time))
        result = _get_task_status(tmp_path)
        assert result["status"] == "failed"

    def test_running_with_fresh_log_stays_running(self, tmp_path):
        """status=running 且 log 刚更新 → 保持 running"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "running",
                "subtasks": [], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("fresh log\n")
        result = _get_task_status(tmp_path)
        assert result["status"] == "running"


class TestSubtaskTitleParsing:
    def test_extracts_current_subtask_title(self, tmp_path):
        """从 execution.log 末尾的 subtask_start 事件解析当前子任务标题"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "running",
                "subtasks": [{"id": "s1"}], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        start_event = json.dumps({"event": "subtask_start", "title": "正在执行的任务"})
        (tmp_path / "execution.log").write_text(
            f"2026-05-27 12:00:00 | INFO | x | something\n"
            f"2026-05-27 12:00:01 | INFO | x | {start_event}\n", encoding="utf-8")
        result = _get_task_status(tmp_path)
        assert result["current"] == "正在执行的任务"

    def test_malformed_log_line_no_crash(self, tmp_path):
        """损坏的 JSON 行不导致崩溃，current 为空"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "running",
                "subtasks": [], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text(
            "2026-05-27 12:00:00 | INFO | x | subtask_start {bad json}\n", encoding="utf-8")
        result = _get_task_status(tmp_path)
        assert result["current"] == ""


class TestDollarPerPass:
    """$/pass rate = 总成本 / 成功完成的子任务数（PRD 北极星指标）。"""

    def test_calculated_when_completed_exists(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "completed",
                "subtasks": [{"id": "s1"}, {"id": "s2"}],
                "results": [
                    {"subtask_id": "s1", "status": "completed"},
                    {"subtask_id": "s2", "status": "failed"},
                ]}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("log\n")
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"cost_usd": 0.10}) + "\n", encoding="utf-8")
        result = _get_task_status(tmp_path)
        # 0.10 / 1 completed = 0.1
        assert result["dollar_per_pass"] == 0.1
        assert result["completed_count"] == 1
        assert result["cost_usd"] == 0.1

    def test_none_when_no_completed(self, tmp_path):
        """无 completed 子任务 → 除零保护 → None"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "failed",
                "subtasks": [{"id": "s1"}],
                "results": [{"subtask_id": "s1", "status": "failed"}]}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "metering.jsonl").write_text(
            json.dumps({"cost_usd": 0.5}) + "\n", encoding="utf-8")
        result = _get_task_status(tmp_path)
        assert result["dollar_per_pass"] is None
        assert result["completed_count"] == 0


class TestStatusAggregation:
    def test_counts_blocked_and_retried(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000", "status": "completed",
                "subtasks": [{"id": f"s{i}"} for i in range(4)],
                "results": [
                    {"subtask_id": "s0", "status": "completed", "retry_count": 2},
                    {"subtask_id": "s1", "status": "no_changes"},
                    {"subtask_id": "s2", "status": "blocked"},
                    {"subtask_id": "s3", "status": "completed", "retry_count": 0},
                ]}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("log\n")
        result = _get_task_status(tmp_path)
        assert result["progress"] == "3/4"  # completed+no_changes+degraded
        assert result["failed"] == 0
        assert result["blocked"] == 1
        assert result["retried_success"] == 1  # s0 重试后成功

    def test_elapsed_with_millisecond_suffix(self, tmp_path):
        """created 含毫秒后缀（如 20260527-120000-545）也能解析"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "20260527-120000-545", "status": "completed",
                "subtasks": [], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("log\n")
        result = _get_task_status(tmp_path)
        # 不抛 ValueError 即可（elapsed 可能为空字符串或时延）
        assert result is not None

    def test_invalid_created_no_crash(self, tmp_path):
        """created 格式完全错误时不崩溃"""
        from agent_go.tui import _get_task_status
        meta = {"task": "x", "created": "garbage", "status": "completed",
                "subtasks": [], "results": []}
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        result = _get_task_status(tmp_path)
        assert result["elapsed"] == ""


# ═══════════════════════════════════════════════════════════════
# _get_tail_lines 边界
# ═══════════════════════════════════════════════════════════════

class TestTailLinesEdgeCases:
    def test_empty_file(self, tmp_path):
        from agent_go.tui import _get_tail_lines
        log = tmp_path / "empty.log"
        log.write_text("", encoding="utf-8")
        assert _get_tail_lines(log) == []

    def test_truncates_long_line_to_100_chars(self, tmp_path):
        from agent_go.tui import _get_tail_lines
        log = tmp_path / "x.log"
        long_msg = "y" * 200
        log.write_text(f"2026-05-27 12:00:00 | INFO | t | {long_msg}\n", encoding="utf-8")
        result = _get_tail_lines(log, 5)
        assert len(result) == 1
        assert len(result[0]) == 100

    def test_keeps_only_last_30_lines_pool(self, tmp_path):
        """内部取 lines[-30:] 后再取 count；30+ 行时只返回尾部"""
        from agent_go.tui import _get_tail_lines
        log = tmp_path / "x.log"
        lines = [f"2026-05-27 12:00:{i:02d} | INFO | t | line{i}" for i in range(50)]
        log.write_text("\n".join(lines), encoding="utf-8")
        result = _get_tail_lines(log, 10)
        assert len(result) <= 10
        assert "line49" in result[-1]
