"""R8 路由归因消费测试（metering is_local 误判纠正）。

llama.cpp R8 响应头 X-Proxy-Route-Target/Actual-Model/Reason/Cost：force_fallback
模型（opus-4-7 等）按 URL/status 会误判 local（/status 声明本地模型但实际走云端），
用 route_target 直接判定最准。call_api（planner/evaluator）读响应头纠正；
executor _verify_local_backend（worker 路径）R8 优先判定。
"""
import json
from unittest.mock import MagicMock, patch

import agent_go.executor as ex
import agent_go.api as api


class _FakeResp:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]


class TestProbeRouteAttribution:
    def setup_method(self):
        ex._route_attr_cache.clear()

    def test_returns_route_headers(self, monkeypatch):
        resp = _FakeResp(headers={
            "X-Proxy-Route-Target": "cloud",
            "X-Proxy-Route-Actual-Model": "deepseek-v4-pro",
            "X-Proxy-Route-Reason": "model_forced_fallback_cloud",
        })
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)
        t, a, r = ex._probe_route_attribution("http://localhost:4000", "claude-opus-4-7")
        assert t == "cloud"
        assert a == "deepseek-v4-pro"
        assert "forced_fallback" in r

    def test_no_r8_returns_empty(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _FakeResp(headers={}))
        assert ex._probe_route_attribution("http://localhost:4000", "claude-sonnet-4-6") == ("", "", "")

    def test_failure_returns_empty(self, monkeypatch):
        def boom(req, timeout=0):
            raise OSError("conn refused")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert ex._probe_route_attribution("http://localhost:4000") == ("", "", "")

    def test_caches_per_model(self, monkeypatch):
        resp = _FakeResp(headers={"X-Proxy-Route-Target": "local",
                                  "X-Proxy-Route-Actual-Model": "Qwen3.6"})
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)
        ex._probe_route_attribution("http://localhost:4000", "a")
        ex._probe_route_attribution("http://localhost:4000", "b")
        assert len(ex._route_attr_cache) == 2  # a/b 各自缓存


class TestVerifyLocalBackendR8:
    def setup_method(self):
        ex._local_verify_cache.clear()
        ex._route_attr_cache.clear()

    def test_r8_cloud_overrides_status_local(self, monkeypatch):
        """R8=cloud 时判非本地（覆盖 /status 声明本地模型的误判）。"""
        monkeypatch.setattr(ex, "_probe_route_attribution",
                            lambda url, model, **k: ("cloud", "glm-5.3", "forced_fallback"))
        # /status 即使声明本地模型也不应被采纳（R8 优先）
        monkeypatch.setattr(ex, "_probe_local_model", lambda url, timeout=3.0: "Qwen3.6-local")
        is_local, actual = ex._verify_local_backend("http://localhost:4000",
                                                    routed_model="claude-opus-4-7")
        assert is_local is False
        assert actual == "glm-5.3"

    def test_r8_local_judges_local(self, monkeypatch):
        monkeypatch.setattr(ex, "_probe_route_attribution",
                            lambda url, model, **k: ("local", "Qwen3.6-35B", "under_threshold"))
        is_local, actual = ex._verify_local_backend("http://localhost:4000",
                                                    routed_model="claude-sonnet-4-6")
        assert is_local is True
        assert actual == "Qwen3.6-35B"

    def test_no_r8_falls_back_to_status(self, monkeypatch):
        """无 R8（旧代理）→ 走现有 /status 判定（兼容）。"""
        monkeypatch.setattr(ex, "_probe_route_attribution", lambda url, model, **k: ("", "", ""))
        monkeypatch.setattr(ex, "_probe_local_model", lambda url, timeout=3.0: "Qwen3.6-local")
        # claude 探测返回同 model（确认本地）；不跑真实 claude -p
        fake_cp = MagicMock(stdout='{"type":"assistant","message":{"model":"Qwen3.6-local"}}\n')
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=fake_cp))
        is_local, actual = ex._verify_local_backend("http://localhost:4000",
                                                    routed_model="claude-sonnet-4-6")
        assert is_local is True
        assert actual == "Qwen3.6-local"


class TestCallApiR8Metering:
    """call_api（planner/evaluator）R8 响应头 → metering 纠正。"""

    def test_meter_event_includes_route_attribution(self, monkeypatch):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        resp = _FakeResp(headers={
            "X-Proxy-Route-Target": "cloud",
            "X-Proxy-Route-Actual-Model": "deepseek-v4-pro",
            "X-Proxy-Route-Reason": "forced_fallback",
            "X-Proxy-Route-Cost": "0.001",
        }, body=body)
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)

        captured = {}
        monkeypatch.setattr(api, "meter_event", lambda path, ev: captured.update(ev))
        cfg = {"_metering_path": "/tmp/m.jsonl",
               "plan_api": {"provider": "openai", "base_url": "http://localhost:4000/v1/chat/completions",
                            "model": "claude-opus-4-7", "api_key": "x"}}
        import logging
        api.call_api(cfg, [{"role": "user", "content": "hi"}], logging.getLogger("t"))

        assert captured["route_target"] == "cloud"
        assert captured["route_actual_model"] == "deepseek-v4-pro"
        assert captured["is_local"] is False
        assert captured["actual_model"] == "deepseek-v4-pro"  # 用真实后端模型
        assert captured["cost_usd"] == 0.001  # 用 R8 route_cost

    def test_no_r8_keeps_existing_behavior(self, monkeypatch):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _FakeResp(headers={}, body=body))
        captured = {}
        monkeypatch.setattr(api, "meter_event", lambda path, ev: captured.update(ev))
        cfg = {"_metering_path": "/tmp/m.jsonl",
               "plan_api": {"provider": "openai", "base_url": "http://x/v1/chat/completions",
                            "model": "m", "api_key": "x"}}
        import logging
        api.call_api(cfg, [{"role": "user", "content": "hi"}], logging.getLogger("t"))
        assert "route_target" not in captured  # 无 R8 不加字段（兼容）
        assert captured["actual_model"] == "m"
