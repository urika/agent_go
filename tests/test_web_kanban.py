"""Web 看板（Kanban）端点集成测试。

仿 test_web_ops.py：起真实 ThreadingHTTPServer + urllib.request；
task_runner.start_run 全部 mock（不触发真实 agent_go 子进程）。
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
import agent_go.kanban as kb
import agent_go.web_server as ws


@pytest.fixture
def ops_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws, kb):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    return adir


@pytest.fixture
def ops_server(ops_env, monkeypatch) -> Generator[str, None, None]:
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
    server.admin_token = ""
    server.viewer_token = ""
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


def _get(url: str):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def _audit_lines(adir: Path) -> list:
    f = adir / "web_audit.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def _mk_task(adir: Path, task_id: str, status: str = "FAILED") -> Path:
    td = adir / task_id
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task_id": task_id, "task": "测试任务", "status": status,
        "status_schema_version": 1, "repo": "/tmp/repo",
        "created": "2026-08-10T10:00:00", "subtasks": [], "results": [],
    }), encoding="utf-8")
    return td


TID = "task-20260810-100000-111-aaaa"


def _create_card(ops_server: str, **over) -> dict:
    body = {"title": "测试卡片", "type": "discussion"}
    body.update(over)
    code, d = _post(f"{ops_server}/api/kanban/cards", body)
    assert code == 200, d
    return d["card"]


class TestGetKanban:
    def test_empty_board(self, ops_server):
        d = _get(f"{ops_server}/api/kanban")
        assert len(d["stages"]) == 5
        assert d["stages"][0]["key"] == "brainstorm"
        assert d["total"] == 0
        assert all(d["cards"][s["key"]] == [] for s in d["stages"])

    def test_grouped_by_stage(self, ops_server):
        c1 = _create_card(ops_server, title="卡1")
        c2 = _create_card(ops_server, title="卡2", stage="design")
        _create_card(ops_server, title="卡3")
        d = _get(f"{ops_server}/api/kanban")
        assert d["total"] == 3
        assert [c["title"] for c in d["cards"]["brainstorm"]] == ["卡1", "卡3"]
        assert [c["id"] for c in d["cards"]["design"]] == [c2["id"]]
        assert c1["id"] in {c["id"] for c in d["cards"]["brainstorm"]}

    def test_archived_not_returned(self, ops_server):
        card = _create_card(ops_server)
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive", {})
        assert code == 200
        d = _get(f"{ops_server}/api/kanban")
        assert d["total"] == 0

    def test_archived_visible_with_archived_param(self, ops_server):
        """备注1：?archived=1 返回已归档卡片（archived:true），默认不返回。"""
        card = _create_card(ops_server)
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive", {})
        assert code == 200
        d = _get(f"{ops_server}/api/kanban?archived=1")
        assert d["total"] == 1
        found = d["cards"]["brainstorm"][0]
        assert found["id"] == card["id"]
        assert found["archived"] is True

    def test_unarchive_returns_to_board(self, ops_server):
        card = _create_card(ops_server)
        _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive", {})
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive",
                        {"archived": False})
        assert code == 200
        d = _get(f"{ops_server}/api/kanban")
        assert d["total"] == 1
        assert d["cards"]["brainstorm"][0]["id"] == card["id"]

    def test_latest_task_derived_from_meta(self, ops_server, ops_env):
        """卡片 task_ids 软链接 → latest_task 从 meta.json 实时派生。"""
        _mk_task(ops_env, TID, status="FAILED")
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        kb.link_task(card["id"], TID)
        d = _get(f"{ops_server}/api/kanban")
        found = d["cards"]["brainstorm"][0]
        assert found["latest_task"]["task_id"] == TID
        assert found["latest_task"]["status"]  # 具体值由 status.py 派生，非空即可
        # 链接任务被清理后 → unknown 兜底
        import shutil
        shutil.rmtree(ops_env / TID)
        d = _get(f"{ops_server}/api/kanban")
        assert d["cards"]["brainstorm"][0]["latest_task"]["status"] == "unknown"


class TestCreateMove:
    def test_create_ok_and_audit(self, ops_server, ops_env):
        card = _create_card(ops_server, title="头脑风暴：看板",
                            description="# 讨论\n内容", stage="requirements")
        assert card["stage"] == "requirements"
        audits = [a for a in _audit_lines(ops_env) if a["op"] == "kanban.create"]
        assert audits and audits[0]["ok"]
        assert audits[0]["params"]["title"] == "头脑风暴：看板"

    def test_create_invalid_type_400(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/cards", {"title": "x", "type": "bad"})
        assert code == 400

    def test_create_implementation_no_repo_422(self, ops_server):
        code, d = _post(f"{ops_server}/api/kanban/cards",
                        {"title": "x", "type": "implementation"})
        assert code == 422
        assert "repo" in d["error"]

    def test_create_empty_title_400(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/cards", {"title": " ", "type": "discussion"})
        assert code == 400

    def test_move_ok(self, ops_server, ops_env):
        card = _create_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/move",
                        {"stage": "design", "note": "评审通过"})
        assert code == 200
        assert d["card"]["stage"] == "design"
        h = d["card"]["history"][-1]
        assert h["action"] == "move" and h["from"] == "brainstorm" and h["to"] == "design"
        assert any(a["op"] == "kanban.move" for a in _audit_lines(ops_env))

    def test_move_invalid_stage_422(self, ops_server):
        card = _create_card(ops_server)
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/move",
                        {"stage": "done"})
        assert code == 422

    def test_move_not_found_404(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/cards/card-ghost000000/move",
                        {"stage": "design"})
        assert code == 404

    def test_move_bad_card_id_400(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/cards/bad%20id/move", {"stage": "design"})
        assert code == 400

    def test_update_ok(self, ops_server):
        card = _create_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/update",
                        {"description": "新描述", "cron": ""})
        assert code == 200
        assert d["card"]["description"] == "新描述"

    def test_update_null_is_ignored(self, ops_server):
        """L2：JSON null 视为未传，不覆盖成字面量 'None'。"""
        card = _create_card(ops_server, title="原标题")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/update",
                        {"repo": None, "title": "新标题"})
        assert code == 200
        assert d["card"]["title"] == "新标题"
        assert d["card"]["repo"] == ""

    def test_update_repo_stripped(self, ops_server):
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/update",
                        {"repo": "  /tmp/repo  "})
        assert code == 200
        assert d["card"]["repo"] == "/tmp/repo"


class TestDispatch:
    def test_dispatch_ok(self, ops_server, ops_env, monkeypatch):
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda repo, task, parallel=1, goal=None, confirm_mode="auto", wait_for_id=True, on_task_id=None, on_exit=None: (on_task_id(TID) if on_task_id else None) or TID)
        card = _create_card(ops_server, type="implementation", repo="/tmp",
                            description="实现详情")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch",
                        {"parallel": 2})
        assert code == 200
        # W2.1 异步派发：dispatch 立即返回 starting，task_id 经 on_task_id 回调关联
        assert d["status"] == "starting"
        # mock 的 on_task_id 同步触发 → dispatch_card 已执行（link_task + 自动流转）
        import agent_go.kanban as _kb
        card_after = _kb.get_card(card["id"])
        assert card_after["task_ids"] == [TID]
        assert card_after["stage"] == "implementation"
        acts = [h["action"] for h in card_after["history"]]
        assert acts[-2:] == ["link", "move"]
        audits = [a for a in _audit_lines(ops_env) if a["op"] == "kanban.dispatch"]
        assert audits and audits[0]["ok"]

    def test_dispatch_discussion_422(self, ops_server):
        card = _create_card(ops_server, type="discussion")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 422
        assert "派发" in d["error"]

    def test_dispatch_repo_missing_422(self, ops_server, monkeypatch):
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        # repo 改成不存在路径后再派发
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/update",
                        {"repo": "/no/such/path-xyz"})
        assert code == 200
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 422
        assert "repo" in d["error"]

    def test_dispatch_not_found_404(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/cards/card-ghost000000/dispatch", {})
        assert code == 404

    def test_dispatch_conflict_running_task_409(self, ops_server, ops_env, monkeypatch):
        """备注4：卡片已有运行中任务（EXECUTING）→ 拒绝重复派发（409），不启动新任务。"""
        _mk_task(ops_env, TID, status="EXECUTING")
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        kb.link_task(card["id"], TID)
        called = []
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda *a, **k: called.append(a) or "should-not-happen")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 409
        assert "运行中" in d["error"]
        assert d["task_id"] == TID
        assert called == []  # start_run 未被调用

    def test_dispatch_ok_after_task_finished(self, ops_server, ops_env, monkeypatch):
        """任务结束后（VERIFICATION_FAILED）可再次派发新任务。"""
        _mk_task(ops_env, TID, status="FAILED")
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        kb.link_task(card["id"], TID)
        TID2 = "task-20260816-100000-222-cccc"
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda repo, task, parallel=1, goal=None, confirm_mode="auto", wait_for_id=True, on_task_id=None, on_exit=None: (on_task_id(TID2) if on_task_id else None) or TID2)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 200
        assert d["status"] == "starting"
        import agent_go.kanban as _kb
        assert _kb.get_card(card["id"])["task_ids"] == [TID, TID2]



    def test_dispatch_exit_notification(self, ops_server, ops_env, monkeypatch):
        """W2.3：任务退出后状态回流 + 通知（on_complete/on_failed 经 notify_event）。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        notified = []
        monkeypatch.setattr("agent_go.notify.notify_event",
                            lambda event, context, config: notified.append((event, context.get("task_id"))))
        # mock start_run：捕获 on_exit 回调，模拟任务完成后触发
        captured = {}
        def fake_run(repo, task, parallel=1, goal=None, confirm_mode="auto", wait_for_id=True, on_task_id=None, on_exit=None):
            captured["on_task_id"] = on_task_id
            captured["on_exit"] = on_exit
            return "task-20260816-100000-999-dddd"
        monkeypatch.setattr(ws.task_runner, "start_run", fake_run)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 200
        # 模拟任务完成：创建 meta（DELIVERY_READY）+ 调 on_exit 回调
        tid = "task-20260816-100000-999-dddd"
        _mk_task(ops_env, tid, status="DELIVERY_READY")
        captured["on_task_id"](tid)
        captured["on_exit"](tid, 0)
        # 验证：通知被触发（on_complete，task_id 匹配）
        assert any(evt == "on_complete" for evt, _ in notified), notified
        # 验证：卡片状态回流到 operations
        assert _kb.get_card(card["id"])["stage"] == "operations"


class TestStatusSnapshot:
    def test_snapshot_caches_and_invalidates(self, ops_env):
        """备注3：任务状态快照按 meta 签名缓存；meta 变化（状态/大小）触发重建。"""
        _mk_task(ops_env, TID, status="FAILED")
        s1 = ws._task_status_snapshot()
        s2 = ws._task_status_snapshot()
        assert s1 is s2  # 缓存命中同一对象
        assert s1[TID]["status"]  # 非空即可
        # 修改 meta 状态 → 签名变化 → 重建快照
        td = ops_env / TID
        meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
        meta["status"] = "COMPLETED"
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        s3 = ws._task_status_snapshot()
        assert s3 is not s1
        assert s3[TID]["status"] != s1[TID]["status"]  # FAILED→COMPLETED 状态变化被捕获

    def test_snapshot_isolated_by_dir(self, ops_env, tmp_path, monkeypatch):
        """快照签名包含 AGENT_GO_DIR：切目录后不命中旧缓存。"""
        _mk_task(ops_env, TID, status="FAILED")
        ws._task_status_snapshot()
        adir2 = tmp_path / "agent_go2"
        adir2.mkdir()
        monkeypatch.setattr(ws, "AGENT_GO_DIR", adir2)
        s = ws._task_status_snapshot()
        assert TID not in s  # 新目录无任务，不返回旧缓存内容


class TestDelete:
    def test_delete_fresh_card_ok(self, ops_server, ops_env):
        card = _create_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/delete", {})
        assert code == 200
        assert d["deleted"] == card["id"]
        assert kb.get_card(card["id"]) is None
        assert any(a["op"] == "kanban.delete" for a in _audit_lines(ops_env))

    def test_delete_dispatched_card_422(self, ops_server):
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        kb.link_task(card["id"], TID)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/delete", {})
        assert code == 422
        assert "不可删除" in d["error"]


class TestArchiveBool:
    def test_archive_string_false_rejected(self, ops_server):
        """L3：archived 必须为真布尔，字符串 'false' 不应被强转为 True。"""
        card = _create_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive",
                        {"archived": "false"})
        assert code == 400
        assert "布尔" in d["error"]
        assert kb.get_card(card["id"])["archived"] is False

    def test_unarchive_via_bool(self, ops_server):
        card = _create_card(ops_server)
        code, _ = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive", {})
        assert code == 200
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/archive",
                        {"archived": False})
        assert code == 200
        assert d["card"]["archived"] is False


class TestAuth:
    def test_token_required_401(self, ops_env):
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
        server.admin_token = "sec"
        server.viewer_token = ""
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address[:2]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/kanban/cards",
                data=json.dumps({"title": "x", "type": "discussion"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req)
                assert False, "应返回 401"
            except urllib.error.HTTPError as e:
                assert e.code == 401
        finally:
            server.shutdown()
            server.server_close()


class TestSseSignature:
    def test_signature_changes_with_kanban_file(self, ops_env):
        sig_before = ws.WebHandler._tasks_signature()
        kb.create_card("触发签名变化", "discussion")
        sig_after = ws.WebHandler._tasks_signature()
        assert sig_before != sig_after
        assert "kanban:" in sig_after
