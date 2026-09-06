"""web 排障页测试：api_trajectory / api_worktree_diff 数据层 + HTTP 路由。

数据构造方式与 test_web_server.py 一致：monkeypatch AGENT_GO_DIR 指向临时目录。
"""
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator

import pytest

TASK_ID = "task-20260905-100000-111-abcd"


@pytest.fixture
def ts_tasks(tmp_path: Path, monkeypatch) -> Generator[dict, None, None]:
    """构造一个带 trajectory 文件与保留 worktree 的模拟任务目录。"""
    import agent_go.web_server as ws

    agent_go_dir = tmp_path / "agent_go_data"
    monkeypatch.setattr(ws, "AGENT_GO_DIR", agent_go_dir)
    monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", agent_go_dir)

    td = agent_go_dir / TASK_ID
    td.mkdir(parents=True)
    (td / "meta.json").write_text(json.dumps({
        "task": "排障测试任务", "status": "VERIFICATION_FAILED",
        "status_schema_version": 1,
        "subtasks": [
            {"id": "sub-1", "title": "好子任务"},
            {"id": "sub-2", "title": "坏子任务"},
        ],
        "results": [
            {"subtask_id": "sub-1", "status": "completed"},
            {"subtask_id": "sub-2", "status": "failed", "failure_reason": "验证失败"},
        ],
    }), encoding="utf-8")
    # sub-1 轨迹：3 条有效事件 + 1 条坏行 + 1 空行（验证坏行跳过）
    traj_dir = td / "trajectory"
    traj_dir.mkdir()
    (traj_dir / "sub-1.jsonl").write_text("\n".join([
        json.dumps({"seq": 1, "time": 1.0, "type": "turn/start", "data": {"turn": 1}}),
        "not-json{{{",
        json.dumps({"seq": 2, "time": 1.1, "type": "assistant/message",
                    "data": {"usage": {"inputTokens": 10, "outputTokens": 5,
                                       "cacheReadTokens": 2}}}),
        "",
        json.dumps({"seq": 3, "time": 1.2, "type": "tool/call",
                    "data": {"name": "bash", "arguments_summary": "ls"}}),
    ]) + "\n", encoding="utf-8")
    # sub-2 保留 worktree（目录 + .git，与 api_worktrees 判定同口径）
    wt = td / "sub-2" / "work"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake", encoding="utf-8")

    yield {"dir": agent_go_dir, "task_id": TASK_ID}


@pytest.fixture
def base_url(ts_tasks) -> Generator[str, None, None]:
    """启动真实短生命周期 HTTP 服务（同 test_web_server.base_url）。"""
    import agent_go.web_server as ws
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


def _get(url: str):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read())


def _get_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class _FakeProc:
    """subprocess.run 返回值替身。"""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_git(monkeypatch, stat: str = "", status: str = "", diff: str = "",
               rc: int = 0):
    """mock web_data 内的 subprocess.run：按 git 子命令分发固定输出。"""
    from agent_go import web_data

    def fake_run(cmd, cwd=None, **kwargs):
        assert cmd[0] == "git"
        err = "boom" if rc else ""
        if "--stat" in cmd:
            return _FakeProc(stat, rc, err)
        if "status" in cmd:
            return _FakeProc(status, rc, err)
        return _FakeProc(diff, rc, err)

    monkeypatch.setattr(web_data.subprocess, "run", fake_run)


class TestApiTrajectory:
    """数据层：api_trajectory。"""

    def test_ok(self, ts_tasks):
        from agent_go.web_data import api_trajectory
        d = api_trajectory(TASK_ID, "sub-1")
        assert d is not None
        assert d["available"] is True
        assert d["truncated"] is False
        # 坏行与空行被跳过，只剩 3 条有效事件
        assert [e["seq"] for e in d["events"]] == [1, 2, 3]
        assert d["events"][1]["data"]["usage"]["inputTokens"] == 10

    def test_missing_file_available_false(self, ts_tasks):
        """sub-2 无轨迹文件 → available=False 而非 None（前端区分「无轨迹」）。"""
        from agent_go.web_data import api_trajectory
        d = api_trajectory(TASK_ID, "sub-2")
        assert d is not None
        assert d["available"] is False
        assert d["events"] == []

    def test_task_not_found_returns_none(self, ts_tasks):
        from agent_go.web_data import api_trajectory
        assert api_trajectory("task-20260905-100000-999-ffff", "sub-1") is None

    def test_invalid_ids_return_none(self, ts_tasks):
        from agent_go.web_data import api_trajectory
        assert api_trajectory("../../etc", "sub-1") is None
        assert api_trajectory(TASK_ID, "../x") is None
        assert api_trajectory(TASK_ID, "a/b") is None

    def test_truncation(self, ts_tasks, monkeypatch):
        from agent_go import web_data
        monkeypatch.setattr(web_data, "MAX_TRAJECTORY_EVENTS", 2)
        d = web_data.api_trajectory(TASK_ID, "sub-1")
        assert d["available"] is True
        assert d["truncated"] is True
        assert len(d["events"]) == 2

    def _write_attempt(self, ts_tasks, sub_id: str, attempt: int, marker: str):
        td = ts_tasks["dir"] / TASK_ID / "trajectory"
        (td / f"{sub_id}.attempt-{attempt}.jsonl").write_text(
            json.dumps({"seq": 1, "time": 1.0, "type": "turn/start",
                        "data": {"turn": 1, "marker": marker}}) + "\n", encoding="utf-8")

    def test_attempt_files_preferred_latest(self, ts_tasks):
        """ADR-010 阶段 2：有 attempt 文件时默认取最新 attempt（而非旧格式 sub.jsonl）。"""
        from agent_go.web_data import api_trajectory
        self._write_attempt(ts_tasks, "sub-1", 1, "first")
        self._write_attempt(ts_tasks, "sub-1", 2, "second")
        d = api_trajectory(TASK_ID, "sub-1")
        assert d["available"] is True
        assert d["attempts"] == [1, 2]
        assert d["attempt"] == 2
        assert d["events"][0]["data"]["marker"] == "second"

    def test_attempt_explicit_selection(self, ts_tasks):
        from agent_go.web_data import api_trajectory
        self._write_attempt(ts_tasks, "sub-1", 1, "first")
        self._write_attempt(ts_tasks, "sub-1", 2, "second")
        d = api_trajectory(TASK_ID, "sub-1", attempt=1)
        assert d["attempt"] == 1
        assert d["events"][0]["data"]["marker"] == "first"

    def test_attempt_missing_returns_unavailable(self, ts_tasks):
        """指定不存在的 attempt → available=False（attempts 列表仍返回）。"""
        from agent_go.web_data import api_trajectory
        self._write_attempt(ts_tasks, "sub-1", 1, "first")
        d = api_trajectory(TASK_ID, "sub-1", attempt=9)
        assert d["available"] is False
        assert d["attempts"] == [1]

    def test_legacy_fallback_when_no_attempts(self, ts_tasks):
        """无 attempt 文件时回退旧格式 <sub>.jsonl（向后兼容）。"""
        from agent_go.web_data import api_trajectory
        d = api_trajectory(TASK_ID, "sub-1")
        assert d["available"] is True
        assert d["attempts"] == []
        assert d["attempt"] == 0
        assert [e["seq"] for e in d["events"]] == [1, 2, 3]


class TestApiWorktreeDiff:
    """数据层：api_worktree_diff（mock subprocess.run，不跑真 git）。"""

    def test_ok(self, ts_tasks, monkeypatch):
        from agent_go.web_data import api_worktree_diff
        _patch_git(monkeypatch, stat=" a.py | 2 +-\n", status=" M a.py\n",
                   diff="diff --git a/a.py b/a.py\n+hello\n")
        d = api_worktree_diff(TASK_ID, "sub-2")
        assert d["available"] is True
        assert d["reason"] == ""
        assert "a.py" in d["stat"]
        assert "M a.py" in d["status"]
        assert "+hello" in d["diff"]
        assert d["diff_truncated"] is False

    def test_diff_truncated(self, ts_tasks, monkeypatch):
        from agent_go import web_data
        big = "x" * (web_data.MAX_DIFF_CHARS + 100)
        _patch_git(monkeypatch, stat="s", status="", diff=big)
        d = web_data.api_worktree_diff(TASK_ID, "sub-2")
        assert d["available"] is True
        assert d["diff_truncated"] is True
        assert len(d["diff"]) == web_data.MAX_DIFF_CHARS

    def test_worktree_missing(self, ts_tasks):
        """sub-1 在 meta 中但无 worktree → available=False + 清理提示（非 None）。"""
        from agent_go.web_data import api_worktree_diff
        d = api_worktree_diff(TASK_ID, "sub-1")
        assert d is not None
        assert d["available"] is False
        assert "worktree" in d["reason"]

    def test_subtask_not_found(self, ts_tasks):
        from agent_go.web_data import api_worktree_diff
        d = api_worktree_diff(TASK_ID, "sub-9")
        assert d is not None
        assert d["available"] is False
        assert d["reason"] == "subtask not found"

    def test_task_not_found_returns_none(self, ts_tasks):
        from agent_go.web_data import api_worktree_diff
        assert api_worktree_diff("task-20260905-100000-999-ffff", "sub-2") is None

    def test_invalid_sub_id_returns_none(self, ts_tasks):
        from agent_go.web_data import api_worktree_diff
        assert api_worktree_diff(TASK_ID, "../x") is None

    def test_git_failure(self, ts_tasks, monkeypatch):
        from agent_go.web_data import api_worktree_diff
        _patch_git(monkeypatch, rc=128)
        d = api_worktree_diff(TASK_ID, "sub-2")
        assert d["available"] is False
        assert "失败" in d["reason"]

    def test_git_timeout(self, ts_tasks, monkeypatch):
        from agent_go import web_data

        def fake_run(cmd, cwd=None, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        monkeypatch.setattr(web_data.subprocess, "run", fake_run)
        d = web_data.api_worktree_diff(TASK_ID, "sub-2")
        assert d["available"] is False
        assert "超时" in d["reason"]


class TestTroubleshootRoutes:
    """HTTP 路由层：trajectory / worktree-diff 的 200/404 语义。"""

    def test_trajectory_200(self, base_url):
        status, d = _get(f"{base_url}/api/tasks/{TASK_ID}/sub-1/trajectory")
        assert status == 200
        assert d["available"] is True
        assert len(d["events"]) == 3

    def test_trajectory_missing_file_200_available_false(self, base_url):
        """无轨迹文件不是 404。"""
        status, d = _get(f"{base_url}/api/tasks/{TASK_ID}/sub-2/trajectory")
        assert status == 200
        assert d["available"] is False

    def test_trajectory_404_unknown_task(self, base_url):
        assert _get_status(
            f"{base_url}/api/tasks/task-20260905-100000-999-ffff/sub-1/trajectory") == 404

    def test_worktree_diff_200(self, base_url, monkeypatch):
        _patch_git(monkeypatch, stat=" a.py | 1 +\n", status="", diff="d")
        status, d = _get(f"{base_url}/api/tasks/{TASK_ID}/sub-2/worktree-diff")
        assert status == 200
        assert d["available"] is True

    def test_worktree_diff_missing_200_available_false(self, base_url):
        status, d = _get(f"{base_url}/api/tasks/{TASK_ID}/sub-1/worktree-diff")
        assert status == 200
        assert d["available"] is False

    def test_worktree_diff_404_unknown_task(self, base_url):
        assert _get_status(
            f"{base_url}/api/tasks/task-20260905-100000-999-ffff/sub-2/worktree-diff") == 404
