"""Tests for agent_go MCP Server (mcp_server.py)."""

import io, json, os, sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_go.mcp_server as mcp_mod
from agent_go.mcp_server import MCPServer, MCPError, TOOLS


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path):
    with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
        s = MCPServer()
        s._max_concurrent = 5
        yield s


# ── Tool schemas ──────────────────────────────────────────────────

class TestToolSchemas:
    def test_four_tools(self):
        assert len(TOOLS) == 4
        names = [t["name"] for t in TOOLS]
        assert names == ["run_task", "resume_task", "inspect_task", "review_task"]

    def test_run_task_required_fields(self):
        t = next(x for x in TOOLS if x["name"] == "run_task")
        assert "repo" in t["inputSchema"]["required"]
        assert "task" in t["inputSchema"]["required"]
        props = t["inputSchema"]["properties"]
        assert props["repo"]["type"] == "string"
        assert props["task"]["type"] == "string"
        assert props["parallel"]["default"] == 1
        assert props["wait"]["default"] is False

    def test_resume_task_required(self):
        t = next(x for x in TOOLS if x["name"] == "resume_task")
        assert t["inputSchema"]["required"] == ["task_id"]

    def test_inspect_task_annotations(self):
        t = next(x for x in TOOLS if x["name"] == "inspect_task")
        assert t["annotations"]["readOnlyHint"] is True
        assert t["annotations"]["destructiveHint"] is False

    def test_review_task_action_enum(self):
        t = next(x for x in TOOLS if x["name"] == "review_task")
        assert set(t["inputSchema"]["properties"]["action"]["enum"]) == {
            "analyze", "approve", "reject", "changes_requested"
        }

    def test_all_tools_have_annotations(self):
        for t in TOOLS:
            assert "annotations" in t
            assert "title" in t["annotations"]
            assert t["annotations"]["readOnlyHint"] in (True, False)


# ── Repo allowlist ────────────────────────────────────────────────

class TestRepoAllowlist:
    def test_default_allows_cwd(self, server):
        cwd = os.getcwd()
        assert server._check_repo_allowed(cwd) is True
        assert server._check_repo_allowed(cwd + "/subdir") is True

    def test_custom_glob_pattern(self, server, tmp_path):
        base = str(tmp_path)
        out = str(tmp_path.parent / "outside")
        server._allowed_repos = [base + "/*"]
        assert server._check_repo_allowed(base + "/proj") is True
        assert server._check_repo_allowed(base + "/proj/sub") is True
        assert server._check_repo_allowed(out) is False

    def test_multiple_patterns(self, server, tmp_path):
        a = str(tmp_path / "a")
        b = str(tmp_path / "b")
        c = str(tmp_path / "c")
        server._allowed_repos = [a + "/*", b + "/*"]
        assert server._check_repo_allowed(a + "/proj") is True
        assert server._check_repo_allowed(b + "/proj") is True
        assert server._check_repo_allowed(c + "/proj") is False

    def test_repo_is_base_dir(self, server, tmp_path):
        d = str(tmp_path)
        server._allowed_repos = [d + "/*"]
        assert server._check_repo_allowed(d) is True


# ── Error model ───────────────────────────────────────────────────

class TestErrorModel:
    def test_mcp_error(self):
        e = MCPError("AGENT_GO_TASK_NOT_FOUND", "任务不存在: task-xxx", retryable=False)
        assert e.code == "AGENT_GO_TASK_NOT_FOUND"
        assert e.retryable is False

    def test_mcp_error_retryable(self):
        e = MCPError("AGENT_GO_TIMEOUT", "超时", retryable=True)
        assert e.retryable is True


# ── JSON-RPC protocol ─────────────────────────────────────────────

class TestProtocol:
    def test_send(self, server, capsys):
        server._send({"jsonrpc": "2.0", "id": 1, "result": "ok"})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"jsonrpc": "2.0", "id": 1, "result": "ok"}

    def test_result(self, server, capsys):
        server._result(42, {"hello": "world"})
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"jsonrpc": "2.0", "id": 42, "result": {"hello": "world"}}

    def test_error(self, server, capsys):
        server._error(1, -32000, "Bad thing", {"detail": "x"})
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["code"] == -32000
        assert resp["error"]["data"]["detail"] == "x"

    def test_notify(self, server, capsys):
        server._notify("notifications/progress", {"progress": 1, "total": 4})
        captured = capsys.readouterr()
        n = json.loads(captured.out)
        assert n["method"] == "notifications/progress"
        assert n["params"]["progress"] == 1


# ── Tool dispatch ─────────────────────────────────────────────────

class TestDispatch:
    def test_unknown_tool(self, server, capsys):
        server._dispatch_tool("nope", {}, None, 1)
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["code"] == -32602

    def test_inspect_nonexistent_task(self, server, capsys):
        server._dispatch_tool("inspect_task", {"task_id": "task-nonexistent"}, None, 1)
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["code"] == -32000
        assert resp["error"]["data"]["error"]["code"] == "AGENT_GO_TASK_NOT_FOUND"

    def test_run_task_repo_not_allowed(self, server, capsys):
        server._allowed_repos = ["/allowed/*"]
        server._dispatch_tool("run_task", {"repo": "/evil/path", "task": "do stuff"}, None, 1)
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["code"] == -32000
        assert resp["error"]["data"]["error"]["code"] == "AGENT_GO_REPO_INVALID"

    def test_resume_nonexistent_task(self, server, capsys):
        server._dispatch_tool("resume_task", {"task_id": "task-nonexistent"}, None, 1)
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["data"]["error"]["code"] == "AGENT_GO_TASK_NOT_FOUND"

    def test_review_nonexistent_task(self, server, capsys):
        server._dispatch_tool("review_task", {"task_id": "task-nonexistent", "action": "analyze"}, None, 1)
        captured = capsys.readouterr()
        resp = json.loads(captured.out)
        assert resp["error"]["data"]["error"]["code"] == "AGENT_GO_TASK_NOT_FOUND"


# ── Inspect task with synthetic data ──────────────────────────────

class TestInspectTask:
    def _setup(self, tmp_path, task_id, meta, sub_dirs=None):
        """Create a synthetic task in AGENT_GO_DIR. Server's AGENT_GO_DIR = tmp_path via fixture."""
        td = tmp_path / task_id
        td.mkdir(parents=True)
        (td / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if sub_dirs:
            for d in sub_dirs:
                (td / d).mkdir(parents=True, exist_ok=True)
        return td

    def test_inspect_with_meta(self, server, tmp_path):
        task_id = "task-test-inspect"
        self._setup(tmp_path, task_id, {
            "task_id": task_id, "task": "测试任务", "repo": "/tmp/repo",
            "status": "completed",
            "subtasks": [{"id": "sub-1", "title": "步骤一"}],
            "results": [{"subtask_id": "sub-1", "title": "步骤一",
                         "status": "completed", "duration_sec": 12,
                         "verify_ok": True, "retry_count": 0,
                         "change_stats": {"files_changed": 2, "insertions": 10, "deletions": 3}}]
        })
        result = server._tool_inspect({"task_id": task_id})
        assert result["task_id"] == task_id
        assert result["status"] == "completed"
        assert result["progress"]["total"] == 1
        assert result["progress"]["completed"] == 1
        assert len(result["subtasks"]) == 1
        assert result["subtasks"][0]["changes"]["files"] == 2

    def test_inspect_with_logs(self, server, tmp_path):
        task_id = "task-test-logs"
        td = self._setup(tmp_path, task_id, {"task_id": task_id, "status": "running", "subtasks": [], "results": []})
        log = "\n".join(f"line {i}" for i in range(100))
        (td / "execution.log").write_text(log, encoding="utf-8")
        result = server._tool_inspect({"task_id": task_id, "include_log_tail": True, "log_lines": 10})
        assert len(result["log_tail"]) == 10
        assert result["log_tail"][0] == "line 90"

    def test_inspect_with_preserved(self, server, tmp_path):
        task_id = "task-test-preserved"
        self._setup(tmp_path, task_id, {
            "task_id": task_id, "status": "failed",
            "subtasks": [{"id": "sub-1"}, {"id": "sub-2"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed"},
                {"subtask_id": "sub-2", "status": "failed", "failure_reason": "编译错误"}
            ]
        }, sub_dirs=["sub-2"])
        result = server._tool_inspect({"task_id": task_id})
        preserved = result["preserved_worktrees"]
        assert len(preserved) == 1
        assert preserved[0]["id"] == "sub-2"
        assert preserved[0]["failure_reason"] == "编译错误"


# ── Cost aggregation ──────────────────────────────────────────────

class TestCostAggregation:
    def test_no_metering(self, server, tmp_path):
        assert server._aggregate_cost(tmp_path) == 0.0

    def test_sum_cost(self, server, tmp_path):
        metering = tmp_path / "metering.jsonl"
        metering.write_text(
            '{"role":"planner","cost_usd":0.05}\n{"role":"worker","cost_usd":0.12}\n{"latency_ms":100}\n',
            encoding="utf-8"
        )
        assert server._aggregate_cost(tmp_path) == 0.17

    def test_handles_bad_lines(self, server, tmp_path):
        metering = tmp_path / "metering.jsonl"
        metering.write_text(
            '{"role":"planner","cost_usd":0.05}\nnot-json\n{"role":"worker","cost_usd":0.10}\n',
            encoding="utf-8"
        )
        assert server._aggregate_cost(tmp_path) == 0.15

    def test_empty_file(self, server, tmp_path):
        (tmp_path / "metering.jsonl").write_text("", encoding="utf-8")
        assert server._aggregate_cost(tmp_path) == 0.0


# ── Wait/events loop ──────────────────────────────────────────────

class TestWaitEvents:
    """Test _wait_with_events: reads --json event stream + polls meta.json."""

    def _mock_proc(self, exited: bool = False, returncode: int = 0):
        proc = MagicMock()
        proc.stdout = io.StringIO()
        if exited:
            proc.poll.return_value = returncode
        else:
            proc.poll.return_value = None
        return proc

    def test_completed_immediately(self, server, tmp_path):
        task_id = "task-test-events"
        td = tmp_path / task_id
        td.mkdir()
        meta = {"task_id": task_id, "status": "completed", "subtasks": [], "results": []}
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        proc = self._mock_proc(exited=True)
        with patch.object(server, "_aggregate_cost", return_value=0.42):
            result = server._wait_with_events(proc, task_id, 10)
        assert result["status"] == "completed"
        assert result["cost_usd"] == 0.42

    def test_timeout_returns_snapshot(self, server, tmp_path):
        task_id = "task-test-timeout"
        td = tmp_path / task_id
        td.mkdir()
        meta = {"task_id": task_id, "status": "running", "subtasks": [], "results": []}
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        proc = self._mock_proc(exited=False)
        with patch.object(server, "_aggregate_cost", return_value=0.0):
            result = server._wait_with_events(proc, task_id, 0.5)
        assert result["status"] == "running"
        assert "timeout_hint" in result

    def test_progress_notification(self, server, tmp_path):
        task_id = "task-test-progress"
        td = tmp_path / task_id
        td.mkdir()
        meta = {
            "task_id": task_id, "status": "running",
            "subtasks": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}, {"id": "s4"}],
            "results": [
                {"subtask_id": "s1", "status": "completed", "duration_sec": 10},
                {"subtask_id": "s2", "status": "completed", "duration_sec": 15},
            ]
        }
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        proc = self._mock_proc(exited=False)
        notifications = []
        original = server._send
        def track(msg):
            if msg.get("method") == "notifications/progress":
                notifications.append(msg["params"])
            original(msg)
        server._send = track
        with patch.object(server, "_aggregate_cost", return_value=0.0):
            result = server._wait_with_events(proc, task_id, 0.5, token="abc123")
        assert any(n.get("progressToken") == "abc123" for n in notifications)

    def test_event_stream_updates_activity(self, server, tmp_path):
        """Verify lifecycle events from stdout update progress message."""
        task_id = "task-test-stream"
        td = tmp_path / task_id
        td.mkdir()
        meta = {"task_id": task_id, "status": "running",
                "subtasks": [{"id": "s1"}, {"id": "s2"}], "results": []}
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        ev_subtask_start = json.dumps({
            "event": "subtask_start", "level": "event",
            "ts": "2026-07-27T12:00:00",
            "data": {"data": {"sub_id": "s1", "title": "Step one"}}
        }) + "\n"
        ev_subtask_done = json.dumps({
            "event": "subtask_complete", "level": "event",
            "ts": "2026-07-27T12:00:05",
            "data": {"data": {"sub_id": "s1", "status": "completed"}}
        }) + "\n"

        proc = MagicMock()
        proc.stdout = io.StringIO(ev_subtask_start + ev_subtask_done)
        proc.poll.return_value = 0

        notifications = []
        original = server._send
        def track(msg):
            if msg.get("method") == "notifications/progress":
                notifications.append(msg["params"])
            original(msg)
        server._send = track

        with patch.object(server, "_aggregate_cost", return_value=0.0):
            result = server._wait_with_events(proc, task_id, 5, token="tok1")

        progress_messages = [n.get("message", "") for n in notifications]
        assert any("Executing s1" in m for m in progress_messages) or \
               any("Completed s1" in m for m in progress_messages)


# ── Thread-safety (concurrent tasks) ──────────────────────────────

class TestConcurrency:
    def test_capacity_limit(self, server):
        server._max_concurrent = 2
        server._running["task-a"] = MagicMock()
        server._running["task-b"] = MagicMock()
        with server._lock:
            assert len(server._running) >= server._max_concurrent

    def test_lock_held(self, server):
        with server._lock:
            server._running["x"] = "test"
        assert server._running["x"] == "test"


# ── Build completed ───────────────────────────────────────────────

class TestBuildCompleted:
    def test_empty(self, server):
        r = server._build_completed("task-x", {})
        assert r["task_id"] == "task-x"
        assert r["status"] == "unknown"
        assert r["cost_usd"] == 0.0

    def test_with_results(self, server):
        meta = {
            "status": "completed",
            "subtasks": [{"id": "s1"}],
            "results": [{"subtask_id": "s1", "status": "completed", "duration_sec": 5,
                         "verify_ok": True, "retry_count": 1,
                         "change_stats": {"files_changed": 1, "insertions": 5, "deletions": 0}}]
        }
        r = server._build_completed("task-y", meta)
        assert r["status"] == "completed"
        assert r["duration_sec"] == 5
        assert r["results"][0]["changes"]["files"] == 1

    def test_timed_out_snapshot(self, server):
        meta = {"status": "running"}
        r = server._build_completed("task-z", meta, timed_out=True)
        assert r["status"] == "running"
        assert "timeout_hint" in r


# ── Agent_go subprocess ───────────────────────────────────────────

class TestSubprocess:
    def test_argv_build(self, server):
        argv = server._argv("run", "/repo", "task desc", "--parallel", "3")
        assert "-m" in argv
        assert "agent_go" in argv
        assert "--yes" in argv
        assert "--json" in argv
        assert argv[3] == "run"
        assert argv[4] == "/repo"
        assert argv[5] == "task desc"
        assert argv[7] == "3"

    def test_argv_resume(self, server):
        argv = server._argv("resume", "task-abc")
        assert argv[3] == "resume"
        assert argv[4] == "task-abc"

    def test_parse_jsonl_last(self, server):
        text = '{"a":1}\n{"b":2}\nnot-json\n{"c":3}\n'
        assert server._parse_jsonl_last(text) == {"c": 3}

    def test_parse_jsonl_last_empty(self, server):
        assert server._parse_jsonl_last("") is None
        assert server._parse_jsonl_last("not-json") is None
