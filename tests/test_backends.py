"""Worker Backend 抽象层单元测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

from agent_go.agents import AgentType
from agent_go.backends import (
    BackendContext,
    BackendRegistry,
    BaseBackend,
    SubtaskResult,
    repair_timeout,
    resolve_backend_name,
    run_repair,
)
from agent_go.backends.claude_backend import ClaudeBackend
from agent_go.backends.agent_loop_backend import AgentLoopBackend


@pytest.fixture
def null_logger():
    return logging.getLogger("null")


class TestBackendRegistry:
    def test_register_and_get(self):
        class FakeBackend(BaseBackend):
            name = "fake"

            def run(self, ctx):
                return SubtaskResult(returncode=0)

        BackendRegistry.register(FakeBackend)
        assert BackendRegistry.get("fake") is FakeBackend
        assert "fake" in BackendRegistry.list()

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            BackendRegistry.get("nonexistent_backend")


class TestResolveBackendName:
    def test_default_is_claude(self):
        assert resolve_backend_name({}, {}, True, True) == "claude"

    def test_agent_loop_requires_enabled(self):
        cfg = {"agent_loop": {"enabled": True}}
        assert resolve_backend_name(cfg, {}, True, True) == "agent_loop"

    def test_agent_loop_requires_headless(self):
        cfg = {"agent_loop": {"enabled": True}}
        assert resolve_backend_name(cfg, {}, False, True) == "claude"

    def test_agent_loop_requires_simple(self):
        cfg = {"agent_loop": {"enabled": True}}
        assert resolve_backend_name(cfg, {}, True, False) == "claude"


class TestClaudeBackend:
    def test_headless_passes_agent_tools(self, tmp_path: Path, null_logger):
        agent = AgentType(
            type_name="architect",
            claude_config={"allowed_tools": ["Read", "Grep", "Glob"]},
        )
        ctx = BackendContext(
            task_md="task",
            worktree=tmp_path,
            env={},
            headless=True,
            agent=agent,
            sub_id="sub-1",
            logger=null_logger,
        )
        with patch("agent_go.backends.claude_backend._run_headless") as mock_h:
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            ClaudeBackend().run(ctx)
        assert mock_h.call_args.kwargs.get("allowed_tools") == ["Read", "Grep", "Glob"]

    def test_headless_no_agent_unrestricted(self, tmp_path: Path, null_logger):
        ctx = BackendContext(
            task_md="task",
            worktree=tmp_path,
            env={},
            headless=True,
            agent=None,
            sub_id="sub-1",
            logger=null_logger,
        )
        with patch("agent_go.backends.claude_backend._run_headless") as mock_h:
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            ClaudeBackend().run(ctx)
        assert mock_h.call_args.kwargs.get("allowed_tools") == []

    def test_headless_result_wraps_kill_reason(self, tmp_path: Path, null_logger):
        ctx = BackendContext(
            task_md="task",
            worktree=tmp_path,
            env={},
            headless=True,
            agent=None,
            sub_id="sub-1",
            logger=null_logger,
        )
        cp = CompletedProcess([], 1, stdout="out", stderr="err")
        cp.kill_reason = "hard_timeout"
        with patch("agent_go.backends.claude_backend._run_headless", return_value=cp):
            res = ClaudeBackend().run(ctx)
        assert res.returncode == 1
        assert res.stdout == "out"
        assert res.kill_reason == "hard_timeout"
        assert res.sandbox_type == "headless"

    def test_interactive_uses_get_claude_command(self, tmp_path: Path, null_logger):
        agent = AgentType(type_name="developer", claude_config={"allowed_tools": ["Read"]})
        ctx = BackendContext(
            task_md="task",
            worktree=tmp_path,
            env={},
            headless=False,
            agent=agent,
            sub_id="sub-1",
            logger=null_logger,
        )
        with patch("agent_go.backends.claude_backend.get_claude_command", return_value=["claude", str(tmp_path)]) as mock_cmd, \
             patch("agent_go.backends.claude_backend.subprocess.run", return_value=CompletedProcess([], 0)):
            ClaudeBackend().run(ctx)
        mock_cmd.assert_called_once_with(agent, tmp_path, headless=False)


class TestAgentLoopBackend:
    def test_run_calls_agent_loop(self, tmp_path: Path, null_logger):
        ctx = BackendContext(
            task_md="# task",
            worktree=tmp_path,
            env={},
            headless=True,
            agent=AgentType(type_name="developer"),
            sub_id="sub-1",
            task_id="task-1",
            tag_name="task-1/sub-1",
            difficulty="easy",
            routed_model="claude-sonnet",
            logger=null_logger,
            config={"plan_api": {"provider": "anthropic", "model": "claude"}},
        )
        fake_loop = MagicMock()
        fake_loop.run.return_value = CompletedProcess([], 0, stdout="ok", stderr="")
        with patch("agent_go.agent_loop.AgentLoop", return_value=fake_loop), \
             patch("agent_go.backends.agent_loop_backend.get_api_key", return_value="sk-key"), \
             patch("agent_go.router.resolve_provider", return_value=None):
            res = AgentLoopBackend().run(ctx)
        assert res.returncode == 0
        assert res.sandbox_type == "agent_loop"
        assert res.backend_time == 0.0
        fake_loop.run.assert_called_once()
        call_kwargs = fake_loop.run.call_args.kwargs
        assert call_kwargs["prompt"] == "# task"
        assert call_kwargs["worktree"] == tmp_path
        assert call_kwargs["pc"].model == "claude-sonnet"
        assert call_kwargs["api_key"] == "sk-key"


class TestRepairTimeout:
    """backends.dispatch.repair_timeout — 与迁移前 executor 三处内联逻辑逐一相等。"""

    def test_difficulty_multiplier(self):
        cfg = {"verification": {"retry_timeout": 300}}
        assert repair_timeout(cfg, "easy", {}) == 300        # 300×1
        assert repair_timeout(cfg, "medium", {}) == 450      # 300×1.5
        assert repair_timeout(cfg, "hard", {}) == 750        # 300×2.5

    def test_difficulty_cap(self):
        cfg = {"verification": {"retry_timeout": 1000}}
        assert repair_timeout(cfg, "easy", {}) == 600
        assert repair_timeout(cfg, "medium", {}) == 900
        assert repair_timeout(cfg, "hard", {}) == 1500

    def test_unknown_difficulty_falls_back_medium(self):
        cfg = {"verification": {"retry_timeout": 300}}
        assert repair_timeout(cfg, "extreme", {}) == 450

    def test_local_model_doubles_with_cap(self):
        cfg = {"verification": {"retry_timeout": 300}}
        env = {"AGENT_GO_IS_LOCAL": "1"}
        assert repair_timeout(cfg, "medium", env) == 900     # 450×2
        assert repair_timeout(cfg, "hard", env) == 1500      # 750×2=1500（未超 3000 封顶）
        cfg_big = {"verification": {"retry_timeout": 2000}}
        assert repair_timeout(cfg_big, "hard", env) == 3000  # 封顶 3000

    def test_defaults_when_config_missing(self):
        assert repair_timeout({}, "medium", {}) == 450
        assert repair_timeout(None, "medium", None) == 450


class TestRunRepair:
    """backends.dispatch.run_repair — 修复路径的 backend 分发与容错。"""

    def _ctx(self, tmp_path: Path, null_logger, config=None):
        return BackendContext(
            task_md="fix prompt", worktree=tmp_path, env={}, headless=True,
            sub_id="sub-1-fix-1", logger=null_logger, config=config or {},
            tag_name="task-1/sub-1",  # 调用方误传非空时必须被强制清空
        )

    def test_default_routes_to_claude(self, tmp_path, null_logger):
        calls = []

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                calls.append(("claude", ctx.tag_name))
                return SubtaskResult(returncode=0)

        with patch("agent_go.backends.dispatch.BackendRegistry.get", return_value=FakeClaude):
            res = run_repair(self._ctx(tmp_path, null_logger), is_simple=True)
        assert res.returncode == 0
        # tag_name 被强制清空：修复路径的 commit 边界归 executor 独占
        assert calls == [("claude", "")]

    def test_agent_loop_chosen_when_enabled_and_simple(self, tmp_path, null_logger):
        calls = []

        class FakeLoop(BaseBackend):
            name = "agent_loop"

            def run(self, ctx):
                calls.append("agent_loop")
                return SubtaskResult(returncode=0, sandbox_type="agent_loop")

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                calls.append("claude")
                return SubtaskResult(returncode=0)

        def _get(name):
            return {"agent_loop": FakeLoop, "claude": FakeClaude}[name]

        cfg = {"agent_loop": {"enabled": True}}
        with patch("agent_go.backends.dispatch.BackendRegistry.get", side_effect=_get):
            res = run_repair(self._ctx(tmp_path, null_logger, config=cfg), is_simple=True)
        assert res.sandbox_type == "agent_loop"
        assert calls == ["agent_loop"]

    def test_agent_loop_failure_falls_back_to_claude(self, tmp_path, null_logger):
        calls = []

        class BrokenLoop(BaseBackend):
            name = "agent_loop"

            def run(self, ctx):
                calls.append("agent_loop")
                raise RuntimeError("boom")

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                calls.append("claude")
                return SubtaskResult(returncode=0)

        def _get(name):
            return {"agent_loop": BrokenLoop, "claude": FakeClaude}[name]

        cfg = {"agent_loop": {"enabled": True}}
        with patch("agent_go.backends.dispatch.BackendRegistry.get", side_effect=_get):
            res = run_repair(self._ctx(tmp_path, null_logger, config=cfg), is_simple=True)
        assert res.returncode == 0
        assert calls == ["agent_loop", "claude"]

    def test_agent_loop_not_used_when_not_simple(self, tmp_path, null_logger):
        cfg = {"agent_loop": {"enabled": True}}

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                return SubtaskResult(returncode=0)

        with patch("agent_go.backends.dispatch.BackendRegistry.get", return_value=FakeClaude):
            res = run_repair(self._ctx(tmp_path, null_logger, config=cfg), is_simple=False)
        assert res.returncode == 0

    def test_explicit_pi_dispatches_to_pi(self, tmp_path, null_logger):
        """worker_backend=pi 时修复路径必须也走 pi（golden 批量曾因此 bug 回落 claude）。"""
        calls = []

        class FakePi(BaseBackend):
            name = "pi"

            def run(self, ctx):
                calls.append("pi")
                return SubtaskResult(returncode=0, sandbox_type="pi")

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                calls.append("claude")
                return SubtaskResult(returncode=0)

        def _get(name):
            return {"pi": FakePi, "claude": FakeClaude}[name]

        cfg = {"worker_backend": "pi"}
        with patch("agent_go.backends.dispatch.BackendRegistry.get", side_effect=_get):
            res = run_repair(self._ctx(tmp_path, null_logger, config=cfg), is_simple=False)
        assert res.sandbox_type == "pi"
        assert calls == ["pi"]

    def test_explicit_pi_failure_falls_back_to_claude(self, tmp_path, null_logger):
        calls = []

        class BrokenPi(BaseBackend):
            name = "pi"

            def run(self, ctx):
                calls.append("pi")
                raise RuntimeError("boom")

        class FakeClaude(BaseBackend):
            name = "claude"

            def run(self, ctx):
                calls.append("claude")
                return SubtaskResult(returncode=0)

        def _get(name):
            return {"pi": BrokenPi, "claude": FakeClaude}[name]

        cfg = {"worker_backend": "pi"}
        with patch("agent_go.backends.dispatch.BackendRegistry.get", side_effect=_get):
            res = run_repair(self._ctx(tmp_path, null_logger, config=cfg), is_simple=False)
        assert res.returncode == 0
        assert calls == ["pi", "claude"]


class TestClaudeBackendProgress:
    """progress=False（修复路径）保持控制台安静：不起 ticker 线程。"""

    def _ctx(self, tmp_path, null_logger, progress):
        return BackendContext(
            task_md="task", worktree=tmp_path, env={}, headless=True,
            agent=None, sub_id="sub-1", logger=null_logger, progress=progress,
        )

    def test_progress_false_skips_ticker_thread(self, tmp_path, null_logger):
        with patch("agent_go.backends.claude_backend._run_headless") as mock_h, \
             patch("agent_go.backends.claude_backend.threading.Thread") as mock_thread:
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            ClaudeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        mock_thread.assert_not_called()

    def test_progress_true_starts_ticker_thread(self, tmp_path, null_logger):
        with patch("agent_go.backends.claude_backend._run_headless") as mock_h, \
             patch("agent_go.backends.claude_backend.threading.Thread") as mock_thread:
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            ClaudeBackend().run(self._ctx(tmp_path, null_logger, progress=True))
        mock_thread.assert_called_once()


class TestResolveBackendNameExplicit:
    """B3：显式声明（subtask.backend / config.worker_backend）优先于自动策略。"""

    def test_subtask_backend_pi_headless(self):
        assert resolve_backend_name({}, {"backend": "pi"}, True, False) == "pi"

    def test_config_worker_backend_pi(self):
        cfg = {"worker_backend": "pi"}
        assert resolve_backend_name(cfg, {}, True, False) == "pi"

    def test_subtask_beats_config(self):
        cfg = {"worker_backend": "pi"}
        assert resolve_backend_name(cfg, {"backend": "agent_loop"}, True, True) == "agent_loop"

    def test_explicit_non_claude_interactive_falls_back(self):
        # pi/opencode 均为非交互 CLI，交互模式强制回退 claude
        assert resolve_backend_name({}, {"backend": "pi"}, False, False) == "claude"
        cfg = {"worker_backend": "pi"}
        assert resolve_backend_name(cfg, {}, False, True) == "claude"

    def test_explicit_claude_passes_through(self):
        assert resolve_backend_name({}, {"backend": "claude"}, False, True) == "claude"

    def test_no_explicit_keeps_b1_behavior(self):
        cfg = {"agent_loop": {"enabled": True}, "worker_backend": ""}
        assert resolve_backend_name(cfg, {}, True, True) == "agent_loop"
        assert resolve_backend_name(cfg, {}, True, False) == "claude"


def _pi_ndjson(*events):
    import json as _json
    return "\n".join(_json.dumps(e) for e in events) + "\n"


def _pi_success_stream(final_text="DONE"):
    """构造一次成功的 pi NDJSON 事件流（工具调用 1 次 + 最终回复）。"""
    return _pi_ndjson(
        {"type": "session", "version": 3, "id": "sess-1", "cwd": "/tmp/x"},
        {"type": "agent_start"},
        {"type": "message_end", "message": {
            "role": "assistant", "content": [], "stopReason": "toolUse",
            "provider": "deepseek", "model": "deepseek-v4-pro",
            "usage": {"input": 100, "output": 50, "cacheRead": 10,
                      "cost": {"total": 0.001}},
        }},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read", "args": {"path": "a.txt"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read", "result": {}, "isError": False},
        {"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": final_text}],
            "stopReason": "stop",
            "provider": "deepseek", "model": "deepseek-v4-pro",
            "usage": {"input": 200, "output": 20, "cacheRead": 0,
                      "cost": {"total": 0.002}},
        }},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    )


def _mock_popen(stdout="", stderr="", returncode=0):
    proc = MagicMock()
    proc.pid = 4321
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class TestPiBackend:
    """B3：PiBackend 命令构造、NDJSON 解析、超时与容错。"""

    def _ctx(self, tmp_path: Path, null_logger, **kw):
        defaults = dict(
            task_md="# 任务\n做点事", worktree=tmp_path, env={}, headless=True,
            sub_id="sub-1", task_id="task-1", logger=null_logger, config={},
        )
        defaults.update(kw)
        return BackendContext(**defaults)

    def test_command_and_parse_success(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        proc = _mock_popen(stdout=_pi_success_stream("全部完成"))
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/usr/local/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc) as mock_popen:
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        cmd = mock_popen.call_args.args[0]
        assert cmd[:4] == ["/usr/local/bin/pi", "-p", "--mode", "json"]
        assert "--no-session" in cmd
        assert cmd[-1].startswith("# 任务")
        assert res.returncode == 0
        assert res.sandbox_type == "pi"
        assert res.stdout == "全部完成"
        # 两轮 assistant usage 聚合：input 100+200, cacheRead 10, output 50+20
        assert res.kill_reason is None
        # pid 生命周期：结束后从 active_pids 移除
        assert 4321 not in self._ctx(tmp_path, null_logger).active_pids

    def test_readonly_restricts_tools(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PI_READONLY_TOOLS, PiBackend
        proc = _mock_popen(stdout=_pi_success_stream())
        ctx = self._ctx(tmp_path, null_logger, progress=False, extra={"readonly": True})
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc) as mock_popen:
            PiBackend().run(ctx)
        cmd = mock_popen.call_args.args[0]
        i = cmd.index("--tools")
        assert cmd[i + 1] == PI_READONLY_TOOLS
        assert "bash" not in cmd[i + 1]

    def test_routed_model_passed_through(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        proc = _mock_popen(stdout=_pi_success_stream())
        ctx = self._ctx(tmp_path, null_logger, progress=False, routed_model="deepseek/deepseek-v4-pro")
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc) as mock_popen:
            PiBackend().run(ctx)
        cmd = mock_popen.call_args.args[0]
        i = cmd.index("--model")
        assert cmd[i + 1] == "deepseek/deepseek-v4-pro"

    def test_no_model_flag_when_unrouted(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        proc = _mock_popen(stdout=_pi_success_stream())
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc) as mock_popen:
            PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert "--model" not in mock_popen.call_args.args[0]

    def test_timeout_kills_and_reports(self, tmp_path, null_logger):
        import subprocess as _sp
        from agent_go.backends.pi_backend import PiBackend
        proc = _mock_popen(returncode=-9)
        proc.communicate.side_effect = [
            _sp.TimeoutExpired(cmd="pi", timeout=5),
            ("", "killed"),
        ]
        ctx = self._ctx(tmp_path, null_logger, progress=False, hard_timeout=5)
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            res = PiBackend().run(ctx)
        proc.kill.assert_called_once()
        assert res.kill_reason == "hard_timeout"
        assert res.returncode == -9

    def test_pi_not_installed(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        with patch("agent_go.backends.pi_backend.shutil.which", return_value=None):
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 127
        assert "pi" in res.stderr

    def test_malformed_lines_tolerated(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        stream = "garbage line\n{broken json\n" + _pi_success_stream("OK")
        proc = _mock_popen(stdout=stream)
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 0
        assert res.stdout == "OK"

    def test_meter_event_written(self, tmp_path, null_logger):
        import json as _json
        from agent_go.backends.pi_backend import PiBackend
        metering = tmp_path / "metering.jsonl"
        ctx = self._ctx(tmp_path, null_logger, progress=False,
                        config={"_metering_path": str(metering)})
        proc = _mock_popen(stdout=_pi_success_stream())
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            PiBackend().run(ctx)
        events = [_json.loads(l) for l in metering.read_text().splitlines() if l.strip()]
        assert len(events) == 1
        ev = events[0]
        assert ev["virtual_model"] == "agentgo-worker-pi"
        assert ev["actual_model"] == "deepseek-v4-pro"
        assert ev["prompt_tokens"] == 310      # 100+10 + 200
        assert ev["completion_tokens"] == 70   # 50 + 20
        assert ev["cost_usd"] == pytest.approx(0.003)
        assert ev["result"] == "success"

    def test_tool_error_stop_logged_but_result_returned(self, tmp_path, null_logger):
        from agent_go.backends.pi_backend import PiBackend
        stream = _pi_ndjson(
            {"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": ""}],
                "stopReason": "error",
                "usage": {"input": 1, "output": 1, "cost": {"total": 0.0}},
            }},
        )
        proc = _mock_popen(stdout=stream, returncode=1, stderr="provider boom")
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        # 非零退出码是正常结果（验证失败走 retry），不在这里触发回退
        assert res.returncode == 1
        assert res.stderr == "provider boom"

    def test_zero_work_error_maps_to_failure(self, tmp_path, null_logger):
        """pi 对 API 级错误（402 余额不足）也退出 0：零产出 error 必须显式失败。"""
        from agent_go.backends.pi_backend import PiBackend
        stream = _pi_ndjson(
            {"type": "session", "version": 3, "id": "s1", "cwd": "/tmp"},
            {"type": "message_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": "402: Insufficient Balance",
                "usage": {"input": 0, "output": 0, "cost": {"total": 0.0}},
            }},
            {"type": "agent_end", "messages": []},
        )
        proc = _mock_popen(stdout=stream, returncode=0)
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 1
        assert "Insufficient Balance" in res.stderr

    def test_midstream_error_with_real_work_stays_success(self, tmp_path, null_logger):
        """事件流中间出现 error 但最终完成（有 tokens + 工具调用 + 最终文本）不误判。"""
        from agent_go.backends.pi_backend import PiBackend
        stream = _pi_ndjson(
            {"type": "message_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": "transient",
                "usage": {"input": 0, "output": 0, "cost": {"total": 0.0}},
            }},
        ) + _pi_success_stream("RECOVERED")
        proc = _mock_popen(stdout=stream, returncode=0)
        with patch("agent_go.backends.pi_backend.shutil.which", return_value="/bin/pi"), \
             patch("agent_go.backends.pi_backend.subprocess.Popen", return_value=proc):
            res = PiBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 0
        assert res.stdout == "RECOVERED"


def _oc_ndjson(*events):
    import json as _json
    return "\n".join(_json.dumps(e) for e in events) + "\n"


def _oc_success_stream(final_text="DONE"):
    """构造一次成功的 opencode NDJSON 事件流（1 次工具调用 + 最终文本回复）。"""
    return _oc_ndjson(
        {"type": "step_start", "sessionID": "ses-1", "timestamp": 1,
         "part": {"id": "p1", "type": "step-start"}},
        {"type": "tool_use", "sessionID": "ses-1", "timestamp": 2,
         "part": {"type": "tool", "tool": "write", "callID": "c1",
                  "state": {"status": "completed", "input": {"filePath": "a.txt"}}}},
        {"type": "step_finish", "sessionID": "ses-1", "timestamp": 3,
         "part": {"type": "step-finish", "reason": "tool-calls", "cost": 0.001,
                  "tokens": {"total": 150, "input": 100, "output": 50, "reasoning": 0,
                             "cache": {"read": 10, "write": 0}}}},
        {"type": "step_start", "sessionID": "ses-1", "timestamp": 4,
         "part": {"id": "p4", "type": "step-start"}},
        {"type": "text", "sessionID": "ses-1", "timestamp": 5,
         "part": {"type": "text", "text": final_text}},
        {"type": "step_finish", "sessionID": "ses-1", "timestamp": 6,
         "part": {"type": "step-finish", "reason": "stop", "cost": 0.002,
                  "tokens": {"total": 220, "input": 200, "output": 20, "reasoning": 0,
                             "cache": {"read": 0, "write": 0}}}},
    )


class TestOpenCodeBackend:
    """B6：OpenCodeBackend 命令构造、NDJSON 解析、超时与容错。"""

    def _ctx(self, tmp_path: Path, null_logger, **kw):
        defaults = dict(
            task_md="# 任务\n做点事", worktree=tmp_path, env={}, headless=True,
            sub_id="sub-1", task_id="task-1", logger=null_logger, config={},
        )
        defaults.update(kw)
        return BackendContext(**defaults)

    def test_command_and_parse_success(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OpenCodeBackend
        proc = _mock_popen(stdout=_oc_success_stream("全部完成"))
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/usr/local/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc) as mock_popen:
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        cmd = mock_popen.call_args.args[0]
        assert cmd[:6] == ["/usr/local/bin/opencode", "run", "--format", "json", "--auto", "--pure"]
        assert cmd[-1].startswith("# 任务")
        assert res.returncode == 0
        assert res.sandbox_type == "opencode"
        assert res.stdout == "全部完成"
        assert res.kill_reason is None
        # pid 生命周期：结束后从 active_pids 移除
        assert 4321 not in self._ctx(tmp_path, null_logger).active_pids

    def test_readonly_uses_plan_agent(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OPENCODE_READONLY_AGENT, OpenCodeBackend
        proc = _mock_popen(stdout=_oc_success_stream())
        ctx = self._ctx(tmp_path, null_logger, progress=False, extra={"readonly": True})
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc) as mock_popen:
            OpenCodeBackend().run(ctx)
        cmd = mock_popen.call_args.args[0]
        i = cmd.index("--agent")
        assert cmd[i + 1] == OPENCODE_READONLY_AGENT

    def test_routed_model_passed_through(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OpenCodeBackend
        proc = _mock_popen(stdout=_oc_success_stream())
        ctx = self._ctx(tmp_path, null_logger, progress=False, routed_model="opencode/mimo-v2.5-free")
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc) as mock_popen:
            OpenCodeBackend().run(ctx)
        cmd = mock_popen.call_args.args[0]
        i = cmd.index("-m")
        assert cmd[i + 1] == "opencode/mimo-v2.5-free"

    def test_no_model_flag_when_unrouted(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OpenCodeBackend
        proc = _mock_popen(stdout=_oc_success_stream())
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc) as mock_popen:
            OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert "-m" not in mock_popen.call_args.args[0]

    def test_timeout_kills_and_reports(self, tmp_path, null_logger):
        """Go 套餐额度耗尽等静默挂起场景：hard_timeout 兜底 kill。"""
        import subprocess as _sp
        from agent_go.backends.opencode_backend import OpenCodeBackend
        proc = _mock_popen(returncode=-9)
        proc.communicate.side_effect = [
            _sp.TimeoutExpired(cmd="opencode", timeout=5),
            ("", "killed"),
        ]
        ctx = self._ctx(tmp_path, null_logger, progress=False, hard_timeout=5)
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            res = OpenCodeBackend().run(ctx)
        proc.kill.assert_called_once()
        assert res.kill_reason == "hard_timeout"
        assert res.returncode == -9

    def test_opencode_not_installed(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OpenCodeBackend
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value=None):
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 127
        assert "opencode" in res.stderr

    def test_malformed_lines_tolerated(self, tmp_path, null_logger):
        from agent_go.backends.opencode_backend import OpenCodeBackend
        stream = "garbage line\n{broken json\n" + _oc_success_stream("OK")
        proc = _mock_popen(stdout=stream)
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 0
        assert res.stdout == "OK"

    def test_meter_event_written(self, tmp_path, null_logger):
        import json as _json
        from agent_go.backends.opencode_backend import OpenCodeBackend
        metering = tmp_path / "metering.jsonl"
        ctx = self._ctx(tmp_path, null_logger, progress=False,
                        config={"_metering_path": str(metering)},
                        routed_model="opencode/mimo-v2.5-free")
        proc = _mock_popen(stdout=_oc_success_stream())
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            OpenCodeBackend().run(ctx)
        events = [_json.loads(l) for l in metering.read_text().splitlines() if l.strip()]
        assert len(events) == 1
        ev = events[0]
        assert ev["virtual_model"] == "agentgo-worker-opencode"
        # 事件流不携带 model 信息，actual_model 取 routed_model
        assert ev["actual_model"] == "opencode/mimo-v2.5-free"
        assert ev["prompt_tokens"] == 310      # (100+10) + 200
        assert ev["completion_tokens"] == 70   # 50 + 20
        assert ev["cost_usd"] == pytest.approx(0.003)
        assert ev["result"] == "success"

    def test_tool_error_counted_but_result_returned(self, tmp_path, null_logger):
        """tool_use state.status=error 计数，非零退出码正常返回（验证失败走 retry）。"""
        from agent_go.backends.opencode_backend import OpenCodeBackend
        stream = _oc_ndjson(
            {"type": "tool_use", "sessionID": "s1",
             "part": {"type": "tool", "tool": "bash",
                      "state": {"status": "error", "input": {}}}},
            {"type": "text", "sessionID": "s1", "part": {"type": "text", "text": "partial"}},
            {"type": "step_finish", "sessionID": "s1",
             "part": {"reason": "stop", "cost": 0.0,
                      "tokens": {"input": 10, "output": 5, "cache": {"read": 0, "write": 0}}}},
        )
        proc = _mock_popen(stdout=stream, returncode=1, stderr="tool boom")
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 1
        assert res.stderr == "tool boom"

    def test_zero_work_exit_zero_maps_to_failure(self, tmp_path, null_logger):
        """退出 0 但零产出（无 tokens/工具调用/最终文本）必须显式失败。"""
        from agent_go.backends.opencode_backend import OpenCodeBackend
        proc = _mock_popen(stdout="", returncode=0)
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 1
        assert "zero output" in res.stderr

    def test_error_event_captured(self, tmp_path, null_logger):
        """防御性：事件流出现 error 事件时记录，但有真实产出不误判失败。"""
        from agent_go.backends.opencode_backend import OpenCodeBackend
        stream = _oc_ndjson(
            {"type": "error", "sessionID": "s1", "message": "transient upstream"},
        ) + _oc_success_stream("RECOVERED")
        proc = _mock_popen(stdout=stream, returncode=0)
        with patch("agent_go.backends.opencode_backend.shutil.which", return_value="/bin/opencode"), \
             patch("agent_go.backends.opencode_backend.subprocess.Popen", return_value=proc):
            res = OpenCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 0
        assert res.stdout == "RECOVERED"


def _zcode_success_json(final_text="DONE"):
    """构造一次成功的 zcode --json 单对象输出。"""
    import json as _json
    return _json.dumps({
        "sessionId": "sess-1",
        "traceId": "trace-1",
        "response": final_text,
        "usage": {"inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 10,
                  "cacheWriteTokens": 0, "totalTokens": 160},
        "eventCount": 12,
        "projection": {"turnCount": 1, "contextUsed": 160},
    }) + "\n"


class TestZCodeBackend:
    """B7：ZCodeBackend 命令构造、单 JSON 输出解析、超时与容错。"""

    def _ctx(self, tmp_path: Path, null_logger, **kw):
        defaults = dict(
            task_md="# 任务\n做点事", worktree=tmp_path, env={}, headless=True,
            sub_id="sub-1", task_id="task-1", logger=null_logger, config={},
        )
        defaults.update(kw)
        return BackendContext(**defaults)

    def _run(self, tmp_path, null_logger, proc, **kw):
        from agent_go.backends.zcode_backend import ZCodeBackend
        with patch("agent_go.backends.zcode_backend._zcode_command_prefix",
                   return_value=(["/bin/ZCode", "zcode.cjs"], "")), \
             patch("agent_go.backends.zcode_backend.subprocess.Popen", return_value=proc) as mock_popen:
            res = ZCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False, **kw))
        return res, mock_popen

    def test_command_and_parse_success(self, tmp_path, null_logger):
        res, mock_popen = self._run(tmp_path, null_logger,
                                    _mock_popen(stdout=_zcode_success_json("全部完成")))
        cmd = mock_popen.call_args.args[0]
        assert cmd[:2] == ["/bin/ZCode", "zcode.cjs"]
        assert "--json" in cmd and "--mode" in cmd
        i = cmd.index("--mode")
        assert cmd[i + 1] == "yolo"          # worker 写执行默认 yolo
        j = cmd.index("--cwd")
        assert cmd[j + 1] == str(tmp_path)
        k = cmd.index("--prompt")
        assert cmd[k + 1].startswith("# 任务")
        assert res.returncode == 0
        assert res.sandbox_type == "zcode"
        assert res.stdout == "全部完成"
        assert res.kill_reason is None
        # ELECTRON_RUN_AS_NODE 必须注入
        assert mock_popen.call_args.kwargs["env"]["ELECTRON_RUN_AS_NODE"] == "1"

    def test_readonly_uses_plan_mode(self, tmp_path, null_logger):
        res, mock_popen = self._run(tmp_path, null_logger,
                                    _mock_popen(stdout=_zcode_success_json()),
                                    extra={"readonly": True})
        cmd = mock_popen.call_args.args[0]
        i = cmd.index("--mode")
        assert cmd[i + 1] == "plan"
        assert res.returncode == 0

    def test_no_settings_flag_when_unrouted(self, tmp_path, null_logger):
        _, mock_popen = self._run(tmp_path, null_logger,
                                  _mock_popen(stdout=_zcode_success_json()))
        assert "--settings" not in mock_popen.call_args.args[0]

    def test_routed_model_mismatch_warns_but_proceeds(self, tmp_path, null_logger, caplog):
        """zcode 无 per-run 模型标志：routed_model 与配置不一致时 warning，按配置执行。"""
        import json as _json
        import agent_go.backends.zcode_backend as zb
        user_cfg = tmp_path / "zcode_user_config.json"
        user_cfg.write_text(_json.dumps({"provider": {"zai": {}}, "model": {"main": "zai/glm-5.2"}}))
        with patch.object(zb, "ZCODE_USER_CONFIG", user_cfg):
            with caplog.at_level(logging.WARNING, logger=null_logger.name):
                res, mock_popen = self._run(tmp_path, null_logger,
                                            _mock_popen(stdout=_zcode_success_json()),
                                            routed_model="glm-5.3-flash")
        cmd = mock_popen.call_args.args[0]
        assert "--settings" not in cmd
        assert res.returncode == 0
        assert any("不一致" in r.message for r in caplog.records)

    def test_configured_model_helper(self, tmp_path, null_logger):
        import json as _json
        import agent_go.backends.zcode_backend as zb
        user_cfg = tmp_path / "cfg.json"
        user_cfg.write_text(_json.dumps({"model": {"main": "zai/glm-5.3-flash"}}))
        with patch.object(zb, "ZCODE_USER_CONFIG", user_cfg):
            assert zb._configured_model(null_logger) == "zai/glm-5.3-flash"
        # 用户 config 缺失 → 空串（不中断）
        with patch.object(zb, "ZCODE_USER_CONFIG", tmp_path / "missing.json"):
            assert zb._configured_model(null_logger) == ""

    def test_timeout_kills_and_reports(self, tmp_path, null_logger):
        import subprocess as _sp
        proc = _mock_popen(returncode=-9)
        proc.communicate.side_effect = [
            _sp.TimeoutExpired(cmd="zcode", timeout=5),
            ("", "killed"),
        ]
        res, _ = self._run(tmp_path, null_logger, proc, hard_timeout=5)
        proc.kill.assert_called_once()
        assert res.kill_reason == "hard_timeout"
        assert res.returncode == -9

    def test_zcode_app_missing(self, tmp_path, null_logger):
        from agent_go.backends.zcode_backend import ZCodeBackend
        with patch("agent_go.backends.zcode_backend._zcode_command_prefix",
                   return_value=([], "ZCode.app not found")):
            res = ZCodeBackend().run(self._ctx(tmp_path, null_logger, progress=False))
        assert res.returncode == 127
        assert "ZCode.app" in res.stderr

    def test_output_with_leading_garbage_tolerated(self, tmp_path, null_logger):
        """stdout 混入非 JSON 前置行时，从首个 { 起截取解析。"""
        stream = "some warning line\n" + _zcode_success_json("OK")
        res, _ = self._run(tmp_path, null_logger, _mock_popen(stdout=stream))
        assert res.returncode == 0
        assert res.stdout == "OK"

    def test_meter_event_written(self, tmp_path, null_logger):
        import json as _json
        import agent_go.backends.zcode_backend as zb
        metering = tmp_path / "metering.jsonl"
        user_cfg = tmp_path / "cfg.json"
        user_cfg.write_text(_json.dumps({"model": {"main": "zai/glm-5.3-flash"}}))
        with patch.object(zb, "ZCODE_USER_CONFIG", user_cfg):
            res, _ = self._run(tmp_path, null_logger,
                               _mock_popen(stdout=_zcode_success_json()),
                               config={"_metering_path": str(metering)},
                               routed_model="glm-5.3-flash")
        assert res.returncode == 0
        events = [_json.loads(x) for x in metering.read_text().splitlines() if x.strip()]
        assert len(events) == 1
        ev = events[0]
        assert ev["virtual_model"] == "agentgo-worker-zcode"
        # 计量按 zcode 配置实际值（model.main），非 routed_model
        assert ev["actual_model"] == "zai/glm-5.3-flash"
        assert ev["prompt_tokens"] == 110      # 100 input + 10 cacheRead
        assert ev["completion_tokens"] == 50
        assert ev["cost_usd"] == 0.0           # Coding Plan 套餐计费，无 cost 字段
        assert ev["result"] == "success"

    def test_zero_work_exit_zero_maps_to_failure(self, tmp_path, null_logger):
        """退出 0 但零产出（无 tokens/最终文本）显式失败。"""
        res, _ = self._run(tmp_path, null_logger, _mock_popen(stdout="", returncode=0))
        assert res.returncode == 1
        assert "zero output" in res.stderr

    def test_turn_failure_returncode_passthrough(self, tmp_path, null_logger):
        """turn 失败（rc=1）正常透传，不在这里触发回退。"""
        res, _ = self._run(tmp_path, null_logger,
                           _mock_popen(stdout="", returncode=1, stderr="turn failed"))
        assert res.returncode == 1
        assert res.stderr == "turn failed"


class TestBackendRoutingB4:
    """B4：声明式路由 worker_backend_by_difficulty（按难度）/ worker_backend_by_type（按 agent_type）。

    优先级：subtask.backend > worker_backend > by_type > by_difficulty > agent_loop 自动 > claude。
    """

    def test_by_difficulty(self):
        cfg = {"worker_backend_by_difficulty": {"easy": "", "medium": "", "hard": "pi"}}
        assert resolve_backend_name(cfg, {"difficulty": "hard"}, True, False) == "pi"
        assert resolve_backend_name(cfg, {"difficulty": "medium"}, True, False) == "claude"

    def test_by_type(self):
        cfg = {"worker_backend_by_type": {"explore": "pi"}}
        assert resolve_backend_name(cfg, {"agent_type": "explore"}, True, False) == "pi"
        assert resolve_backend_name(cfg, {"agent_type": "developer"}, True, False) == "claude"

    def test_by_type_beats_by_difficulty(self):
        cfg = {
            "worker_backend_by_type": {"explore": "agent_loop"},
            "worker_backend_by_difficulty": {"hard": "pi"},
        }
        sub = {"agent_type": "explore", "difficulty": "hard"}
        assert resolve_backend_name(cfg, sub, True, False) == "agent_loop"

    def test_global_explicit_beats_by_type(self):
        cfg = {"worker_backend": "pi", "worker_backend_by_type": {"explore": "agent_loop"}}
        assert resolve_backend_name(cfg, {"agent_type": "explore"}, True, False) == "pi"

    def test_subtask_backend_beats_all(self):
        cfg = {"worker_backend": "pi", "worker_backend_by_type": {"explore": "pi"}}
        sub = {"backend": "claude", "agent_type": "explore"}
        assert resolve_backend_name(cfg, sub, True, False) == "claude"

    def test_routed_non_claude_requires_headless(self):
        cfg = {"worker_backend_by_difficulty": {"hard": "pi"}}
        assert resolve_backend_name(cfg, {"difficulty": "hard"}, False, False) == "claude"
        cfg2 = {"worker_backend_by_type": {"explore": "pi"}}
        assert resolve_backend_name(cfg2, {"agent_type": "explore"}, False, True) == "claude"

    def test_empty_routing_keeps_b1_behavior(self):
        cfg = {
            "agent_loop": {"enabled": True},
            "worker_backend_by_difficulty": {"easy": "", "medium": "", "hard": ""},
            "worker_backend_by_type": {},
        }
        assert resolve_backend_name(cfg, {"difficulty": "easy"}, True, True) == "agent_loop"
        assert resolve_backend_name(cfg, {"difficulty": "easy"}, True, False) == "claude"

    def test_repair_path_uses_difficulty_routing(self, tmp_path, null_logger):
        """run_repair 从 ctx 合成 subtask 视图，by_difficulty 对修复路径同样生效。"""
        calls = []

        class FakePi(BaseBackend):
            name = "pi"

            def run(self, ctx):
                calls.append("pi")
                return SubtaskResult(returncode=0, sandbox_type="pi")

        cfg = {"worker_backend_by_difficulty": {"hard": "pi"}}
        ctx = BackendContext(
            task_md="fix", worktree=tmp_path, env={}, headless=True,
            sub_id="sub-1-fix-1", logger=null_logger, config=cfg,
            difficulty="hard", agent_type="developer",
        )
        with patch("agent_go.backends.dispatch.BackendRegistry.get", return_value=FakePi):
            res = run_repair(ctx, is_simple=False)
        assert res.sandbox_type == "pi"
        assert calls == ["pi"]


class TestBackendPromo:
    """backend_promo 促销窗口路由（如 GLM flash 夜间免费仅 ZCode 本体）。

    优先级：显式声明（subtask/config/by_type/by_difficulty）> promo > agent_loop > claude。
    promo 额外要求：时间窗内 + headless + backend 本机可用。
    """

    _PROMO = {"backend": "zcode", "start": "2026-09-04", "end": "2026-09-20",
              "daily_start": "23:00", "daily_end": "09:00", "tz_offset": 8}

    def _now(self, iso: str):
        from datetime import datetime, timezone, timedelta
        return datetime.fromisoformat(iso).replace(tzinfo=timezone(timedelta(hours=8)))

    def test_time_active_cross_midnight(self):
        from agent_go.backends.registry import _promo_time_active
        # 跨午夜时段 23:00-09:00：23:30 / 02:00 / 08:59 在窗内，12:00 / 22:59 不在
        assert _promo_time_active(self._PROMO, self._now("2026-09-10T23:30:00"))
        assert _promo_time_active(self._PROMO, self._now("2026-09-10T02:00:00"))
        assert _promo_time_active(self._PROMO, self._now("2026-09-10T08:59:00"))
        assert not _promo_time_active(self._PROMO, self._now("2026-09-10T12:00:00"))
        assert not _promo_time_active(self._PROMO, self._now("2026-09-10T22:59:00"))

    def test_time_active_date_range(self):
        from agent_go.backends.registry import _promo_time_active
        # 日期闭区间 09-04 至 09-20
        assert _promo_time_active(self._PROMO, self._now("2026-09-04T23:30:00"))
        assert _promo_time_active(self._PROMO, self._now("2026-09-20T08:00:00"))
        assert not _promo_time_active(self._PROMO, self._now("2026-09-03T23:30:00"))
        assert not _promo_time_active(self._PROMO, self._now("2026-09-21T02:00:00"))

    def test_time_active_non_crossing_daily_window(self):
        from agent_go.backends.registry import _promo_time_active
        promo = {"daily_start": "09:00", "daily_end": "18:00", "tz_offset": 8}
        assert _promo_time_active(promo, self._now("2026-09-10T10:00:00"))
        assert not _promo_time_active(promo, self._now("2026-09-10T20:00:00"))

    def _resolve_with_promo(self, promo, headless=True, available=True, extra_cfg=None):
        """在固定窗口时间（09-10 23:30）下解析 backend。"""
        cfg = {"backend_promo": promo}
        if extra_cfg:
            cfg.update(extra_cfg)

        class FakeZcode(BaseBackend):
            name = "zcode"

            def run(self, ctx):
                return SubtaskResult(returncode=0)

        FakeZcode.available = classmethod(lambda cls: available)
        with patch("agent_go.backends.registry._promo_time_active", return_value=True), \
             patch("agent_go.backends.registry.BackendRegistry.get", return_value=FakeZcode):
            return resolve_backend_name(cfg, {}, headless, False)

    def test_promo_active_routes_to_backend(self):
        assert self._resolve_with_promo(self._PROMO) == "zcode"

    def test_promo_requires_headless(self):
        assert self._resolve_with_promo(self._PROMO, headless=False) == "claude"

    def test_promo_backend_unavailable_falls_through(self):
        assert self._resolve_with_promo(self._PROMO, available=False) == "claude"

    def test_explicit_beats_promo(self):
        assert self._resolve_with_promo(self._PROMO, extra_cfg={"worker_backend": "pi"}) == "pi"

    def test_promo_inactive_keeps_default(self):
        with patch("agent_go.backends.registry._promo_time_active", return_value=False):
            assert resolve_backend_name({"backend_promo": self._PROMO}, {}, True, False) == "claude"

    def test_empty_promo_no_change(self):
        assert resolve_backend_name({"backend_promo": {}}, {}, True, False) == "claude"

    def test_promo_claude_backend_allowed(self):
        """promo 指定 claude 时也允许（窗口内收敛到默认路径的显式表达）。"""
        promo = dict(self._PROMO, backend="claude")
        with patch("agent_go.backends.registry._promo_time_active", return_value=True):
            assert resolve_backend_name({"backend_promo": promo}, {}, False, False) == "claude"
