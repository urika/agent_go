"""profiles 模块 + Web 写端点测试（Web 操作台 M1：R1-R4/R8）。

覆盖：URL 规范化 / 本地 profile 模板生成 / local-cloud 激活闭环 /
备份机制 / 健康检查 mismatch / load_config .current_profile fallback /
POST 端点鉴权与错误处理。
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator

import pytest

import agent_go.profiles as prof
import agent_go.config as cfg
import agent_go.web_server as ws


@pytest.fixture
def profile_env(tmp_path: Path, monkeypatch) -> Path:
    """把 profiles/config/web_server 的数据目录全部指向临时目录。"""
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    return adir


class TestNormalizeUrl:
    def test_strip_chat_completions(self):
        assert prof.normalize_local_url("http://localhost:4000/v1/chat/completions") == "http://localhost:4000"

    def test_strip_v1_and_slash(self):
        assert prof.normalize_local_url("http://localhost:4000/v1/") == "http://localhost:4000"

    def test_plain(self):
        assert prof.normalize_local_url("http://localhost:4000") == "http://localhost:4000"


class TestGenerateLocalProfile:
    def test_structure(self):
        p = prof.generate_local_profile("http://localhost:4000/v1/chat/completions", "Qwen3.6-test")
        assert p["plan_api"]["provider"] == "openai"
        assert p["plan_api"]["base_url"] == "http://localhost:4000/v1/chat/completions"
        assert p["plan_api"]["api_key"] == ""
        assert p["worker_backends"] == {m: "http://localhost:4000" for m in prof.ROUTE_MODELS}
        assert p["worker_models"]["easy"] == "claude-haiku-4-5"
        assert p["goal"]["policy"] == "force"
        assert p["evaluator"]["enabled"] is True
        assert p["local_model_cost"] == {"Qwen3.6-test": prof.DEFAULT_LOCAL_COST}

    def test_no_real_model_no_cost(self):
        p = prof.generate_local_profile("http://localhost:4000")
        assert "local_model_cost" not in p


class TestProbeLocalModels:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(prof, "_http_get_json",
                            lambda url, headers=None, timeout=0: (200, {"data": [{"id": "m1"}, {"id": "m2"}]}))
        assert prof.probe_local_models("http://localhost:4000") == ["m1", "m2"]

    def test_unreachable(self, monkeypatch):
        def boom(url, headers=None, timeout=0):
            raise prof.ProfileError("无法连接")
        monkeypatch.setattr(prof, "_http_get_json", boom)
        with pytest.raises(prof.ProfileError):
            prof.probe_local_models("http://localhost:4000")


class TestActivateLocal:
    def test_success(self, profile_env, monkeypatch):
        monkeypatch.setattr(prof, "probe_local_models", lambda url: ["Qwen3.6-test"])
        result = prof.activate_local("http://localhost:4000")
        assert result["real_model"] == "Qwen3.6-test"
        # profile 文件 + marker + 备份
        data = json.loads((profile_env / "profiles" / "local.json").read_text())
        assert data["worker_backends"]["claude-sonnet-4-6"] == "http://localhost:4000"
        assert (profile_env / ".current_profile").read_text() == "local"
        backups = list((profile_env / "profiles").glob("backup-*.json"))
        assert len(backups) == 1

    def test_unreachable_aborts(self, profile_env, monkeypatch):
        def boom(url):
            raise prof.ProfileError("无法连接")
        monkeypatch.setattr(prof, "probe_local_models", boom)
        with pytest.raises(prof.ProfileError, match="本地代理不可达"):
            prof.activate_local("http://localhost:9999")
        # 未修改任何配置（R1 验收：失败中止）
        assert not (profile_env / ".current_profile").exists()
        assert not (profile_env / "profiles" / "local.json").exists()


class TestActivateCloud:
    def test_clears_marker(self, profile_env):
        (profile_env / ".current_profile").write_text("local", encoding="utf-8")
        result = prof.activate_cloud()
        assert result["previous_profile"] == "local"
        assert not (profile_env / ".current_profile").exists()
        assert Path(result["backup_path"]).exists()


class TestActivateProfile:
    def test_not_found(self, profile_env):
        with pytest.raises(prof.ProfileError, match="不存在"):
            prof.activate_profile("ghost")

    def test_activate_existing(self, profile_env):
        (profile_env / "profiles").mkdir(exist_ok=True)
        (profile_env / "profiles" / "mine.json").write_text("{}", encoding="utf-8")
        result = prof.activate_profile("mine")
        assert (profile_env / ".current_profile").read_text() == "mine"
        assert result["profile"] == "mine"


class TestListProfiles:
    def test_modes(self, profile_env):
        pdir = profile_env / "profiles"
        pdir.mkdir()
        (pdir / "local.json").write_text(json.dumps(
            {"worker_backends": {"a": "http://localhost:4000"}}), encoding="utf-8")
        (pdir / "backup-20260101-000000.json").write_text("{}", encoding="utf-8")
        (profile_env / ".current_profile").write_text("local", encoding="utf-8")
        info = prof.list_profiles()
        assert info["mode"] == "local"
        assert info["current"] == "local"
        by_name = {p["name"]: p for p in info["profiles"]}
        assert by_name["local"]["active"] is True
        assert by_name["local"]["mode"] == "local"
        assert by_name["backup-20260101-000000"]["is_backup"] is True

    def test_cloud_default(self, profile_env):
        assert prof.list_profiles()["mode"] == "cloud"


class TestHealthCheck:
    def test_mismatch_true(self, profile_env, monkeypatch):
        monkeypatch.setattr(prof, "probe_endpoint",
                            lambda url, key="": {"ok": True, "url": url})
        monkeypatch.setattr(prof, "probe_local_models", lambda url: ["NewModel-X"])
        config = {
            "plan_api": {"base_url": "http://localhost:4000/v1/chat/completions",
                         "local_models": ["claude-sonnet-4-6"]},
            "worker_backends": {"claude-sonnet-4-6": "http://localhost:4000"},
            "evaluator": {"enabled": False},
        }
        h = prof.health_check(config)
        assert h["mismatch"] is True
        assert "suggestion" in h
        assert h["evaluator"]["skipped"] is True

    def test_mismatch_false_when_known(self, profile_env, monkeypatch):
        monkeypatch.setattr(prof, "probe_endpoint",
                            lambda url, key="": {"ok": True, "url": url})
        monkeypatch.setattr(prof, "probe_local_models", lambda url: ["claude-sonnet-4-6"])
        config = {
            "plan_api": {"base_url": "http://localhost:4000/v1/chat/completions",
                         "local_models": ["claude-sonnet-4-6"]},
            "worker_backends": {"claude-sonnet-4-6": "http://localhost:4000"},
            "evaluator": {"enabled": False},
        }
        assert prof.health_check(config)["mismatch"] is False

    def test_no_local_backend(self, profile_env, monkeypatch):
        monkeypatch.setattr(prof, "probe_endpoint",
                            lambda url, key="": {"ok": True, "url": url})
        h = prof.health_check({"plan_api": {"base_url": "https://api.x.com/v1/messages"},
                               "evaluator": {"enabled": False}})
        assert h["local_proxy"]["skipped"] is True
        assert h["mismatch"] is False


class TestModelsUrl:
    def test_chat_completions(self):
        assert prof._models_url("http://h:4000/v1/chat/completions") == "http://h:4000/v1/models"

    def test_messages(self):
        assert prof._models_url("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com/v1/models"

    def test_root_gets_v1(self):
        assert prof._models_url("http://localhost:4000") == "http://localhost:4000/v1/models"


class TestLoadConfigFallback:
    def test_current_profile_marker(self, profile_env):
        """load_config 在 env 未设置时读 .current_profile（M1 热生效链路）。"""
        pdir = profile_env / "profiles"
        pdir.mkdir(exist_ok=True)
        (pdir / "local.json").write_text(json.dumps(
            {"worker_models": {"easy": "claude-haiku-4-5", "medium": "m", "hard": "h"},
             "custom_marker_field": "from-local"}), encoding="utf-8")
        (profile_env / ".current_profile").write_text("local", encoding="utf-8")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cfg, "AGENT_GO_DIR", profile_env)
        monkeypatch.setattr(cfg, "CONFIG_PATH", profile_env / "config.json")
        monkeypatch.delenv("AGENT_GO_PROFILE", raising=False)
        try:
            config = cfg.load_config()
        finally:
            monkeypatch.undo()
        assert config.get("custom_marker_field") == "from-local"

    def test_env_overrides_marker(self, profile_env):
        pdir = profile_env / "profiles"
        pdir.mkdir(exist_ok=True)
        (pdir / "local.json").write_text('{"a": 1}', encoding="utf-8")
        (pdir / "other.json").write_text('{"b": 2}', encoding="utf-8")
        (profile_env / ".current_profile").write_text("local", encoding="utf-8")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(cfg, "AGENT_GO_DIR", profile_env)
        monkeypatch.setattr(cfg, "CONFIG_PATH", profile_env / "config.json")
        monkeypatch.setenv("AGENT_GO_PROFILE", "other")
        try:
            config = cfg.load_config()
        finally:
            monkeypatch.undo()
        assert config.get("b") == 2
        assert "a" not in config


# ── Web 写端点（R3/R4/R8）───────────────────────────────────

@pytest.fixture
def write_server(profile_env, monkeypatch) -> Generator[str, None, None]:
    """起真实 HTTP server（无 token），mock 网络探测。"""
    monkeypatch.setattr(prof, "probe_local_models", lambda url: ["Qwen3.6-test"])
    monkeypatch.setattr(prof, "probe_endpoint",
                        lambda url, key="": {"ok": True, "url": url, "latency_ms": 1})
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
    server.token = ""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict, token: str = ""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWriteApi:
    def test_local_then_cloud(self, write_server, profile_env):
        code, data = _post(f"{write_server}/api/profile/local", {})
        assert code == 200
        assert data["profile"] == "local"
        assert (profile_env / ".current_profile").read_text() == "local"
        code, data = _post(f"{write_server}/api/profile/cloud", {})
        assert code == 200
        assert not (profile_env / ".current_profile").exists()

    def test_activate_invalid_name(self, write_server):
        code, _ = _post(f"{write_server}/api/profile/activate", {"name": "../evil"})
        assert code == 400

    def test_activate_missing(self, write_server):
        code, data = _post(f"{write_server}/api/profile/activate", {"name": "ghost"})
        assert code == 422
        assert "error" in data

    def test_local_unreachable_422(self, write_server, monkeypatch):
        def boom(url):
            raise prof.ProfileError("无法连接")
        monkeypatch.setattr(prof, "probe_local_models", boom)
        code, data = _post(f"{write_server}/api/profile/local", {"url": "http://localhost:9999"})
        assert code == 422
        assert "本地代理不可达" in data["error"]

    def test_bad_json_400(self, write_server):
        req = urllib.request.Request(f"{write_server}/api/profile/local",
                                     data=b"{not-json", method="POST")
        try:
            urllib.request.urlopen(req)
            assert False, "should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_unknown_post_404(self, write_server):
        code, _ = _post(f"{write_server}/api/nope", {})
        assert code == 404

    def test_get_profiles_and_health(self, write_server):
        with urllib.request.urlopen(f"{write_server}/api/profiles") as r:
            assert json.loads(r.read())["mode"] == "cloud"
        with urllib.request.urlopen(f"{write_server}/api/health") as r:
            d = json.loads(r.read())
            assert d["plan"]["ok"] is True
            assert d["mismatch"] is False


class TestWriteApiAuth:
    """R8：token 模式下所有写端点鉴权。"""

    def test_token_guard(self, profile_env, monkeypatch):
        monkeypatch.setattr(prof, "probe_local_models", lambda url: ["m"])
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
        server.token = "sec"
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address[:2]
        base = f"http://127.0.0.1:{port}"
        try:
            code, _ = _post(f"{base}/api/profile/cloud", {})
            assert code == 401
            code, _ = _post(f"{base}/api/profile/cloud", {}, token="wrong")
            assert code == 401
            code, _ = _post(f"{base}/api/profile/cloud", {}, token="sec")
            assert code == 200
        finally:
            server.shutdown()
            server.server_close()
