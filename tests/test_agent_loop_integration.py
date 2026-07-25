"""测试 executor.py 的 agent_loop 混合策略分支（方案 C 简单任务路径）。

覆盖 executor.py:862-893 — 当 config.agent_loop.enabled=True 且子任务被判定为"简单"时，
绕过 claude -p，直接走 AgentLoop.run()（直接 API + 工具执行）。

AgentLoop 本身已在 test_agent_loop.py 单测；本文件聚焦 executor 与 AgentLoop 的
胶水层：配置开关、路由解析、sandbox_type 标记、失败原因收集。
"""

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.executor import run_subtask, _is_simple_task


# ═══════════════════════════════════════════════════════════════
# 共享 fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Test", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    d = tmp_path / ".agent_go" / "task-loop"
    d.mkdir(parents=True)
    return d


def _mock_cp(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _simple_subtask():
    """不含探索性关键词、依赖数 ≤ 2 的简单任务（_is_simple_task 返回 True）。"""
    return {
        "id": "sub-1", "title": "简单修改",
        "description": "修改 main.py",
        "agent_prompt": "请修改 main.py 中的 print 语句",
        "verification": "", "risks": [],
        "depends_on": [], "skills": [],
        "agent_type": "developer",
    }


def _complex_subtask():
    """含探索性关键词（重构），_is_simple_task 返回 False。"""
    return {
        **_simple_subtask(),
        "id": "sub-2",
        "title": "重构模块",
        "agent_prompt": "请重构整个认证模块",
    }


# ═══════════════════════════════════════════════════════════════
# _is_simple_task 补强（覆盖 working-tree 新增规则，已有主测在 test_executor.py）
# ═══════════════════════════════════════════════════════════════

class TestIsSimpleTaskNewRules:
    """working-tree 版 _is_simple_task 新增的两条判定规则。

    实现（executor.py:746-770）依次检查：
      1. agent_type ∈ {architect, reviewer} → 复杂
      2. 关键词（探索/调研/重构/迁移/refactor/migrate/explore）→ 复杂
      3. files_hint 含 >1 个 ** 通配符 → 复杂
      4. depends_on > 2 → 复杂
    """

    def test_architect_agent_type_is_complex(self):
        """规则 1：architect agent_type 直接判复杂"""
        assert _is_simple_task({"agent_type": "architect"}) is False

    def test_reviewer_agent_type_is_complex(self):
        """规则 1：reviewer agent_type 直接判复杂"""
        assert _is_simple_task({"agent_type": "reviewer"}) is False

    def test_developer_agent_type_not_auto_complex(self):
        """developer/tester 不触发规则 1，仍按其他规则判定"""
        assert _is_simple_task({"agent_type": "developer"}) is True
        assert _is_simple_task({"agent_type": "tester"}) is True

    def test_files_hint_many_wildcards_complex(self):
        """规则 3：files_hint 含 >1 个 ** → 涉及文件多 → 复杂"""
        assert _is_simple_task({"files_hint": "src/**/*.py test/**/*.py"}) is False

    def test_files_hint_single_wildcard_still_simple(self):
        """规则 3 边界：仅 1 个 ** 仍属简单"""
        assert _is_simple_task({"files_hint": "src/**/*.py"}) is True

    def test_files_hint_no_wildcard_simple(self):
        assert _is_simple_task({"files_hint": "src/main.py"}) is True

    def test_explore_keyword_in_description(self):
        """规则 2：explore 关键词触发复杂"""
        assert _is_simple_task({"description": "explore the codebase"}) is False

    def test_refactor_keyword_in_description(self):
        assert _is_simple_task({"description": "refactor module"}) is False

    def test_exactly_two_dependencies_still_simple(self):
        """规则 4 边界：depends_on 长度等于 2 仍属简单（> 2 才复杂）"""
        assert _is_simple_task({"depends_on": ["a", "b"]}) is True

    def test_three_dependencies_is_complex(self):
        assert _is_simple_task({"depends_on": ["a", "b", "c"]}) is False


# ═══════════════════════════════════════════════════════════════
# executor.py:862-893 agent_loop 分支
# ═══════════════════════════════════════════════════════════════

class TestAgentLoopBranchEnabled:
    """agent_loop.enabled=True 时的分支选择。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_simple_task_uses_agent_loop(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """简单任务 + agent_loop.enabled + headless → 走 AgentLoop，不走 _run_headless"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        # AgentLoop.run 返回 CompletedProcess（兼容现有接口）
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=0)
        mock_loop_cls.return_value = mock_loop_inst

        config = {"agent_loop": {"enabled": True, "max_turns": 5}}

        run_subtask("test-task", _simple_subtask(), temp_repo, task_dir, logger,
                    headless=True, config=config)

        mock_loop_cls.assert_called_once()
        mock_loop_inst.run.assert_called_once()
        mock_headless.assert_not_called()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_complex_task_falls_back_to_headless(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """复杂任务（探索性关键词）即使 agent_loop.enabled=True 也走 _run_headless"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)

        config = {"agent_loop": {"enabled": True}}

        run_subtask("test-task", _complex_subtask(), temp_repo, task_dir, logger,
                    headless=True, config=config)

        mock_loop_cls.assert_not_called()
        mock_headless.assert_called_once()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_disabled_by_default(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """agent_loop.enabled 缺省/False → 永远走 _run_headless（向后兼容）"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)

        run_subtask("test-task", _simple_subtask(), temp_repo, task_dir, logger,
                    headless=True, config={})

        mock_loop_cls.assert_not_called()
        mock_headless.assert_called_once()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_interactive_mode_skips_agent_loop(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """headless=False（交互模式）即使 enabled 也不走 agent_loop"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)

        config = {"agent_loop": {"enabled": True}}

        with patch("shutil.which", return_value=None):
            run_subtask("test-task", _simple_subtask(), temp_repo, task_dir, logger,
                        headless=False, config=config)

        mock_loop_cls.assert_not_called()


class TestAgentLoopRoutingResolution:
    """agent_loop 分支内的路由解析（resolve_provider → plan_api fallback）。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.router.resolve_provider")
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_uses_router_primary_when_route_resolved(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_resolve, mock_agent, temp_repo, task_dir, logger,
    ):
        """resolve_provider 返回非 None → 用 route.primary 作为 pc"""
        from agent_go.router import ProviderConfig, RoleRoute
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        primary = ProviderConfig(provider="anthropic", base_url="http://x", model="worker-m")
        mock_resolve.return_value = RoleRoute(role="worker", primary=primary)
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=0)
        mock_loop_cls.return_value = mock_loop_inst

        config = {"agent_loop": {"enabled": True}}
        run_subtask("t", _simple_subtask(), temp_repo, task_dir, logger,
                    headless=True, config=config)

        pc = mock_loop_inst.run.call_args.kwargs["pc"]
        assert pc is primary

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.router.resolve_provider", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_falls_back_to_plan_api_when_router_unavailable(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_resolve, mock_agent, temp_repo, task_dir, logger,
    ):
        """resolve_provider 返回 None → 用 plan_api 构建 ProviderConfig"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=0)
        mock_loop_cls.return_value = mock_loop_inst

        config = {
            "agent_loop": {"enabled": True},
            "plan_api": {
                "provider": "openai",
                "base_url": "http://plan-api/v1/chat",
                "model": "plan-m",
            },
        }
        run_subtask("t", _simple_subtask(), temp_repo, task_dir, logger,
                    headless=True, config=config)

        pc = mock_loop_inst.run.call_args.kwargs["pc"]
        assert pc.provider == "openai"
        assert pc.base_url == "http://plan-api/v1/chat"
        assert pc.model == "plan-m"


class TestAgentLoopResultPropagation:
    """AgentLoop.run 的返回值如何传播到 run_subtask 的结果。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_success_marks_agent_loop_sandbox(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """AgentLoop 返回 exit 0 → status=completed/no_changes，sandbox_type=agent_loop

        subprocess.run 被 mock 返回空 stdout，git status 无变更 → 'no_changes'。
        关键断言是 sandbox_type 标记与状态非 failed。
        """
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=0)
        mock_loop_cls.return_value = mock_loop_inst

        config = {"agent_loop": {"enabled": True}}
        result = run_subtask("t", _simple_subtask(), temp_repo, task_dir, logger,
                             headless=True, config=config)

        assert result["status"] in ("completed", "no_changes")
        assert result["sandbox_type"] == "agent_loop"
        assert result["failure_reason"] == ""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_loop_exit_nonzero_status_failed(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """AgentLoop 达到 max_turns（exit 1）→ status=failed

        sandbox_type='agent_loop' 走"交互未正常完成"分支（非 headless 分支）。
        """
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=1)
        mock_loop_cls.return_value = mock_loop_inst

        config = {"agent_loop": {"enabled": True}}
        result = run_subtask("t", _simple_subtask(), temp_repo, task_dir, logger,
                             headless=True, config=config)

        assert result["status"] == "failed"
        assert result["sandbox_type"] == "agent_loop"
        # sandbox_type != "headless" → 用"交互未正常完成"措辞
        assert "未正常完成" in result["failure_reason"]


class TestAgentLoopMeteringPassthrough:
    """config 通过 _metering_path 传给 AgentLoop，由其内部写 worker 计量。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.agent_loop.AgentLoop")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_config_passed_to_loop_intact(
        self, mock_wt, mock_subprocess, mock_headless, mock_loop_cls,
        mock_agent, temp_repo, task_dir, logger,
    ):
        """完整 config 传给 AgentLoop.run，AgentLoop 内部读 _metering_path/max_turns"""
        mock_wt.return_value = (True, "")
        mock_subprocess.return_value = _mock_cp()
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_loop_inst = MagicMock()
        mock_loop_inst.run.return_value = _mock_cp(returncode=0)
        mock_loop_cls.return_value = mock_loop_inst

        config = {
            "agent_loop": {"enabled": True, "max_turns": 7},
            "_metering_path": "/tmp/agent/metering.jsonl",
        }
        run_subtask("t", _simple_subtask(), temp_repo, task_dir, logger,
                    headless=True, config=config)

        passed_config = mock_loop_inst.run.call_args.kwargs["config"]
        assert passed_config is config  # 原始 config 对象透传
