"""AG-2 验证机械前置层测试：MechanicalGate + ChainEvalStrategy。"""

import logging

import pytest

import agent_go.evaluator as evaluator_mod
from agent_go.verify_chain import ChainEvalStrategy, MechanicalGate


@pytest.fixture
def logger():
    return logging.getLogger("test_verify_chain")


class TestMechanicalGate:
    def test_empty_diff_fails(self):
        v = MechanicalGate().verify("", "修复登录模块")
        assert v["passed"] is False
        rules = {c["rule_name"]: c for c in v["checks"]}
        assert rules["non_empty_diff"]["passed"] is False
        assert "empty diff" in rules["non_empty_diff"]["detail"]

    def test_whitespace_diff_fails(self):
        assert MechanicalGate().verify("   \n  ", "task")["passed"] is False

    def test_valid_diff_passes(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        v = MechanicalGate().verify(diff, "修复 a.py 的逻辑")
        assert v["passed"] is True

    def test_non_diff_content_passes(self):
        # 非 diff 类输出（text 任务）只要有内容即合法（与上游同规则）
        assert MechanicalGate().verify("一些文本输出", "task")["passed"] is True

    def test_topic_relevance_advisory_never_blocks(self, logger):
        # 中文描述关键词与 diff 无重叠 → 只记 advisory，不拦截
        v = MechanicalGate().verify("--- a/x.py\n+++ b/x.py\n", "修复登录模块的认证逻辑", logger)
        assert v["passed"] is True
        advisory = [c for c in v["checks"] if c["rule_name"] == "topic_relevance"]
        assert advisory and advisory[0]["passed"] is True
        assert "advisory" in advisory[0]["detail"]

    def test_first_failure(self):
        v = MechanicalGate().verify("", "task")
        assert "empty diff" in MechanicalGate.first_failure(v)
        ok = MechanicalGate().verify("content", "task")
        assert MechanicalGate.first_failure(ok) == "mechanical_check_failed"


class TestChainEvalStrategy:
    def _subtask(self):
        return {"id": "s1", "title": "修复登录", "description": "修复登录模块"}

    def test_empty_diff_short_circuits_zero_cost(self, monkeypatch, tmp_path, logger):
        called = []
        monkeypatch.setattr(evaluator_mod, "_get_worktree_diff",
                            lambda wt, base_commit="": "")
        monkeypatch.setattr(evaluator_mod, "_default_semantic_eval",
                            lambda *a, **kw: called.append(1) or {"passed": True})

        result = ChainEvalStrategy()(self._subtask(), tmp_path, "pytest -q",
                                     [], {}, logger)
        assert result["passed"] is False
        assert result["cost_usd"] == 0.0
        assert result["confidence"] == 1.0
        assert result["reason"].startswith("[mechanical]")
        assert called == [], "机械闸拦截时不得发起 LLM 调用"

    def test_pass_delegates_to_default(self, monkeypatch, tmp_path, logger):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        monkeypatch.setattr(evaluator_mod, "_get_worktree_diff",
                            lambda wt, base_commit="": diff)
        sentinel = {"passed": True, "confidence": 0.9, "reason": "ok",
                    "suggestions": "", "cost_usd": 0.01, "latency_ms": 100}
        monkeypatch.setattr(evaluator_mod, "_default_semantic_eval",
                            lambda *a, **kw: dict(sentinel))

        result = ChainEvalStrategy()(self._subtask(), tmp_path, "pytest -q",
                                     [], {}, logger)
        assert result["passed"] is True
        assert result["cost_usd"] == 0.01
        # 机械检查痕迹附加（审计），不改结论
        assert "mechanical_checks" in result
        assert all(c["passed"] for c in result["mechanical_checks"])

    def test_diff_base_prefers_pre_work_head(self, monkeypatch, tmp_path, logger):
        seen = {}

        def fake_diff(wt, base_commit=""):
            seen["base"] = base_commit
            return "--- a/a.py\n+++ b/a.py\n"

        monkeypatch.setattr(evaluator_mod, "_get_worktree_diff", fake_diff)
        monkeypatch.setattr(evaluator_mod, "_default_semantic_eval",
                            lambda *a, **kw: {"passed": True})
        ChainEvalStrategy()(self._subtask(), tmp_path, "v", [],
                            {"_pre_work_head": "abc123", "_base_commit": "root"}, logger)
        assert seen["base"] == "abc123"


class TestStrategyRegistration:
    def test_chain_registered(self):
        names = [s["name"] for s in evaluator_mod.list_strategies()]
        assert "chain" in names

    def test_evaluate_routes_to_chain(self, monkeypatch, tmp_path, logger):
        monkeypatch.setattr(evaluator_mod, "_get_worktree_diff",
                            lambda wt, base_commit="": "")
        monkeypatch.setattr(evaluator_mod, "_default_semantic_eval",
                            lambda *a, **kw: {"passed": True})
        result = evaluator_mod.evaluate(
            {"id": "s1", "title": "t", "description": "d"},
            tmp_path, "pytest -q", [],
            {"evaluator": {"strategy": "chain"}}, logger)
        assert result["passed"] is False  # 空 diff 被机械闸拦截
        assert result["cost_usd"] == 0.0
