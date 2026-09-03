"""测试 goal_injector.py — Stop Hook 注入（设计稿 §3.4）

覆盖: condition 生成、settings.json/verify-goal.sh 写入、
白名单拒绝（不安全命令不写入）、cleanup、executor 集成调用点。
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.goal_injector import GoalInjector
from agent_go.executor import run_subtask


class TestBuildGoalCondition:
    def test_from_commands(self):
        cond = GoalInjector.build_goal_condition(["pytest tests/", "ruff check"])
        assert cond == "以下验证命令全部退出码为0: pytest tests/ && ruff check"

    def test_custom_condition_passthrough(self):
        cond = GoalInjector.build_goal_condition(["pytest"], custom_condition="自定义条件")
        assert cond == "自定义条件"


class TestInject:
    def test_writes_settings_and_script(self, tmp_path):
        wt = tmp_path / "work"
        wt.mkdir()
        ok = GoalInjector.inject(wt, ["pytest tests/"])
        assert ok is True

        settings = json.loads((wt / GoalInjector.GOAL_SETTINGS_FILE).read_text(encoding="utf-8"))
        stop_hook = settings["hooks"]["Stop"][0]
        assert stop_hook["hooks"][0]["command"] == "scripts/verify-goal.sh"
        assert stop_hook["hooks"][0]["type"] == "command"

        script = wt / GoalInjector.GOAL_HOOK_SCRIPT
        content = script.read_text(encoding="utf-8")
        assert "set -e" in content
        assert "pytest tests/" in content
        # 可执行权限
        assert script.stat().st_mode & stat.S_IXUSR

    def test_unsafe_command_rejected(self, tmp_path):
        """未过白名单的命令不写入脚本"""
        wt = tmp_path / "work"
        wt.mkdir()
        ok = GoalInjector.inject(wt, ["rm -rf /tmp/x"])
        assert ok is False
        assert not (wt / GoalInjector.GOAL_SETTINGS_FILE).exists()

    def test_mixed_commands_keep_safe_only(self, tmp_path):
        wt = tmp_path / "work"
        wt.mkdir()
        ok = GoalInjector.inject(wt, ["pytest tests/", "rm -rf /tmp/x"])
        assert ok is True
        content = (wt / GoalInjector.GOAL_HOOK_SCRIPT).read_text(encoding="utf-8")
        assert "pytest tests/" in content
        assert "rm -rf" not in content

    def test_cleanup(self, tmp_path):
        wt = tmp_path / "work"
        wt.mkdir()
        GoalInjector.inject(wt, ["pytest tests/"])
        GoalInjector.cleanup(wt)
        assert not (wt / GoalInjector.GOAL_SETTINGS_FILE).exists()
        assert not (wt / GoalInjector.GOAL_HOOK_SCRIPT).exists()


# ═══════════════════════════════════════════════════════════════
# executor 集成：TASK.md /goal 注入 + Stop Hook 调用点
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
    d = tmp_path / ".agent_go" / "task-goal"
    d.mkdir(parents=True)
    return d


def _mock_cp(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _subtask():
    return {
        "id": "sub-1", "title": "goal 测试", "description": "d",
        "agent_prompt": "do work", "verification": "pytest tests/",
        "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
    }


def _git_ok(args, **kwargs):
    cmd_str = " ".join(args) if isinstance(args, list) else str(args)
    if "status" in cmd_str and "--porcelain" in cmd_str:
        return _mock_cp(stdout="M  src/main.py\n")
    if "diff" in cmd_str and "--stat" in cmd_str:
        return _mock_cp(stdout="src/main.py | 2 +-")
    if "pytest" in cmd_str:
        return _mock_cp(returncode=0, stdout="1 passed")
    return _mock_cp()


class TestExecutorGoalIntegration:
    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.backends.claude_backend._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_goal_injected_when_enabled(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """goal.enabled=true：TASK.md 含 /goal 段落和字面 condition"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_ok

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True, config={"goal": {"enabled": True}})

        task_md = mock_headless.call_args_list[0][0][0]
        assert task_md.startswith('/goal "')
        assert "Goal Context" in task_md

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.backends.claude_backend._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_goal_prefix_degrades_when_prompt_too_long(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """TASK.md 超 4000 字符（Claude /goal 上限）时降级：去掉 /goal 前缀、保留 Goal Context。

        实测复现（2026-08-12 弱模型困难任务实验）：force 模式下 json 任务 TASK.md
        6553 字节 ≈4423 字符，Claude CLI 把整个 prompt 当作 goal condition 并拒绝
        （"Goal condition is limited to 4000 characters"），exit 0 零产出假失败。
        """
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_ok

        long_task = _subtask()
        long_task["agent_prompt"] = "详细说明。" * 900  # 使总 prompt > 3800 字符
        run_subtask("test-task", long_task, temp_repo, task_dir,
                    logger, headless=True, config={"goal": {"enabled": True}})

        task_md = mock_headless.call_args_list[0][0][0]
        assert not task_md.startswith('/goal "')
        assert "Goal Context" in task_md
        assert "详细说明" in task_md

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.backends.claude_backend._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_goal_not_injected_by_default(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """goal.enabled 默认 false：TASK.md 不含 /goal 段落"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_ok

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True, config={"verification": {}})

        task_md = mock_headless.call_args_list[0][0][0]
        assert "/goal" not in task_md

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.backends.claude_backend._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_stop_hook_injected_when_enabled(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """goal.enable_goal_hook=true：worktree 内写入 Hook 文件，subtask 结束后自动清理"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_ok

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True,
                    config={"goal": {"enable_goal_hook": True}})

        worktree = task_dir / "sub-1" / "work"
        from agent_go.goal_injector import GoalInjector
        # cleanup 已执行：Hook 文件应被移除/恢复
        assert not (worktree / GoalInjector.GOAL_SETTINGS_FILE).exists()
        assert not (worktree / GoalInjector.GOAL_HOOK_SCRIPT).exists()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.backends.claude_backend._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_stop_hook_not_injected_by_default(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """默认不注入 Stop Hook"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_ok

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True, config={"verification": {}})

        worktree = task_dir / "sub-1" / "work"
        from agent_go.goal_injector import GoalInjector
        assert not (worktree / GoalInjector.GOAL_SETTINGS_FILE).exists()
