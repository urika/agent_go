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
        "status": "DELIVERY_READY",
        "status_schema_version": 1,
        "repo": "/tmp/repo-a",
        "created": "2026-08-02T10:00:00",
        "subtasks": [
            {"id": "sub-1", "title": "子任务1", "difficulty": "easy",
             "agent_type": "developer", "depends_on": [], "skills": ["test"]},
            {"id": "sub-2", "title": "子任务2", "difficulty": "hard",
             "agent_type": "architect", "depends_on": ["sub-1"], "skills": [],
             "description": "描述2", "verification": ["pytest -q"],
             "files_hint": ["a.py"], "risks": ["风险"]},
        ],
        # 注意：results 故意按完成顺序（sub-2 先完成）存放，且带 subtask_id ——
        # 复现运行中任务的真实 meta.json 形态，验证 API 按 subtask_id 匹配
        # 而非按下标配对（下标配对会把 sub-1 显示成 failed）
        "results": [
            {"subtask_id": "sub-2", "status": "failed", "duration_sec": 30.0,
             "retry_count": 2, "verify_ok": False, "exit_code": 1,
             "summary": "失败", "failure_reason": "测试未通过",
             "worktree": "/tmp/wt/sub-2",
             "verification_results": [{"command": "pytest", "type": "shell",
                                       "passed": False, "duration_sec": 3.0}]},
            {"subtask_id": "sub-1", "status": "completed", "duration_sec": 10.5,
             "retry_count": 0, "verify_ok": True, "exit_code": 0,
             "summary": "完成", "agent_type_source": "llm",
             "worktree": "/tmp/wt/sub-1",
             "verification_results": [{"command": "pytest", "type": "shell",
                                       "passed": True, "duration_sec": 2.0}],
             "change_stats": {"files_changed": 1}},
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
        "task": "测试任务 B", "status": "EXECUTING", "status_schema_version": 1,
        "repo": "/tmp/repo-b",
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
        assert statuses["task-20260802-100000-111-aaaa"] == "DELIVERY_READY"
        assert statuses["task-20260802-110000-222-bbbb"] == "EXECUTING"

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
        assert d["status"] == "DELIVERY_READY"
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

    def test_results_matched_by_subtask_id_not_index(self, base_url, mock_tasks):
        """results 乱序（完成顺序 ≠ 子任务顺序）时仍按 subtask_id 配对。

        fixture 中 results[0] 是 sub-2(failed)、results[1] 是 sub-1(completed)，
        下标配对会把 sub-1 错标为 failed。
        """
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}")
        by_id = {s["id"]: s for s in d["subtasks"]}
        assert by_id["sub-1"]["status"] == "completed"
        assert by_id["sub-1"]["verify_ok"] is True
        assert by_id["sub-2"]["status"] == "failed"
        assert by_id["sub-2"]["retry_count"] == 2


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

    def test_log_sub1_not_confused_with_sub10(self, base_url, mock_tasks):
        """sub-1 不能误命中 sub-10 的日志行（子串匹配回归）。"""
        td = mock_tasks["dir"] / mock_tasks["task1"]
        (td / "execution.log").write_text(
            "[subtask] sub-10 start\nsub-10 exclusive line\n"
            "[subtask] sub-1 start\nsub-1 own line\n",
            encoding="utf-8")
        _, d = _get(f"{base_url}/api/tasks/{mock_tasks['task1']}/sub-1/log")
        texts = [ln["text"] for ln in d["lines"]]
        assert any("sub-1 own line" in t for t in texts)
        assert not any("sub-10 exclusive line" in t for t in texts)


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
            # query token（EventSource 无法自定义请求头的场景）→ 200
            with urllib.request.urlopen(f"{base}/api/tasks?token=sec") as r:
                assert r.status == 200
            # 错误 query token → 401
            try:
                urllib.request.urlopen(f"{base}/api/tasks?token=bad")
                assert False, "should 401"
            except urllib.error.HTTPError as e:
                assert e.code == 401
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


# ═══════════════════════════════════════════════════════════════
# 路径穿越防护（P0-1）
# ═══════════════════════════════════════════════════════════════

class TestPathTraversal:
    """task_id / sub_id 严格校验，防路径穿越。"""

    @pytest.mark.parametrize("tid", [
        "../../etc/passwd", "..%2F..%2Fetc", "task-../../../etc",
        "task-20260802-100000-111-aaaa/../../etc", "task-/etc/passwd",
        "", "task-", "task-2026", "not-a-task",
    ])
    def test_invalid_task_id_rejected(self, tid):
        import agent_go.web_server as ws
        assert ws._valid_task_id(tid) is False
        assert ws._task_dir(tid) is None
        assert ws.api_task(tid) is None
        assert ws.api_metering(tid) is None
        assert ws.api_plan(tid) is None
        assert ws.api_replay(tid) is None
        assert ws.api_assessment(tid) is None

    @pytest.mark.parametrize("tid", [
        "task-20260802-100000-111-aaaa",  # 新格式
        "task-20260515-103800",            # 旧格式
    ])
    def test_valid_task_id_accepted(self, tid):
        import agent_go.web_server as ws
        assert ws._valid_task_id(tid) is True

    @pytest.mark.parametrize("sid", [
        "sub/../../etc", "sub-1/../other", "../etc", "sub 1", "sub\\x00",
    ])
    def test_invalid_sub_id_rejected(self, sid):
        import agent_go.web_server as ws
        assert ws._valid_sub_id(sid) is False

    def test_valid_sub_id_accepted(self):
        import agent_go.web_server as ws
        for s in ["sub-1", "sub_2", "subA", "1"]:
            assert ws._valid_sub_id(s) is True


# ═══════════════════════════════════════════════════════════════
# 全局视图（P0-2）
# ═══════════════════════════════════════════════════════════════

class TestOverview:
    """总览大盘：KPI + 7 天成本趋势。"""

    def test_overview_kpi(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_overview()
        assert "kpi" in d
        # mock_tasks 造了 2 个任务（1 DELIVERY_READY + 1 EXECUTING）
        assert d["kpi"]["total"] == 2
        assert d["kpi"]["delivered"] == 1
        assert d["kpi"]["in_progress"] == 1
        assert d["kpi"]["today_cost"] >= 0  # 不崩溃即可（ts 可能不含今日）

    def test_overview_cost_trend_7d(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_overview()
        trend = d["cost_trend_7d"]
        assert len(trend) == 7
        # 每天都有 date + cost 字段
        for day in trend:
            assert "date" in day and "cost" in day
            assert day["cost"] >= 0


class TestCost:
    """全局成本分析。"""

    def test_cost_aggregation(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_cost()
        # mock_tasks 的 task1 metering 有 0.01 + 0.02 = 0.03
        assert d["total_cost"] >= 0.03
        assert len(d["by_model"]) >= 1
        assert len(d["by_role"]) >= 1
        # by_model 至少有 m1 或 deepseek-v4-pro
        model_names = [m["name"] for m in d["by_model"]]
        assert "m1" in model_names or "deepseek-v4-pro" in model_names

    def test_top_tasks_sorted_desc(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_cost()
        tops = d["top_tasks"]
        if len(tops) >= 2:
            assert tops[0]["cost"] >= tops[1]["cost"]
        assert all("task_id" in t and "cost" in t for t in tops)

    def test_by_model_has_pct(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_cost()
        for m in d["by_model"]:
            assert "pct" in m and 0 <= m["pct"] <= 100


class TestModels:
    """模型生产力对比。"""

    def test_production_aggregation(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_models()
        # mock_tasks 的 worker 调用用 m1
        prod = d["production"]
        assert isinstance(prod, list)
        # 应该至少有 m1
        models = [p["model"] for p in prod]
        assert "m1" in models
        m1 = next(p for p in prod if p["model"] == "m1")
        assert m1["calls"] >= 1
        assert m1["cost"] > 0
        assert m1["task_count"] >= 1

    def test_bench_may_be_empty(self, mock_tasks):
        """bench 数据可选（无 results.jsonl 时不崩溃）。"""
        import agent_go.web_server as ws
        d = ws.api_models()
        assert "bench" in d
        assert isinstance(d["bench"], list)


# ═══════════════════════════════════════════════════════════════
# 数据对象黑洞（P1）
# ═══════════════════════════════════════════════════════════════

class TestAssessment:
    """假阳性评估事件。"""

    def test_assessment_with_data(self, mock_tasks):
        import agent_go.web_server as ws
        # 造 assessment.jsonl
        td = mock_tasks["dir"] / mock_tasks["task1"]
        (td / "assessment.jsonl").write_text("\n".join([
            json.dumps({"passed": True, "confidence": 0.9,
                        "evaluator_model": "gpt-5", "reason": "正确"}),
            json.dumps({"passed": False, "confidence": 0.3,
                        "evaluator_model": "gpt-5", "reason": "错误"}),
        ]) + "\n", encoding="utf-8")
        d = ws.api_assessment(mock_tasks["task1"])
        assert d["total"] == 2
        assert d["passed"] == 1
        assert d["failed"] == 1
        assert d["false_positive_rate"] == 0.5
        assert d["by_evaluator_model"]["gpt-5"] == 2

    def test_assessment_empty(self, mock_tasks):
        """任务无 assessment.jsonl 时返回空聚合（不 None）。"""
        import agent_go.web_server as ws
        d = ws.api_assessment(mock_tasks["task2"])
        assert d["total"] == 0
        assert d["false_positive_rate"] == 0

    def test_assessment_invalid_task_id(self, mock_tasks):
        import agent_go.web_server as ws
        assert ws.api_assessment("../../etc") is None


class TestCrossJudge:
    """交叉评判矩阵。"""

    def test_cross_judge_with_data(self, mock_tasks, monkeypatch):
        import agent_go.web_server as ws
        # 造 cross_judge_scores.jsonl（在 cwd 下）
        scores_file = mock_tasks["dir"] / "cross_judge_scores.jsonl"
        monkeypatch.setattr(ws.Path, "cwd", staticmethod(lambda: mock_tasks["dir"]))
        scores_file.write_text("\n".join([
            json.dumps({"candidate_model": "claude", "judge_model": "gpt-5",
                        "semantic_score": 4.0, "false_positive": False}),
            json.dumps({"candidate_model": "claude", "judge_model": "claude",
                        "semantic_score": -1, "error": "自评禁止（LLM-as-Judge 自偏）"}),
        ]) + "\n", encoding="utf-8")
        d = ws.api_cross_judge()
        assert d["total_records"] == 2
        assert d["self_blocked"] == 1

    def test_cross_judge_empty(self, mock_tasks, monkeypatch):
        import agent_go.web_server as ws
        monkeypatch.setattr(ws.Path, "cwd", staticmethod(lambda: mock_tasks["dir"]))
        d = ws.api_cross_judge()
        assert d["total_records"] == 0
        assert d["self_blocked"] == 0


class TestBenchResults:
    """bench 模型对照结果。"""

    def test_bench_results(self, mock_tasks, monkeypatch):
        import agent_go.web_server as ws
        # 让 _bench_results_path 指向 tmp_path 下的文件
        bench_file = mock_tasks["dir"] / "results.jsonl"
        bench_file.write_text("\n".join([
            json.dumps({"model": "m1", "completed": 3, "failed": 1,
                        "total_cost_usd": 0.5, "pass_rate": 0.75}),
            json.dumps({"model": "m2", "completed": 2, "failed": 2,
                        "total_cost_usd": 0.3, "pass_rate": 0.5}),
        ]) + "\n", encoding="utf-8")
        # patch _bench_results_path
        monkeypatch.setattr(ws, "_bench_results_path", lambda: bench_file)
        d = ws.api_bench_results()
        assert d["total_runs"] == 2
        assert len(d["by_model"]) == 2
        m1 = next(m for m in d["by_model"] if m["model"] == "m1")
        assert m1["runs"] == 1
        assert m1["avg_pass_rate"] == 0.75


class TestBaseline:
    """baseline（bench 裸跑 + cost 门禁）。"""

    def test_baseline_returns_both(self, mock_tasks, monkeypatch):
        import agent_go.web_server as ws
        # patch 路径让两个 baseline 都指向 tmp
        bench_bl = mock_tasks["dir"] / "baseline.jsonl"
        bench_bl.write_text(json.dumps({"model": "claude", "completed": 1}) + "\n",
                            encoding="utf-8")
        cost_bl = mock_tasks["dir"] / "cost_baseline.json"
        cost_bl.write_text(json.dumps({"dollar_per_pass_rate": 0.05}), encoding="utf-8")
        monkeypatch.setattr(ws, "_resolve_workspace_file",
                            lambda n: bench_bl if "baseline.jsonl" in n else cost_bl)
        # cost_baseline 用 CONFIG_PATH.parent，patch web_server 模块级引用
        monkeypatch.setattr(ws, "CONFIG_PATH", cost_bl)
        d = ws.api_baseline()
        assert d["bench_baseline"]["total_runs"] == 1
        assert d["cost_gate_baseline"]["data"]["dollar_per_pass_rate"] == 0.05


# ═══════════════════════════════════════════════════════════════
# 配置查看 + 磁盘运维（P2）
# ═══════════════════════════════════════════════════════════════

class TestConfig:
    """配置查看（含 api_key 脱敏）。"""

    def test_config_masks_api_key(self, mock_tasks, monkeypatch):
        import agent_go.web_server as ws
        # patch load_config 返回带 api_key 的配置
        fake_cfg = {"plan_api": {"api_key": "sk-1234567890abcdef",
                                  "model": "m1"}}
        monkeypatch.setattr(ws, "load_config", lambda: fake_cfg)
        monkeypatch.setattr(ws, "CONFIG_PATH", mock_tasks["dir"] / "config.json")
        d = ws.api_config()
        key = d["config"]["plan_api"]["api_key"]
        assert key != "sk-1234567890abcdef"  # 已脱敏
        assert "..." in key
        assert key.startswith("sk-1") and key.endswith("cdef")

    def test_config_short_key_fully_masked(self, mock_tasks, monkeypatch):
        """短 key（≤8 字符）完全遮蔽为 ***（不透出任何字符）。"""
        import agent_go.web_server as ws
        fake_cfg = {"plan_api": {"api_key": "short"}}
        monkeypatch.setattr(ws, "load_config", lambda: fake_cfg)
        monkeypatch.setattr(ws, "CONFIG_PATH", mock_tasks["dir"] / "config.json")
        d = ws.api_config()
        assert d["config"]["plan_api"]["api_key"] == "***"

    def test_config_masks_keys_in_lists(self, mock_tasks, monkeypatch):
        """list 嵌套 dict 里的敏感字段也要脱敏。"""
        import agent_go.web_server as ws
        fake_cfg = {"backends": [{"name": "b1", "token": "tok-123456789"},
                                 {"name": "b2"}]}
        monkeypatch.setattr(ws, "load_config", lambda: fake_cfg)
        monkeypatch.setattr(ws, "CONFIG_PATH", mock_tasks["dir"] / "config.json")
        d = ws.api_config()
        masked = d["config"]["backends"][0]["token"]
        assert masked != "tok-123456789"
        assert "..." in masked
        assert d["config"]["backends"][1]["name"] == "b2"  # 非敏感字段不受影响


class TestStorage:
    """磁盘占用 + 孤儿目录。"""

    def test_storage_aggregation(self, mock_tasks):
        import agent_go.web_server as ws
        d = ws.api_storage()
        assert d["task_count"] == 2  # mock_tasks 造了 2 个
        assert d["total_size"] > 0  # 字节数（meta+log+metering 至少几百字节）
        assert "total_size_mb" in d
        assert len(d["top_tasks"]) == 2
        # 排序：大的在前
        if len(d["top_tasks"]) >= 2:
            assert d["top_tasks"][0]["size"] >= d["top_tasks"][1]["size"]

    def test_storage_detects_orphans(self, mock_tasks):
        """无 meta.json 的目录被识别为孤儿。"""
        import agent_go.web_server as ws
        orphan = mock_tasks["dir"] / "task-20260802-900000-999-cccc"
        orphan.mkdir()
        (orphan / "execution.log").write_text("log only", encoding="utf-8")
        d = ws.api_storage()
        assert d["orphan_count"] >= 1
        orphan_names = [o["name"] for o in d["orphans"]]
        assert orphan.name in orphan_names

    def test_storage_empty_dir(self, tmp_path, monkeypatch):
        """AGENT_GO_DIR 不存在时安全返回。"""
        import agent_go.web_server as ws
        monkeypatch.setattr(ws, "AGENT_GO_DIR", tmp_path / "nonexistent")
        d = ws.api_storage()
        assert d["total_size"] == 0
        assert d["task_count"] == 0
