"""测试 cli.py — CLI 参数解析和命令分发

全覆盖: _build_parser, cmd_list, cmd_show, cmd_config, cmd_clean, cmd_status (basic routing),
        cmd_resume (mock 管道/配置/logger)，cmd_inspect (真实临时任务目录)
部分覆盖: cmd_run (mock 管道)
"""

import sys
import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.cli import _build_parser, main
from agent_go.config import DEFAULT_CONFIG


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

    def test_run_parser_baseline_flags(self):
        parser = _build_parser()
        args = parser.parse_args(["run", "/tmp/repo", "task", "--allow-dirty"])
        assert args.allow_dirty is True
        assert args.baseline is False

        args = parser.parse_args(["run", "/tmp/repo", "task", "--baseline"])
        assert args.baseline is True
        assert args.allow_dirty is False

        # 默认两者皆 False
        args = parser.parse_args(["run", "/tmp/repo", "task"])
        assert args.allow_dirty is False
        assert args.baseline is False

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

    def test_eval_bench_source_batch_parser(self):
        """S10-P1：eval bench 支持 --source-batch 批次标识。"""
        parser = _build_parser()
        args = parser.parse_args([
            "eval", "bench", "--tasks", "eval_suite",
            "--candidate-models", "claude-haiku-4-5",
            "--repeat", "1", "--source-batch", "smoke-20260801",
        ])
        assert args.command == "eval"
        assert args.source_batch == "smoke-20260801"

    def test_eval_bench_source_batch_default_empty(self):
        """未指定 --source-batch 时默认空串。"""
        parser = _build_parser()
        args = parser.parse_args(["eval", "bench"])
        assert getattr(args, "source_batch", "") == ""


class TestPlanPreflightRepair:
    def test_repairs_invalid_verification_before_execution(self, tmp_path):
        from agent_go.cli import _preflight_repair_plan

        task_dir = tmp_path / "task"
        task_dir.mkdir()
        initial = {
            "overview": "test",
            "steps": [{"id": "s1", "title": "test", "verification": "grep OK out.txt"}],
        }
        repaired = {
            "overview": "test",
            "steps": [{"id": "s1", "title": "test", "verification": "pytest tests -q"}],
        }
        config = {
            "behavior": {"plan_preflight_repair_enabled": True, "max_plan_repairs": 1},
            "skills": {"auto_discover": False},
        }
        with patch("agent_go.cli.generate_plan", return_value=repaired) as mock_generate:
            result, iteration, history, quality = _preflight_repair_plan(
                initial,
                task="test task",
                repo=tmp_path,
                config=config,
                logger=MagicMock(),
                task_dir=task_dir,
                skill_plan_context="",
                spec_context="",
                initial_docs="",
                iteration=1,
            )

        assert result == repaired
        assert iteration == 2
        assert len(history) == 1
        assert quality["status"] == "passed"
        assert "Plan 预检修复反馈" in mock_generate.call_args.args[4]


class TestCmdRunBaseline:
    """A3 未提交基线处理：cmd_run 启动时的 dirty 检测与处置。"""

    @pytest.fixture(autouse=True)
    def _restore_console(self):
        # cmd_run 会 set_default_console(quiet)（headless 隐含 quiet），
        # 泄漏全局默认 console 会抑制后续测试的 console.print 输出 → 保存/恢复。
        from agent_go.console import get_default_console, set_default_console
        prev = get_default_console()
        yield
        set_default_console(prev)

    def _make_dirty_repo(self, tmp_path):
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        def g(*a):
            return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)
        assert g("init", "-b", "main").returncode == 0
        (repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
        g("add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        # 制造未提交改动
        (repo / "a.py").write_text("print('changed')\n", encoding="utf-8")
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        return repo

    def _run_args(self, repo, *extra):
        parser = _build_parser()
        return parser.parse_args(["run", str(repo), "task", *extra])

    def test_headless_dirty_aborts(self, tmp_path):
        from agent_go.cli import cmd_run
        repo = self._make_dirty_repo(tmp_path)
        with patch("agent_go.cli.generate_plan") as mock_gen:
            with pytest.raises(SystemExit) as exc:
                cmd_run(self._run_args(repo, "--yes"))
        assert exc.value.code == 1  # EX_ERROR fail-safe
        mock_gen.assert_not_called()  # 未进入 Plan 生成

    def test_headless_allow_dirty_continues(self, tmp_path):
        from agent_go.cli import cmd_run
        repo = self._make_dirty_repo(tmp_path)
        # --allow-dirty 越过早退；后续在 load_config 处停下以避免跑完整 pipeline
        with patch("agent_go.cli.generate_plan") as mock_gen, \
             patch("agent_go.cli.load_config", side_effect=RuntimeError("stop-after-hook")):
            with pytest.raises(RuntimeError, match="stop-after-hook"):
                cmd_run(self._run_args(repo, "--yes", "--allow-dirty"))
        mock_gen.assert_not_called()  # 停在 hook 之后、plan 之前

    def test_headless_baseline_commits(self, tmp_path):
        import subprocess
        from agent_go.cli import cmd_run
        repo = self._make_dirty_repo(tmp_path)
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                     capture_output=True, text=True).stdout.strip()
        with patch("agent_go.cli.load_config", side_effect=RuntimeError("stop-after-hook")):
            with pytest.raises(RuntimeError, match="stop-after-hook"):
                cmd_run(self._run_args(repo, "--yes", "--baseline"))
        # 未提交改动已被 commit 为基线：工作区 clean，HEAD 前进
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                                capture_output=True, text=True).stdout.strip()
        assert status == ""
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                    capture_output=True, text=True).stdout.strip()
        assert head_after != head_before


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

    def test_clean_fixture_worktrees_calls_prune(self, tmp_path):
        """--fixture-worktrees → 调用 _prune_fixture_repo_worktrees，不删任务目录。"""
        from agent_go.cli import cmd_clean
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        with patch("agent_go.cli._prune_fixture_repo_worktrees") as mock_prune:
            with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
                cmd_clean(MagicMock(fixture_worktrees=True, older_than=None))
        mock_prune.assert_called_once()
        assert task_dir.exists(), "--fixture-worktrees 不应删除任务目录"

    def test_prune_fixture_repo_worktrees_skips_non_git(self, tmp_path):
        """_prune_fixture_repo_worktrees：非 git 目录跳过（不触发 prune）。"""
        from agent_go.cli import _prune_fixture_repo_worktrees
        # 构造一个 AGENT_GO_DIR 下的任务 meta 指向「非 git 目录」
        non_git = tmp_path / "repo-non-git"
        non_git.mkdir()
        task_dir = tmp_path / "task-x"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "repo": str(non_git), "status": "completed",
        }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path), \
             patch("agent_go.git_utils._worktree_prune") as mock_prune, \
             patch("agent_go.cli.subprocess.run") as mock_run:
            mock_prune.return_value = (True, "")
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n")
            # fixtures_base 注入 tmp：避免扫描真实 eval_suite/fixtures（有 .git + worktree）
            _prune_fixture_repo_worktrees(fixtures_base=str(tmp_path))

        # 非 git 候选仓库不应被 prune
        pruned_paths = [c.args[0] for c in mock_prune.call_args_list]
        assert not any(str(non_git) in str(p) for p in pruned_paths), \
            f"非 git 目录不应被 prune: {pruned_paths}"


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

    def test_all_plan_attempts_fail_fallback(self, tmp_path):
        """3 次 generate_plan 全部失败（plan=None）：降级到 decompose_fallback，不抛 UnboundLocalError。

        回归测试：修复前 cmd_run 在 plan=None 时缺少 else 分支，subtasks 未定义，
        confirm_subtasks 抛 UnboundLocalError（cli.py:682）。
        """
        mock_fb, mock_p2s, mock_pipe = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=None,  # plan=None 时不会走到 confirm_plan
            plan_side_effect=[RuntimeError("api down"),
                              RuntimeError("api down"),
                              RuntimeError("api down")],
        )
        # plan=None → 走新加的 else 分支 → decompose_fallback 被调用
        mock_fb.assert_called_once()
        # plan_to_subtasks 不该被调用（plan 是 None，走不到）
        mock_p2s.assert_not_called()
        # pipeline 正常执行（降级 subtasks 也能跑）
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


class TestCmdRunRegenerateDocs:
    """cmd_run 选择 R 重新生成 Plan 时保留 D 挂载的参考文档（ISSUE-22 回归）

    修复前：重生成时 generate_plan 的 reference_docs 硬编码传 ""，
    确认环节用 D 挂载的参考文档在重生成时丢失。
    """

    def _make_args(self, repo):
        parser = _build_parser()
        return parser.parse_args(["run", str(repo), "test task"])

    def _run_with_mocks(self, tmp_path, confirm_side_effect):
        from agent_go.cli import cmd_run
        repo = tmp_path / "repo"
        repo.mkdir()
        home = tmp_path / "agent_go_home"
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("agent_go.cli.load_config", return_value={"behavior": {}}), \
             patch("agent_go.cli.setup_logger", return_value=MagicMock()), \
             patch("agent_go.cli._detect_tool_versions", return_value={}), \
             patch("agent_go.cli.load_agent_type", return_value=None), \
             patch("agent_go.cli.generate_plan", side_effect=[plan, dict(plan)]) as mock_gen, \
             patch("agent_go.cli.confirm_plan", side_effect=confirm_side_effect), \
             patch("agent_go.cli.read_reference_docs", return_value="DOC_CONTENT") as mock_read, \
             patch("agent_go.cli.plan_to_subtasks", return_value=[{"id": "s1", "title": "t"}]), \
             patch("agent_go.cli.plan_to_md", return_value="# plan"), \
             patch("agent_go.cli.confirm_subtasks", side_effect=lambda subs, cfg, log: subs), \
             patch("agent_go.cli._run_pipeline"):
            cmd_run(self._make_args(repo))
        return mock_gen, mock_read

    def test_regenerate_plan_passes_reference_docs(self, tmp_path):
        """R 重生成：用 final_doc_paths 重新读取参考文档并传入 generate_plan"""
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        mock_gen, mock_read = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[(None, ["doc.md"]), (plan, ["doc.md"])],
        )
        assert mock_gen.call_count == 2
        # 重生成（第 2 次调用）的 reference_docs 为重新读取的文档内容
        assert mock_gen.call_args_list[1].args[5] == "DOC_CONTENT"
        mock_read.assert_called_once()
        assert mock_read.call_args.args[0] == ["doc.md"]

    def test_regenerate_plan_empty_docs_passes_empty(self, tmp_path):
        """R 重生成：无参考文档时传空字符串，且不调用 read_reference_docs"""
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        mock_gen, mock_read = self._run_with_mocks(
            tmp_path,
            confirm_side_effect=[(None, []), (plan, [])],
        )
        assert mock_gen.call_count == 2
        assert mock_gen.call_args_list[1].args[5] == ""
        mock_read.assert_not_called()


class TestCmdResume:
    """cmd_resume 中断任务恢复

    _run_pipeline / load_config / setup_logger 全部 mock，
    任务目录用 tmp_path 真实文件（meta.json、result.json、worktree 占位）。
    """

    def _make_task(self, home, task_id, meta, subtask_results=None, worktrees=()):
        """构造 ~/.agent_go/<task_id> 目录。

        subtask_results: {sid: result_dict} → 写 <sid>/result.json
        worktrees: 有 work/.git 的子任务 id 列表（模拟保留的 worktree）
        """
        td = home / task_id
        td.mkdir(parents=True)
        (td / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        for sid, result in (subtask_results or {}).items():
            sd = td / sid
            sd.mkdir(parents=True, exist_ok=True)
            if result is not None:
                (sd / "result.json").write_text(json.dumps(result), encoding="utf-8")
        for sid in worktrees:
            (td / sid / "work" / ".git").mkdir(parents=True)
        return td

    def _base_meta(self, task_id, repo, status="paused"):
        return {
            "task_id": task_id, "task": "测试恢复", "repo": str(repo),
            "status": status,
            "subtasks": [
                {"id": "sub-1", "title": "步骤一"},
                {"id": "sub-2", "title": "步骤二"},
            ],
            "results": [],
        }

    def _run_resume(self, home, args, argv, config):
        """以 mock 环境执行 cmd_resume，返回 _run_pipeline 的 mock。"""
        from agent_go.cli import cmd_resume
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("sys.argv", argv), \
             patch("agent_go.cli.load_config", return_value=config), \
             patch("agent_go.cli.setup_logger", return_value=MagicMock()), \
             patch("agent_go.cli._run_pipeline") as mock_pipe:
            cmd_resume(args)
        return mock_pipe

    def test_resume_nonexistent_task(self, tmp_path, capsys):
        """任务目录不存在 → 报错并退出"""
        from agent_go.cli import cmd_resume
        home = tmp_path / ".agent_go"
        home.mkdir()
        args = _build_parser().parse_args(["resume", "task-ghost"])
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            with pytest.raises(SystemExit):
                cmd_resume(args)
        assert "任务不存在" in capsys.readouterr().out

    def test_resume_usage_without_task_id(self, capsys):
        """sys.argv 模式缺 task_id → 打印 Usage 并退出"""
        from agent_go.cli import cmd_resume
        with patch("sys.argv", ["agent_go", "resume"]):
            with pytest.raises(SystemExit):
                cmd_resume(None)
        assert "Usage" in capsys.readouterr().out

    def test_resume_rejects_completed_task(self, tmp_path, capsys):
        """已完成任务不可恢复 → 报错并退出"""
        from agent_go.cli import cmd_resume
        home = tmp_path / ".agent_go"
        meta = self._base_meta("task-done", tmp_path / "repo", status="completed")
        self._make_task(home, "task-done", meta)
        args = _build_parser().parse_args(["resume", "task-done"])
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            with pytest.raises(SystemExit):
                cmd_resume(args)
        out = capsys.readouterr().out
        assert "无法恢复" in out
        assert "completed" in out

    def test_resume_paused_task_pipeline_args(self, tmp_path):
        """paused 任务恢复：结果从 result.json 重建，completed/worktree 正确分类"""
        home = tmp_path / ".agent_go"
        task_id = "task-paused"
        meta = self._base_meta(task_id, tmp_path / "repo")
        meta["remote_url"] = "origin"
        td = self._make_task(
            home, task_id, meta,
            subtask_results={
                "sub-1": {"subtask_id": "sub-1", "status": "completed", "summary": "done"},
                "sub-2": {"subtask_id": "sub-2", "status": "failed", "failure_reason": "verify failed"},
            },
            worktrees=["sub-1"],  # sub-2 无 .git，不进 worktree_map
        )
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)

        mock_pipe.assert_called_once()
        call = mock_pipe.call_args
        # 位置参数: confirmed, repo, task_dir, logger, config, headless,
        #           parallel, issue_ref, meta, worktree_map, results_map, completed_ids
        assert [s["id"] for s in call.args[0]] == ["sub-1", "sub-2"]
        assert call.args[9] == {"sub-1": td / "sub-1" / "work"}      # worktree_map
        assert set(call.args[10]) == {"sub-1"}                       # results_map（failed 不 seed，乐观重跑）
        assert call.args[11] == {"sub-1"}                            # completed_ids
        assert call.kwargs["remote_url"] == "origin"                 # 取自 meta.json
        passed_config = call.args[4]
        assert passed_config["_task_id"] == task_id
        assert passed_config["_metering_path"].endswith("metering.jsonl")
        # --yes → 自动确认全开
        assert passed_config["behavior"]["auto_confirm_plan"] is True
        assert passed_config["behavior"]["auto_confirm_subtasks"] is True
        assert passed_config["behavior"]["auto_verify_subtask"] is True
        # meta.json 状态置回 running 并回写 remote_url
        saved = json.loads((td / "meta.json").read_text(encoding="utf-8"))
        assert saved["status"] == "running"
        assert saved["remote_url"] == "origin"

    def test_resume_completed_status_variants(self, tmp_path):
        """completed/no_changes/degraded 均视为已完成，failed 不算"""
        home = tmp_path / ".agent_go"
        task_id = "task-status"
        meta = self._base_meta(task_id, tmp_path / "repo")
        meta["results"] = [
            {"subtask_id": "sub-1", "status": "no_changes"},
            {"subtask_id": "sub-2", "status": "degraded"},
        ]
        self._make_task(home, task_id, meta)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)
        assert mock_pipe.call_args.args[11] == {"sub-1", "sub-2"}

    def test_resume_max_retries_override(self, tmp_path):
        """--max-retries 覆盖 config.verification.max_retries"""
        home = tmp_path / ".agent_go"
        task_id = "task-retry"
        self._make_task(home, task_id, self._base_meta(task_id, tmp_path / "repo"))
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes", "--max-retries", "5"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)
        assert mock_pipe.call_args.args[4]["verification"]["max_retries"] == 5

    def test_resume_no_verify_block(self, tmp_path):
        """--no-verify-block 关闭验证失败阻断"""
        home = tmp_path / ".agent_go"
        task_id = "task-noblock"
        self._make_task(home, task_id, self._base_meta(task_id, tmp_path / "repo"))
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes", "--no-verify-block"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)
        assert mock_pipe.call_args.args[4]["verification"]["block_on_failure"] is False

    def test_resume_cli_remote_overrides_meta(self, tmp_path):
        """命令行 --remote 优先于 meta.json 中的 remote_url"""
        home = tmp_path / ".agent_go"
        task_id = "task-remote"
        meta = self._base_meta(task_id, tmp_path / "repo")
        meta["remote_url"] = "origin"
        td = self._make_task(home, task_id, meta)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes"])
        mock_pipe = self._run_resume(
            home, args,
            ["agent_go", "resume", task_id, "--yes", "--remote", "upstream"],
            config)
        assert mock_pipe.call_args.kwargs["remote_url"] == "upstream"
        saved = json.loads((td / "meta.json").read_text(encoding="utf-8"))
        assert saved["remote_url"] == "upstream"

    def test_resume_corrupt_result_json_skipped(self, tmp_path):
        """损坏的 result.json 被跳过，恢复流程继续（ISSUE-15 回归）

        修复前：except 块引用的局部 logger 在循环之后才赋值，
        损坏文件必抛 UnboundLocalError 中断恢复。
        修复后：setup_logger 提前到恢复循环之前，损坏文件仅记 debug 日志并跳过。
        """
        home = tmp_path / ".agent_go"
        task_id = "task-corrupt"
        meta = self._base_meta(task_id, tmp_path / "repo")
        td = self._make_task(home, task_id, meta,
                             subtask_results={"sub-1": None, "sub-2": None})
        (td / "sub-1" / "result.json").write_text("{broken", encoding="utf-8")
        (td / "sub-2" / "result.json").write_text(
            json.dumps({"subtask_id": "sub-2", "status": "failed"}), encoding="utf-8")
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)
        # 恢复未被中断，pipeline 正常执行
        mock_pipe.assert_called_once()
        call = mock_pipe.call_args
        # 损坏的 sub-1 被跳过；sub-2 的 failed 结果是条件态不 seed（乐观重跑），
        # 两者都不进入 results_map
        assert set(call.args[10]) == set()
        # failed 不计入 completed_ids
        assert call.args[11] == set()

    def test_resume_failed_not_seeded_unblocks_downstream(self, tmp_path):
        """resume 不 seed 历史 failed 结果——下游不再被「过期失败」级联阻断。

        回归：task-20260821-174050-200-c0e8 场景。上游 sub-1 首跑因 socket 拷贝
        (Errno 102) 崩失败，resume 重跑时如果保留 failed 结果进 results_map，
        wave-0 级联阻断会用过期失败把下游 sub-2 永久标 blocked（即使 sub-1
        本次重跑成功也无法解锁）。修复：failed 结果不 seed（乐观重跑），
        级联按本次重跑的真实结果重新评估。
        """
        home = tmp_path / ".agent_go"
        task_id = "task-cascade"
        meta = self._base_meta(task_id, tmp_path / "repo")
        meta["results"] = [
            {"subtask_id": "sub-1", "status": "failed", "failure_reason": "socket err"},
            {"subtask_id": "sub-2", "status": "blocked",
             "failure_reason": "上游依赖失败，级联阻断", "blocked_by": ["sub-1"]},
        ]
        td = self._make_task(home, task_id, meta)
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        args = _build_parser().parse_args(["resume", task_id, "--yes"])
        mock_pipe = self._run_resume(
            home, args, ["agent_go", "resume", task_id, "--yes"], config)
        # blocked 被解锁（未 seed）、failed 被清空（未 seed）→ results_map 为空，
        # completed_ids 为空 → sub-1/sub-2 都进入 remaining 待重跑
        assert set(mock_pipe.call_args.args[10]) == set()
        assert mock_pipe.call_args.args[11] == set()


class TestCmdInspect:
    """cmd_inspect 保留 worktree 现场查看（真实临时任务目录）"""

    def _make_task(self, home, task_id, subtasks, write_meta=True):
        td = home / task_id
        td.mkdir(parents=True)
        if write_meta:
            (td / "meta.json").write_text(json.dumps({
                "task_id": task_id, "task": "测试巡检", "status": "failed",
                "subtasks": subtasks, "results": [],
            }), encoding="utf-8")
        return td

    def _make_subtask(self, td, sid, result=None, preserved=None,
                      worktree=False, task_md=False):
        sd = td / sid
        sd.mkdir(parents=True, exist_ok=True)
        if result is not None:
            (sd / "result.json").write_text(json.dumps(result), encoding="utf-8")
        if preserved is not None:
            (sd / ".preserved").write_text(json.dumps(preserved), encoding="utf-8")
        if worktree:
            (sd / "work" / ".git").mkdir(parents=True)
        if task_md:
            (sd / "TASK.md").write_text("# task", encoding="utf-8")

    def _args(self, task_id, as_json=False, show_all=False):
        argv = ["inspect", task_id]
        if as_json:
            argv.append("--json")
        if show_all:
            argv.append("--all")
        return _build_parser().parse_args(argv)

    def test_inspect_nonexistent_task(self, tmp_path, capsys):
        """任务不存在 → 提示并正常返回（不退出）"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        home.mkdir()
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args("task-ghost"))
        assert "任务不存在" in capsys.readouterr().out

    def test_inspect_no_preserved(self, tmp_path, capsys):
        """全部子任务正常完成且无保留标记 → 提示无保留 worktree"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        td = self._make_task(home, "task-ok", [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "completed"})
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args("task-ok"))
        assert "没有保留的 worktree" in capsys.readouterr().out

    def test_inspect_preserved_failed_subtask(self, tmp_path, capsys):
        """保留的失败子任务：展示路径、分支、失败原因、TASK.md"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-fail"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(
            td, "sub-1",
            result={"subtask_id": "sub-1", "status": "failed",
                    "failure_reason": "验证失败", "summary": "改了 2 个文件",
                    "verify_ok": False},
            preserved={"subtask_id": "sub-1", "status": "failed",
                       "failure_reason": "验证失败",
                       "branch": f"agent_go/{task_id}/sub-1"},
            worktree=True, task_md=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id))
        out = capsys.readouterr().out
        assert "保留现场" in out
        assert "sub-1 [保留]" in out
        assert "验证失败" in out
        assert "改了 2 个文件" in out
        assert "验证: 失败" in out
        assert str(td / "sub-1" / "work") in out
        assert f"agent_go/{task_id}/sub-1" in out
        assert "TASK.md" in out

    def test_inspect_failed_without_marker_still_listed(self, tmp_path, capsys):
        """failed + worktree 存在但无 .preserved 标记 → 默认也列出"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-nomarker"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "failed"},
                           worktree=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id))
        out = capsys.readouterr().out
        assert "sub-1" in out
        assert "[保留]" not in out  # 无标记 → 不带保留标签

    def test_inspect_all_shows_completed(self, tmp_path, capsys):
        """--all 显示全部子任务（含已完成无保留的）"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-all"
        td = self._make_task(home, task_id, [
            {"id": "sub-1", "title": "步骤一"},
            {"id": "sub-2", "title": "步骤二"},
        ])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "completed"})
        self._make_subtask(td, "sub-2",
                           result={"subtask_id": "sub-2", "status": "failed"},
                           preserved={"subtask_id": "sub-2", "status": "failed",
                                      "branch": f"agent_go/{task_id}/sub-2"},
                           worktree=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id, show_all=True))
        out = capsys.readouterr().out
        assert "sub-1" in out and "sub-2" in out

    def test_inspect_default_hides_completed(self, tmp_path, capsys):
        """默认模式隐藏已完成且无保留标记的子任务"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-hide"
        td = self._make_task(home, task_id, [
            {"id": "sub-1", "title": "步骤一"},
            {"id": "sub-2", "title": "步骤二"},
        ])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "completed"})
        self._make_subtask(td, "sub-2",
                           result={"subtask_id": "sub-2", "status": "failed"},
                           preserved={"subtask_id": "sub-2", "status": "failed",
                                      "branch": f"agent_go/{task_id}/sub-2"},
                           worktree=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id))
        out = capsys.readouterr().out
        assert "sub-1" not in out
        assert "sub-2" in out

    def test_inspect_missing_worktree(self, tmp_path, capsys):
        """有保留标记但 worktree 已被清理 → 明确提示"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-cleaned"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "failed"},
                           preserved={"subtask_id": "sub-1", "status": "failed",
                                      "branch": f"agent_go/{task_id}/sub-1"})
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id))
        out = capsys.readouterr().out
        assert "sub-1 [保留]" in out
        assert "worktree 不存在" in out

    def test_inspect_missing_meta(self, tmp_path, capsys):
        """任务目录存在但缺 meta.json → 按无子任务处理"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        self._make_task(home, "task-nometa", [], write_meta=False)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args("task-nometa"))
        assert "没有保留的 worktree" in capsys.readouterr().out

    def test_inspect_corrupt_files_no_crash(self, tmp_path, capsys):
        """损坏的 result.json / .preserved 不崩溃，状态降级为 unknown"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-broken"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        sd = td / "sub-1"
        sd.mkdir()
        (sd / "result.json").write_text("{broken", encoding="utf-8")
        (sd / ".preserved").write_text("not json", encoding="utf-8")
        (sd / "work" / ".git").mkdir(parents=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id))
        out = capsys.readouterr().out
        assert "unknown" in out
        # 分支名回退到默认命名规则
        assert f"agent_go/{task_id}/sub-1" in out

    def test_inspect_json_output(self, tmp_path, capsys):
        """--json 输出机器可读结构：task_id + entries 字段完整"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-json"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(
            td, "sub-1",
            result={"subtask_id": "sub-1", "status": "failed",
                    "failure_reason": "验证失败", "verify_ok": False},
            preserved={"subtask_id": "sub-1", "status": "failed",
                       "branch": f"agent_go/{task_id}/sub-1"},
            worktree=True, task_md=True)
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id, as_json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["task_id"] == task_id
        assert len(data["entries"]) == 1
        e = data["entries"][0]
        assert e["id"] == "sub-1"
        assert e["title"] == "步骤一"
        assert e["status"] == "failed"
        assert e["worktree_exists"] is True
        assert e["is_preserved"] is True
        assert e["worktree_path"] == str(td / "sub-1" / "work")
        assert e["branch"] == f"agent_go/{task_id}/sub-1"
        assert e["failure_reason"] == "验证失败"
        assert e["verify_ok"] is False
        assert e["has_task_md"] is True

    def test_inspect_json_empty_entries(self, tmp_path, capsys):
        """--json 无保留现场时仍输出合法 JSON（entries 为空）"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-json-empty"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "completed"})
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id, as_json=True))
        data = json.loads(capsys.readouterr().out)
        assert data == {"task_id": task_id, "entries": []}

    def test_inspect_json_all_includes_cleaned_worktree(self, tmp_path, capsys):
        """--json --all：已完成且 worktree 已清理的子任务也在 entries 中"""
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        task_id = "task-json-all"
        td = self._make_task(home, task_id, [{"id": "sub-1", "title": "步骤一"}])
        self._make_subtask(td, "sub-1",
                           result={"subtask_id": "sub-1", "status": "completed"})
        with patch("agent_go.cli.AGENT_GO_DIR", home):
            cmd_inspect(self._args(task_id, as_json=True, show_all=True))
        data = json.loads(capsys.readouterr().out)
        assert len(data["entries"]) == 1
        e = data["entries"][0]
        assert e["status"] == "completed"
        assert e["worktree_exists"] is False
        assert e["worktree_path"] == ""


# ═══════════════════════════════════════════════════════════════
# --auto-init flag（cmd_run 自动 git init）
# ═══════════════════════════════════════════════════════════════

class TestCmdRunAutoInit:
    """cmd_run 的 --auto-init flag"""

    def _make_args(self, repo, auto_init=False):
        parser = _build_parser()
        tokens = ["run", str(repo), "test task"]
        if auto_init:
            tokens.append("--auto-init")
        return parser.parse_args(tokens)

    def _run_cmd_run(self, args, tmp_path):
        """复用 TestCmdRunFallback 的 mock 套路，跑 cmd_run 但不打断流程。"""
        from agent_go.cli import cmd_run
        home = tmp_path / "agent_go_home"
        plan = {"overview": "o", "steps": [{"id": "s1", "title": "t", "description": "d"}]}
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("agent_go.cli.load_config", return_value={"behavior": {}}), \
             patch("agent_go.cli.setup_logger", return_value=MagicMock()), \
             patch("agent_go.cli._detect_tool_versions", return_value={}), \
             patch("agent_go.cli.load_agent_type", return_value=None), \
             patch("agent_go.cli.generate_plan", return_value=plan), \
             patch("agent_go.cli.confirm_plan", return_value=(plan, [])), \
             patch("agent_go.cli.plan_to_subtasks", return_value=[{"id": "s1", "title": "t"}]), \
             patch("agent_go.cli.plan_to_md", return_value="# plan"), \
             patch("agent_go.cli.confirm_subtasks", side_effect=lambda subs, cfg, log: subs), \
             patch("agent_go.cli._run_pipeline"):
            cmd_run(args)

    def test_parser_default_off(self):
        """默认不开 --auto-init"""
        parser = _build_parser()
        args = parser.parse_args(["run", "/tmp/repo"])
        assert args.auto_init is False

    def test_parser_flag_on(self):
        """--auto-init 解析为 True"""
        parser = _build_parser()
        args = parser.parse_args(["run", "/tmp/repo", "--auto-init"])
        assert args.auto_init is True

    def test_auto_init_creates_git_for_non_git_dir(self, tmp_path):
        """非 git 目录 + --auto-init → 跑完后 .git 存在且有 commit"""
        import subprocess
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
        assert not (repo / ".git").exists()

        self._run_cmd_run(self._make_args(repo, auto_init=True), tmp_path)

        assert (repo / ".git").is_dir()
        r = subprocess.run(["git", "log", "--oneline"], cwd=str(repo),
                           capture_output=True, text=True)
        assert r.returncode == 0
        assert "init (auto-created by agent_go)" in r.stdout

    def test_auto_init_skipped_when_already_git(self, tmp_path):
        """已是 git 仓库 → --auto-init 不再 init（不破坏已有历史）"""
        import subprocess
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "original-init"], cwd=str(repo), check=True)
        original_head = subprocess.run(["git", "rev-parse", "HEAD"],
                                       cwd=str(repo), capture_output=True, text=True).stdout.strip()

        self._run_cmd_run(self._make_args(repo, auto_init=True), tmp_path)

        # HEAD 不应改变（没有追加 auto-init 的 commit）
        new_head = subprocess.run(["git", "rev-parse", "HEAD"],
                                  cwd=str(repo), capture_output=True, text=True).stdout.strip()
        assert original_head == new_head

    def test_auto_init_off_does_not_touch_non_git(self, tmp_path):
        """默认（不开 --auto-init）→ 非 git 目录保持原样"""
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")

        self._run_cmd_run(self._make_args(repo, auto_init=False), tmp_path)

        assert not (repo / ".git").exists()


# ---------------------------------------------------------------------------
# _cleanup_stale_tasks — 心跳判活
# ---------------------------------------------------------------------------

class TestCleanupStaleTasks:
    """stale 清理改用心跳判活：心跳新鲜 → 不误杀；心跳冻结 → 判 stale_aborted。"""

    def _running_task(self, tmp_path, task_id, hours_old=3):
        td = tmp_path / task_id
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": task_id, "status": "running",
        }), encoding="utf-8")
        old = time.time() - hours_old * 3600
        os.utime(td / "meta.json", (old, old))
        return td

    def test_fresh_heartbeat_keeps_running(self, tmp_path):
        """meta.json 很旧但 heartbeat 新鲜（30s 前）→ 任务仍在运行，不判 stale。"""
        from agent_go.cli import _cleanup_stale_tasks
        td = self._running_task(tmp_path, "task-fresh")
        hb = td / "heartbeat"
        hb.touch()
        fresh = time.time() - 30
        os.utime(hb, (fresh, fresh))

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            cleaned = _cleanup_stale_tasks(max_age_hours=1)

        assert cleaned == 0
        assert json.loads((td / "meta.json").read_text(encoding="utf-8"))["status"] == "running"

    def test_frozen_heartbeat_marks_stale(self, tmp_path):
        """heartbeat 冻结（进程死亡，mtime 3h 前）→ 判 stale_aborted。"""
        from agent_go.cli import _cleanup_stale_tasks
        td = self._running_task(tmp_path, "task-dead")
        hb = td / "heartbeat"
        hb.touch()
        old = time.time() - 3 * 3600
        os.utime(hb, (old, old))

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            cleaned = _cleanup_stale_tasks(max_age_hours=1)

        assert cleaned == 1
        meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "stale_aborted"
        assert "stale_aborted_at" in meta

    def test_no_heartbeat_falls_back_to_meta_mtime(self, tmp_path):
        """无 heartbeat 文件 → 回退 meta.json mtime（兼容旧任务目录）。"""
        from agent_go.cli import _cleanup_stale_tasks
        self._running_task(tmp_path, "task-nohb")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            cleaned = _cleanup_stale_tasks(max_age_hours=1)

        assert cleaned == 1

    def test_completed_task_never_aborted(self, tmp_path):
        """status=completed 的任务即使心跳很旧也不判 stale。"""
        from agent_go.cli import _cleanup_stale_tasks
        td = tmp_path / "task-done"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": "task-done", "status": "completed",
        }), encoding="utf-8")
        old = time.time() - 24 * 3600
        os.utime(td / "meta.json", (old, old))
        (td / "heartbeat").touch()
        os.utime(td / "heartbeat", (old, old))

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            cleaned = _cleanup_stale_tasks(max_age_hours=1)

        assert cleaned == 0
        assert json.loads((td / "meta.json").read_text(encoding="utf-8"))["status"] == "completed"


class TestCmdGovernance:
    """M1.4 governance 命令：traceability + architecture compliance 展示"""

    def _make_args(self, task_id, json_mode=False):
        from argparse import Namespace
        return Namespace(task_id=task_id, json_mode=json_mode)

    def test_governance_json_output(self, tmp_path):
        from agent_go.cli import cmd_governance
        task_dir = tmp_path / "task-gov"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "task_id": "task-gov", "task": "测试",
            "subtasks": [{
                "id": "sub-1", "title": "实现",
                "requirement_ids": ["REQ-001"], "acceptance_criteria_ids": ["AC-001"],
                "verification": "pytest", "verification_results": [{"passed": True}],
            }],
            "plan_quality": {"requirement_ids": ["REQ-001"]},
            "delivery_branch": "agent_go/task-gov/delivery",
            "accepted_delivery": True,
        }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print") as mock_print:
                cmd_governance(self._make_args("task-gov", json_mode=True))
        output = mock_print.call_args[0][0]
        assert '"traceability"' in output
        assert '"assessment"' in output

    def test_governance_missing_task_errors(self, tmp_path):
        from agent_go.cli import cmd_governance
        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print") as mock_print:
                cmd_governance(self._make_args("task-none"))
        joined = "\n".join(c[0][0] for c in mock_print.call_args_list if c[0])
        assert "任务不存在" in joined

    def test_governance_text_output_has_assessment(self, tmp_path):
        from agent_go.cli import cmd_governance
        task_dir = tmp_path / "task-gov2"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps({
            "task_id": "task-gov2", "task": "测试",
            "subtasks": [{
                "id": "sub-1", "title": "实现",
                "requirement_ids": ["REQ-001"],
                "verification": "pytest", "verification_results": [],
            }],
            "plan_quality": {"requirement_ids": ["REQ-001"]},
        }), encoding="utf-8")

        with patch("agent_go.cli.AGENT_GO_DIR", tmp_path):
            with patch("builtins.print") as mock_print:
                cmd_governance(self._make_args("task-gov2"))
        joined = "\n".join(c[0][0] for c in mock_print.call_args_list if c[0])
        assert "追踪状态" in joined


class TestInspectDiagHints:
    """C6：inspect 输出代理诊断提示（R14-R16 curl 入口）"""

    def _make_failed_task(self, home, task_id="task-diag"):
        td = home / task_id
        (td / "sub-1").mkdir(parents=True)
        (td / "meta.json").write_text(json.dumps({
            "task_id": task_id, "task": "t", "status": "failed",
            "subtasks": [{"id": "sub-1", "title": "步骤一"}], "results": [],
        }), encoding="utf-8")
        (td / "sub-1" / "result.json").write_text(json.dumps({
            "subtask_id": "sub-1", "status": "failed", "failure_reason": "verify 失败",
        }), encoding="utf-8")
        (td / "sub-1" / ".preserved").write_text(json.dumps({"branch": "b"}), encoding="utf-8")
        return task_id

    def test_hints_shown_with_local_proxy(self, tmp_path, capsys):
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        home.mkdir()
        task_id = self._make_failed_task(home)
        args = _build_parser().parse_args(["inspect", task_id])
        fake_cfg = {"plan_api": {"worker_base_url": "http://127.0.0.1:4000"}}
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("agent_go.config.load_config", return_value=fake_cfg):
            cmd_inspect(args)
        out = capsys.readouterr().out
        assert "代理诊断" in out
        assert "/api/session/" in out
        assert "ledger" in out and "archive?view=sent" in out and "metrics" in out

    def test_hints_hidden_without_local_proxy(self, tmp_path, capsys):
        from agent_go.cli import cmd_inspect
        home = tmp_path / ".agent_go"
        home.mkdir()
        task_id = self._make_failed_task(home)
        args = _build_parser().parse_args(["inspect", task_id])
        fake_cfg = {"plan_api": {"base_url": "https://api.anthropic.com/v1/messages"}}
        with patch("agent_go.cli.AGENT_GO_DIR", home), \
             patch("agent_go.config.load_config", return_value=fake_cfg):
            cmd_inspect(args)
        assert "代理诊断" not in capsys.readouterr().out
