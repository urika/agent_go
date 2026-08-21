"""kanban import-spec 测试（spec → 看板卡片）。"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import agent_go.kanban as _kb


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Task Spec: 实现 safe_json_load\n\n"
        "## §1 目标\n实现 safe_json_load 工具函数\n\n"
        "## §5 验收标准\n- 非法 JSON 返回默认 {}\n- 合法 JSON 正确解析\n",
        encoding="utf-8",
    )
    return spec


def test_cli_import_spec_creates_card(spec_file, tmp_path, monkeypatch):
    """CLI import-spec：spec → 卡片（automation=auto）。"""
    monkeypatch.setattr(_kb, "AGENT_GO_DIR", tmp_path / "ag")
    (tmp_path / "ag").mkdir(exist_ok=True)
    from agent_go import cli as _cli
    args = type("A", (), {"spec_path": str(spec_file), "stage": "brainstorm",
                          "repo": "/tmp", "type": "implementation"})()
    _cli.cmd_kanban_import_spec(args)
    board = _kb.load_board(force=True)
    cards = board.get("cards", [])
    assert len(cards) == 1
    c = cards[0]
    assert c["spec_path"] == str(spec_file)
    assert c["automation"] == "auto"
    assert c["type"] == "implementation"
    assert "safe_json_load" in c["title"]


@pytest.fixture
def spec_server(tmp_path: Path, monkeypatch):
    import agent_go.profiles as prof
    import agent_go.config as cfg
    import agent_go.web_server as ws
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
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


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestImportSpecApi:
    def test_import_spec_ok(self, spec_server, spec_file):
        code, d = _post(f"{spec_server}/api/kanban/import-spec",
                        {"spec_path": str(spec_file), "repo": "/tmp"})
        assert code == 200
        assert d["ok"] is True
        assert d["card"]["spec_path"] == str(spec_file)
        assert d["card"]["automation"] == "auto"
        assert "flow" in d

    def test_import_spec_missing_path_400(self, spec_server):
        code, d = _post(f"{spec_server}/api/kanban/import-spec", {"spec_path": ""})
        assert code == 400

    def test_import_spec_not_found_422(self, spec_server):
        code, d = _post(f"{spec_server}/api/kanban/import-spec",
                        {"spec_path": "/nonexistent/spec.md"})
        assert code == 422
