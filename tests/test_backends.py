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
