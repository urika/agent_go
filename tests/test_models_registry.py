"""模型实体三层配置（P1）测试：registry / key_ref / evaluator 角色 / 声明式 thinking。"""
import json

import pytest

import agent_go.models_registry as mr


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "MODELS_PATH", tmp_path / "models.json")
    mr._registry_cache = None
    mr._registry_mtime = 0.0
    return tmp_path


def _write(path, models):
    """models.json 顶层直接 model_id → 嵌套结构（endpoint/reasoning/output/limits/cost）。"""
    path.write_text(json.dumps(models, ensure_ascii=False), encoding="utf-8")


GLM = {
    "provider": "anthropic",
    "endpoint": {"base_url": "https://x/v1/messages", "key_ref": "GLM_KEY"},
    "reasoning": {"thinking": {"format": "anthropic", "required": True, "budget_tokens": 8192}},
    "output": {"json_compliance": "strict", "needs_response_format": False},
    "quality_tags": ["plan_strong"],
}
DSPRO = {
    "provider": "openai",
    "endpoint": {"base_url": "https://x/v1/chat/completions"},
    "reasoning": {"thinking": {"format": "openai", "required": True}},
    "output": {"json_compliance": "loose", "needs_response_format": True},
}
LOCAL = {
    "provider": "openai",
    "endpoint": {"base_url": "http://localhost:4000/v1/chat/completions"},
    "reasoning": {"thinking": {"format": "openai", "required": False}},
    "cost": {"tco_per_call": 0.0005},
}


class TestRegistry:
    def test_empty_when_missing(self, registry_env):
        assert mr.load_registry() == {}
        assert mr.get_model("glm-5.3") is None

    def test_load_and_get(self, registry_env):
        _write(registry_env / "models.json", {"glm-5.3": GLM})
        m = mr.get_model("glm-5.3")
        assert m is not None
        assert m.provider == "anthropic"
        assert m.thinking.required is True
        assert m.thinking.budget_tokens == 8192
        assert m.output.json_compliance == "strict"
        assert "plan_strong" in m.quality_tags

    def test_output_needs_response_format(self, registry_env):
        _write(registry_env / "models.json", {"deepseek-v4-pro": DSPRO})
        assert mr.get_model("deepseek-v4-pro").output.needs_response_format is True

    def test_cost_tco(self, registry_env):
        _write(registry_env / "models.json", {"local-mlx": LOCAL})
        assert mr.get_model("local-mlx").cost.tco_per_call == 0.0005


class TestResolveKey:
    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-123")
        assert mr.resolve_key("env:MY_KEY") == "sk-123"

    def test_bare_var(self, monkeypatch):
        monkeypatch.setenv("OTHER_KEY", "sk-456")
        assert mr.resolve_key("OTHER_KEY") == "sk-456"

    def test_empty(self):
        assert mr.resolve_key("") == ""
        assert mr.resolve_key(None) == ""

    def test_secret_ref(self, tmp_path):
        secret = tmp_path / "secret.conf"
        secret.write_text('export PROXY_CLOUD_API_KEY="sk-secret-1"\n', encoding="utf-8")
        assert mr.resolve_key(f"secret:{secret}#PROXY_CLOUD_API_KEY") == "sk-secret-1"


class TestResolveRole:
    def test_evaluator_role(self):
        from agent_go.router import resolve_role
        config = {"router": {"enabled": True, "roles": {
            "evaluator": {"provider": "anthropic", "base_url": "https://x", "model": "glm-5.3",
                          "api_key": "k", "thinking": True, "thinking_budget": 8192}}}}
        r = resolve_role("evaluator", config)
        assert r is not None
        assert r.primary.model == "glm-5.3"
        assert r.primary.thinking is True
        assert r.primary.thinking_budget == 8192

    def test_router_disabled_returns_none(self):
        from agent_go.router import resolve_role
        assert resolve_role("evaluator", {"router": {"enabled": False, "roles": {}}}) is None

    def test_missing_role_returns_none(self):
        from agent_go.router import resolve_role
        assert resolve_role("evaluator", {"router": {"enabled": True, "roles": {}}}) is None


class TestDeclarativeThinking:
    """call_api._resolve_thinking_payload：② binding 覆盖 > ① registry 声明式 > 不开启。"""

    def _payload(self, api_cfg, model, provider="anthropic"):
        from agent_go.api import _resolve_thinking_payload
        return _resolve_thinking_payload(api_cfg, model, provider)

    def test_registry_required_anthropic(self, registry_env):
        _write(registry_env / "models.json", {"glm-5.3": GLM})
        p = self._payload({}, "glm-5.3", "anthropic")
        assert p == {"type": "enabled", "budget_tokens": 8192}

    def test_registry_required_openai(self, registry_env):
        _write(registry_env / "models.json", {"deepseek-v4-pro": DSPRO})
        p = self._payload({}, "deepseek-v4-pro", "openai")
        assert p == {"type": "enabled"}

    def test_no_required_empty(self, registry_env):
        _write(registry_env / "models.json", {"local-mlx": LOCAL})
        assert self._payload({}, "local-mlx", "openai") == {}

    def test_binding_overrides_budget(self, registry_env):
        """② binding(api_cfg.thinking/thinking_budget) 覆盖 ① 默认值。"""
        _write(registry_env / "models.json", {"glm-5.3": GLM})
        p = self._payload({"thinking": True, "thinking_budget": 2048}, "glm-5.3", "anthropic")
        assert p == {"type": "enabled", "budget_tokens": 2048}

    def test_unknown_model_empty(self, registry_env):
        assert self._payload({}, "unknown-model", "openai") == {}
