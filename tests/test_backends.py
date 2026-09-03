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
