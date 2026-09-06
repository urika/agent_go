"""web 🧪 Bench 页测试：api_bench_review / api_task_file / api_delivery_diff
数据层 + HTTP 路由。

数据构造方式与 test_web_troubleshoot.py 一致：monkeypatch AGENT_GO_DIR 指向临时
目录；eval_suite 目录经 web_server._resolve_workspace_file patch 到 tmp_path。
"""
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator

import pytest

AGENT_T1 = "task-20260905-100000-111-abcd"
AGENT_T2 = "task-20260905-100000-222-ef01"
BATCH = "results_b8_test_20260905.jsonl"


def _rec(task_id, repeat, backend, model, binary, task_dir, **kw):
    r = {
        "task_id": task_id, "suite": "golden", "repeat": repeat,
        "worker_backend": backend, "model": model,
        "binary_pass": binary, "semantic_pass": binary,
        "kill_reason": kw.get("kill_reason"), "elapsed_sec": 12.5,
        "total_cost_usd": 0.05, "accepted_delivery": binary,
        "difficulty": "easy", "task_dir": task_dir,
    }
    r.update(kw)
    return r


@pytest.fixture
def bench_env(tmp_path: Path, monkeypatch) -> Generator[dict, None, None]:
    """造 eval_suite/results_*.jsonl + 题目 yaml + 两个 agent_go 任务目录。"""
    import agent_go.web_server as ws

    agent_go_dir = tmp_path / "agent_go_data"
    agent_go_dir.mkdir(parents=True)
    monkeypatch.setattr(ws, "AGENT_GO_DIR", agent_go_dir)
    monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", agent_go_dir)

    suite_dir = tmp_path / "eval_suite"
    suite_dir.mkdir()
    monkeypatch.setattr(ws, "_resolve_workspace_file", lambda name: tmp_path / name)

    # T1：完整任务目录（meta 带交付字段 + 产物文件）
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    td1 = agent_go_dir / AGENT_T1
    (td1 / "sub-1").mkdir(parents=True)
    (td1 / "meta.json").write_text(json.dumps({
        "task": "bench 测试任务", "repo": str(repo_dir),
        "base_commit": "abc123def456", "delivery_branch": "agent_go/delivery/x",
        "subtasks": [{"id": "sub-1", "title": "s1"}],
    }), encoding="utf-8")
    (td1 / "sub-1" / "TASK.md").write_text("# 任务说明\n做点什么\n", encoding="utf-8")
    (td1 / "sub-1" / "context.md").write_text("上下文\n", encoding="utf-8")
    (td1 / "execution.log").write_text("log line 1\n", encoding="utf-8")

    # T2：meta 缺交付字段（delivery-diff 降级分支）
    td2 = agent_go_dir / AGENT_T2
    td2.mkdir()
    (td2 / "meta.json").write_text(json.dumps({"task": "无交付字段"}), encoding="utf-8")

    # 结果文件：两臂（dsh/glm ×2 repeats + claude/opus ×1），两道题
    records = [
        _rec("add-format-helper", 2, "dsh", "glm-4.6", False, str(td1),
             kill_reason="timeout"),
        _rec("add-format-helper", 1, "dsh", "glm-4.6", True, str(td1)),
        _rec("add-format-helper", 1, "claude", "opus", True,
             str(agent_go_dir / "task-20260905-100000-999-ffff")),  # 目录不存在 → 已清理
        _rec("fix-typo", 1, "dsh", "glm-4.6", False, str(td2)),
    ]
    (suite_dir / BATCH).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # 题目原文：golden → golden_tasks/tasks/；legacy 题只存在于 eval_suite/tasks/
    golden_tasks = suite_dir / "golden_tasks" / "tasks"
    golden_tasks.mkdir(parents=True)
    (golden_tasks / "01-add-format-helper.yaml").write_text(
        "id: add-format-helper\ndifficulty: easy\nrepo: demo-repo\n"
        "task: |\n  实现一个格式化 helper\n  第二行\n"
        "verification:\n  - pytest tests/ -q\n", encoding="utf-8")
    legacy = suite_dir / "tasks"
    legacy.mkdir()
    (legacy / "99-fix-typo.yaml").write_text(
        "id: fix-typo\ndifficulty: trivial\ntask: 修个错别字\n"
        "verification: []\n", encoding="utf-8")

    yield {"dir": agent_go_dir, "suite": suite_dir, "repo": repo_dir}


@pytest.fixture
def base_url(bench_env) -> Generator[str, None, None]:
    """启动真实短生命周期 HTTP 服务（同 test_web_troubleshoot.base_url）。"""
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
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_git(monkeypatch, stat: str = "", diff: str = "", rc: int = 0):
    """mock web_data 内的 subprocess.run：--stat 与全文 diff 分发固定输出。"""
    from agent_go import web_data

    def fake_run(cmd, cwd=None, **kwargs):
        assert cmd[0] == "git"
        err = "boom" if rc else ""
        if "--stat" in cmd:
            return _FakeProc(stat, rc, err)
        return _FakeProc(diff, rc, err)

    monkeypatch.setattr(web_data.subprocess, "run", fake_run)


class TestApiBenchReview:
    """数据层：api_bench_review / api_bench_result_files。"""

    def test_result_files_listed(self, bench_env):
        from agent_go.web_data import api_bench_result_files
        d = api_bench_result_files()
        assert d["count"] == 1
        assert d["batches"][0]["name"] == BATCH

    def test_invalid_batch_name_rejected(self, bench_env):
        from agent_go.web_data import api_bench_review
        assert api_bench_review("../evil.jsonl") is None
        assert api_bench_review("results_../../etc/passwd.jsonl") is None
        assert api_bench_review("evil.jsonl") is None
        assert api_bench_review("") is None

    def test_missing_batch_returns_none(self, bench_env):
        from agent_go.web_data import api_bench_review
        assert api_bench_review("results_nope_20260101.jsonl") is None

    def test_parse_ok(self, bench_env):
        from agent_go.web_data import api_bench_review
        d = api_bench_review(BATCH)
        assert d is not None
        assert d["batch"] == BATCH
        assert d["yaml_available"] is True
        assert d["total_runs"] == 4
        # 臂汇总：两臂分组
        arms = {a["label"]: a for a in d["arms"]}
        assert set(arms) == {"dsh / glm-4.6", "claude / opus"}
        assert arms["dsh / glm-4.6"]["binary_pass"] == 1
        assert arms["dsh / glm-4.6"]["total"] == 3
        assert arms["dsh / glm-4.6"]["cost"] == pytest.approx(0.15)
        # 任务按出现顺序，题目原文来自 yaml（suite 目录 + fallback 目录）
        assert [t["task_id"] for t in d["tasks"]] == ["add-format-helper", "fix-typo"]
        t0 = d["tasks"][0]
        assert "格式化 helper" in t0["task_text"]
        assert t0["verification"] == ["pytest tests/ -q"]
        assert t0["repo"] == "demo-repo"
        assert d["tasks"][1]["task_text"] == "修个错别字"
        # run 按臂+repeat 排序：claude/opus rep1 → dsh rep1 → dsh rep2
        order = [(r["arm"], r["repeat"]) for r in t0["runs"]]
        assert order == [("claude / opus", 1), ("dsh / glm-4.6", 1), ("dsh / glm-4.6", 2)]
        r0 = t0["runs"][1]
        assert r0["agent_task_id"] == AGENT_T1
        assert r0["task_dir_exists"] is True
        assert r0["kill_reason"] == ""
        # 目录不存在的 run 标记 task_dir_exists=False（前端灰显「已清理」）
        assert t0["runs"][0]["task_dir_exists"] is False

    def test_yaml_missing_degrades(self, bench_env, monkeypatch):
        """无 PyYAML（runtime 环境）：task_text 空 + yaml_available=False，其余照常。"""
        from agent_go.web_data import api_bench_review
        monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml → ImportError
        d = api_bench_review(BATCH)
        assert d is not None
        assert d["yaml_available"] is False
        assert d["tasks"][0]["task_text"] == ""
        assert d["tasks"][0]["verification"] == []
        # difficulty 从 results 记录兜底
        assert d["tasks"][0]["difficulty"] == "easy"
        assert len(d["arms"]) == 2


class TestApiTaskFile:
    """数据层：api_task_file（白名单 / 截断 / 不存在）。"""

    def test_task_md_ok(self, bench_env):
        from agent_go.web_data import api_task_file
        d = api_task_file(AGENT_T1, "sub-1", "TASK.md")
        assert d["available"] is True
        assert "任务说明" in d["content"]
        assert d["truncated"] is False
        assert d["size"] > 0

    def test_execution_log_root(self, bench_env):
        """execution.log 在任务根目录，sub_id 传 "-"。"""
        from agent_go.web_data import api_task_file
        d = api_task_file(AGENT_T1, "-", "execution.log")
        assert d["available"] is True
        assert "log line 1" in d["content"]

    def test_whitelist_reject(self, bench_env):
        from agent_go.web_data import api_task_file
        assert api_task_file(AGENT_T1, "sub-1", "meta.json") is None
        assert api_task_file(AGENT_T1, "sub-1", "../../etc/passwd") is None

    def test_invalid_ids(self, bench_env):
        from agent_go.web_data import api_task_file
        assert api_task_file("../../etc", "sub-1", "TASK.md") is None
        assert api_task_file(AGENT_T1, "../x", "TASK.md") is None
        assert api_task_file("task-20260905-100000-999-ffff", "sub-1", "TASK.md") is None

    def test_missing_file_available_false(self, bench_env):
        from agent_go.web_data import api_task_file
        d = api_task_file(AGENT_T2, "sub-1", "TASK.md")
        assert d is not None
        assert d["available"] is False

    def test_truncation(self, bench_env, monkeypatch):
        from agent_go import web_data
        monkeypatch.setattr(web_data, "MAX_TASK_FILE_CHARS", 5)
        d = web_data.api_task_file(AGENT_T1, "sub-1", "TASK.md")
        assert d["available"] is True
        assert d["truncated"] is True
        assert len(d["content"]) == 5


class TestApiDeliveryDiff:
    """数据层：api_delivery_diff（mock subprocess.run，不跑真 git）。"""

    def test_ok(self, bench_env, monkeypatch):
        from agent_go.web_data import api_delivery_diff
        _patch_git(monkeypatch, stat=" a.py | 2 +-\n",
                   diff="diff --git a/a.py b/a.py\n+hello\n")
        d = api_delivery_diff(AGENT_T1)
        assert d["available"] is True
        assert "a.py" in d["stat"]
        assert "+hello" in d["diff"]
        assert d["delivery_branch"] == "agent_go/delivery/x"
        assert d["diff_truncated"] is False

    def test_diff_truncated(self, bench_env, monkeypatch):
        from agent_go import web_data
        big = "x" * (web_data.MAX_DIFF_CHARS + 100)
        _patch_git(monkeypatch, stat="s", diff=big)
        d = web_data.api_delivery_diff(AGENT_T1)
        assert d["available"] is True
        assert d["diff_truncated"] is True
        assert len(d["diff"]) == web_data.MAX_DIFF_CHARS

    def test_missing_meta_fields(self, bench_env):
        from agent_go.web_data import api_delivery_diff
        d = api_delivery_diff(AGENT_T2)
        assert d is not None
        assert d["available"] is False
        assert "缺少" in d["reason"]

    def test_repo_dir_missing(self, bench_env):
        from agent_go.web_data import api_delivery_diff
        (Path(bench_env["dir"]) / AGENT_T1 / "meta.json").write_text(json.dumps({
            "repo": str(bench_env["repo"] / "nope"),
            "base_commit": "abc", "delivery_branch": "br",
        }), encoding="utf-8")
        d = api_delivery_diff(AGENT_T1)
        assert d["available"] is False
        assert "不存在" in d["reason"]

    def test_git_failure(self, bench_env, monkeypatch):
        from agent_go.web_data import api_delivery_diff
        _patch_git(monkeypatch, rc=128)
        d = api_delivery_diff(AGENT_T1)
        assert d["available"] is False
        assert "失败" in d["reason"]

    def test_git_timeout(self, bench_env, monkeypatch):
        from agent_go import web_data

        def fake_run(cmd, cwd=None, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        monkeypatch.setattr(web_data.subprocess, "run", fake_run)
        d = web_data.api_delivery_diff(AGENT_T1)
        assert d["available"] is False
        assert "失败" in d["reason"]  # 超时归一为 rc=-1 → 「git diff --stat 失败: git 命令超时」
        assert "超时" in d["reason"]

    def test_task_not_found_returns_none(self, bench_env):
        from agent_go.web_data import api_delivery_diff
        assert api_delivery_diff("task-20260905-100000-999-ffff") is None
        assert api_delivery_diff("../../etc") is None


class TestBenchRoutes:
    """HTTP 路由层：200/404/400 语义。"""

    def test_bench_batches_200(self, base_url):
        status, d = _get(f"{base_url}/api/bench/batches")
        assert status == 200
        assert d["batches"][0]["name"] == BATCH

    def test_review_200(self, base_url):
        status, d = _get(f"{base_url}/api/bench/review?batch={BATCH}")
        assert status == 200
        assert d["batch"] == BATCH
        assert len(d["tasks"]) == 2

    def test_review_404_unknown_batch(self, base_url):
        assert _get_status(
            f"{base_url}/api/bench/review?batch=results_nope_20260101.jsonl") == 404

    def test_review_400_missing_param(self, base_url):
        assert _get_status(f"{base_url}/api/bench/review") == 400

    def test_review_traversal_rejected_404(self, base_url):
        assert _get_status(f"{base_url}/api/bench/review?batch=..%2F..%2Fevil.jsonl") == 404

    def test_delivery_diff_200(self, base_url, monkeypatch):
        _patch_git(monkeypatch, stat=" a.py | 1 +\n", diff="d")
        status, d = _get(f"{base_url}/api/tasks/{AGENT_T1}/delivery-diff")
        assert status == 200
        assert d["available"] is True

    def test_delivery_diff_200_unavailable(self, base_url):
        """meta 缺交付字段不是 404。"""
        status, d = _get(f"{base_url}/api/tasks/{AGENT_T2}/delivery-diff")
        assert status == 200
        assert d["available"] is False

    def test_delivery_diff_404_unknown_task(self, base_url):
        assert _get_status(
            f"{base_url}/api/tasks/task-20260905-100000-999-ffff/delivery-diff") == 404

    def test_file_200(self, base_url):
        status, d = _get(f"{base_url}/api/tasks/{AGENT_T1}/sub-1/file?name=TASK.md")
        assert status == 200
        assert d["available"] is True

    def test_file_200_log(self, base_url):
        status, d = _get(f"{base_url}/api/tasks/{AGENT_T1}/-/file?name=execution.log")
        assert status == 200
        assert d["available"] is True

    def test_file_404_whitelist(self, base_url):
        assert _get_status(f"{base_url}/api/tasks/{AGENT_T1}/sub-1/file?name=meta.json") == 404

    def test_file_400_missing_name(self, base_url):
        assert _get_status(f"{base_url}/api/tasks/{AGENT_T1}/sub-1/file") == 400
