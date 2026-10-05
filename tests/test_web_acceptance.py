"""spec-to-test web 控制台面测试（ADR-012）：确认门人审回执 + 只读验收数据面。

覆盖：
- spec_test.sanitize_review / merge_review（回执校验与合并，白名单/体量）
- web_confirm 回执透传（acceptance_state 回填）
- POST /api/tasks/<id>/confirm 带 acceptance（合法 200 落盘 / 非法 400 不落盘）
- GET /api/tasks/<id>/acceptance（冻结件+文件内容+运行结果；404；未启用为空）
- api_task 摘要字段
- 前端模板包含人审与只读面板钩子（防回退）
"""

import json
import threading
from pathlib import Path
from typing import Generator

import pytest

import agent_go.config as cfg
import agent_go.profiles as prof
import agent_go.web_server as ws
from agent_go import web_confirm
from agent_go.config import DEFAULT_CONFIG


def _cfg(**overrides):
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    c.setdefault("spec_test", {})
    c["spec_test"].update(overrides)
    return c


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    for mod in (prof, cfg, ws):
        monkeypatch.setattr(mod, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(prof, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    return adir


@pytest.fixture
def web_server_url(web_env, monkeypatch) -> Generator[str, None, None]:
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


def _mk_task(adir: Path, task_id: str, acceptance_meta: dict = None, results=None) -> Path:
    td = adir / task_id
    td.mkdir(parents=True, exist_ok=True)
    meta = {
        "task_id": task_id, "task": "测试任务", "status": "VERIFICATION_FAILED",
        "status_schema_version": 1, "repo": "/tmp/repo",
        "created": "2026-10-05T10:00:00", "subtasks": [], "results": results or [],
    }
    if acceptance_meta is not None:
        meta["acceptance"] = acceptance_meta
    (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return td


def _freeze(task_dir: Path, logger, reviewed=True, source="drafted"):
    from agent_go import spec_test
    draft = {
        "files": [{"path": "test_acc.py", "content": "def test_ok():\n    assert True\n"}],
        "commands": ["pytest tests/acceptance/test_acc.py -q"],
        "notes": "n", "model": "m",
    }
    return spec_test.freeze(task_dir, draft, reviewed=reviewed, source=source,
                            logger=logger, frozen_dir="tests/acceptance")


def _http_get(url: str) -> tuple:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _http_post(url: str, body: dict) -> tuple:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# ── 回执校验与合并（spec_test）────────────────────────────────────────

class TestSanitizeReview:
    def test_approved_with_files(self):
        from agent_go import spec_test
        out = spec_test.sanitize_review(
            {"decision": "approved", "edits": 1,
             "files": [{"path": "t.py", "content": "x = 1"}]}, _cfg())
        assert out == {"decision": "approved", "edits": 1,
                       "files": [{"path": "t.py", "content": "x = 1"}]}

    def test_skipped_without_files(self):
        from agent_go import spec_test
        assert spec_test.sanitize_review({"decision": "skipped"}, _cfg()) == {"decision": "skipped", "edits": 0}

    @pytest.mark.parametrize("payload", [
        None, [], {"decision": "maybe"}, {"decision": "approved", "files": [{"path": "../x.py", "content": "x"}]},
        {"decision": "approved", "files": [{"path": "/abs.py", "content": "x"}]},
        {"decision": "approved", "files": [{"path": "t.py", "content": ""}]},
        {"decision": "approved", "files": [{"path": "t.py", "content": "x" * 30000}]},
    ])
    def test_rejects_invalid(self, payload):
        from agent_go import spec_test
        assert spec_test.sanitize_review(payload, _cfg()) is None

    def test_merge_review_overwrites_known_paths(self, logger):
        from agent_go import spec_test
        draft = {"files": [{"path": "a.py", "content": "old"}, {"path": "b.py", "content": "keep"}]}
        spec_test.merge_review(draft, {"files": [{"path": "a.py", "content": "new"}]}, logger)
        assert draft["files"][0]["content"] == "new"
        assert draft["files"][1]["content"] == "keep"

    def test_merge_review_ignores_unknown_paths(self, logger):
        from agent_go import spec_test
        draft = {"files": [{"path": "a.py", "content": "x"}]}
        spec_test.merge_review(draft, {"files": [{"path": "ghost.py", "content": "y"}]}, logger)
        assert draft["files"][0]["content"] == "x"


# ── web_confirm 回执透传 ──────────────────────────────────────────────

class TestWebConfirmAcceptance:
    def test_state_filled_from_decision(self, tmp_path, logger):
        td = tmp_path / "task"
        td.mkdir()
        state: dict = {}
        # 模拟 web 端写回执：直接构造 decision 文件后调用 web_confirm（stage 匹配即命中）
        (td / web_confirm.DECISION_FILE).write_text(json.dumps({
            "stage": "plan", "decision": "Y",
            "acceptance": {"decision": "approved", "edits": 1,
                           "files": [{"path": "t.py", "content": "x"}]},
        }), encoding="utf-8")
        decision = web_confirm.web_confirm("plan", {"steps": []}, td, logger, acceptance_state=state)
        assert decision == "Y"
        assert state["decision"] == "approved" and state["edits"] == 1
        assert state["files"] == [{"path": "t.py", "content": "x"}]

    def test_state_untouched_without_acceptance(self, tmp_path, logger):
        td = tmp_path / "task"
        td.mkdir()
        state: dict = {}
        (td / web_confirm.DECISION_FILE).write_text(
            json.dumps({"stage": "plan", "decision": "Y"}), encoding="utf-8")
        assert web_confirm.web_confirm("plan", {}, td, logger, acceptance_state=state) == "Y"
        assert state == {}


# ── HTTP 面：确认回执 + 只读验收数据 ─────────────────────────────────

class TestConfirmAcceptanceHttp:
    def test_valid_acceptance_written_to_decision(self, web_env, web_server_url, logger):
        td = _mk_task(web_env, "task-20261005-100000-001-acc1")
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "plan", "payload": {"steps": []}, "ts": "2026-10-05T10:00:00Z",
                        "timeout_sec": 600}), encoding="utf-8")
        status, body = _http_post(web_server_url + "/api/tasks/task-20261005-100000-001-acc1/confirm", {
            "stage": "plan", "decision": "Y",
            "acceptance": {"decision": "approved", "edits": 2,
                           "files": [{"path": "t.py", "content": "x = 1"}]},
        })
        assert status == 200, body
        assert body["acceptance"] == {"decision": "approved", "edits": 2, "files": 1}
        decision = json.loads((td / "confirmation_decision.json").read_text(encoding="utf-8"))
        assert decision["acceptance"]["files"][0]["path"] == "t.py"

    def test_invalid_acceptance_rejected_and_not_written(self, web_env, web_server_url):
        td = _mk_task(web_env, "task-20261005-100000-002-acc2")
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "plan", "payload": {}, "ts": "2026-10-05T10:00:00Z",
                        "timeout_sec": 600}), encoding="utf-8")
        status, body = _http_post(web_server_url + "/api/tasks/task-20261005-100000-002-acc2/confirm", {
            "stage": "plan", "decision": "Y",
            "acceptance": {"decision": "approved", "files": [{"path": "../../etc/passwd", "content": "x"}]},
        })
        assert status == 400, body
        assert not (td / "confirmation_decision.json").exists(), "非法回执不得落盘"

    def test_acceptance_only_for_plan_stage(self, web_env, web_server_url):
        td = _mk_task(web_env, "task-20261005-100000-003-a3c3")
        (td / "pending_confirmation.json").write_text(
            json.dumps({"stage": "subtasks", "payload": {"subtasks": []}, "ts": "2026-10-05T10:00:00Z",
                        "timeout_sec": 600}), encoding="utf-8")
        status, body = _http_post(web_server_url + "/api/tasks/task-20261005-100000-003-a3c3/confirm", {
            "stage": "subtasks", "decision": "Y", "acceptance": {"decision": "approved"},
        })
        assert status == 400 and "仅支持 stage=plan" in body.get("error", "")


class TestAcceptanceReadApi:
    def test_frozen_payload(self, web_env, web_server_url, logger):
        tid = "task-20261005-110000-001-f10a"
        td = _mk_task(web_env, tid, acceptance_meta={
            "enabled": True, "source": "drafted", "reviewed": True,
            "commands": ["pytest tests/acceptance/test_acc.py -q"],
        }, results=[{"subtask_id": "sub-1", "verification_results": [
            {"type": "acceptance", "command": "pytest tests/acceptance/test_acc.py -q",
             "exit_code": 0, "attempt": 1},
            {"type": "acceptance_restore", "attempt": 2, "restored": ["tests/acceptance/test_acc.py"]},
        ]}])
        _freeze(td, logger)
        status, body = _http_get(web_server_url + f"/api/tasks/{tid}/acceptance")
        assert status == 200
        assert body["enabled"] is True
        assert body["manifest"]["reviewed"] is True
        assert body["files"][0]["path"] == "test_acc.py"
        assert "assert True" in body["files"][0]["content"]
        types = [r["type"] for r in body["runtime"]]
        assert "acceptance" in types and "acceptance_restore" in types

    def test_draft_only_payload(self, web_env, web_server_url, logger):
        from agent_go import spec_test
        tid = "task-20261005-110000-002-d0a1"
        td = _mk_task(web_env, tid, acceptance_meta={"enabled": True, "frozen": False, "degraded": True,
                                                     "review_decision": "skipped"})
        spec_test.save_draft(td, {"files": [{"path": "t.py", "content": "x"}],
                                  "commands": ["pytest t.py"]}, logger, reason="user_skipped")
        status, body = _http_get(web_server_url + f"/api/tasks/{tid}/acceptance")
        assert status == 200
        assert body["manifest"] is None and body["draft"]["reason"] == "user_skipped"
        assert body["meta"]["degraded"] is True

    def test_unknown_task_404(self, web_server_url):
        status, _ = _http_get(web_server_url + "/api/tasks/task-20261005-999999-999-0000/acceptance")
        assert status == 404

    def test_not_enabled_empty(self, web_env, web_server_url):
        tid = "task-20261005-110000-003-91a1"
        _mk_task(web_env, tid)
        status, body = _http_get(web_server_url + f"/api/tasks/{tid}/acceptance")
        assert status == 200 and body["enabled"] is False and body["manifest"] is None

    def test_task_summary_contains_acceptance(self, web_env, web_server_url, logger):
        tid = "task-20261005-110000-004-50a1"
        td = _mk_task(web_env, tid, acceptance_meta={"enabled": True, "frozen": True,
                                                     "source": "task", "reviewed": True})
        _freeze(td, logger, source="task")
        status, body = _http_get(web_server_url + f"/api/tasks/{tid}")
        assert status == 200
        acc = body["acceptance"]
        assert acc["enabled"] is True and acc["frozen"] is True and acc["source"] == "task"


class TestFrontendHooks:
    def test_spa_contains_review_and_panel_hooks(self):
        from agent_go.web_frontend import _SPA_HTML
        for marker in ("_acceptance_draft", "renderAcceptanceDraft", "accSkip",
                       "accPanel", "renderAcceptancePanel", "/acceptance"):
            assert marker in _SPA_HTML, f"前端缺少验收管线钩子: {marker}"


class TestCliWebChannelWiring:
    """cli._confirm_plan_channel 的 web 人审接线：草稿随 payload 下发、回执合并回草稿。"""

    def test_payload_carries_draft_and_state_merges(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import web_confirm as wc_mod

        captured = {}

        def _fake_web_confirm(stage, payload, task_dir, logger_, timeout=None, acceptance_state=None):
            captured["payload"] = payload
            # 模拟 web 端：编辑了草稿文件并批准
            if acceptance_state is not None:
                acceptance_state.update({
                    "decision": "approved", "edits": 1,
                    "files": [{"path": "test_acc.py", "content": "def test_ok():\n    assert 1\n"}],
                })
            return "Y"

        monkeypatch.setattr(wc_mod, "web_confirm", _fake_web_confirm)
        config = _cfg(enabled=True)
        config.setdefault("behavior", {})["web_confirm_plan"] = True
        draft = {"files": [{"path": "test_acc.py", "content": "def test_ok():\n    assert 0\n"}],
                 "commands": ["pytest tests/acceptance/test_acc.py -q"], "notes": "n"}
        state: dict = {}
        plan, _docs = cli_mod._confirm_plan_channel(
            {"steps": [{"id": 1}]}, config, Path("/tmp/repo"), logger, 1, "task",
            tmp_path / "task", acceptance=draft, acceptance_state=state)

        assert plan == {"steps": [{"id": 1}]}
        assert captured["payload"]["_acceptance_draft"]["files"][0]["path"] == "test_acc.py"
        assert captured["payload"]["_acceptance_draft"]["commands"]
        # 回执合并：编辑后的内容覆盖草稿（冻结将按编辑版）
        assert draft["files"][0]["content"] == "def test_ok():\n    assert 1\n"
        assert state["decision"] == "approved" and state["edits"] == 1

    def test_invalid_web_review_degrades_to_unreviewed(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import web_confirm as wc_mod

        def _fake_web_confirm(stage, payload, task_dir, logger_, timeout=None, acceptance_state=None):
            if acceptance_state is not None:
                acceptance_state.update({"decision": "approved",
                                         "files": [{"path": "../evil.py", "content": "x"}]})
            return "Y"

        monkeypatch.setattr(wc_mod, "web_confirm", _fake_web_confirm)
        config = _cfg(enabled=True)
        config.setdefault("behavior", {})["web_confirm_plan"] = True
        draft = {"files": [{"path": "t.py", "content": "x"}], "commands": ["pytest t.py"]}
        state: dict = {}
        cli_mod._confirm_plan_channel({}, config, Path("/tmp/repo"), logger, 1, "task",
                                      tmp_path / "task", acceptance=draft, acceptance_state=state)
        assert state == {}, "非法回执必须清空 → 按未人审处理（不冻结 oracle）"

    def test_freeze_resanitizes_reviewed_draft(self, tmp_path, logger):
        from agent_go import cli as cli_mod
        # 人审后的草稿超体量 → 清洗掉该文件 → 无可用文件 → 不冻结（降级）
        draft = {"files": [{"path": "t.py", "content": "x" * 30000}],
                 "commands": ["pytest tests/acceptance/t.py"]}
        out = cli_mod._freeze_acceptance_after_review(
            tmp_path / "t1", draft, {"decision": "approved", "edits": 1}, _cfg(enabled=True), logger)
        assert out is None
        # 正常草稿 → 冻结
        draft_ok = {"files": [{"path": "t.py", "content": "def test_a():\n    assert 1\n"}],
                    "commands": ["pytest tests/acceptance/t.py"]}
        out2 = cli_mod._freeze_acceptance_after_review(
            tmp_path / "t2", draft_ok, {"decision": "approved", "edits": 1}, _cfg(enabled=True), logger)
        assert out2 is not None and out2["review_edits"] == 1
