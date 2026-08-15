"""P0 reliability: provider fallback chains and evaluator arbitration."""

from unittest.mock import patch

from agent_go.evaluator import _default_semantic_eval
from agent_go.executor import _fallback_model_for_retry


def test_worker_fallback_chain_precedes_legacy_mapping():
    cfg = {
        "worker_models_fallback_chain": {"hard": ["glm-5.3", "deepseek-v4-pro"]},
        "worker_models_fallback": {"hard": "local-mlx"},
    }
    assert _fallback_model_for_retry(cfg, "hard", 1, "kimi-k3") == "glm-5.3"
    assert _fallback_model_for_retry(cfg, "hard", 2, "glm-5.3") == "deepseek-v4-pro"
    assert _fallback_model_for_retry(cfg, "hard", 3, "deepseek-v4-pro") == "local-mlx"


def test_worker_fallback_chain_empty_preserves_legacy_mapping():
    cfg = {"worker_models_fallback": {"medium": "glm-5.3"}}
    assert _fallback_model_for_retry(cfg, "medium", 1, "kimi-k3") == "glm-5.3"
    assert _fallback_model_for_retry(cfg, "medium", 1, "glm-5.3") == ""


def test_evaluator_low_confidence_uses_next_provider(tmp_path, logger):
    config = {
        "_task_id": "t-p0",
        "plan_api": {"provider": "openai", "model": "local", "base_url": "http://local"},
        "evaluator": {
            "enabled": True,
            "arbitration": {"confidence_threshold": 0.5},
        },
        "router": {
            "enabled": True,
            "roles": {
                "evaluator": {
                    "provider": "moonshot", "model": "kimi-k3", "base_url": "http://k3",
                    "fallbacks": [
                        {"provider": "zhipu", "model": "glm-5.3", "base_url": "http://glm"},
                    ],
                }
            },
        },
    }
    primary = '{"passed": false, "confidence": 0.2, "reason": "不确定", "suggestions": ""}'
    fallback = '{"passed": true, "confidence": 0.9, "reason": "测试和实现均完整", "suggestions": ""}'
    route_metering = [
        {"actual_provider": "moonshot", "actual_model": "kimi-k3", "result": "success"},
        {"actual_provider": "zhipu", "actual_model": "glm-5.3", "result": "fallback"},
    ]
    with patch("agent_go.router.call_with_role", side_effect=[
        (primary, route_metering[0]), (fallback, route_metering[1])
    ]), patch("agent_go.evaluator._get_worktree_diff", return_value="diff --git a/a.py b/a.py"):
        result = _default_semantic_eval(
            {"id": "sub-1", "title": "t", "description": "d"},
            tmp_path, "pytest tests/", [], config, logger,
        )
    assert result["passed"] is True
    assert result["confidence"] == 0.9
