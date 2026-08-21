"""C4 葬礼回写链路测试：重试后成功 → 「失败模式 + 解法」回写全局 Problem。

覆盖：record_resolution 创建/更新/空参、derive_retry_pattern 优先级、
以及「回写后的 resolved Problem 能被 KnowledgeStore 注入」的闭环。
"""

from unittest.mock import patch

from agent_go.problems import (derive_retry_pattern, load, record_resolution)


class TestDeriveRetryPattern:
    def test_kill_reason_priority(self):
        vrs = [{"command": "pytest tests/", "exit_code": 1}]
        assert derive_retry_pattern(vrs, "verify_revert") == "verify_revert"
        # "none" 视为无 kill_reason
        assert derive_retry_pattern(vrs, "none") == "pytest tests/"

    def test_first_failed_cmd(self):
        vrs = [{"command": "echo ok", "exit_code": 0},
               {"command": "pytest tests/", "exit_code": 1}]
        assert derive_retry_pattern(vrs, "") == "pytest tests/"

    def test_rejected_cmd_skipped(self):
        vrs = [{"command": "rm -rf /", "exit_code": 126, "rejected": True},
               {"type": "semantic", "passed": False, "reason": "实现不完整"}]
        assert derive_retry_pattern(vrs, "") == "实现不完整"

    def test_semantic_fallback(self):
        vrs = [{"type": "semantic", "passed": False, "reason": "缺少边界处理"}]
        assert derive_retry_pattern(vrs, "") == "缺少边界处理"

    def test_empty(self):
        assert derive_retry_pattern([], "") == ""
        assert derive_retry_pattern([{"command": "x", "exit_code": 0}], "") == ""


class TestRecordResolution:
    def test_create_resolved_with_funeral(self, tmp_path):
        """新建 → 直接 resolved 且带 resolution_summary（葬礼数据）。"""
        path = tmp_path / "problems.jsonl"
        prob = record_resolution(
            path, failure_pattern="pytest tests/ 失败",
            failure_class="retry_recovered", task_id="task-1", subtask_id="sub-1",
            resolution_summary="验证失败后经 2 次重试通过。失败模式: pytest tests/。修复涉及: main.py")
        assert prob is not None
        assert prob.status == "resolved"
        assert "重试" in prob.resolution_summary
        assert prob.resolved_by == "task-1/sub-1"

        # 持久化可被 load 读回
        loaded = load(path)
        assert len(loaded) == 1
        assert loaded[0].status == "resolved"
        assert loaded[0].resolution_summary

    def test_upsert_recurrence_and_new_summary(self, tmp_path):
        """同一 pattern 再次回写：occurrence 累计 + 解法更新为最新。"""
        path = tmp_path / "problems.jsonl"
        p1 = record_resolution(path, failure_pattern="pytest tests/ 失败",
                               task_id="task-1", subtask_id="sub-1",
                               resolution_summary="解法 A")
        p2 = record_resolution(path, failure_pattern="pytest tests/ 失败",
                               task_id="task-2", subtask_id="sub-3",
                               resolution_summary="解法 B")
        assert p1.id == p2.id
        loaded = load(path)
        assert len(loaded) == 1
        assert loaded[0].occurrence_count == 2
        assert loaded[0].status == "resolved"
        assert loaded[0].resolution_summary == "解法 B"

    def test_empty_params_noop(self, tmp_path):
        path = tmp_path / "problems.jsonl"
        assert record_resolution(path, failure_pattern="", resolution_summary="x") is None
        assert record_resolution(path, failure_pattern="x", resolution_summary="") is None
        assert not path.exists() or not path.read_text().strip()

    def test_written_funeral_injectable_by_knowledge(self, tmp_path, logger):
        """闭环：回写的 resolved Problem 能被 KnowledgeStore 匹配注入（含解法）。"""
        from agent_go.knowledge import build_repair_knowledge
        path = tmp_path / "problems.jsonl"
        record_resolution(path, failure_pattern="pytest tests/ 失败",
                          task_id="task-1", subtask_id="sub-1",
                          resolution_summary="先 source venv 再跑 pytest")
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-9"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger)
        assert result["sources"], "回写的 Problem 应可被注入匹配"
        assert "source venv" in result["text"], "解法摘要应进入注入文本"
