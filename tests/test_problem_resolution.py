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

    def test_root_cause_persisted(self, tmp_path):
        """LLM 根因总结经 record_resolution 落盘到 Problem.root_cause。"""
        path = tmp_path / "problems.jsonl"
        prob = record_resolution(
            path, failure_pattern="pytest tests/ 失败",
            task_id="task-1", subtask_id="sub-1",
            resolution_summary="根因: 模块未实现。解法: 新建 storage.py。",
            root_cause="模块未实现")
        assert prob is not None
        loaded = load(path)
        assert loaded[0].root_cause == "模块未实现"
        # 复发重开再 resolved：新 root_cause 覆盖
        record_resolution(path, failure_pattern="pytest tests/ 失败",
                          task_id="task-2", subtask_id="sub-1",
                          resolution_summary="根因: 竞态。解法: 加锁。",
                          root_cause="竞态条件")
        assert load(path)[0].root_cause == "竞态条件"


class TestSummarizeResolution:
    """LLM 根因级解法总结：开关 / 无模型 / 成功 / 非 JSON / 异常 全路径 fail-open。"""

    def test_disabled_by_config(self):
        from agent_go.problems import summarize_resolution
        with patch("agent_go.api.call_api") as m:
            r = summarize_resolution("p", "out", "fix",
                                     config={"knowledge": {"resolution_llm": False},
                                             "planner_api": {"model": "x"}})
        assert r is None
        m.assert_not_called()

    def test_no_model_noop(self):
        from agent_go.problems import summarize_resolution
        assert summarize_resolution("p", "out", "fix", config={}) is None
        assert summarize_resolution("p", "out", "fix",
                                    config={"knowledge": {}}) is None

    def test_llm_success(self, logger):
        from agent_go.problems import summarize_resolution
        cfg = {"knowledge": {"resolution_llm": True}, "planner_api": {"model": "x"}}
        with patch("agent_go.api.call_api",
                   return_value='前言 {"root_cause": "import 路径错", "fix_approach": "补 __init__.py"} 后记'):
            r = summarize_resolution("pytest 失败", "ModuleNotFoundError", "新建文件",
                                     config=cfg, logger=logger)
        assert r == {"root_cause": "import 路径错", "fix_approach": "补 __init__.py"}

    def test_llm_non_json_degrades(self, logger):
        from agent_go.problems import summarize_resolution
        cfg = {"knowledge": {"resolution_llm": True}, "planner_api": {"model": "x"}}
        with patch("agent_go.api.call_api", return_value="这不是 JSON"):
            assert summarize_resolution("p", "o", "f", config=cfg, logger=logger) is None
        # 缺字段也降级
        with patch("agent_go.api.call_api", return_value='{"root_cause": "x"}'):
            assert summarize_resolution("p", "o", "f", config=cfg, logger=logger) is None

    def test_llm_exception_degrades(self, logger):
        from agent_go.problems import summarize_resolution
        cfg = {"knowledge": {"resolution_llm": True}, "planner_api": {"model": "x"}}
        with patch("agent_go.api.call_api", side_effect=RuntimeError("api down")):
            assert summarize_resolution("p", "o", "f", config=cfg, logger=logger) is None
