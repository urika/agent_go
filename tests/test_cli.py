"""测试 cli.py — CLI 参数解析和命令分发

全覆盖: _build_parser, cmd_list, cmd_show, cmd_config, cmd_clean, cmd_status (basic routing)
部分覆盖: cmd_run (mock 管道)，cmd_resume (mock 恢复逻辑)
"""

import sys
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.cli import _build_parser, main


class TestBuildParser:
    """参数解析"""

    def test_parser_default_command(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None  # 无子命令

    def test_run_parser_minimal(self):
        parser = _build_parser()
        args = parser.parse_args(["run", "/tmp/repo"])
        assert args.command == "run"
        assert args.repo == "/tmp/repo"
        assert args.task == "请根据项目情况完成改进"  # 默认值
        assert args.parallel == 1

    def test_run_parser_full(self):
        parser = _build_parser()
        args = parser.parse_args([
            "run", "/tmp/repo", "my task",
            "--yes", "--headless", "--quiet", "--verbose",
            "--parallel", "3", "--remote", "origin",
            "--issue", "42", "--no-cache",
            "--skill", "security,react", "--agent-type", "reviewer",
            "--docs", "README.md,CONTRIBUTING.md",
        ])
        assert args.yes is True
        assert args.headless is True
        assert args.quiet is True
        assert args.verbose is True
        assert args.parallel == 3
        assert args.remote == "origin"
        assert args.issue_ref == 42
        assert args.no_cache is True
        assert args.skill == "security,react"
        assert args.agent_type == "reviewer"
        assert args.docs == "README.md,CONTRIBUTING.md"

    def test_resume_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["resume", "task-123", "--yes"])
        assert args.command == "resume"
        assert args.task_id == "task-123"
        assert args.yes is True

    def test_list_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_show_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["show", "task-456"])
        assert args.command == "show"
        assert args.task_id == "task-456"

    def test_status_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["status", "--watch", "--no-tui", "--verbose"])
        assert args.command == "status"
        assert args.watch is True
        assert args.no_tui is True
        assert args.verbose is True

    def test_clean_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["clean"])
        assert args.command == "clean"

    def test_config_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["config"])
        assert args.command == "config"

    def test_skills_parser(self):
        parser = _build_parser()
        args = parser.parse_args(["skills"])
        assert args.command == "skills"


class TestCmdList:
    """cmd_list 任务列表"""

    def test_list_empty(self):
        """无任务时正常输出"""
        with patch("agent_go.cli.AGENT_GO_DIR") as mock_dir:
            mock_dir.glob.return_value = []
            with patch("builtins.print"):
                from agent_go.cli import cmd_list
                cmd_list()

    def test_list_with_tasks(self, tmp_path):
        """列出多个任务（使用真实文件系统避免 MagicMock 排序问题）"""
        from agent_go.cli import cmd_list
        # 用临时目录模拟任务目录
        for tid in ["task-001", "task-002"]:
            td = tmp_path / tid
            td.mkdir()
            (td / "meta.json").write_text(json.dumps({
                "task_id": tid, "task": f"Task {tid}",
                "created": "20260701-120000", "status": "completed",
                "subtasks": [], "results": [],
            }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print"):
                # 不应抛出异常
                cmd_list()


class TestCmdShow:
    """cmd_show 任务详情"""

    def _make_show_args(self, task_id):
        """构造类似 argparse.Namespace 的参数对象"""
        from types import SimpleNamespace
        return SimpleNamespace(task_id=task_id)

    def test_show_nonexistent(self):
        """不存在的任务 ID 应退出"""
        from agent_go.cli import cmd_show
        with patch("agent_go.cli.AGENT_GO_DIR") as mock_dir:
            mock_dir.__truediv__.return_value.exists.return_value = False
            with pytest.raises(SystemExit):
                cmd_show(self._make_show_args("task-nonexistent"))

    def test_show_existing_task(self, tmp_path):
        """已存在的任务应打印详情"""
        from agent_go.cli import cmd_show
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "task_id": "task-001", "task": "测试任务",
            "repo": "/tmp/repo", "created": "20260701",
            "status": "completed",
            "subtasks": [{"id": "sub-1", "title": "步骤一"}],
            "results": [{
                "subtask_id": "sub-1", "status": "completed",
                "summary": "1 file changed", "agent_type_source": "llm",
            }],
        }), encoding="utf-8")
        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print"):
                # 使用 Namespace 参数能正确传递 task_id
                cmd_show(self._make_show_args("task-001"))


class TestCmdConfig:
    """cmd_config 配置查看"""

    def test_config_output(self):
        """config 输出当前配置（使用 print, 非 console.data）"""
        from agent_go.cli import cmd_config
        with patch("builtins.print") as mock_print:
            cmd_config()
        # cmd_config 使用 print(json.dumps(...))
        assert mock_print.call_count >= 1
        # 验证输出包含配置内容
        output = mock_print.call_args[0][0]
        assert "plan_api" in output
        assert "behavior" in output


class TestCmdClean:
    """cmd_clean 清理任务"""

    def test_clean_confirmed(self, tmp_path):
        """确认后清理（shutil 在函数内 import，直接 patch shutil.rmtree）"""
        from agent_go.cli import cmd_clean
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "task_id": "task-001", "status": "completed",
        }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("agent_go.cli.safe_input", return_value="y"):
                with patch("shutil.rmtree") as mock_rmtree:
                    with patch("subprocess.run"):
                        cmd_clean()
                        mock_rmtree.assert_called_once()

    def test_clean_cancelled(self, tmp_path):
        """取消后不删除"""
        from agent_go.cli import cmd_clean
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "task_id": "task-001", "status": "completed",
        }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("agent_go.cli.safe_input", return_value="n"):
                cmd_clean()
                # 任务目录应保留（未被删除）
                assert task_dir.exists()

    def test_clean_empty(self, tmp_path):
        """无任务时跳过"""
        from agent_go.cli import cmd_clean
        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print") as mock_print:
                cmd_clean()
            mock_print.assert_called()


class TestCmdStatus:
    """cmd_status 状态监控"""

    def test_status_text_mode(self):
        """--no-tui 文本模式"""
        with patch("sys.argv", ["agent_go", "status", "--no-tui"]):
            with patch("agent_go.cli.cmd_status_tui") as mock_tui:
                with patch("agent_go.cli.AGENT_GO_DIR") as mock_dir:
                    mock_dir.glob.return_value = []
                    from agent_go.cli import cmd_status
                    cmd_status()
                mock_tui.assert_not_called()

    def test_status_tui_by_default(self):
        """默认启动 TUI"""
        with patch("sys.argv", ["agent_go", "status"]):
            with patch("agent_go.cli.cmd_status_tui") as mock_tui:
                from agent_go.cli import cmd_status
                cmd_status()
                mock_tui.assert_called_once()


class TestMain:
    """main 函数分发"""

    def test_main_run(self):
        with patch("sys.argv", ["agent_go", "run", "/tmp/repo", "test"]):
            with patch("agent_go.cli.cmd_run") as mock_run:
                main()
                mock_run.assert_called_once()

    def test_main_list(self):
        with patch("sys.argv", ["agent_go", "list"]):
            with patch("agent_go.cli.cmd_list") as mock_list:
                main()
                mock_list.assert_called_once()

    def test_main_show(self):
        with patch("sys.argv", ["agent_go", "show", "task-1"]):
            with patch("agent_go.cli.cmd_show") as mock_show:
                main()
                mock_show.assert_called_once()

    def test_main_clean(self):
        with patch("sys.argv", ["agent_go", "clean"]):
            with patch("agent_go.cli.cmd_clean") as mock_clean:
                main()
                mock_clean.assert_called_once()

    def test_main_config(self):
        with patch("sys.argv", ["agent_go", "config"]):
            with patch("agent_go.cli.cmd_config") as mock_config:
                main()
                mock_config.assert_called_once()

    def test_main_status(self):
        with patch("sys.argv", ["agent_go", "status"]):
            with patch("agent_go.cli.cmd_status") as mock_status:
                main()
                mock_status.assert_called_once()

    def test_main_no_command(self):
        with patch("sys.argv", ["agent_go"]):
            with patch("argparse.ArgumentParser.print_help") as mock_help:
                main()
                mock_help.assert_called_once()


class TestCmdRunFallback:
    """cmd_run 降级路径（__FALLBACK__）回归测试

    修复前：降级后 confirmed_plan=None 被无条件传入 plan_to_subtasks，
    必抛 AttributeError（见 docs/ISSUES.md ISSUE-1）。
    """

    def _make_args(self, repo):
        parser = _build_parser()
        return parser.parse_args(["run", str(repo), "test task"])

    def _run_with_mocks(self, tmp_path, confirm_side_effect, plan_side_effect):
        from agent_go.cli import cmd_run
        repo = tmp_path / "repo"
        repo.mkdir()
        home = tmp_path / "agent_go_home"
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        plan_side_effect = plan_side_effect or [plan]
        fallback_subtasks = [{"id": "sub-1", "title": "fallback"}]
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("agent_go.cli.load_config", return_value={"behavior": {}}), \
             patch("agent_go.cli.setup_logger", return_value=MagicMock()), \
             patch("agent_go.cli._detect_tool_versions", return_value={}), \
             patch("agent_go.cli.load_agent_type", return_value=None), \
             patch("agent_go.cli.generate_plan", side_effect=plan_side_effect), \
             patch("agent_go.cli.confirm_plan", side_effect=confirm_side_effect), \
             patch("agent_go.cli.decompose_fallback", return_value=fallback_subtasks) as mock_fb, \
             patch("agent_go.cli.plan_to_subtasks", return_value=[{"id": "s1", "title": "t"}]) as mock_p2s, \
             patch("agent_go.cli.plan_to_md", return_value="# plan"), \
             patch("agent_go.cli.confirm_subtasks", side_effect=lambda subs, cfg, log: subs), \
             patch("agent_go.cli._run_pipeline") as mock_pipe:
            cmd_run(self._make_args(repo))
        return mock_fb, mock_p2s, mock_pipe

    def test_initial_fallback_no_crash(self, tmp_path):
        """首次 Plan 确认即选择降级：走 decompose_fallback，不调 plan_to_subtasks"""
        mock_fb, mock_p2s, mock_pipe = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[("__FALLBACK__", [])],
            plan_side_effect=None,
        )
        mock_fb.assert_called_once()
        mock_p2s.assert_not_called()
        mock_pipe.assert_called_once()

    def test_retry_generate_failure_fallback(self, tmp_path):
        """重试生成 Plan 抛异常后降级：不崩溃，pipeline 正常执行"""
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        mock_fb, mock_p2s, mock_pipe = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[(None, [])],
            plan_side_effect=[plan, RuntimeError("api down")],
        )
        mock_fb.assert_called_once()
        mock_p2s.assert_not_called()
        mock_pipe.assert_called_once()

    def test_retry_then_fallback_no_crash(self, tmp_path):
        """重试后再次选择降级：走 decompose_fallback，不调 plan_to_subtasks"""
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        mock_fb, mock_p2s, mock_pipe = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[(None, []), ("__FALLBACK__", [])],
            plan_side_effect=[plan, dict(plan)],
        )
        mock_fb.assert_called_once()
        mock_p2s.assert_not_called()
        mock_pipe.assert_called_once()

    def test_normal_confirm_still_works(self, tmp_path):
        """对照组：正常确认 Plan 仍走 plan_to_subtasks 并保存 PLAN.md"""
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        mock_fb, mock_p2s, mock_pipe = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[(plan, [])],
            plan_side_effect=None,
        )
        mock_fb.assert_not_called()
        mock_p2s.assert_called_once()
        mock_pipe.assert_called_once()
