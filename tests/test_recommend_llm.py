"""M6.4 规则初筛 + LLM 精排测试。"""
import logging

import pytest

from agent_go.bench import identify_deterministic_issues, llm_rerank_recommendation


class TestIdentifyIssues:
    def _models(self):
        return {
            "claude-opus-4-7": {"pass_rate": 0.5, "dollar_per_pass": 0.45},
            "glm-5.3": {"pass_rate": 0.83, "dollar_per_pass": 0.03},
        }

    def test_budget_overrun(self):
        """$/pass 超预算候选问题。"""
        issues = identify_deterministic_issues(self._models(), [], budget_per_pass=0.10)
        types = [i["type"] for i in issues]
        assert "cost_over_budget" in types
        ov = next(i for i in issues if i["type"] == "cost_over_budget")
        assert any("claude-opus-4-7" in e for e in ov["evidence"])

    def test_failure_class_concentration(self):
        """failure_class 集中 >50% 识别。"""
        records = [
            {"failure_class": "verification_failure", "task_id": "a", "accepted_delivery": False},
            {"failure_class": "verification_failure", "task_id": "b", "accepted_delivery": False},
            {"failure_class": "verification_failure", "task_id": "c", "accepted_delivery": False},
            {"failure_class": None, "task_id": "d", "accepted_delivery": True},
        ]
        issues = identify_deterministic_issues(self._models(), records)
        assert any(i["type"] == "failure_class_concentration" for i in issues)

    def test_env_drift(self):
        """环境漂移：actual_model != routed_model。"""
        records = [{"failure_class": None, "accepted_delivery": True,
                    "routed_model": "claude-opus-4-7", "actual_model": "unsloth/Qwen3.6"}]
        issues = identify_deterministic_issues(self._models(), records)
        assert any(i["type"] == "environment_drift" for i in issues)

    def test_no_issue_clean(self, monkeypatch):
        monkeypatch.setattr("agent_go.problems.load", lambda p: [])
        issues = identify_deterministic_issues(
            {"glm-5.3": {"pass_rate": 0.9, "dollar_per_pass": 0.05}}, [])
        assert issues == []


class TestLlmRerank:
    def _models(self):
        return {
            "claude-opus-4-7": {"pass_rate": 0.5, "dollar_per_pass": 0.45},
            "glm-5.3": {"pass_rate": 0.83, "dollar_per_pass": 0.03},
        }

    def _rec(self):
        return {
            "worker_models": {"easy": "glm-5.3", "medium": "glm-5.3", "hard": "kimi-k3"},
            "roles": {"planner": {"model": "kimi-k3"}, "evaluator": {"model": "glm-5.3"}},
        }

    def test_llm_success_overrides(self, monkeypatch):
        """LLM 精排成功：覆盖 worker_models/roles + 返回 ranking/cautions。"""
        def fake_call_api(config, messages, logger):
            return '{"worker_models": {"easy": "glm-5.3", "medium": "glm-5.3", "hard": "glm-5.3"}, "ranking": ["glm-5.3 最优"], "issues_addressed": ["cost_over_budget"], "cautions": ["注意环境漂移"]}'
        monkeypatch.setattr("agent_go.api.call_api", fake_call_api)
        rec, llm_info = llm_rerank_recommendation(
            self._rec(), [{"type": "cost_over_budget", "severity": "high", "detail": "x", "evidence": []}], self._models(), {}, logging.getLogger("t"))
        assert rec["worker_models"]["hard"] == "glm-5.3"
        assert llm_info["llm_ranking"] == ["glm-5.3 最优"]
        assert llm_info["cautions"] == ["注意环境漂移"]

    def test_llm_failure_fallback(self, monkeypatch):
        """LLM 失败回退规则候选。"""
        def fake_call_api(config, messages, logger):
            raise RuntimeError("API 不可用")
        monkeypatch.setattr("agent_go.api.call_api", fake_call_api)
        rec, llm_info = llm_rerank_recommendation(
            self._rec(), [], self._models(), {}, logging.getLogger("t"))
        assert rec["worker_models"]["hard"] == "kimi-k3"  # 规则候选保留
        assert llm_info["llm_error"] is not None
