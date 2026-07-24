"""角色感知模型路由 — 单元测试 & 集成测试。"""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from agent_go.router import (
    ProviderConfig,
    RoleRoute,
    CircuitBreaker,
    resolve_provider,
    call_with_role,
    _build_metering,
)
from agent_go.metrics import estimate_cost, DEFAULT_PRICING


# ═══════════════════════════════════════════════════════════════
# ProviderConfig Tests
# ═══════════════════════════════════════════════════════════════

class TestProviderConfig:
    def test_from_dict_full(self):
        data = {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com/v1/messages",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8192,
            "temperature": 0.1,
            "max_concurrency": 8,
            "timeout_ms": 60000,
        }
        pc = ProviderConfig.from_dict(data)
        assert pc.provider == "anthropic"
        assert pc.model == "claude-sonnet-4-20250514"
        assert pc.max_tokens == 8192
        assert pc.temperature == 0.1
        assert pc.max_concurrency == 8
        assert pc.timeout_ms == 60000

    def test_from_dict_defaults(self):
        pc = ProviderConfig.from_dict({})
        assert pc.provider == "custom"
        assert pc.max_tokens == 4096
        assert pc.temperature == 0.2
        assert pc.max_concurrency == 4
        assert pc.timeout_ms == 120000


# ═══════════════════════════════════════════════════════════════
# CircuitBreaker Tests
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_initial_state(self):
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert cb.failure_count == 0
        assert cb.allow_request() is True

    def test_opens_after_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            assert cb.allow_request() is True
            cb.record_failure()
        assert cb.state == "open"
        assert cb.allow_request() is False

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
        for _ in range(2):
            cb.record_failure()
        assert cb.state == "open"
        # cooldown_seconds=0, 立即进入半开
        assert cb.allow_request() is True
        assert cb.state == "half_open"

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
        for _ in range(2):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.allow_request() is True
        cb.record_success()
        assert cb.state == "closed"

    def test_half_open_failure_opens_again(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0, half_open_requests=1)
        for _ in range(2):
            cb.record_failure()
        cb.allow_request()  # enters half_open
        cb.record_failure()  # half_open_count reaches limit
        assert cb.state == "open"

    def test_half_open_limited_requests(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0, half_open_requests=2)
        for _ in range(2):
            cb.record_failure()
        assert cb.allow_request() is True   # half_open, count=0
        assert cb.allow_request() is True   # half_open, count=1
        assert cb.allow_request() is False  # half_open, count=2 >= limit

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == "closed"


# ═══════════════════════════════════════════════════════════════
# resolve_provider Tests
# ═══════════════════════════════════════════════════════════════

class TestResolveProvider:
    def test_returns_none_when_disabled(self):
        config = {"router": {"enabled": False}}
        assert resolve_provider("architect", config) is None

    def test_returns_none_when_no_router_key(self):
        config = {}
        assert resolve_provider("architect", config) is None

    def test_architect_maps_to_planner(self):
        config = {
            "router": {
                "enabled": True,
                "agent_type_mapping": {"architect": "planner"},
                "roles": {
                    "planner": {
                        "provider": "anthropic",
                        "base_url": "https://api.anthropic.com/v1/messages",
                        "model": "claude-sonnet-4-20250514",
                    }
                },
            }
        }
        route = resolve_provider("architect", config)
        assert route is not None
        assert route.role == "planner"
        assert route.primary.provider == "anthropic"
        assert route.primary.model == "claude-sonnet-4-20250514"
        assert route.fallback is None

    def test_developer_maps_to_worker_with_fallback(self):
        config = {
            "router": {
                "enabled": True,
                "agent_type_mapping": {"developer": "worker"},
                "roles": {
                    "worker": {
                        "provider": "custom",
                        "base_url": "http://localhost:11434/v1",
                        "model": "qwen3-coder",
                        "fallback": {
                            "provider": "anthropic",
                            "base_url": "https://api.anthropic.com/v1/messages",
                            "model": "claude-haiku-4-5-20251001",
                        },
                    }
                },
            }
        }
        route = resolve_provider("developer", config)
        assert route is not None
        assert route.role == "worker"
        assert route.primary.provider == "custom"
        assert route.fallback is not None
        assert route.fallback.provider == "anthropic"
        assert route.fallback.model == "claude-haiku-4-5-20251001"

    def test_unknown_agent_type_defaults_to_worker(self):
        config = {
            "router": {
                "enabled": True,
                "agent_type_mapping": {},
                "roles": {
                    "worker": {
                        "provider": "openai",
                        "base_url": "https://api.openai.com/v1",
                        "model": "gpt-4o-mini",
                    }
                },
            }
        }
        route = resolve_provider("unknown_type", config)
        assert route is not None
        assert route.role == "worker"

    def test_returns_none_when_role_not_configured(self):
        config = {
            "router": {
                "enabled": True,
                "agent_type_mapping": {"tester": "worker"},
                "roles": {},
            }
        }
        assert resolve_provider("tester", config) is None


# ═══════════════════════════════════════════════════════════════
# _build_metering Tests
# ═══════════════════════════════════════════════════════════════

class TestBuildMetering:
    def test_success_metering(self):
        metering = _build_metering(
            role="worker", actual_provider="custom", actual_model="qwen3-coder",
            prompt_tokens=1000, completion_tokens=500, cost_usd=0.0,
            latency_ms=500.0, result="success", fallback_reason="",
            task_id="task-1", subtask_id="sub-1",
        )
        assert metering["role"] == "worker"
        assert metering["virtual_model"] == "agentgo-worker"
        assert metering["result"] == "success"
        assert metering["fallback_reason"] == ""
        assert metering["cost_usd"] == 0.0
        assert metering["task_id"] == "task-1"

    def test_fallback_metering(self):
        metering = _build_metering(
            role="worker", actual_provider="anthropic", actual_model="claude-haiku-4-5-20251001",
            prompt_tokens=2000, completion_tokens=800, cost_usd=0.0048,
            latency_ms=1200.0, result="fallback", fallback_reason="primary_unavailable",
            task_id="task-2", subtask_id="",
        )
        assert metering["result"] == "fallback"
        assert metering["fallback_reason"] == "primary_unavailable"
        assert metering["cost_usd"] == 0.0048


# ═══════════════════════════════════════════════════════════════
# estimate_cost Tests
# ═══════════════════════════════════════════════════════════════

class TestEstimateCost:
    def test_anthropic_sonnet_cost(self):
        cost = estimate_cost("anthropic", "claude-sonnet-4-20250514", 1000000, 500000)
        expected = (1000000 / 1_000_000) * 3.0 + (500000 / 1_000_000) * 15.0
        assert cost == pytest.approx(expected)

    def test_local_model_zero_cost(self):
        cost = estimate_cost("custom", "qwen3-coder", 1000000, 1000000)
        assert cost == 0.0

    def test_unknown_model_zero_cost(self):
        cost = estimate_cost("unknown", "unknown-model", 1000, 1000)
        assert cost == 0.0

    def test_zero_tokens(self):
        cost = estimate_cost("anthropic", "claude-sonnet-4-20250514", 0, 0)
        assert cost == 0.0

    def test_deepseek_cost(self):
        cost = estimate_cost("deepseek", "deepseek-chat", 1_000_000, 1_000_000)
        expected = 0.27 + 1.10
        assert cost == pytest.approx(expected)

    def test_pricing_table_has_expected_entries(self):
        assert ("anthropic", "claude-sonnet-4-20250514") in DEFAULT_PRICING
        assert ("anthropic", "claude-haiku-4-5-20251001") in DEFAULT_PRICING
        assert ("openai", "gpt-4o-mini") in DEFAULT_PRICING
        assert ("custom", "*") in DEFAULT_PRICING


# ═══════════════════════════════════════════════════════════════
# call_with_role Integration Tests
# ═══════════════════════════════════════════════════════════════

class TestCallWithRole:
    def test_primary_success(self):
        """测试 primary provider 正常返回。"""
        import logging
        logger = logging.getLogger("test")

        route = RoleRoute(
            role="planner",
            primary=ProviderConfig(
                provider="anthropic",
                base_url="https://api.anthropic.com/v1/messages",
                model="claude-sonnet-4-20250514",
            ),
        )

        mock_response = {
            "content": [{"text": "plan json here"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = \
                json.dumps(mock_response).encode()

            content, metering = call_with_role(
                route,
                [{"role": "user", "content": "test"}],
                "fake-key",
                logger,
            )

        assert "plan json here" in content
        assert metering["role"] == "planner"
        assert metering["result"] == "success"
        assert metering["actual_provider"] == "anthropic"

    def test_fallback_on_primary_failure(self):
        """测试 primary 失败时降级到 fallback。"""
        import logging
        logger = logging.getLogger("test")

        route = RoleRoute(
            role="worker",
            primary=ProviderConfig(
                provider="custom",
                base_url="http://localhost:11434/v1",
                model="qwen3-coder",
            ),
            fallback=ProviderConfig(
                provider="anthropic",
                base_url="https://api.anthropic.com/v1/messages",
                model="claude-haiku-4-5-20251001",
            ),
        )

        mock_response = {
            "content": [{"text": "fallback result"}],
            "usage": {"input_tokens": 50, "output_tokens": 25},
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            # Primary 失败
            mock_urlopen.side_effect = [
                OSError("Connection refused"),
                # Fallback 成功
                MagicMock(
                    __enter__=MagicMock(return_value=MagicMock(
                        read=MagicMock(return_value=json.dumps(mock_response).encode()),
                    )),
                    __exit__=MagicMock(return_value=None),
                ),
            ]

            content, metering = call_with_role(
                route,
                [{"role": "user", "content": "test"}],
                "fake-key",
                logger,
            )

        assert "fallback result" in content
        assert metering["result"] == "fallback"
        assert metering["fallback_reason"] == "primary_unavailable"
        assert metering["actual_provider"] == "anthropic"

    def test_raises_when_no_fallback(self):
        """测试无 fallback 时 primary 失败应抛出异常。"""
        import logging
        logger = logging.getLogger("test")

        route = RoleRoute(
            role="planner",
            primary=ProviderConfig(
                provider="anthropic",
                base_url="https://api.anthropic.com/v1/messages",
                model="claude-sonnet-4-20250514",
            ),
            fallback=None,
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [OSError("Connection refused"), OSError("Connection refused")]
            with pytest.raises(RuntimeError, match="路由调用失败"):
                call_with_role(
                    route,
                    [{"role": "user", "content": "test"}],
                    "fake-key",
                    logger,
                )


# ═══════════════════════════════════════════════════════════════
# Config Integration Tests
# ═══════════════════════════════════════════════════════════════

class TestConfigIntegration:
    def test_router_block_in_default_config(self):
        """验证 DEFAULT_CONFIG 包含 router 块。"""
        from agent_go.config import DEFAULT_CONFIG
        assert "router" in DEFAULT_CONFIG
        router = DEFAULT_CONFIG["router"]
        assert router["enabled"] is False
        assert "roles" in router
        assert "agent_type_mapping" in router
        assert "circuit_breaker" in router
        assert router["agent_type_mapping"]["architect"] == "planner"
        assert router["agent_type_mapping"]["developer"] == "worker"
        assert router["circuit_breaker"]["failure_threshold"] == 5

    def test_router_disabled_by_default(self):
        """默认配置下 router 返回 None，不影响现有行为。"""
        from agent_go.config import DEFAULT_CONFIG
        assert resolve_provider("architect", DEFAULT_CONFIG) is None
        assert resolve_provider("developer", DEFAULT_CONFIG) is None


# ═══════════════════════════════════════════════════════════════
# CLI Router Command Tests
# ═══════════════════════════════════════════════════════════════

class TestCliRouter:
    def test_router_subcommand_registered(self):
        """验证 router 子命令已在 parser 中注册。"""
        from agent_go.cli import _build_parser
        parser = _build_parser()
        help_text = parser.format_help()
        assert "router" in help_text.lower()