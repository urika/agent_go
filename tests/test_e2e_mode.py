"""hard 端到端模式（e2e）测试：判定 + 子任务构造。

对照实验证实：hard（功能系统级）任务 Plan→拆分→局部执行丢失全局上下文导致失败，
端到端自主执行保留全局视野可完成。本模块实现"拆分 vs 端到端"判定框架。
"""
from agent_go.cli import _should_e2e, _build_e2e_subtask


class _Args:
    e2e = False
    split = False


class TestShouldE2E:
    def test_flag_e2e_overrides(self):
        class A: e2e = True; split = False
        ok, reason = _should_e2e("add a helper", {}, A())
        assert ok is True and "e2e" in reason

    def test_flag_split_overrides_hard(self):
        class A: e2e = False; split = True
        ok, _ = _should_e2e("race condition", {"min_difficulty": "hard"}, A())
        assert ok is False

    def test_l1_hard_triggers(self):
        ok, reason = _should_e2e("任意任务", {"min_difficulty": "hard"}, _Args())
        assert ok is True and "hard" in reason

    def test_l1_easy_medium_split(self):
        for d in ("easy", "medium"):
            ok, _ = _should_e2e("任意任务", {"min_difficulty": d}, _Args())
            assert ok is False

    def test_l2_arch_signals(self):
        for text in ("Fix race condition in storage", "重构系统架构",
                     "Refactor the pipeline", "性能优化 database queries",
                     "implement atomic write", "cross-file refactor"):
            ok, reason = _should_e2e(text, {}, _Args())
            assert ok is True, f"应判定端到端: {text}"

    def test_l3_default_split(self):
        for text in ("add a helper function", "fix typo in readme",
                     "write a unit test for utils"):
            ok, _ = _should_e2e(text, {}, _Args())
            assert ok is False, f"应判定拆分: {text}"

    def test_config_e2e_hard(self):
        ok, _ = _should_e2e("任意任务", {"e2e_hard": True}, _Args())
        assert ok is True


class TestBuildE2ESubtask:
    def test_structure_compatible(self):
        st = _build_e2e_subtask("Fix race condition\n要求1...", {"task_verification": ["pytest tests/ -q"]})
        # 与 run_subtask 期望字段兼容
        for key in ("id", "title", "description", "agent_prompt", "verification",
                    "depends_on", "skills", "agent_type", "difficulty"):
            assert key in st, f"缺字段: {key}"
        assert st["id"] == "sub-e2e"
        assert st["difficulty"] == "hard"          # → worker_models.hard（云端强模型）
        assert st["depends_on"] == []
        assert st["verification"] == ["pytest tests/ -q"]

    def test_verification_array_passthrough(self):
        """任务级 verification 数组原样传递（run_subtask 逐条执行）。"""
        cmds = ["pytest tests/test_concurrent.py -q", "python -c 'import src'"]
        st = _build_e2e_subtask("任务", {"task_verification": cmds})
        assert st["verification"] == cmds

    def test_no_verification_empty(self):
        st = _build_e2e_subtask("任务", {})
        assert st["verification"] == []

    def test_global_context_in_description(self):
        st = _build_e2e_subtask("Fix race condition in storage", {})
        assert "端到端模式" in st["description"]
        assert "全局视野" in st["description"]
        assert "Fix race condition" in st["description"]


class TestParallelClamp:
    """M5.3.1：--parallel 并发上限保护（clamp 1-8）。"""

    def test_normal(self):
        from agent_go.cli import _parse_parallel
        assert _parse_parallel("5") == 5

    def test_upper_bound(self):
        from agent_go.cli import _parse_parallel
        assert _parse_parallel("100") == 8
        assert _parse_parallel("9") == 8

    def test_lower_bound(self):
        from agent_go.cli import _parse_parallel
        assert _parse_parallel("0") == 1
        assert _parse_parallel("-3") == 1

    def test_invalid_fallback(self):
        from agent_go.cli import _parse_parallel
        assert _parse_parallel("abc") == 3
        assert _parse_parallel("") == 3
