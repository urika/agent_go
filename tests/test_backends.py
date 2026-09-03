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
    resolve_backend_name,
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
