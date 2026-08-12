"""Web 任务操作端点测试（M2：R5a/R6/R7/R9/R10/R16）。

task_runner 与 _run_cli 全部 mock（不触发真实 agent_go 子进程）；
任务目录在 tmp_path 下构造，clean 为真实删除（无副作用）。
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator

import pytest

import agent_go.profiles as prof
import agent_go.config as cfg
import agent_go.web_server as ws


@pytest.fixture
def ops_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    return adir


def _mk_task(adir: Path, task_id: str, status: str = "FAILED",
             mtime_age_sec: float = 0) -> Path:
    td = adir / task_id
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "task_id": task_id, "task": "测试任务", "status": status,
        "status_schema_version": 1, "repo": "/tmp/repo",
        "created": "2026-08-10T10:00:00", "subtasks": [], "results": [],
    }), encoding="utf-8")
    if mtime_age_sec:
        old = time.time() - mtime_age_sec
        import os
        os.utime(td, (old, old))
    return td


@pytest.fixture
def ops_server(ops_env, monkeypatch) -> Generator[str, None, None]:
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


def _delete(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="DELETE")
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


TID = "task-20260810-100000-111-aaaa"


class TestRun:
    def test_bad_repo_400(self, ops_server):
        code, d = _post(f"{ops_server}/api/tasks/run", {"repo": "/nope", "task": "x"})
        assert code == 400

    def test_relative_repo_400(self, ops_server):
        code, _ = _post(f"{ops_server}/api/tasks/run", {"repo": "rel/path", "task": "x"})
        assert code == 400

    def test_empty_task_400(self, ops_server):
        code, _ = _post(f"{ops_server}/api/tasks/run", {"repo": "/tmp", "task": " "})
        assert code == 400

    def test_run_ok(self, ops_server, ops_env, monkeypatch):
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda repo, task, parallel=1, goal=None, confirm_mode="auto": TID)
        code, d = _post(f"{ops_server}/api/tasks/run", {"repo": "/tmp", "task": "改进代码", "parallel": 2})
        assert code == 200
        assert d["task_id"] == TID
        assert d["confirm_mode"] == "auto"
        audits = _audit_lines(ops_env)
        assert any(a["op"] == "tasks.run" and a["ok"] for a in audits)

    def test_run_local_proxy_down_422(self, ops_server, ops_env, monkeypatch):
        """R5a/D3：local 模式代理不可达 → 启动即报错，不放行。"""
        (ops_env / ".current_profile").write_text("local", encoding="utf-8")
        def boom(url):
            raise prof.ProfileError("无法连接")
        monkeypatch.setattr(ws, "probe_local_models", boom)
        monkeypatch.setattr(ws.task_runner, "start_run",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应启动")))
        code, d = _post(f"{ops_server}/api/tasks/run", {"repo": "/tmp", "task": "x"})
        assert code == 422
        assert "代理不可达" in d["error"]


class TestResumeCancel:
    def test_resume_not_found(self, ops_server):
        code, _ = _post(f"{ops_server}/api/tasks/{TID}/resume", {})
        assert code == 404

    def test_resume_running_409(self, ops_server, ops_env):
        _mk_task(ops_env, TID, status="EXECUTING")
        code, d = _post(f"{ops_server}/api/tasks/{TID}/resume", {})
        assert code == 409
        assert "运行中" in d["error"]

    def test_resume_ok(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID, status="FAILED")
        monkeypatch.setattr(ws.task_runner, "start_resume",
                            lambda tid, parallel=1: tid)
        monkeypatch.setattr(ws.task_runner, "is_running", lambda k: False)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/resume", {"parallel": 2})
        assert code == 200
        assert d["status"] == "resumed"

    def test_cancel_no_handle_409(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID, status="EXECUTING")
        monkeypatch.setattr(ws.task_runner, "cancel", lambda k: False)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/cancel", {})
        assert code == 409
        assert "不受本 web 实例管理" in d["error"]

    def test_cancel_ok(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID, status="EXECUTING")
        monkeypatch.setattr(ws.task_runner, "cancel", lambda k: True)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/cancel", {})
        assert code == 200
        assert d["status"] == "cancelling"


class TestClean:
    def test_clean_old_needs_confirm(self, ops_server):
        code, _ = _post(f"{ops_server}/api/tasks/clean-old", {"days": 30})
        assert code == 400

    def test_clean_old_removes(self, ops_server, ops_env):
        _mk_task(ops_env, TID, mtime_age_sec=10 * 86400)
        code, d = _post(f"{ops_server}/api/tasks/clean-old", {"days": 7, "confirm": True})
        assert code == 200
        assert TID in d["removed"]
        assert not (ops_env / TID).exists()

    def test_clean_old_skips_recent(self, ops_server, ops_env):
        _mk_task(ops_env, TID)  # 刚修改
        code, d = _post(f"{ops_server}/api/tasks/clean-old", {"days": 7, "confirm": True})
        assert code == 200
        assert d["removed"] == []
        assert (ops_env / TID).exists()

    def test_delete_needs_confirm(self, ops_server, ops_env):
        _mk_task(ops_env, TID)
        code, _ = _delete(f"{ops_server}/api/tasks/{TID}", {})
        assert code == 400

    def test_delete_running_409(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID, status="EXECUTING")
        monkeypatch.setattr(ws.task_runner, "is_running", lambda k: True)
        code, _ = _delete(f"{ops_server}/api/tasks/{TID}", {"confirm": True})
        assert code == 409

    def test_delete_ok(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        monkeypatch.setattr(ws.task_runner, "is_running", lambda k: False)
        code, d = _delete(f"{ops_server}/api/tasks/{TID}", {"confirm": True})
        assert code == 200
        assert TID in d["removed"]
        assert not (ops_env / TID).exists()
        assert any(a["op"] == "tasks.delete" for a in _audit_lines(ops_env))


class TestReview:
    def test_review_decision_invalid_400(self, ops_server, ops_env):
        _mk_task(ops_env, TID)
        code, _ = _post(f"{ops_server}/api/tasks/{TID}/review/decision", {"decision": "maybe"})
        assert code == 400

    def test_review_decision_approve(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        calls = []
        def fake_cli(argv, timeout=180):
            calls.append(argv)
            return {"ok": True, "exit_code": 0, "stdout": "✅ 审查通过", "stderr": ""}
        monkeypatch.setattr(ws, "_run_cli", fake_cli)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/review/decision", {"decision": "approve"})
        assert code == 200
        assert "--approve" in calls[0]
        # D4：审批决策必写审计
        audits = [a for a in _audit_lines(ops_env) if a["op"] == "tasks.review.decision"]
        assert audits and audits[0]["params"]["decision"] == "approve"

    def test_review_trigger_shallow(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        monkeypatch.setattr(ws, "_run_cli",
                            lambda argv, timeout=180: {"ok": True, "exit_code": 0, "stdout": "报告", "stderr": ""})
        code, _ = _post(f"{ops_server}/api/tasks/{TID}/review", {})
        assert code == 200

    def test_review_trigger_deep_async(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        monkeypatch.setattr(ws.task_runner, "start_review_deep", lambda tid: f"review:{tid}")
        code, d = _post(f"{ops_server}/api/tasks/{TID}/review", {"deep": True})
        assert code == 200
        assert d["status"] == "review_started"

    def test_get_review_empty(self, ops_server, ops_env):
        _mk_task(ops_env, TID)
        d = _get(f"{ops_server}/api/tasks/{TID}/review")
        assert d["decision"] is None

    def test_get_review_decision(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        (td / "review.json").write_text(json.dumps({"decision": "approved"}), encoding="utf-8")
        d = _get(f"{ops_server}/api/tasks/{TID}/review")
        assert d["decision"] == "approved"


class TestMerge:
    def test_merge_preview_no_branch(self, ops_server, ops_env):
        _mk_task(ops_env, TID)
        d = _get(f"{ops_server}/api/tasks/{TID}/merge-preview")
        assert d["mergeable"] is False
        assert "error" in d

    def test_merge_ok(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        monkeypatch.setattr(ws, "_run_cli",
                            lambda argv, timeout=180: {"ok": True, "exit_code": 0, "stdout": "merged abc123", "stderr": ""})
        code, d = _post(f"{ops_server}/api/tasks/{TID}/merge", {"push": True})
        assert code == 200
        audits = [a for a in _audit_lines(ops_env) if a["op"] == "tasks.merge"]
        assert audits and audits[0]["params"]["push"] is True

    def test_merge_conflict_payload(self, ops_server, ops_env, monkeypatch):
        """D3：merge 失败时回显 conflicts（来自 mergeability 预检）。"""
        td = _mk_task(ops_env, TID)
        meta = json.loads((td / "meta.json").read_text())
        meta["repo"] = ""  # 无有效 repo → preview 给 error，conflicts 空
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        monkeypatch.setattr(ws, "_run_cli",
                            lambda argv, timeout=180: {"ok": False, "exit_code": 1, "stdout": "", "stderr": "conflict"})
        code, d = _post(f"{ops_server}/api/tasks/{TID}/merge", {})
        assert code == 422
        assert "conflicts" in d


class TestAuditToken:
    def test_audit_records_token_hash(self, ops_env, monkeypatch):
        """R16：token 模式下审计含 auth 哈希（非明文）。"""
        monkeypatch.setattr(ws.task_runner, "start_run", lambda *a, **k: TID)
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), ws.WebHandler)
        server.token = "sec"
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        host, port = server.server_address[:2]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/run",
                data=json.dumps({"repo": "/tmp", "task": "x"}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer sec"}, method="POST")
            with urllib.request.urlopen(req) as r:
                assert r.status == 200
        finally:
            server.shutdown()
            server.server_close()
        audits = _audit_lines(ops_env)
        assert audits and audits[0]["auth"] and audits[0]["auth"] != "sec"


class TestTaskRunner:
    """task_runner 子进程管理（mock Popen，不起真实进程）。"""

    def _fake_proc(self, lines=(), returncode=None):
        from unittest.mock import MagicMock
        proc = MagicMock()
        stdout = MagicMock()
        stdout.readline.side_effect = list(lines) + [""]
        proc.stdout = stdout
        proc.stderr = iter([])
        proc.poll.return_value = returncode
        proc.wait.return_value = returncode or 0
        proc.returncode = returncode or 0
        return proc

    def test_start_run_argv(self, monkeypatch):
        from agent_go.task_runner import TaskRunner
        runner = TaskRunner()
        spawned = []
        proc = self._fake_proc(lines=['{"task_id": "%s"}\n' % TID])
        monkeypatch.setattr(runner, "_spawn", lambda argv: (spawned.append(argv), proc)[1])
        tid = runner.start_run("/tmp/repo", "任务x", parallel=3, goal=True)
        assert tid == TID
        argv = spawned[0]
        # --json 是顶层 parser 参数，必须位于子命令之前（argparse 不接受子命令后的顶层 flag）
        assert argv.index("--json") < argv.index("run")
        assert "/tmp/repo" in argv and "任务x" in argv
        assert "--parallel" in argv and "3" in argv
        assert "--yes" in argv and "--goal" in argv

    def test_start_run_no_goal_flag(self, monkeypatch):
        from agent_go.task_runner import TaskRunner
        runner = TaskRunner()
        spawned = []
        proc = self._fake_proc(lines=['{"task_id": "%s"}\n' % TID])
        monkeypatch.setattr(runner, "_spawn", lambda argv: (spawned.append(argv), proc)[1])
        runner.start_run("/tmp/repo", "t", goal=False)
        assert "--no-goal" in spawned[0]

    def test_read_task_id_fallback_latest_dir(self, tmp_path, monkeypatch):
        from agent_go.task_runner import TaskRunner
        import agent_go.task_runner as tr
        monkeypatch.setattr(tr, "AGENT_GO_DIR", tmp_path)
        latest = tmp_path / TID
        latest.mkdir()
        runner = TaskRunner()
        proc = self._fake_proc(lines=[], returncode=1)
        proc.poll.return_value = 1  # 进程已退出 → 走 fallback
        tid = runner._read_task_id(proc, timeout=0.1)
        assert tid == TID

    def test_cancel_no_handle(self):
        from agent_go.task_runner import TaskRunner
        assert TaskRunner().cancel("ghost") is False

    def test_cancel_sends_sigint(self):
        import signal
        from unittest.mock import MagicMock
        from agent_go.task_runner import TaskRunner
        runner = TaskRunner()
        proc = MagicMock()
        proc.poll.return_value = None  # 运行中
        runner._procs[TID] = proc
        assert runner.cancel(TID) is True
        proc.send_signal.assert_called_once_with(signal.SIGINT)

    def test_is_running(self):
        from unittest.mock import MagicMock
        from agent_go.task_runner import TaskRunner
        runner = TaskRunner()
        proc = MagicMock()
        proc.poll.return_value = None
        runner._procs["k"] = proc
        assert runner.is_running("k") is True
        assert runner.is_running("ghost") is False
        proc.poll.return_value = 0
        assert runner.is_running("k") is False

    def test_running_keys(self):
        from unittest.mock import MagicMock
        from agent_go.task_runner import TaskRunner
        runner = TaskRunner()
        alive, dead = MagicMock(), MagicMock()
        alive.poll.return_value = None
        dead.poll.return_value = 0
        runner._procs.update({"a": alive, "b": dead})
        assert runner.running_keys() == ["a"]


# ── M3 端点（R11-R17 + R5b）────────────────────────────────

class TestPr:
    def test_pr_url_parsed(self, ops_server, ops_env, monkeypatch):
        _mk_task(ops_env, TID)
        monkeypatch.setattr(ws, "_run_cli",
                            lambda argv, timeout=180: {"ok": True, "exit_code": 0,
                                                       "stdout": "PR created: https://github.com/x/y/pull/42\n", "stderr": ""})
        code, d = _post(f"{ops_server}/api/tasks/{TID}/pr", {"push": True})
        assert code == 200
        assert d["pr_url"] == "https://github.com/x/y/pull/42"

    def test_pr_offline_default(self, ops_server, ops_env, monkeypatch):
        """安全默认：不带 push → --offline（只生成 PR.md）。"""
        _mk_task(ops_env, TID)
        calls = []
        monkeypatch.setattr(ws, "_run_cli",
                            lambda argv, timeout=180: (calls.append(argv),
                                                       {"ok": True, "exit_code": 0, "stdout": "PR.md 已生成", "stderr": ""})[1])
        code, _ = _post(f"{ops_server}/api/tasks/{TID}/pr", {})
        assert code == 200
        assert "--offline" in calls[0] and "--push" not in calls[0]


class TestDeviation:
    def test_deviation_aggregation(self, ops_server, ops_env):
        from agent_go.deviation import DeviationEvent, DEVIATION_FILENAME
        from dataclasses import asdict
        td = _mk_task(ops_env, TID)
        events = [
            DeviationEvent(task_id=TID, subtask_id="s1", deviation_type="spec_deviation",
                           root_cause_category="vague_spec", summary="规格模糊"),
            DeviationEvent(task_id=TID, subtask_id="s2", deviation_type="spec_deviation",
                           root_cause_category="missing_context", summary="缺上下文"),
            DeviationEvent(task_id=TID, subtask_id="s3", deviation_type="acceptance_gap",
                           root_cause_category="vague_spec", summary="验收缺口"),
        ]
        (td / DEVIATION_FILENAME).write_text(
            "\n".join(json.dumps(asdict(e)) for e in events), encoding="utf-8")
        d = _get(f"{ops_server}/api/tasks/{TID}/deviation")
        assert d["total"] == 3
        assert d["by_type"]["spec_deviation"] == 2
        assert d["by_root_cause"]["vague_spec"] == 2
        assert len(d["events"]) == 3

    def test_deviation_not_found(self, ops_server):
        try:
            _get(f"{ops_server}/api/tasks/task-20990101-000000/deviation")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestLocalTco:
    def test_tco_aggregation(self, ops_server, ops_env, monkeypatch):
        td = _mk_task(ops_env, TID)
        records = [
            {"is_local": True, "actual_model": "Qwen3.6", "prompt_tokens": 100, "completion_tokens": 50},
            {"is_local": True, "actual_model": "Qwen3.6", "prompt_tokens": 200, "completion_tokens": 50},
            {"is_local": True, "actual_model": "OtherLocal", "prompt_tokens": 10, "completion_tokens": 5},
            {"is_local": False, "actual_model": "deepseek-v4", "prompt_tokens": 999, "completion_tokens": 1},
        ]
        (td / "metering.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8")
        monkeypatch.setattr("agent_go.metrics._local_tco_loaded", True)
        monkeypatch.setattr("agent_go.metrics._local_tco_cost", {"Qwen3.6": 0.001})
        d = _get(f"{ops_server}/api/local-tco")
        assert d["estimated"] is True
        assert d["total_calls"] == 3
        by = {r["model"]: r for r in d["by_model"]}
        assert by["Qwen3.6"]["calls"] == 2
        assert by["Qwen3.6"]["tco_usd"] == 0.002
        assert by["OtherLocal"]["configured"] is False
        assert "OtherLocal" in d["unconfigured_models"]


class TestConfigPut:
    def test_put_whitelist_field(self, ops_server, ops_env):
        code, d = _put(f"{ops_server}/api/config",
                       {"field": "local_model_cost", "value": {"Qwen3.6": 0.0007}})
        assert code == 200
        data = json.loads((ops_env / "config.json").read_text())
        assert data["local_model_cost"] == {"Qwen3.6": 0.0007}

    def test_put_nested_field(self, ops_server, ops_env):
        code, _ = _put(f"{ops_server}/api/config",
                       {"field": "plan_api.worker_base_url", "value": "http://localhost:4000"})
        assert code == 200
        data = json.loads((ops_env / "config.json").read_text())
        assert data["plan_api"]["worker_base_url"] == "http://localhost:4000"

    def test_put_non_whitelist_422(self, ops_server):
        code, d = _put(f"{ops_server}/api/config", {"field": "api_key", "value": "x"})
        assert code == 422
        assert "白名单" in d["error"]

    def test_put_wrong_type_422(self, ops_server):
        code, d = _put(f"{ops_server}/api/config", {"field": "local_models", "value": "not-a-list"})
        assert code == 422
        assert "类型" in d["error"]

    def test_put_writes_to_active_profile(self, ops_server, ops_env):
        """profile 激活时写入 profile 文件而非 config.json（R14 语义）。"""
        pdir = ops_env / "profiles"
        pdir.mkdir()
        (pdir / "local.json").write_text("{}", encoding="utf-8")
        (ops_env / ".current_profile").write_text("local", encoding="utf-8")
        code, d = _put(f"{ops_server}/api/config", {"field": "goal", "value": {"enabled": True}})
        assert code == 200
        assert "local.json" in d["saved_to"]
        data = json.loads((pdir / "local.json").read_text())
        assert data["goal"] == {"enabled": True}


class TestConfigDiff:
    def test_diff_fields(self, ops_server, ops_env):
        pdir = ops_env / "profiles"
        pdir.mkdir()
        (pdir / "local.json").write_text(json.dumps(
            {"worker_models": {"easy": "claude-haiku-4-5", "medium": "m", "hard": "h"},
             "goal": {"enabled": True}}), encoding="utf-8")
        d = _get(f"{ops_server}/api/config/diff?name=local")
        assert d["diff_count"] > 0
        fields = [x["field"] for x in d["diffs"]]
        assert any(f.startswith("goal") for f in fields)

    def test_diff_not_found(self, ops_server):
        try:
            _get(f"{ops_server}/api/config/diff?name=ghost")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestWorktrees:
    def test_worktrees_list(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        meta = json.loads((td / "meta.json").read_text())
        meta["subtasks"] = [{"id": "sub-1", "title": "t"}, {"id": "sub-2", "title": "t2"}]
        (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        # sub-1: 保留的 worktree；sub-2: completed 且无保留 → 不出现
        wt = td / "sub-1" / "work"
        (wt / ".git").mkdir(parents=True)
        (td / "sub-1" / ".preserved").write_text(json.dumps({"branch": "agent_go/t/sub-1"}), encoding="utf-8")
        (td / "sub-1" / "result.json").write_text(json.dumps({"status": "failed", "failure_reason": "verify failed"}), encoding="utf-8")
        (td / "sub-2" / "result.json").parent.mkdir(parents=True, exist_ok=True)
        (td / "sub-2" / "result.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        d = _get(f"{ops_server}/api/tasks/{TID}/worktrees")
        assert len(d["worktrees"]) == 1
        w = d["worktrees"][0]
        assert w["subtask_id"] == "sub-1"
        assert w["preserved"] is True
        assert w["branch"] == "agent_go/t/sub-1"


def _put(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWebConfirm:
    """R5b：web_confirm 文件协议 + confirm 端点 + cli 通道分发。"""

    def test_web_confirm_y(self, ops_env):
        from agent_go.web_confirm import web_confirm, DECISION_FILE, PENDING_FILE
        td = _mk_task(ops_env, TID)
        # 后台线程模拟 web 决策
        def decide():
            time.sleep(0.3)
            (td / DECISION_FILE).write_text(json.dumps({"stage": "plan", "decision": "Y"}), encoding="utf-8")
        threading.Thread(target=decide, daemon=True).start()
        import logging
        decision = web_confirm("plan", {"title": "t"}, td, logging.getLogger("t"), timeout=5)
        assert decision == "Y"
        assert not (td / PENDING_FILE).exists()
        assert not (td / DECISION_FILE).exists()

    def test_web_confirm_timeout_returns_n(self, ops_env):
        from agent_go.web_confirm import web_confirm, PENDING_FILE
        td = _mk_task(ops_env, TID)
        import logging
        decision = web_confirm("plan", {}, td, logging.getLogger("t"), timeout=0.5)
        assert decision == "N"
        assert not (td / PENDING_FILE).exists()

    def test_confirm_endpoint_no_pending_409(self, ops_server, ops_env):
        _mk_task(ops_env, TID)
        code, d = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "plan", "decision": "Y"})
        assert code == 409
        assert "无待确认项" in d["error"]

    def test_confirm_endpoint_stage_mismatch_409(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "subtasks", "payload": {}, "ts": "x", "timeout_sec": 1800}), encoding="utf-8")
        code, d = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "plan", "decision": "Y"})
        assert code == 409
        assert "stage 不匹配" in d["error"]

    def test_confirm_endpoint_invalid_decision_400(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "subtasks", "payload": {}, "ts": "x", "timeout_sec": 1800}), encoding="utf-8")
        code, _ = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "subtasks", "decision": "R"})
        assert code == 400  # subtasks 只允许 Y/N

    def test_confirm_endpoint_ok(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "plan", "payload": {}, "ts": "x", "timeout_sec": 1800}), encoding="utf-8")
        code, d = _post(f"{ops_server}/api/tasks/{TID}/confirm", {"stage": "plan", "decision": "R"})
        assert code == 200
        assert d["decision"] == "R"
        dec = json.loads((td / "confirmation_decision.json").read_text())
        assert dec["decision"] == "R"
        assert any(a["op"] == "tasks.confirm" for a in _audit_lines(ops_env))

    def test_pending_get(self, ops_server, ops_env):
        td = _mk_task(ops_env, TID)
        d = _get(f"{ops_server}/api/tasks/{TID}/pending-confirmation")
        assert d["pending"] is None
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "plan", "payload": {"title": "x"}, "ts": "t", "timeout_sec": 1800}), encoding="utf-8")
        d = _get(f"{ops_server}/api/tasks/{TID}/pending-confirmation")
        assert d["pending"]["stage"] == "plan"

    def test_cli_channel_dispatch(self, ops_env):
        """cli._confirm_plan_channel：web_confirm_plan 标志 → web 协议（Y）。"""
        import logging
        from agent_go.cli import _confirm_plan_channel
        td = _mk_task(ops_env, TID)
        config = {"behavior": {"web_confirm_plan": True}}
        plan = {"title": "计划"}
        def decide():
            time.sleep(0.2)
            (td / "confirmation_decision.json").write_text(
                json.dumps({"stage": "plan", "decision": "Y"}), encoding="utf-8")
        threading.Thread(target=decide, daemon=True).start()
        result, docs = _confirm_plan_channel(plan, config, None, logging.getLogger("t"),
                                             iteration=1, task="t", plan_dir=td)
        assert result == plan

    def test_cli_channel_dispatch_r(self, ops_env):
        from agent_go.cli import _confirm_plan_channel
        import logging
        td = _mk_task(ops_env, TID)
        config = {"behavior": {"web_confirm_plan": True}}
        def decide():
            time.sleep(0.2)
            (td / "confirmation_decision.json").write_text(
                json.dumps({"stage": "plan", "decision": "R"}), encoding="utf-8")
        threading.Thread(target=decide, daemon=True).start()
        result, _ = _confirm_plan_channel({"title": "p"}, config, None, logging.getLogger("t"),
                                          iteration=1, task="t", plan_dir=td)
        assert result is None  # R → None（外层重生成）
