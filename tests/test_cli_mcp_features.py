"""Tests for CLI/MCP 保留项落地 (R-1~R-5):

R-1 波次进度卡片 (_estimate_wave_count)
R-2 SKILL.md 自描述 (skills show)
R-3 多 profile (--profile / AGENT_GO_PROFILE)
R-4 增量 Plan 迭代 + 实时 Diff (compute_plan_diff / show_plan_diff)
R-5 Sampling 原语 (request_sampling / sampling_confirm / cancel_task confirm)
"""

import io, json, os, sys, threading, time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_go.config as config_mod
import agent_go.mcp_server as mcp_mod
import agent_go.pipeline as pipeline_mod
import agent_go.skills as skills_mod
import agent_go.ui as ui_mod


# ── R-1: 波次估算 ──────────────────────────────────────────────

class TestWaveEstimate:
    def _subtask(self, sid, deps=None):
        return {"id": sid, "depends_on": deps or []}

    def test_serial_chain(self):
        subs = [self._subtask("s1"), self._subtask("s2", ["s1"]), self._subtask("s3", ["s2"])]
        assert pipeline_mod._estimate_wave_count(subs) == 3

    def test_parallel_wave(self):
        subs = [self._subtask("s1"), self._subtask("s2"), self._subtask("s3", ["s1", "s2"])]
        assert pipeline_mod._estimate_wave_count(subs) == 2

    def test_no_deps_single_wave(self):
        subs = [self._subtask("s1"), self._subtask("s2"), self._subtask("s3")]
        assert pipeline_mod._estimate_wave_count(subs) == 1

    def test_empty(self):
        assert pipeline_mod._estimate_wave_count([]) == 0

    def test_cycle_terminates(self):
        subs = [self._subtask("s1", ["s2"]), self._subtask("s2", ["s1"])]
        assert pipeline_mod._estimate_wave_count(subs) == 0

    def test_resume_skips_completed(self):
        subs = [self._subtask("s1"), self._subtask("s2", ["s1"])]
        assert pipeline_mod._estimate_wave_count(subs, {"s1"}) == 1


# ── R-2: SKILL.md 自描述 ───────────────────────────────────────

class TestSkillShow:
    def _make_skill(self, tmp_path, name="test-skill"):
        sd = tmp_path / name
        sd.mkdir()
        (sd / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\nallowed-tools: Read, Grep\n---\n\n"
            "# Usage\n\nDo things carefully.\n", encoding="utf-8")
        return sd

    def test_get_skill_full(self, tmp_path):
        sd = self._make_skill(tmp_path)
        with patch.object(skills_mod, "AGENT_GO_SKILLS_DIR", tmp_path):
            info = skills_mod.get_skill_full("test-skill")
        assert info["name"] == "test-skill"
        assert info["description"] == "A test skill"
        assert info["allowed_tools"] == ["Read", "Grep"]  # 属性解析为列表
        assert info["frontmatter"]["allowed-tools"] == "Read, Grep"  # 原始值为字符串
        assert "# Usage" in info["raw"]

    def test_get_skill_full_missing(self, tmp_path):
        with patch.object(skills_mod, "AGENT_GO_SKILLS_DIR", tmp_path):
            assert skills_mod.get_skill_full("nope") is None


# ── R-3: 多 profile ────────────────────────────────────────────

class TestProfile:
    @pytest.fixture(autouse=True)
    def _clean_env(self):
        saved = os.environ.get("AGENT_GO_PROFILE")
        yield
        if saved is None:
            os.environ.pop("AGENT_GO_PROFILE", None)
        else:
            os.environ["AGENT_GO_PROFILE"] = saved

    def test_profile_used(self, tmp_path):
        (tmp_path / "profiles").mkdir()
        (tmp_path / "profiles" / "work.json").write_text(
            json.dumps({"plan_api": {"model": "work-model"}}), encoding="utf-8")
        os.environ["AGENT_GO_PROFILE"] = "work"
        with patch.object(config_mod, "AGENT_GO_DIR", tmp_path):
            cfg = config_mod.load_config()
        assert cfg["plan_api"]["model"] == "work-model"

    def test_profile_legacy_path(self, tmp_path):
        (tmp_path / "config.home.json").write_text(
            json.dumps({"plan_api": {"model": "home-model"}}), encoding="utf-8")
        os.environ["AGENT_GO_PROFILE"] = "home"
        with patch.object(config_mod, "AGENT_GO_DIR", tmp_path):
            cfg = config_mod.load_config()
        assert cfg["plan_api"]["model"] == "home-model"

    def test_profile_missing_falls_back(self, tmp_path):
        os.environ["AGENT_GO_PROFILE"] = "nonexistent"
        with patch.object(config_mod, "AGENT_GO_DIR", tmp_path):
            cfg = config_mod.load_config()
        assert cfg.get("behavior") is not None  # 回退默认

    def test_config_path_beats_profile(self, tmp_path):
        (tmp_path / "profiles").mkdir()
        (tmp_path / "profiles" / "work.json").write_text(
            json.dumps({"plan_api": {"model": "work"}}), encoding="utf-8")
        custom = tmp_path / "custom.json"
        custom.write_text(json.dumps({"plan_api": {"model": "custom"}}), encoding="utf-8")
        os.environ["AGENT_GO_PROFILE"] = "work"
        with patch.object(config_mod, "AGENT_GO_DIR", tmp_path):
            cfg = config_mod.load_config(config_path=str(custom))
        assert cfg["plan_api"]["model"] == "custom"


# ── R-4: Plan diff ─────────────────────────────────────────────

class TestPlanDiff:
    def _plan(self, steps):
        return {"overview": "o", "steps": steps}

    def _step(self, sid, title, desc="d", files=None, verification=""):
        return {"id": sid, "title": title, "description": desc,
                "files": files or [], "verification": verification}

    def test_no_change(self):
        p1 = self._plan([self._step("s1", "A")])
        p2 = self._plan([self._step("s1", "A")])
        assert ui_mod.compute_plan_diff(p1, p2) == []

    def test_added_removed(self):
        p1 = self._plan([self._step("s1", "A"), self._step("s2", "B")])
        p2 = self._plan([self._step("s1", "A"), self._step("s3", "C")])
        changes = ui_mod.compute_plan_diff(p1, p2)
        types = {c["type"] for c in changes}
        assert types == {"added", "removed"}

    def test_modified_fields(self):
        p1 = self._plan([self._step("s1", "A", files=["a.py"])])
        p2 = self._plan([self._step("s1", "A", files=["a.py", "b.py"])])
        changes = ui_mod.compute_plan_diff(p1, p2)
        assert len(changes) == 1
        assert changes[0]["type"] == "modified"
        assert "files" in changes[0]["fields"]

    def test_overview_change(self):
        p1 = self._plan([self._step("s1", "A")])
        p2 = self._plan([self._step("s1", "A")])
        p2["overview"] = "changed"
        changes = ui_mod.compute_plan_diff(p1, p2)
        assert any(c["type"] == "overview" for c in changes)

    def test_show_plan_diff_output(self, capsys):
        p1 = self._plan([self._step("s1", "A"), self._step("s2", "B")])
        p2 = self._plan([self._step("s1", "A"), self._step("s3", "C")])
        ui_mod.show_plan_diff(p1, p2, force=True)
        captured = capsys.readouterr()
        assert "🆕" in captured.out
        assert "🗑️" in captured.out


# ── R-5: Sampling 原语 ─────────────────────────────────────────

class TestSampling:
    def test_request_sampling_roundtrip(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            sent = []
            server._send = lambda m: sent.append(json.loads(json.dumps(m)))

            def respond():
                time.sleep(0.1)
                req = sent[0]
                msg = {"jsonrpc": "2.0", "id": req["id"],
                       "result": {"role": "assistant",
                                  "content": [{"type": "text", "text": "Yes"}]}}
                with server._lock:
                    ev = server._deferred.pop(msg["id"])
                    server._deferred_result[msg["id"]] = msg
                ev.set()

            t = threading.Thread(target=respond, daemon=True)
            t.start()
            resp = server.request_sampling("确认?", timeout=5)
            t.join()
            assert resp["content"][0]["text"] == "Yes"
            assert sent[0]["method"] == "sampling/createMessage"

    def test_request_sampling_timeout(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            server._send = lambda m: None
            t0 = time.time()
            assert server.request_sampling("hi", timeout=1.0) is None
            assert time.time() - t0 >= 0.9

    def test_request_sampling_http_unavailable(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer(notification_sink=lambda m: None)
            assert server.request_sampling("hi", timeout=1) is None

    def test_sampling_confirm_yes(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            server.request_sampling = lambda *a, **k: {"content": [{"type": "text", "text": "Y"}]}
            assert server.sampling_confirm("确认?") is True

    def test_sampling_confirm_no(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            server.request_sampling = lambda *a, **k: {"content": [{"type": "text", "text": "No"}]}
            assert server.sampling_confirm("确认?") is False

    def test_sampling_confirm_unavailable_failopen(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            server.request_sampling = lambda *a, **k: None
            assert server.sampling_confirm("确认?") is True  # fail-open

    def test_cancel_task_confirm_rejected(self, tmp_path):
        """confirm=true 且 Host 拒绝 → 任务保持运行。"""
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            task_id = "task-cf"
            td = tmp_path / task_id
            td.mkdir()
            (td / "meta.json").write_text(json.dumps(
                {"task_id": task_id, "status": "running", "subtasks": [], "results": []}),
                encoding="utf-8")
            proc = io.StringIO()  # dummy
            from unittest.mock import MagicMock
            proc = MagicMock()
            proc.poll.return_value = None
            server._running[task_id] = proc
            server.sampling_confirm = lambda *a, **k: False  # Host 拒绝
            r = server._tool_cancel_task({"task_id": task_id, "confirm": True})
            assert r["cancelled"] is False
            proc.terminate.assert_not_called()
            # meta.json 未被标记
            meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
            assert meta["status"] == "running"

    def test_cancel_task_confirm_accepted(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            server = mcp_mod.MCPServer()
            task_id = "task-cf2"
            td = tmp_path / task_id
            td.mkdir()
            (td / "meta.json").write_text(json.dumps(
                {"task_id": task_id, "status": "running", "subtasks": [], "results": []}),
                encoding="utf-8")
            from unittest.mock import MagicMock
            proc = MagicMock()
            proc.poll.return_value = None
            server._running[task_id] = proc
            server.sampling_confirm = lambda *a, **k: True  # Host 确认
            r = server._tool_cancel_task({"task_id": task_id, "confirm": True})
            assert r["cancelled"] is True
            proc.terminate.assert_called_once()
