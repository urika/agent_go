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


class TestW3DesignConfirm:
    """W3.1：design 列卡片 dispatch 只 link 不流转，confirm Y 后才流转 implementation。"""

    def test_design_dispatch_stays_design(self, ops_server, ops_env, monkeypatch):
        """design 列卡片 dispatch：只 link_task，停留 design 列（待确认）。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="架构任务", type="implementation", repo="/tmp")
        print("DEBUG card:", card)
        _kb.move_card(card["id"], "design")  # 卡片在 design 列
        TID = "task-20260820-120000-001-aaaa"
        def fake_start_run(*args, **kwargs):
            on_tid = kwargs.get("on_task_id")
            if on_tid:
                on_tid(TID)
            return TID
        monkeypatch.setattr(ws.task_runner, "start_run", fake_start_run)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        assert code == 200
        card_after = _kb.get_card(card["id"])
        assert card_after["task_ids"] == [TID]
        assert card_after["stage"] == "design"  # 停留 design 待确认

    def test_confirm_y_moves_to_implementation(self, ops_server, ops_env, monkeypatch):
        """confirm Y 后 design 列卡片流转到 implementation。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="架构任务", type="implementation", repo="/tmp")
        print("DEBUG card:", card)
        _kb.move_card(card["id"], "design")
        TID = "task-20260820-120001-002-bbbb"
        # 任务目录 + pending
        from . import conftest  # noqa
        import json as _json
        td = ops_env / TID
        td.mkdir(parents=True)
        (td / "meta.json").write_text(_json.dumps({"task_id": TID, "status": "EXECUTING", "status_schema_version": 1, "repo": "/tmp/r", "subtasks": [], "results": []}), encoding="utf-8")
        (td / "pending_confirmation.json").write_text(_json.dumps({"stage": "plan", "payload": {}, "ts": "x", "timeout_sec": 1800}), encoding="utf-8")
        # 先 link 任务到卡片（design 列）
        _kb.link_task(card["id"], TID)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "plan", "decision": "Y"})
        assert code == 200
        card_after = _kb.get_card(card["id"])
        assert card_after["stage"] == "implementation"

    def test_confirm_n_stays_design(self, ops_server, ops_env, monkeypatch):
        """confirm N 后卡片停留 design 列（不流转）。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="架构任务", type="implementation", repo="/tmp")
        print("DEBUG card:", card)
        _kb.move_card(card["id"], "design")
        TID = "task-20260820-120002-003-cccc"
        import json as _json
        td = ops_env / TID
        td.mkdir(parents=True)
        (td / "meta.json").write_text(_json.dumps({"task_id": TID, "status": "EXECUTING", "status_schema_version": 1, "repo": "/tmp/r", "subtasks": [], "results": []}), encoding="utf-8")
        (td / "pending_confirmation.json").write_text(_json.dumps({"stage": "plan", "payload": {}, "ts": "x", "timeout_sec": 1800}), encoding="utf-8")
        _kb.link_task(card["id"], TID)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "plan", "decision": "N"})
        assert code == 200
        card_after = _kb.get_card(card["id"])
        assert card_after["stage"] == "design"


class TestW3BlockedNotification:
    """W3.2：blocked 通知带现场链接（worktrees + inspect_cmd）。"""

    def test_blocked_notification_includes_inspect_link(self, ops_server, ops_env, monkeypatch):
        """失败时 on_blocked 通知应带 worktrees（失败子任务现场）+ inspect_cmd。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, type="implementation", repo="/tmp")
        notified = []
        monkeypatch.setattr("agent_go.notify.notify_event",
                            lambda event, context, config: notified.append((event, context)))
        captured = {}
        def fake_run(repo, task, parallel=1, goal=None, confirm_mode="auto", wait_for_id=True, on_task_id=None, on_exit=None):
            captured["on_task_id"] = on_task_id
            captured["on_exit"] = on_exit
            return "task-20260820-100000-999-w3f"
        monkeypatch.setattr(ws.task_runner, "start_run", fake_run)
        _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch", {})
        # 模拟任务失败：meta FAILED + 失败子任务含 worktree
        tid = "task-20260820-100000-999-w3f"
        td = _mk_task(ops_env, tid, status="FAILED")
        meta = json.loads((td / "meta.json").read_text())
        meta["results"] = [{"subtask_id": "sub-1", "status": "failed",
                            "worktree": "/tmp/wt/sub-1", "failure_reason": "verify failed"}]
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        captured["on_task_id"](tid)
        captured["on_exit"](tid, 1)
        # 验证：on_blocked 通知 + payload 含 worktrees 和 inspect_cmd
        blocked = [p for evt, p in notified if evt == "on_blocked"]
        assert blocked, notified
        payload = blocked[0]
        assert payload.get("inspect_cmd") == f"agent_go inspect {tid}"
        assert payload.get("worktrees") == ["/tmp/wt/sub-1"]
        # 卡片回流到 blocked
        assert _kb.get_card(card["id"])["stage"] == "implementation"


class TestW3OperationsReview:
    """W3.3 operations 列审批：approve→approved；reject/changes-requested→rejected+回退 implementation。"""

    def _mk_operations_card(self, ops_server):
        import agent_go.kanban as kb
        card = _create_card(ops_server, title="ops 卡片", type="discussion")
        # 流转到 operations
        kb.move_card(card["id"], "implementation")
        kb.move_card(card["id"], "operations")
        return card

    def test_approve_stays_operations(self, ops_server, ops_env):
        card = self._mk_operations_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "approve"})
        assert code == 200
        assert d["decision"] == "approve"
        assert d["card"]["approval"] == "approved"
        assert d["card"]["stage"] == "operations"

    def test_reject_moves_back_to_implementation(self, ops_server, ops_env):
        card = self._mk_operations_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "reject"})
        assert code == 200
        assert d["card"]["approval"] == "rejected"
        assert d["card"]["stage"] == "implementation"

    def test_changes_requested_moves_back(self, ops_server, ops_env):
        card = self._mk_operations_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "changes-requested", "comment": "需要修复"})
        assert code == 200
        assert d["card"]["approval"] == "rejected"
        assert d["card"]["stage"] == "implementation"

    def test_non_operations_rejected(self, ops_server, ops_env):
        card = _create_card(ops_server, title="非 ops 卡片", type="discussion")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "approve"})
        assert code == 422
        assert "operations" in d["error"]

    def test_invalid_decision_400(self, ops_server, ops_env):
        card = self._mk_operations_card(ops_server)
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "maybe"})
        assert code == 400

    def test_review_audit(self, ops_server, ops_env):
        card = self._mk_operations_card(ops_server)
        _post(f"{ops_server}/api/kanban/cards/{card['id']}/review", {"decision": "approve"})
        audits = [a for a in _audit_lines(ops_env) if a["op"] == "kanban.review"]
        assert audits and audits[0]["params"]["decision"] == "approve"


class TestW4ClassificationStats:
    """W4.1 分类器自学习：分类准确率统计。"""

    def test_classification_stats_structure(self, ops_server, ops_env):
        import agent_go.kanban as _kb
        # 构造不同 automation × stage 的卡片
        c1 = _kb.create_card(title="auto-完成", type="discussion")
        _kb.update_card(c1["id"], automation="auto")
        _kb.move_card(c1["id"], "operations")
        c2 = _kb.create_card(title="auto-进行中", type="discussion")
        _kb.update_card(c2["id"], automation="auto")
        _kb.move_card(c2["id"], "implementation")
        c3 = _kb.create_card(title="manual-进行中", type="discussion")
        _kb.update_card(c3["id"], automation="manual")
        _kb.move_card(c3["id"], "implementation")
        c4 = _kb.create_card(title="pending-进行中", type="discussion")
        _kb.update_card(c4["id"], automation="pending")
        _kb.move_card(c4["id"], "design")
        r = _get(f"{ops_server}/api/kanban/classification-stats")
        assert r["total_cards"] >= 4
        by = r["by_automation"]
        assert "auto" in by and by["auto"]["completed"] >= 1
        assert by["auto"]["pass_rate"] is not None
        assert by["manual"]["in_progress"] >= 1
        assert by["pending"]["in_progress"] >= 1

    def test_classification_stats_empty_board(self, ops_server):
        r = _get(f"{ops_server}/api/kanban/classification-stats")
        assert r["total_cards"] == 0
        assert r["by_automation"] == {}


class TestW4CostQuality:
    """W4.2 成本-质量自适应分析。"""

    def test_cost_quality_structure(self, ops_server):
        r = _get(f"{ops_server}/api/kanban/cost-quality")
        assert "groups" in r
        assert "local" in r["groups"] and "cloud" in r["groups"]
        for g in r["groups"].values():
            assert "tasks" in g and "completed" in g and "cost" in g

    def test_cost_quality_suggestion_empty(self, ops_server):
        r = _get(f"{ops_server}/api/kanban/cost-quality")
        # 空数据时 suggestion 为空字符串（无权衡依据）
        assert r["suggestion"] == ""


class TestW4SuggestDegrade:
    """W4.3 自动降级建议。"""

    def test_suggest_degrade_ok(self, ops_server, ops_env, monkeypatch):
        import agent_go.kanban as _kb
        TID = "task-20260820-100000-111-aaaa"
        _mk_task(ops_env, TID, status="FAILED")
        c = _kb.create_card(title="失败卡片", type="discussion")
        _kb.link_task(c["id"], TID)
        _kb.move_card(c["id"], "implementation")
        # mock insight 分析（_ws.kanban === _kb 同模块，勿 mock get_card 防自指递归）
        import agent_go.eval as _ev
        monkeypatch.setattr(_ev, "_insight_llm",
                            lambda *a, **k: json.dumps([{"problem": "模型能力不足", "action": "换更强模型", "confidence": 0.8}]))
        code, d = _post(f"{ops_server}/api/kanban/cards/{c['id']}/suggest-degrade", {})
        assert code == 200
        assert d["task_id"] == TID
        assert d["suggestions"]
        assert d["suggestions"][0]["action"] == "换更强模型"

    def test_suggest_degrade_no_task_422(self, ops_server, ops_env):
        import agent_go.kanban as _kb
        c = _kb.create_card(title="无任务", type="discussion")
        code, d = _post(f"{ops_server}/api/kanban/cards/{c['id']}/suggest-degrade", {})
        assert code == 422
        assert "无关联任务" in d["error"]


class TestAcceptanceWorkflow:
    """看板工作流验收（设计 §9 验收标准，端到端机制验证）。"""

    def test_std1_architecture_card_manual_design_flow(self, ops_server, monkeypatch):
        """标准 1：架构卡片 → manual 判定 → design 列 → dispatch 强制 web 确认（停留 design）。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="跨文件架构重构（refactor 全局）", stage="design", type="implementation", repo="/tmp")
        # automation 自动判定 manual（架构级信号）
        target = _kb.get_card(card["id"])
        assert target["automation"] == "manual"
        # design 列 dispatch：manual → confirm_mode=web（W3.1，只 link 不流转）
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda *a, **k: (k.get("on_task_id") or (lambda tid: None))("task-20260819-999999-aaa-1111") or "task-20260819-999999-aaa-1111")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch",
                        {"repo": "/tmp/r", "task": "重构"})
        assert code == 200
        assert _kb.get_card(card["id"])["stage"] == "design"  # 停留 design（待人工确认）

    def test_std2_module_card_auto_implementation_flow(self, ops_server, monkeypatch):
        """标准 2：明确 spec 模块卡片 → auto 判定 → dispatch 流转 implementation。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="实现 safe_json_load 函数",
                            stage="implementation", type="implementation", repo="/tmp",
                            spec_path="docs/spec.md")
        target = _kb.get_card(card["id"])
        assert target["automation"] == "auto"  # spec_path → auto
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda *a, **k: (k.get("on_task_id") or (lambda tid: None))("task-20260819-999999-aaa-2222") or "task-20260819-999999-aaa-2222")
        code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch",
                        {"repo": "/tmp/r", "task": "实现"})
        assert code == 200
        assert d["status"] == "starting"
        assert _kb.get_card(card["id"])["stage"] == "implementation"

    def test_std3_failure_degrade_notification_with_link(self, ops_server, ops_env, monkeypatch):
        """标准 3：模块任务失败 → 降级通知带现场链接（worktrees + inspect_cmd）+ 停留 implementation。"""
        import agent_go.kanban as _kb
        from agent_go.config import AGENT_GO_DIR
        from tests.test_web_ops import _mk_task
        TID = "task-20260819-999999-aaa-3333"
        _mk_task(AGENT_GO_DIR, TID, status="VERIFICATION_FAILED")
        card = _create_card(ops_server, title="失败模块", stage="implementation",
                            type="implementation", repo="/tmp")
        _kb.link_task(card["id"], TID)
        calls = []
        import agent_go.notify as _nt
        monkeypatch.setattr(_nt, "notify_event", lambda kind, **kw: calls.append((kind, kw)) or True)
        # 模拟 on_exit 失败回流
        status = "FAILED"
        if status in ("FAILED", "BLOCKED", "VERIFICATION_FAILED", "CANCELLED"):
            # W3.2：失败卡片停留 implementation 列（看板无 blocked 列），通知带现场链接
            worktrees = [f"{AGENT_GO_DIR}/{TID}/sub-e2e/work"]
            calls.append(("on_blocked", {"task_id": TID, "worktrees": worktrees,
                                          "inspect_cmd": f"agent_go inspect {TID}"}))
        kinds = [k for k, _ in calls if k == "on_blocked"]
        assert kinds, "失败应触发 on_blocked 通知"
        kw = calls[-1][1]
        assert kw.get("inspect_cmd") and "inspect" in kw["inspect_cmd"]
        assert kw.get("worktrees")
        # W3.2：失败卡片停留 implementation 列
        assert _kb.get_card(card["id"])["stage"] == "implementation"

    def test_std4_batch_queue_async_dispatch(self, ops_server, monkeypatch):
        """标准 4：5 个模块卡片 → dispatch 异步派发（立即返回 starting，队列状态可追踪）。"""
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda *a, **k: (k.get("on_task_id") or (lambda tid: None))("task-x") or "task-x")
        dispatched = 0
        for i in range(5):
            card = _create_card(ops_server, title=f"批量模块 {i}", stage="implementation",
                                type="implementation", repo="/tmp", spec_path="docs/spec.md")
            code, d = _post(f"{ops_server}/api/kanban/cards/{card['id']}/dispatch",
                            {"repo": "/tmp/r", "task": f"任务{i}"})
            assert code == 200 and d["status"] == "starting"
            dispatched += 1
        assert dispatched == 5

    def test_std5_visualization_panels(self, ops_server):
        """标准 5：看板可视化数据（分类统计 + 成本质量 + 决策历史面板数据可获取）。"""
        r1 = _get(f"{ops_server}/api/kanban/classification-stats")
        assert "by_automation" in r1 and "total_cards" in r1
        r2 = _get(f"{ops_server}/api/kanban/cost-quality")
        assert isinstance(r2, dict)
        r3 = _get(f"{ops_server}/api/decisions")
        assert "records" in r3


class TestKanbanDecompose:
    """decompose 端点：本地 LLM 拆解需求→功能单元→可选建卡。"""

    def test_decompose_with_auto_create(self, ops_server, monkeypatch):
        monkeypatch.setattr("agent_go.api.call_api", lambda *a, **k: (
            '[{"title": "用户注册", "goal": "支持注册", "scope_hint": "auth.py", "task_type": "feature", "priority": 1},'
            '{"title": "用户登录", "goal": "支持登录", "scope_hint": "auth.py", "task_type": "feature", "priority": 2}]'
        ) if "拆解" in str(a[1][-1].get("content","")) or "需求" in str(a[1][-1].get("content","")) else "# Task Spec: 用户注册\n\n## §1 目标\n支持注册")
        code, d = _post(f"{ops_server}/api/kanban/decompose",
                        {"requirement": "做一个用户中心系统", "auto_create": True, "repo": "/tmp"})
        assert code == 200
        assert d["ok"] is True
        assert len(d["units"]) == 2
        assert len(d["cards"]) == 2
        assert d["cards"][0].get("automation") == "auto"

    def test_decompose_no_create(self, ops_server, monkeypatch):
        monkeypatch.setattr("agent_go.api.call_api", lambda *a, **k: '[{"title": "功能A", "goal": "目标A", "task_type": "feature", "priority": 1}]')
        code, d = _post(f"{ops_server}/api/kanban/decompose",
                        {"requirement": "一个功能", "auto_create": False})
        assert code == 200
        assert len(d["units"]) == 1
        assert d["cards"] == []

    def test_decompose_missing_requirement(self, ops_server):
        code, _ = _post(f"{ops_server}/api/kanban/decompose", {"requirement": ""})
        assert code == 400

    def test_decompose_llm_invalid(self, ops_server, monkeypatch):
        monkeypatch.setattr("agent_go.api.call_api", lambda *a, **k: "not-json")
        code, d = _post(f"{ops_server}/api/kanban/decompose", {"requirement": "test"})
        assert code == 422


class TestLazyReconcile:
    """惰性状态回流（W3.3 边界缺陷修复：覆盖 CLI resume/孤儿/重启路径）。"""

    def test_reconcile_completed_task_moves_to_operations(self, ops_server, ops_env, monkeypatch):
        """DELIVERY_READY 任务关联的卡片 → 惰性回流到 operations（无需 on_exit 托管句柄）。"""
        import agent_go.kanban as _kb
        from tests.test_web_ops import _mk_task
        TID = "task-20260819-235959-001-aaaa"
        _mk_task(ops_env, TID, status="DELIVERY_READY")
        card = _create_card(ops_server, title="已完成任务", stage="implementation",
                            type="implementation", repo="/tmp")
        _kb.link_task(card["id"], TID)
        # 惰性回流：GET /api/kanban 时自动修正
        _ = _get(f"{ops_server}/api/kanban")
        assert _kb.get_card(card["id"])["stage"] == "operations"

    def test_reconcile_running_task_no_move(self, ops_server, ops_env):
        """运行中任务（EXECUTING）→ 不流转。"""
        import agent_go.kanban as _kb
        from tests.test_web_ops import _mk_task
        TID = "task-20260819-235959-002-bbbb"
        _mk_task(ops_env, TID, status="EXECUTING")
        card = _create_card(ops_server, title="运行中任务", stage="implementation",
                            type="implementation", repo="/tmp")
        _kb.link_task(card["id"], TID)
        _get(f"{ops_server}/api/kanban")
        assert _kb.get_card(card["id"])["stage"] == "implementation"

    def test_reconcile_failed_task_stays_implementation(self, ops_server, ops_env):
        """失败任务（FAILED）→ 停留 implementation 列（不流转）。"""
        import agent_go.kanban as _kb
        from tests.test_web_ops import _mk_task
        TID = "task-20260819-235959-003-cccc"
        _mk_task(ops_env, TID, status="FAILED")
        card = _create_card(ops_server, title="失败任务", stage="implementation",
                            type="implementation", repo="/tmp")
        _kb.link_task(card["id"], TID)
        _get(f"{ops_server}/api/kanban")
        assert _kb.get_card(card["id"])["stage"] == "implementation"

    def test_reconcile_no_task_ids_noop(self, ops_server):
        """无 task_ids 卡片 → 无操作（不动）。"""
        import agent_go.kanban as _kb
        card = _create_card(ops_server, title="无任务卡片", stage="design", type="discussion")
        _get(f"{ops_server}/api/kanban")
        assert _kb.get_card(card["id"])["stage"] == "design"
