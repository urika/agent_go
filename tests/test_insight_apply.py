"""M6.5 insight --apply-suggestion 测试。"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """临时生效配置文件。"""
    from agent_go import profiles
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"plan_api": {"model": "old"}}), encoding="utf-8")
    monkeypatch.setattr(profiles, "active_config_source", lambda: target)
    return target


class TestApplyInsightAction:
    def test_worker_models(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("worker_models", {"easy": "m1", "hard": "m2"})
        assert r["applied"] is True
        assert json.loads(cfg_file.read_text())["worker_models"] == {"easy": "m1", "hard": "m2"}
        assert Path(r["backup"]).exists()

    def test_fallback_chain(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("fallback_chain", {"difficulty": "hard", "chain": ["a", "b"]})
        assert r["applied"] is True
        data = json.loads(cfg_file.read_text())
        assert data["worker_models_fallback_chain"]["hard"] == ["a", "b"]

    def test_role_model(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("role_model", {"role": "evaluator", "model": "glm-5.3"})
        assert r["applied"] is True
        data = json.loads(cfg_file.read_text())
        assert data["router"]["roles"]["evaluator"]["model"] == "glm-5.3"

    def test_cost_budget(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("cost_budget", {"max_budget_usd": 0.05})
        assert r["applied"] is True
        assert json.loads(cfg_file.read_text())["cost_control"]["max_budget_usd"] == 0.05

    def test_manual_skipped(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("manual", {})
        assert r["applied"] is False

    def test_unknown_type_skipped(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("weird_type", {"x": 1})
        assert r["applied"] is False

    def test_role_model_missing_fields(self, cfg_file):
        from agent_go.eval import _apply_insight_action
        r = _apply_insight_action("role_model", {"role": "", "model": ""})
        assert r["applied"] is False
