"""任务级互斥锁（M5.2 冲突处理）测试。"""
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
def lock_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    return adir


def _mk_task(adir: Path, task_id: str, status: str = "FAILED") -> Path:
    td = adir / task_id
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task_id": task_id, "task": "t", "status": status,
        "status_schema_version": 1, "repo": "/tmp/repo",
        "created": "2026-08-16T10:00:00", "subtasks": [], "results": [],
    }), encoding="utf-8")
    return td


class TestTaskLock:
    """锁工具语义。"""

    def test_acquire_release(self, tmp_path):
        from agent_go.task_lock import TaskLock, is_task_locked
        td = tmp_path / "task-x"
        td.mkdir()
        assert is_task_locked(td) is False
        lock = TaskLock(td).acquire()
        assert is_task_locked(td) is True
        lock.release()
        assert is_task_locked(td) is False

    def test_second_acquire_rejected(self, tmp_path):
        from agent_go.task_lock import TaskLock, is_task_locked
        td = tmp_path / "task-x"
        td.mkdir()
        lock = TaskLock(td).acquire()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                TaskLock(td).acquire()
        finally:
            lock.release()
        assert is_task_locked(td) is False

    def test_context_manager(self, tmp_path):
        from agent_go.task_lock import TaskLock, is_task_locked
        td = tmp_path / "task-x"
        td.mkdir()
        with TaskLock(td):
            assert is_task_locked(td) is True
        assert is_task_locked(td) is False


@pytest.fixture
def lock_server(lock_env, monkeypatch) -> Generator[str, None, None]:
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


TID = "task-20260816-100000-111-aaaa"


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWebLockConflict:
    """web 操作前置探测：任务锁被持有 → 409。"""

    def test_resume_locked_409(self, lock_server, lock_env, monkeypatch):
        from agent_go.task_lock import TaskLock
        td = _mk_task(lock_env, TID)
        lock = TaskLock(td).acquire()
        try:
            code, d = _post(f"{lock_server}/api/tasks/{TID}/resume", {})
            assert code == 409
            assert "被其他进程持有" in d["error"]
        finally:
            lock.release()

    def test_resume_unlocked_ok(self, lock_server, lock_env, monkeypatch):
        _mk_task(lock_env, TID)
        monkeypatch.setattr(ws.task_runner, "start_resume", lambda tid, parallel=1: tid)
        monkeypatch.setattr(ws.task_runner, "is_running", lambda k: False)
        code, d = _post(f"{lock_server}/api/tasks/{TID}/resume", {})
        assert code == 200

    def test_merge_locked_409(self, lock_server, lock_env):
        from agent_go.task_lock import TaskLock
        td = _mk_task(lock_env, TID)
        lock = TaskLock(td).acquire()
        try:
            code, d = _post(f"{lock_server}/api/tasks/{TID}/merge", {})
            assert code == 409
            assert "被其他进程持有" in d["error"]
        finally:
            lock.release()

    def test_cmd_merge_locked_fails(self, lock_env, monkeypatch, capsys):
        """CLI merge：锁被持有 → 报错退出。"""
        from agent_go.task_lock import TaskLock
        td = _mk_task(lock_env, TID)
        meta = json.loads((td / "meta.json").read_text())
        meta["delivery_branch"] = "agent_go/x/delivery"
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        lock = TaskLock(td).acquire()
        try:
            from agent_go.cli import cmd_merge
            with pytest.raises(SystemExit):
                cmd_merge(type("A", (), {"task_id": TID, "push": False, "remote": "origin"})())
        finally:
            lock.release()


class TestNotes:
    """M5.2.3 任务备注（协作沟通）。"""

    def test_notes_empty(self, lock_server, lock_env):
        _mk_task(lock_env, TID)
        with urllib.request.urlopen(f"{lock_server}/api/tasks/{TID}/notes") as r:
            d = json.loads(r.read())
        assert d["notes"] == []

    def test_add_and_read_note(self, lock_server, lock_env):
        _mk_task(lock_env, TID)
        code, d = _post(f"{lock_server}/api/tasks/{TID}/notes", {"text": "这个任务需要人工复核"})
        assert code == 200
        assert d["note"]["text"] == "这个任务需要人工复核"
        assert d["note"]["author"] == "local"
        with urllib.request.urlopen(f"{lock_server}/api/tasks/{TID}/notes") as r:
            d2 = json.loads(r.read())
        assert len(d2["notes"]) == 1
        assert d2["notes"][0]["text"] == "这个任务需要人工复核"

    def test_empty_text_rejected(self, lock_server, lock_env):
        _mk_task(lock_env, TID)
        code, d = _post(f"{lock_server}/api/tasks/{TID}/notes", {"text": "   "})
        assert code == 422
        assert "不能为空" in d["error"]

    def test_notes_not_found(self, lock_server):
        code, _ = _post(f"{lock_server}/api/tasks/{TID}/notes", {"text": "x"})
        assert code == 404

    def test_note_audited(self, lock_server, lock_env):
        _mk_task(lock_env, TID)
        _post(f"{lock_server}/api/tasks/{TID}/notes", {"text": "备注"})
        audit = lock_env / "web_audit.jsonl"
        assert audit.exists()
        assert any("tasks.note" in l for l in audit.read_text().splitlines())
