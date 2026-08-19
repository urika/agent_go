"""测试 diag.py — llama-defender 诊断数据面客户端（C1-C7 公共层）"""

from unittest.mock import patch
import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go import diag


class TestSessionKey:
    def test_prefix_is_8_char_distinguishable(self):
        """代理截断前 8 字符，前缀必须逐会话可区分"""
        k1 = diag.session_key("task-001", "sub-1")
        k2 = diag.session_key("task-001", "sub-2")
        assert len(diag.session_key8(k1)) == 8
        assert diag.session_key8(k1) != diag.session_key8(k2)

    def test_deterministic(self):
        assert diag.session_key("t", "s") == diag.session_key("t", "s")

    def test_sanitized_and_bounded(self):
        k = diag.session_key("task/with bad 字符", "sub x" * 30)
        assert len(k) <= 64
        assert all(c.isalnum() or c in "-_" for c in k)

    def test_no_sub_id(self):
        k = diag.session_key("task-001")
        assert diag.session_key8(k) == k[:8]


class TestLocalProxyBaseUrl:
    def test_worker_base_url_first(self):
        """A-1：worker_base_url（统一入口）优先于 deprecated 的 worker_backends。"""
        cfg = {"worker_backends": {"m1": "http://127.0.0.1:4000/v1/messages"},
               "plan_api": {"worker_base_url": "http://localhost:9000"}}
        assert diag.local_proxy_base_url(cfg) == "http://localhost:9000"

    def test_fallback_worker_backends(self):
        cfg = {"worker_backends": {"m1": "http://127.0.0.1:4000/v1/messages"}}
        assert diag.local_proxy_base_url(cfg) == "http://127.0.0.1:4000/v1/messages"

    def test_fallback_worker_base_url(self):
        cfg = {"plan_api": {"worker_base_url": "http://localhost:4000/"}}
        assert diag.local_proxy_base_url(cfg) == "http://localhost:4000"

    def test_cloud_only_returns_empty(self):
        cfg = {"worker_backends": {"m1": "https://api.example.com"},
               "plan_api": {"base_url": "https://api.anthropic.com"}}
        assert diag.local_proxy_base_url(cfg) == ""

    def test_empty_config(self):
        assert diag.local_proxy_base_url({}) == ""


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class TestFetchJson:
    BASE = "http://127.0.0.1:4000"

    def test_success(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp({"a": 1})):
            assert diag.fetch_json(self.BASE, "/api/status") == {"a": 1}

    def test_network_error_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert diag.fetch_json(self.BASE, "/api/status") is None

    def test_http_error_with_json_body_returned(self):
        """501 + {"supported": false} 这类结构化降级 body 要返回给调用方"""
        err = urllib.error.HTTPError(
            self.BASE + "/api/backend/props", 501, "err", {},
            io.BytesIO(json.dumps({"supported": False}).encode()))
        with patch("urllib.request.urlopen", side_effect=err):
            assert diag.fetch_json(self.BASE, "/api/backend/props") == {"supported": False}

    def test_bad_json_returns_none(self):
        class _Bad:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b"<html>not json</html>"

        with patch("urllib.request.urlopen", return_value=_Bad()):
            assert diag.fetch_json(self.BASE, "/api/status") is None

    def test_empty_base_url(self):
        assert diag.fetch_json("", "/api/status") is None


class TestEndpointHelpers:
    BASE = "http://127.0.0.1:4000"

    def test_get_sessions(self):
        payload = {"sessions": [{"key": "abc", "key_source": "header"}]}
        with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
            sessions = diag.get_sessions(self.BASE)
        assert sessions == [{"key": "abc", "key_source": "header"}]

    def test_get_sessions_fail_open(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            assert diag.get_sessions(self.BASE) == []

    def test_get_session_metrics_ok(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp({"turns": 3, "hit_ratio_p50": 0.9})):
            m = diag.get_session_metrics(self.BASE, "abcd1234")
        assert m["turns"] == 3

    def test_get_session_metrics_404_returns_none(self):
        err = urllib.error.HTTPError(
            self.BASE, 404, "nf", {}, io.BytesIO(json.dumps({"error": "unknown key"}).encode()))
        with patch("urllib.request.urlopen", side_effect=err):
            assert diag.get_session_metrics(self.BASE, "abcd1234") is None

    def test_get_session_metrics_empty_key(self):
        assert diag.get_session_metrics(self.BASE, "") is None

    def test_get_ctx_config(self):
        payload = {"state": "healthy",
                   "ctx_config": {"diag_enabled": True, "compression_mode": "semantic"},
                   "route_config": {"route_enabled": True}}
        with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
            cc = diag.get_ctx_config(self.BASE)
        assert cc["ctx_config"]["compression_mode"] == "semantic"
        assert cc["route_config"]["route_enabled"] is True

    def test_get_ctx_config_old_proxy(self):
        """旧代理 /api/status 缺失 → None（fail-open）"""
        err = urllib.error.HTTPError(self.BASE, 404, "nf", {}, io.BytesIO(b'{"detail": "Not found"}'))
        with patch("urllib.request.urlopen", side_effect=err):
            assert diag.get_ctx_config(self.BASE) is None

    def test_get_backend_props_501_structured(self):
        err = urllib.error.HTTPError(
            self.BASE, 501, "ni", {}, io.BytesIO(json.dumps({"supported": False}).encode()))
        with patch("urllib.request.urlopen", side_effect=err):
            props = diag.get_backend_props(self.BASE)
        assert props == {"supported": False}
