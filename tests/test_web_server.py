"""web_server 观察平台测试（W1-W6 验收）。

覆盖：任务清单 / 任务详情 / 子任务明细 / 日志 / metering / replay / SSE 签名 / 鉴权。
通过 monkeypatch AGENT_GO_DIR 指向临时目录构造模拟任务数据。
"""
import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def mock_tasks(tmp_path: Path, monkeypatch) -> Generator[dict, None, None]:
    """构造两个模拟任务目录，并把 AGENT_GO_DIR 指向临时目录。"""
    import agent_go.web_server as ws

    agent_go_dir = tmp_path / "agent_go_data"
    monkeypatch.setattr(ws, "AGENT_GO_DIR", agent_go_dir)
    monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", agent_go_dir)

    task1 = agent_go_dir / "task-20260802-100000-111-aaaa"
    task1.mkdir(parents=True)
    (task1 / "meta.json").write_text(json.dumps({
        "task": "测试任务 A",
        "status": "completed",
        "repo": "/tmp/repo-a",
        "created_at": "2026-08-02T10:00:00",
        "subtasks": [
            {"id": "sub-1", "title": "子任务1", "difficulty": "easy",
             "agent_type": "developer", "depends_on": [], "skills": ["test"]},
            {"id": "sub-2", "title": "子任务2", "difficulty": "hard",
             "agent_type": "architect", "depends_on": ["sub-1"], "skills": [],
             "description": "描述2", "verification": ["pytest -q"],
             "files_hint": ["a.py"], "risks": ["风险"]},
        ],
        "results": [
            {"status": "completed", "duration_sec": 10.5, "retry_count": 0,
             "verify_ok": True, "exit_code": 0, "summary": "完成",
             "agent_type_source": "llm", "worktree": "/tmp/wt/sub-1",
             "verification_results": [{"command": "pytest", "type": "shell",
                                       "passed": True, "duration_sec": 2.0}],
             "change_stats": {"files_changed": 1}},
            {"status": "failed", "duration_sec": 30.0, "retry_count": 2,
             "verify_ok": False, "exit_code": 1, "summary": "失败",
             "failure_reason": "测试未通过", "worktree": "/tmp/wt/sub-2",
             "verification_results": [{"command": "pytest", "type": "shell",
                                       "passed": False, "duration_sec": 3.0}]},
        ],
    }), encoding="utf-8")
    (task1 / "execution.log").write_text(
        "[subtask] sub-1 start\ninfo line\n[subtask] sub-2 start\nsub-2 line\n",
        encoding="utf-8")
    (task1 / "metering.jsonl").write_text("\n".join([
        json.dumps({"role": "planner", "actual_model": "deepseek-v4-pro",
                    "prompt_tokens": 100, "completion_tokens": 50,
                    "cost_usd": 0.01, "latency_ms": 1000, "result": "success"}),
        json.dumps({"role": "worker", "subtask_id": "sub-1",
                    "virtual_model": "agentgo-worker", "actual_model": "m1",
                    "prompt_tokens": 200, "completion_tokens": 80,
                    "cost_usd": 0.02, "latency_ms": 2000, "result": "success"}),
    ]) + "\n", encoding="utf-8")
    (task1 / "PLAN.md").write_text("# Plan\n步骤说明", encoding="utf-8")

    task2 = agent_go_dir / "task-20260802-110000-222-bbbb"
    task2.mkdir(parents=True)
    (task2 / "meta.json").write_text(json.dumps({
        "task": "测试任务 B", "status": "running", "repo": "/tmp/repo-b",
        "subtasks": [{"id": "sub-1", "title": "进行中"}],
        "results": [],
    }), encoding="utf-8")

    yield {"dir": agent_go_dir, "task1": task1.name, "task2": task2.name}


@pytest.fixture
def base_url(mock_tasks) -> Generator[str, None, None]:
    """启动真实短生命周期 HTTP 服务。"""
    import agent_go.web_server as ws
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


def _get(url: str):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read())


class TestApiTasks:
    """W1: 任务清单。"""

    def test_list_tasks(self, base_url):
        status, data = _get(f"{base_url}/api/tasks")
        assert status == 200
        assert len(data["tasks"]) == 2
        statuses = {t["id"]: t["status"] for t in data["tasks"]}
        assert statuses["task-20260802-100000-111-aaaa"] == "completed"
        assert statuses["task-20260802-110000-222-bbbb"] == "running"

    def test_task_summary_fields(self, base_url, mock_tasks):
        _, data = _get(f"{base_url}/api/tasks")
        t = [x for x in data["tasks"] if x["id"] == mock_tasks["task1"]][0]
        assert t["subtask_count"] == 2
        assert t["completed"] == 1
        assert t["failed"] == 1
        assert t["total_retries"] == 2
        assert t["cost_usd"] == 0.03  # planner 0.01 + worker 0.02


class TestApiTaskDetail:
    """W2: 任务详情 + 子任务主要属性。"""

    def test_detail(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}")
        assert d["status"] == "completed"
        assert len(d["subtasks"]) == 2
        s1 = d["subtasks"][0]
        assert s1["id"] == "sub-1"
        assert s1["difficulty"] == "easy"
        assert s1["agent_type"] == "developer"
        assert s1["status"] == "completed"
        s2 = d["subtasks"][1]
        assert s2["difficulty"] == "hard"
        assert s2["retry_count"] == 2
        assert s2["verify_ok"] is False
        assert s2["depends_on"] == ["sub-1"]

    def test_not_found(self, base_url):
        try:
            urllib.request.urlopen(f"{base_url}/api/tasks/task-nope")
            assert False, "should 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestApiSubtaskDetail:
    """W3: 子任务展开显示验证结果/改动统计。"""

    def test_detail_ok(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/sub-1/detail")
        assert d["id"] == "sub-1"
        assert d["result"]["verify_ok"] is True
        assert d["result"]["verification_results"][0]["passed"] is True
        assert d["result"]["change_stats"] == {"files_changed": 1}
        assert d["result"]["agent_type_source"] == "llm"

    def test_detail_failed(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/sub-2/detail")
        assert d["result"]["status"] == "failed"
        assert d["result"]["failure_reason"] == "测试未通过"
        assert d["result"]["verification_results"][0]["passed"] is False
        assert d["description"] == "描述2"
        assert d["risks"] == ["风险"]


class TestApiSubtaskLog:
    """W4: 子任务日志段。"""

    def test_log(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/sub-2/log")
        lines = d["lines"]
        assert lines
        assert any("sub-2" in ln["text"] for ln in lines)

    def test_log_missing_file(self, base_url, mock_tasks):
        # task2 无 execution.log
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task2']}/sub-1/log")
        assert d["lines"] == []


class TestApiMetering:
    """W5: metering 按 role 聚合。"""

    def test_summary(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/metering")
        assert d["summary"]["planner"]["count"] == 1
        assert d["summary"]["planner"]["cost_usd"] == 0.01
        assert d["summary"]["worker"]["count"] == 1
        assert d["summary"]["worker"]["cost_usd"] == 0.02
        assert len(d["rows"]) == 2


class TestApiReplay:
    """W5 附：replay 时间线。"""

    def test_replay(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/replay")
        assert "timeline" in d
        assert "summary" in d


class TestApiPlan:
    """Plan 展示。"""

    def test_plan(self, base_url, mock_tasks):
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/plan")
        assert "Plan" in d["plan_md"]


class TestAuth:
    """token 鉴权。"""

    def test_auth(self, mock_tasks):
        import agent_go.web_server as ws
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
        server.token = "sec"
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address[:2]
        base = f"http://127.0.0.1:{port}"
        try:
            # 无 token → 401
            try:
                urllib.request.urlopen(f"{base}/api/tasks")
                assert False, "should 401"
            except urllib.error.HTTPError as e:
                assert e.code == 401
            # 带 token → 200
            req = urllib.request.Request(
                f"{base}/api/tasks",
                headers={"Authorization": "Bearer sec"})
            with urllib.request.urlopen(req) as r:
                assert r.status == 200
            # 首页无需鉴权
            with urllib.request.urlopen(f"{base}/") as r:
                assert r.status == 200
        finally:
            server.shutdown()
            server.server_close()


class TestSSE:
    """SSE 事件流（短连接验证签名刷新）。"""

    def test_signature_changes_on_new_task(self, mock_tasks):
        import agent_go.web_server as ws
        before = ws.WebHandler._tasks_signature()
        new_dir = mock_tasks["dir"] / "task-new"
        new_dir.mkdir()
        (new_dir / "meta.json").write_text(json.dumps({"status": "running"}),
                                           encoding="utf-8")
        after = ws.WebHandler._tasks_signature()
        assert before != after

    def test_signature_stable_without_change(self, mock_tasks):
        import agent_go.web_server as ws
        assert (ws.WebHandler._tasks_signature()
                == ws.WebHandler._tasks_signature())


class TestServeConfig:
    """serve_web 参数。"""

    def test_serve_web_signature(self):
        import agent_go.web_server as ws
        import inspect
        sig = inspect.signature(ws.serve_web)
        assert sig.parameters["token"].default is None
        assert sig.parameters["port"].default == 8091
        assert sig.parameters["host"].default == "127.0.0.1"
