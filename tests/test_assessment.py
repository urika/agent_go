"""测试 assessment.py — 数据模型、持久化、分析聚合。"""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.assessment import (
    AssessmentEvent,
    write, load, load_all,
    compute_false_positive_rate,
    summarize_by_strategy,
)


def _event(**kw) -> AssessmentEvent:
    defaults = dict(task_id="t1", subtask_id="s1", trigger_source="auto")
    defaults.update(kw)
    return AssessmentEvent(**defaults)


# ═══════════════════════════════════════════════════════════════
# 读写
# ═══════════════════════════════════════════════════════════════

class TestAssessmentReadWrite:
    def test_write_then_load_roundtrip(self, tmp_path):
        """写一条事件 → 读回来 → 字段一致"""
        path = tmp_path / "assessment.jsonl"
        e = _event(task_id="t1", subtask_id="s1", trigger_source="auto",
                   verification="pytest", passed=True, confidence=0.8)
        write(path, e)
        loaded = load(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].task_id == "t1"
        assert loaded[0].subtask_id == "s1"
        assert loaded[0].passed is True
        assert loaded[0].confidence == 0.8

    def test_append_multiple_events(self, tmp_path):
        """追加写入多条事件 → 全部读回"""
        path = tmp_path / "assessment.jsonl"
        for i in range(3):
            write(path, _event(task_id=f"t{i}", subtask_id=f"s{i}"))
        loaded = load(tmp_path)
        assert len(loaded) == 3

    def test_load_empty_dir(self, tmp_path):
        """无 assessment.jsonl → 空列表"""
        assert load(tmp_path) == []

    def test_load_all_scans_all_tasks(self, tmp_path):
        """load_all 扫描所有 task-* 目录"""
        for tid in ["task-001", "task-002"]:
            td = tmp_path / tid
            td.mkdir()
            path = td / "assessment.jsonl"
            write(path, _event(task_id=tid, subtask_id="s1"))
        events = load_all(tmp_path)
        assert len(events) == 2
        assert {e.task_id for e in events} == {"task-001", "task-002"}

    def test_metering_fallback(self, tmp_path):
        """无 assessment.jsonl → 回退解析 metering.jsonl 中的 evaluator 事件"""
        path = tmp_path / "metering.jsonl"
        path.write_text("\n".join([
            json.dumps({"role": "evaluator", "task_id": "t1", "subtask_id": "s1",
                        "result": "quality_fail", "cost_usd": 0.01}),
            json.dumps({"role": "evaluator", "task_id": "t1", "subtask_id": "s2",
                        "result": "success", "cost_usd": 0.02}),
            json.dumps({"role": "planner", "task_id": "t1"}),  # 非 evaluator，被过滤
        ]), encoding="utf-8")
        events = load(tmp_path)
        assert len(events) == 2
        assert events[0].passed is False  # quality_fail
        assert events[1].passed is True   # success


# ═══════════════════════════════════════════════════════════════
# 假阳性率计算
# ═══════════════════════════════════════════════════════════════

class TestComputeFalsePositiveRate:
    def test_empty_list(self):
        fp = compute_false_positive_rate([])
        assert fp["total_evaluated"] == 0
        assert fp["false_positive_rate"] is None

    def test_all_passed(self):
        events = [_event(passed=True, confidence=0.9) for _ in range(5)]
        fp = compute_false_positive_rate(events)
        assert fp["total_evaluated"] == 5
        assert fp["flagged"] == 0
        assert fp["false_positive_rate"] == 0
        assert fp["avg_confidence"] == 0.9

    def test_mixed_results(self):
        events = [
            _event(passed=True, confidence=0.9),
            _event(passed=False, confidence=0.3),
            _event(passed=False, confidence=0.2),
            _event(passed=True, confidence=0.8),
        ]
        fp = compute_false_positive_rate(events)
        assert fp["total_evaluated"] == 4
        assert fp["flagged"] == 2
        assert fp["false_positive_rate"] == 50  # 2/4
        assert fp["avg_confidence"] == 0.85      # avg of passed events

    def test_auto_trigger_rate(self):
        events = [
            _event(trigger_source="auto", passed=True),
            _event(trigger_source="auto", passed=True),
            _event(trigger_source="manual", passed=True),
        ]
        fp = compute_false_positive_rate(events)
        assert fp["auto_trigger_rate"] == pytest.approx(66.7, rel=1)


# ═══════════════════════════════════════════════════════════════
# 按策略分组
# ═══════════════════════════════════════════════════════════════

class TestSummarizeByStrategy:
    def test_grouped_by_strategy_name(self):
        events = [
            _event(evaluator_strategy="default", passed=True),
            _event(evaluator_strategy="default", passed=False),
            _event(evaluator_strategy="strict", passed=True),
        ]
        grouped = summarize_by_strategy(events)
        assert set(grouped.keys()) == {"default", "strict"}
        assert grouped["default"]["total_evaluated"] == 2
        assert grouped["strict"]["total_evaluated"] == 1
