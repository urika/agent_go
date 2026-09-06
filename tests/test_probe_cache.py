"""探测 TTL 缓存测试（2026-09-06 probe 洪泛修复）。

背景：原「进程内 dict + 仅成功缓存」在代理忙时形成探测洪泛——失败不缓存，
每次调用重发 POST（实测 1525 次/小时，与占满 --max-num-seqs 1 的大请求互相
踩踏，挤死并行任务）；且 web 派发/批跑多进程各自冷启动。修复：失败短 TTL
退避 + 文件层跨进程共享（conftest autouse 已把文件层隔离到 tmp_path）。
"""
from unittest.mock import MagicMock

import agent_go.executor as ex


class _FakeResp:
    def __init__(self, headers=None):
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return b"{}"


class TestFailureBackoff:
    def test_failure_cached_with_backoff(self, monkeypatch):
        """失败也缓存（短 TTL 退避）：TTL 内重复调用不再发请求（洪泛修复核心）。"""
        calls = []

        def boom(req, timeout=0):
            calls.append(1)
            raise OSError("conn refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        for _ in range(5):
            assert ex._probe_route_attribution("http://localhost:4000") == ("", "", "")
        assert len(calls) == 1  # 5 次调用只发 1 次探测

    def test_no_r8_response_cached_as_failure(self, monkeypatch):
        """无 R8 头（旧代理）按失败退避：60s 内不重发，兼容路径仍按 TTL 恢复。"""
        calls = []

        def resp(req, timeout=0):
            calls.append(1)
            return _FakeResp(headers={})

        monkeypatch.setattr("urllib.request.urlopen", resp)
        for _ in range(3):
            assert ex._probe_route_attribution("http://x") == ("", "", "")
        assert len(calls) == 1

    def test_verify_failure_conservative_and_backoff(self, monkeypatch):
        """verify 判定失败 (False, "") 短 TTL 退避：不反复走 /status + claude 探测。"""
        monkeypatch.setattr(ex, "_probe_route_attribution", lambda *a, **k: ("", "", ""))
        status_calls = []

        def status_fail(url, timeout=3.0):
            status_calls.append(1)
            return ""

        monkeypatch.setattr(ex, "_probe_local_model", status_fail)
        monkeypatch.setattr("subprocess.run", MagicMock(stdout=""))
        for _ in range(3):
            assert ex._verify_local_backend("http://x") == (False, "")
        assert len(status_calls) == 1  # 后两次命中退避缓存


class TestCrossProcessFileCache:
    def test_success_shared_across_processes(self, monkeypatch):
        """成功结果落盘：模拟新进程（内存清空）后仍命中文件层，不重发探测。"""
        resp = _FakeResp(headers={"X-Proxy-Route-Target": "local",
                                  "X-Proxy-Route-Actual-Model": "Qwen3.6"})
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)
        assert ex._probe_route_attribution("http://localhost:4000", "m") == ("local", "Qwen3.6", "")
        assert ex._PROBE_CACHE_PATH.exists()

        ex._route_attr_cache.clear()  # 模拟新进程：内存冷启动

        def must_not_call(req, timeout=0):
            raise AssertionError("文件层命中时不应重发探测")

        monkeypatch.setattr("urllib.request.urlopen", must_not_call)
        assert ex._probe_route_attribution("http://localhost:4000", "m") == ("local", "Qwen3.6", "")

    def test_failure_backoff_shared_across_processes(self, monkeypatch):
        """失败退避跨进程生效：新进程在 TTL 内不重发（多任务批跑不再各自轰炸）。"""
        def boom(req, timeout=0):
            raise OSError("down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert ex._probe_route_attribution("http://x", "m") == ("", "", "")
        ex._route_attr_cache.clear()
        assert ex._probe_route_attribution("http://x", "m") == ("", "", "")  # 文件层退避命中


class TestTTLExpiry:
    def test_success_ttl_expiry_reprobes(self, monkeypatch):
        """成功结果超过 TTL 后重新探测（SIGHUP 模型切换的自愈窗口）。"""
        resp = _FakeResp(headers={"X-Proxy-Route-Target": "cloud",
                                  "X-Proxy-Route-Actual-Model": "glm"})
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)
        assert ex._probe_route_attribution("http://x")[0] == "cloud"

        import json
        import time
        old = time.time() - ex._PROBE_TTL_OK_SEC - 1
        raw = json.loads(ex._PROBE_CACHE_PATH.read_text())
        for e in raw["entries"].values():
            e["ts"] = old
        ex._PROBE_CACHE_PATH.write_text(json.dumps(raw))
        for k in list(ex._route_attr_cache):
            v, ok, _ = ex._route_attr_cache[k]
            ex._route_attr_cache[k] = (v, ok, old)

        resp2 = _FakeResp(headers={"X-Proxy-Route-Target": "local",
                                   "X-Proxy-Route-Actual-Model": "Qwen-new"})
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp2)
        assert ex._probe_route_attribution("http://x") == ("local", "Qwen-new", "")

    def test_corrupt_file_fail_open(self, monkeypatch):
        """缓存文件损坏 → 当作无缓存（fail-open），探测主路径不受影响。"""
        ex._PROBE_CACHE_PATH.write_text("{corrupt")
        resp = _FakeResp(headers={"X-Proxy-Route-Target": "local",
                                  "X-Proxy-Route-Actual-Model": "m"})
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: resp)
        assert ex._probe_route_attribution("http://x") == ("local", "m", "")


class TestVerifyCacheSemantics:
    def test_r8_cloud_verdict_cached_long(self, monkeypatch):
        """R8 判云（is_local=False）也是成功判定 → 长 TTL，不重复探测。"""
        monkeypatch.setattr(ex, "_probe_route_attribution",
                            lambda url, model, **k: ("cloud", "glm-5.3", "forced"))
        status_calls = []

        def status_probe(url, timeout=3.0):
            status_calls.append(1)
            return ""

        monkeypatch.setattr(ex, "_probe_local_model", status_probe)
        assert ex._verify_local_backend("http://x", routed_model="m") == (False, "glm-5.3")
        assert ex._verify_local_backend("http://x", routed_model="m") == (False, "glm-5.3")
        assert status_calls == []  # 第二次命中 verify 缓存

    def test_seeded_bare_tuple_still_honored(self, monkeypatch):
        """旧形态播种（裸 tuple）仍视为有效命中（test_executor 播种语义兼容）。"""
        ex._local_verify_cache["http://seed"] = (False, "glm-4.7")
        assert ex._verify_local_backend("http://seed") == (False, "glm-4.7")
