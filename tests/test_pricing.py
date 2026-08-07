"""S12 运行前模型-价格预检：pricing 辅助函数测试。"""
import pytest

from agent_go.pricing import (
    resolve_price,
    missing_price_models,
    format_price_for_report,
    MODEL_PRICES,
)


class TestResolvePrice:
    def test_exact_match(self):
        p = resolve_price("glm-4.7")
        assert p is not None
        assert p["prompt"] == pytest.approx(0.5556)
        assert p["completion"] == pytest.approx(2.2222)

    def test_anthropic_route(self):
        assert resolve_price("claude-haiku-4-5") is not None
        assert resolve_price("claude-opus-4-7") is not None

    def test_unknown_returns_none(self):
        assert resolve_price("unknown-xyz") is None

    def test_empty_returns_none(self):
        assert resolve_price("") is None
        assert resolve_price(None) is None

    def test_version_suffix_fallback(self):
        """带日期后缀的模型名回退到基础名定价。"""
        p = resolve_price("claude-haiku-4-5-20251001")
        assert p is not None
        assert p == MODEL_PRICES["claude-haiku-4-5"]


class TestMissingPriceModels:
    def test_all_known(self):
        assert missing_price_models(["glm-4.7", "claude-opus-4-7"]) == []

    def test_missing_detected(self):
        assert missing_price_models(["unknown-xyz"]) == ["unknown-xyz"]

    def test_mixed(self):
        miss = missing_price_models(["glm-4.7", "unknown-xyz", "claude-haiku-4-5"])
        assert miss == ["unknown-xyz"]

    def test_empty_list(self):
        assert missing_price_models([]) == []


class TestFormatPriceForReport:
    def test_known_model(self):
        s = format_price_for_report("glm-4.7")
        assert "glm-4.7" in s
        assert "0.5556" in s
        assert "⚠️" not in s

    def test_missing_model(self):
        s = format_price_for_report("unknown-xyz")
        assert "⚠️ 缺定价" in s
