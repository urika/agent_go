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


# ═══════════════════════════════════════════════════════════════
# CR-G2: MODEL_TIER 反查 + worker_models 错配校验
# ═══════════════════════════════════════════════════════════════

from agent_go.pricing import model_tier, validate_worker_tier, MODEL_TIER


class TestModelTier:
    def test_known_models(self):
        assert model_tier("claude-opus-4-8") == "frontier"
        assert model_tier("claude-sonnet-5") == "value"
        assert model_tier("claude-haiku-4-5") == "lite"

    def test_suffix_stripped_variant(self):
        """带版本后缀的变体回退到基础名分级。"""
        assert model_tier("claude-haiku-4-5-20251001") == "lite"

    def test_unknown_returns_none(self):
        assert model_tier("some-custom-model") is None
        assert model_tier("qwen3-coder-local") is None

    def test_empty_returns_none(self):
        assert model_tier("") is None
        assert model_tier(None) is None  # type: ignore[arg-type]

    def test_all_tiered_models_resolvable(self):
        """MODEL_TIER 里每个模型都能被 model_tier 解析回自己的 tier。"""
        for tier, mods in MODEL_TIER.items():
            for m in mods:
                assert model_tier(m) == tier, f"{m} 应为 {tier}"


class TestValidateWorkerTier:
    def test_hard_slot_lite_warns(self):
        """hard 槽填 lite 模型 → 能力不足告警。"""
        issues = validate_worker_tier({"easy": "", "medium": "", "hard": "claude-haiku-4-5"})
        assert len(issues) == 1
        assert issues[0][0] == "hard"
        assert issues[0][2] == "lite"

    def test_easy_slot_frontier_warns(self):
        """easy 槽填 frontier 模型 → 过贵告警。"""
        issues = validate_worker_tier({"easy": "claude-opus-4-8", "medium": "", "hard": ""})
        assert len(issues) == 1
        assert issues[0][0] == "easy"
        assert issues[0][2] == "frontier"

    def test_correct_config_no_issues(self):
        """合理配置（hard=frontier, medium=value, easy=lite）→ 无告警。"""
        issues = validate_worker_tier({
            "easy": "claude-haiku-4-5", "medium": "claude-sonnet-5", "hard": "claude-opus-4-8"})
        assert issues == []

    def test_ungraded_model_no_issue(self):
        """未分级模型（自定义/本地）不报错配。"""
        issues = validate_worker_tier({"easy": "", "medium": "", "hard": "my-local-27b"})
        assert issues == []

    def test_empty_config_no_issue(self):
        assert validate_worker_tier({"easy": "", "medium": "", "hard": ""}) == []

    def test_non_dict_returns_empty(self):
        assert validate_worker_tier(None) == []  # type: ignore[arg-type]
        assert validate_worker_tier("not a dict") == []  # type: ignore[arg-type]
