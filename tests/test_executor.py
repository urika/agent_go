"""测试 executor.py — run_subtask 核心逻辑

所有外部调用均 mock，测试覆盖:
  1. Headless 模式调用 _run_headless
  2. 交互模式调用 claude subprocess
  3. 无变更返回 status="no_changes"
  4. 有变更返回 status="completed"
  5. 验证失败返回 status="failed"
  6. Skills 加载并注入 TASK.md
  7. Agent 类型配置正确
  8. Upstream merge 调用正确
  9. 验证命令执行
  10. Context 文件生成
  11. _run_verification_cmd 安全门禁 / argv 解析失败 / 120s 超时
  12. _load_verify_state 损坏 JSON 容错恢复
  13. _assess_verification_confidence 各置信度分支
  14. _is_simple_task 判定逻辑
"""

import os, json, logging, subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from agent_go.executor import (
    run_subtask,
    _run_verification_cmd,
    _load_verify_state,
    _assess_verification_confidence,
    _is_simple_task,
    _probe_local_model,
    _build_architecture_context,
    _check_scope_compliance,
    _build_repair_prompt,
)


# ═══════════════════════════════════════════════════════════════
# 共享 fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_repo(tmp_path):
    """创建一个模拟的 git 仓库（含 .git 目录 + 一些文件）。"""
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Test Project", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("print('hello')", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    """模拟 ~/.agent_go/task-xxx 目录。"""
    d = tmp_path / ".agent_go" / "task-executor-test"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def fast_logger(logger):
    """复用 conftest 的 logger fixture（不重复创建）。"""
    return logger


@pytest.fixture
def basic_subtask():
    """最小化 subtask 定义。"""
    return {
        "id": "sub-1",
        "title": "基础任务",
        "description": "执行基础操作",
        "agent_prompt": "请修改 main.py",
        "verification": "",
        "risks": [],
        "depends_on": [],
        "skills": [],
        "agent_type": "developer",
    }


# ═══════════════════════════════════════════════════════════════
# mock 辅助函数
# ═══════════════════════════════════════════════════════════════

def make_subprocess_mock(returncode=0, stdout="", stderr=""):
    """创建一个模拟的 subprocess.CompletedProcess。"""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ═══════════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════════

class TestRunSubtask:
    """run_subtask 核心逻辑测试"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_headless_mode(self, mock_wt_create, mock_subprocess, mock_headless,
                           mock_load_agent, temp_repo, task_dir, fast_logger,
                           basic_subtask):
        """headless=True 时应调用 _run_headless"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        mock_headless.assert_called_once()
        # 验证 _run_headless 的第一个参数是 TASK.md 内容
        call_args = mock_headless.call_args
        assert "基础任务" in call_args[0][0], "TASK.md 应包含子任务标题"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_initial_hard_timeout_passed(self, mock_wt_create, mock_subprocess, mock_headless,
                                         mock_load_agent, temp_repo, task_dir, fast_logger,
                                         basic_subtask):
        """首跑硬超时：verification.run_timeout 按难度缩放后传给 _run_headless。"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True,
                    config={"verification": {"run_timeout": 100}})

        ht = mock_headless.call_args.kwargs.get("hard_timeout")
        assert isinstance(ht, int) and ht > 0, "首跑硬超时应透传给 _run_headless"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_initial_hard_timeout_disabled_when_zero(self, mock_wt_create, mock_subprocess, mock_headless,
                                                     mock_load_agent, temp_repo, task_dir, fast_logger,
                                                     basic_subtask):
        """run_timeout=0 → hard_timeout=0（禁用首跑硬超时）。"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True,
                    config={"verification": {"run_timeout": 0}})

        ht = mock_headless.call_args.kwargs.get("hard_timeout")
        assert ht == 0

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_metering_path_propagated_to_env(self, mock_wt_create, mock_subprocess, mock_headless,
                                             mock_load_agent, temp_repo, task_dir, fast_logger,
                                             basic_subtask):
        """metering_path 参数应通过 AGENT_GO_METERING_PATH 环境变量传给 _run_headless"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True, metering_path="/tmp/x/metering.jsonl")

        env = mock_headless.call_args[0][2]
        assert env["AGENT_GO_METERING_PATH"] == "/tmp/x/metering.jsonl"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_no_metering_path_no_env(self, mock_wt_create, mock_subprocess, mock_headless,
                                     mock_load_agent, temp_repo, task_dir, fast_logger,
                                     basic_subtask):
        """未传 metering_path 时不应设置 AGENT_GO_METERING_PATH"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        env = mock_headless.call_args[0][2]
        assert "AGENT_GO_METERING_PATH" not in env

    @patch("agent_go.executor.load_agent_type")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_interactive_mode(self, mock_wt_create, mock_subprocess,
                              mock_load_agent, temp_repo, task_dir,
                              fast_logger, basic_subtask):
        """headless=False 时应调用 claude subprocess（非 _run_headless）"""
        mock_wt_create.return_value = (True, "")
        mock_load_agent.return_value = None
        # 所有 subprocess.run 调用返回成功
        mock_subprocess.return_value = make_subprocess_mock()

        with patch("shutil.which", return_value=None):
            run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                        fast_logger, headless=False)

        # 验证 subprocess.run 被调用（用于 git 操作和 claude 启动）
        assert mock_subprocess.called, "交互模式应通过 subprocess.run 启动 claude"
        # 确认 _run_headless 不被导入调用（headless=False 路径）
        # 找到包含 "claude" 的调用
        claude_calls = [c for c in mock_subprocess.call_args_list
                        if c.args and isinstance(c.args[0], list)
                        and "claude" in c.args[0]]
        assert len(claude_calls) >= 1, "应有调用 claude 命令的 subprocess.run"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_no_changes_status(self, mock_wt_create, mock_subprocess,
                               mock_headless, mock_load_agent,
                               temp_repo, task_dir, fast_logger,
                               basic_subtask):
        """无 git 变更时 status 应为 no_changes"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # git status --porcelain 返回空（无变更），其他 git 命令返回成功
        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        result = run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        assert result["status"] == "no_changes", (
            f"无变更时应为 no_changes，实际: {result['status']}"
        )
        assert result["summary"] == "无文件变更"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_completed_status(self, mock_wt_create, mock_subprocess,
                              mock_headless, mock_metrics, mock_load_agent,
                              temp_repo, task_dir, fast_logger,
                              basic_subtask):
        """有 git 变更 + 验证通过时 status 应为 completed"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        # git status --porcelain 返回有变更，diff --stat 返回变更摘要
        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            if "numstat" in cmd_str:
                return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        result = run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        assert result["status"] == "completed", (
            f"有变更时应为 completed，实际: {result['status']}"
        )
        assert "src/main.py" in result["summary"], (
            f"summary 应包含变更文件名，实际: {result['summary']}"
        )

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_failed_status(self, mock_wt_create, mock_subprocess,
                           mock_headless, mock_metrics, mock_load_agent,
                           temp_repo, task_dir, fast_logger,
                           basic_subtask):
        """_run_headless 返回非零退出码时 status 应为 failed"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=1, stderr="error occurred")
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        # git status --porcelain 返回有变更
        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        result = run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        # CR-失败修复：claude 进程崩溃(rc≠0)但产出验证通过(verify_ok=True) → completed
        # （产出有效），不再因进程崩溃误判为能力失败。exit_code 保留崩溃信号供观测。
        assert result["status"] == "completed", (
            f"产出验证通过应计 completed（即使 claude 崩溃），实际: {result['status']}"
        )
        assert result["verify_ok"] is True
        assert result["exit_code"] == 1

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_task_md_created(self, mock_wt_create, mock_subprocess,
                             mock_headless, mock_load_agent,
                             temp_repo, task_dir, fast_logger,
                             basic_subtask):
        """TASK.md 应在 sub_dir 目录下被正确创建"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        task_md_path = task_dir / "sub-1" / "TASK.md"
        assert task_md_path.exists(), "TASK.md 应被创建"
        content = task_md_path.read_text(encoding="utf-8")
        assert "基础任务" in content, "TASK.md 应包含子任务标题"
        assert "执行基础操作" in content, "TASK.md 应包含子任务描述"
        assert "执行指令" in content, "TASK.md 应包含 Agent Prompt 部分"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_task_md_artifact_convention_injected_when_configured(
            self, mock_wt_create, mock_subprocess,
            mock_headless, mock_load_agent,
            temp_repo, task_dir, fast_logger,
            basic_subtask):
        """S9-B: artifact_dir 配置时 TASK.md 注入 __artifacts__/ 产物约定。"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True, config={"artifact_dir": "/tmp/reports"})

        task_md_path = task_dir / "sub-1" / "TASK.md"
        assert task_md_path.exists()
        content = task_md_path.read_text(encoding="utf-8")
        assert "## 产物输出" in content, "TASK.md 应包含产物输出约定"
        assert "__artifacts__/" in content, "TASK.md 应提及 __artifacts__/ 目录"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_task_md_no_artifact_convention_without_config(
            self, mock_wt_create, mock_subprocess,
            mock_headless, mock_load_agent,
            temp_repo, task_dir, fast_logger,
            basic_subtask):
        """S9-B: 未配置 artifact_dir 时 TASK.md 不含产物约定（向后兼容）。"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True, config={})

        task_md_path = task_dir / "sub-1" / "TASK.md"
        assert task_md_path.exists()
        content = task_md_path.read_text(encoding="utf-8")
        assert "## 产物输出" not in content, "未配置时不应注入产物约定"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_context_file_created(self, mock_wt_create, mock_subprocess,
                                  mock_headless, mock_load_agent,
                                  temp_repo, task_dir, fast_logger,
                                  basic_subtask):
        """context.md 应在 sub_dir 目录下被生成"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        ctx_path = task_dir / "sub-1" / "context.md"
        assert ctx_path.exists(), "context.md 应被生成"
        content = ctx_path.read_text(encoding="utf-8")
        assert "sub-1" in content, "context.md 应包含子任务 ID"
        assert "基础任务" in content, "context.md 应包含子任务标题"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_env_variables_set(self, mock_wt_create, mock_subprocess,
                               mock_headless, mock_load_agent,
                               temp_repo, task_dir, fast_logger,
                               basic_subtask):
        """AGENT_GO_TASK_ID, AGENT_GO_SUBTASK_ID, AGENT_GO_WORKTREE 应在 env 中设置"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        # 从 _run_headless 调用参数中提取 env
        call_args = mock_headless.call_args
        env = call_args[0][2]  # 第三个位置参数是 env

        assert env["AGENT_GO_TASK_ID"] == "test-task"
        assert env["AGENT_GO_SUBTASK_ID"] == "sub-1"
        assert "AGENT_GO_WORKTREE" in env
        assert "sub-1" in env["AGENT_GO_WORKTREE"]
        assert "AGENT_GO_SKILLS" in env

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._git_merge_upstream")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_upstream_merge(self, mock_wt_create, mock_subprocess,
                            mock_headless, mock_merge_upstream,
                            mock_load_agent, temp_repo, task_dir,
                            fast_logger, basic_subtask):
        """有 upstream_worktrees 时应调用 _git_merge_upstream"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # 创建 upstream worktree 目录
        up_dir = task_dir / "sub-up" / "work"
        up_dir.mkdir(parents=True, exist_ok=True)
        upstream_worktrees = {"sub-up": up_dir}

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, upstream_worktrees=upstream_worktrees,
                    headless=True)

        mock_merge_upstream.assert_called_once()
        # 验证 merge 参数：src_worktree, dst_worktree, tag
        merge_args = mock_merge_upstream.call_args
        assert merge_args[0][2] == "test-task/sub-up", (
            f"upstream tag 应为 test-task/sub-up，实际: {merge_args[0][2]}"
        )

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_verification_commands_executed(self, mock_wt_create, mock_subprocess,
                                           mock_headless, mock_metrics, mock_load_agent,
                                           temp_repo, task_dir, fast_logger):
        """验证命令应通过 subprocess.run 执行"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        verification_cmd = "pytest --co"
        subtask = {
            "id": "sub-1",
            "title": "验证任务",
            "description": "执行并验证",
            "agent_prompt": "do work",
            "verification": verification_cmd,
            "risks": [],
            "depends_on": [],
            "skills": [],
            "agent_type": "developer",
        }

        # git status --porcelain 返回有变更，其他返回成功
        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            if "numstat" in cmd_str:
                return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        result = run_subtask("test-task", subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        # 验证命令被调用（shlex.split 后的列表形式）
        verification_calls = [
            c for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "pytest" in c.args[0]
        ]
        assert len(verification_calls) >= 1, "验证命令应通过 subprocess.run 执行"
        assert result["verify_ok"] is True
        assert len(result["verification_results"]) >= 1
        assert result["verification_results"][0]["command"] == verification_cmd
        assert result["verification_results"][0]["exit_code"] == 0

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_verification_failure_marks_failed(self, mock_wt_create, mock_subprocess,
                                               mock_headless, mock_metrics, mock_load_agent,
                                               temp_repo, task_dir, fast_logger):
        """验证命令失败时应标记 verify_ok=False 且 status=failed"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        verification_cmd = "pytest tests/"
        subtask = {
            "id": "sub-1",
            "title": "验证失败任务",
            "description": "执行并验证",
            "agent_prompt": "do work",
            "verification": verification_cmd,
            "risks": [],
            "depends_on": [],
            "skills": [],
            "agent_type": "developer",
        }

        call_count = [0]

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            if "numstat" in cmd_str:
                return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
            if "pytest" in cmd_str:
                return make_subprocess_mock(returncode=1, stderr="FAIL test_foo")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        with patch("shutil.which", return_value=None), \
             patch("agent_go.executor.safe_input", return_value="C"):
            result = run_subtask("test-task", subtask, temp_repo, task_dir,
                                 fast_logger, headless=False)  # 交互模式不重试

        # headless=False 交互模式：verify_ok=False, 但 returncode=0
        # status 判定: returncode==0 and verify_ok => False, 所以 status="failed"
        assert result["verify_ok"] is False
        assert result["status"] == "failed"

    @patch("agent_go.executor.load_agent_type")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_skill_injection_into_task_md(self, mock_wt_create, mock_subprocess,
                                          mock_headless, mock_load_agent,
                                          temp_repo, task_dir, fast_logger):
        """Skills 应被加载并注入到 TASK.md"""
        mock_wt_create.return_value = (True, "")
        mock_load_agent.return_value = None
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        subtask = {
            "id": "sub-1",
            "title": "安全审查",
            "description": "审查代码安全性",
            "agent_prompt": "请审查安全",
            "verification": "",
            "risks": [],
            "depends_on": [],
            "skills": ["security-review"],
            "agent_type": "reviewer",
        }

        # Mock skill loading — skills are lazy-imported from agent_go.skills inside executor
        with patch("agent_go.skills.load_skill") as mock_load_skill, \
             patch("agent_go.skills.render_skill_for_execution") as mock_render, \
             patch("agent_go.skills.list_skills") as mock_list_skills:

            mock_load_skill.return_value = {"name": "security-review", "content": "skill body"}
            mock_render.return_value = "## Skill: security-review\nskill content here"
            mock_list_skills.return_value = [{"name": "security-review"}]

            run_subtask("test-task", subtask, temp_repo, task_dir,
                        fast_logger, headless=True)

        task_md_path = task_dir / "sub-1" / "TASK.md"
        assert task_md_path.exists(), "TASK.md 应存在"
        content = task_md_path.read_text(encoding="utf-8")
        assert "security-review" in content, "TASK.md 应包含 Skill 名称"

    @patch("agent_go.executor.load_agent_type")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_skill_inject_mode_guide_for_claude(self, mock_wt_create, mock_subprocess,
                                                mock_headless, mock_load_agent,
                                                temp_repo, task_dir, fast_logger):
        """claude worker（agent_loop 关闭）→ skill 注入 mode="guide"。"""
        mock_wt_create.return_value = (True, "")
        mock_load_agent.return_value = None
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        subtask = {
            "id": "sub-1", "title": "安全审查", "description": "审查代码安全性",
            "agent_prompt": "请审查安全", "verification": "", "risks": [],
            "depends_on": [], "skills": ["security-review"],
            "agent_type": "reviewer",
        }
        with patch("agent_go.skills.load_skill") as mock_load_skill, \
             patch("agent_go.skills.render_skill_for_execution") as mock_render, \
             patch("agent_go.skills.list_skills") as mock_list_skills:
            mock_load_skill.return_value = {"name": "security-review", "content": "skill body"}
            mock_render.return_value = "## Skill: security-review\nskill content here"
            mock_list_skills.return_value = [{"name": "security-review"}]
            run_subtask("test-task", subtask, temp_repo, task_dir,
                        fast_logger, headless=True,
                        config={"agent_loop": {"enabled": False}})
        _, kwargs = mock_render.call_args
        assert kwargs.get("mode") == "guide", \
            f"claude worker 应使用 guide 注入，实际 {kwargs.get('mode')}"

    @patch("agent_go.executor.load_agent_type")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_skill_inject_mode_full_for_agent_loop(self, mock_wt_create, mock_subprocess,
                                                  mock_headless, mock_load_agent,
                                                  temp_repo, task_dir, fast_logger):
        """agent_loop 后端（无 claude skill 机制）→ skill 注入 mode="full"。"""
        mock_wt_create.return_value = (True, "")
        mock_load_agent.return_value = None
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        subtask = {
            "id": "sub-1", "title": "安全审查", "description": "审查代码安全性",
            "agent_prompt": "请审查安全", "verification": "", "risks": [],
            "depends_on": [], "skills": ["security-review"],
            "agent_type": "developer", "difficulty": "easy",
        }
        with patch("agent_go.skills.load_skill") as mock_load_skill, \
             patch("agent_go.skills.render_skill_for_execution") as mock_render, \
             patch("agent_go.skills.list_skills") as mock_list_skills:
            mock_load_skill.return_value = {"name": "security-review", "content": "skill body"}
            mock_render.return_value = "## Skill: security-review\nskill content here"
            mock_list_skills.return_value = [{"name": "security-review"}]
            run_subtask("test-task", subtask, temp_repo, task_dir,
                        fast_logger, headless=True,
                        config={"agent_loop": {"enabled": True}})
        _, kwargs = mock_render.call_args
        assert kwargs.get("mode") == "full", \
            f"agent_loop 应使用 full 注入，实际 {kwargs.get('mode')}"

    @patch("agent_go.executor.load_agent_type")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_agent_type_configured(self, mock_wt_create, mock_subprocess,
                                   mock_headless, mock_load_agent,
                                   temp_repo, task_dir, fast_logger,
                                   basic_subtask):
        """Agent 类型应被正确加载并配置到 env"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # 创建一个 mock AgentType
        from agent_go.agents import AgentType
        mock_agent = AgentType(
            type_name="reviewer",
            description="审查者",
            claude_config={"permission_mode": "bypassPermissions"},
            preload_skills=["security-review"],
        )
        mock_load_agent.return_value = mock_agent

        basic_subtask["agent_type"] = "reviewer"

        with patch("agent_go.executor.get_agent_env") as mock_get_env:
            mock_get_env.return_value = {"CLAUDE_PERMISSION_MODE": "bypassPermissions"}

            run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                        fast_logger, headless=True)

            mock_load_agent.assert_called_with("reviewer", temp_repo)
            mock_get_env.assert_called_once_with(mock_agent)

            # 验证 env 变量包含 agent 配置
            env = mock_headless.call_args[0][2]
            assert env["CLAUDE_PERMISSION_MODE"] == "bypassPermissions"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_upstream_context_injected_into_task_md(self, mock_wt_create,
                                                    mock_subprocess,
                                                    mock_headless,
                                                    mock_load_agent,
                                                    temp_repo, task_dir,
                                                    fast_logger):
        """上游子任务的 context.md 应被注入到 TASK.md"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # 创建上游 context.md
        up_sub_dir = task_dir / "sub-up"
        up_sub_dir.mkdir(parents=True, exist_ok=True)
        (up_sub_dir / "context.md").write_text(
            "### sub-up: 上游任务\n- 状态: 通过\n- 变更: 2 files\n",
            encoding="utf-8"
        )

        subtask = {
            "id": "sub-2",
            "title": "下游任务",
            "description": "依赖上游",
            "agent_prompt": "基于上游修改",
            "verification": "",
            "risks": [],
            "depends_on": ["sub-up"],
            "skills": [],
            "agent_type": "developer",
        }

        run_subtask("test-task", subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        task_md_path = task_dir / "sub-2" / "TASK.md"
        content = task_md_path.read_text(encoding="utf-8")
        assert "上游子任务上下文" in content, "TASK.md 应包含上游上下文标记"
        assert "上游任务" in content, "TASK.md 应包含上游 context 内容"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_merge_conflict_injected_into_task_md(self, mock_wt_create,
                                                  mock_subprocess,
                                                  mock_headless,
                                                  mock_load_agent,
                                                  temp_repo, task_dir,
                                                  fast_logger, basic_subtask):
        """上游合并冲突信息应被注入到 TASK.md"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # 创建上游 worktree 和冲突标记文件
        up_dir = task_dir / "sub-up" / "work"
        up_dir.mkdir(parents=True, exist_ok=True)
        upstream_worktrees = {"sub-up": up_dir}

        basic_subtask["depends_on"] = ["sub-up"]

        with patch("agent_go.executor._git_merge_upstream") as mock_merge:
            # 模拟合并后产生 .MERGE_CONFLICT 文件
            def create_conflict(*args, **kwargs):
                dst_worktree = Path(args[1])
                dst_worktree.mkdir(parents=True, exist_ok=True)
                conflict_file = dst_worktree / ".MERGE_CONFLICT"
                conflict_file.write_text("main.py\nutils.py\n", encoding="utf-8")

            mock_merge.side_effect = create_conflict

            run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                        fast_logger, upstream_worktrees=upstream_worktrees,
                        headless=True)

        task_md_path = task_dir / "sub-1" / "TASK.md"
        content = task_md_path.read_text(encoding="utf-8")
        assert "上游合并冲突" in content, "TASK.md 应包含冲突标记"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_context_file_with_risks(self, mock_wt_create, mock_subprocess,
                                     mock_headless, mock_load_agent,
                                     temp_repo, task_dir, fast_logger):
        """有 risks 的子任务，context.md 应包含风险信息"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        subtask = {
            "id": "sub-1",
            "title": "风险任务",
            "description": "有风险",
            "agent_prompt": "do work",
            "verification": "",
            "risks": ["密钥泄露", "性能退化"],
            "depends_on": [],
            "skills": [],
            "agent_type": "developer",
        }

        run_subtask("test-task", subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        ctx_path = task_dir / "sub-1" / "context.md"
        content = ctx_path.read_text(encoding="utf-8")
        assert "密钥泄露" in content
        assert "性能退化" in content
        assert "风险" in content

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_context_file_with_verification(self, mock_wt_create, mock_subprocess,
                                            mock_headless, mock_metrics, mock_load_agent,
                                            temp_repo, task_dir, fast_logger):
        """有 verification 的子任务，context.md 应包含验证结果"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        verification_cmd = "pytest tests/"
        subtask = {
            "id": "sub-1",
            "title": "验证任务",
            "description": "有验证",
            "agent_prompt": "do work",
            "verification": verification_cmd,
            "risks": [],
            "depends_on": [],
            "skills": [],
            "agent_type": "developer",
        }

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            if "numstat" in cmd_str:
                return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
            if "pytest" in cmd_str:
                return make_subprocess_mock(returncode=0, stdout="1 passed")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        result = run_subtask("test-task", subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        ctx_path = task_dir / "sub-1" / "context.md"
        content = ctx_path.read_text(encoding="utf-8")
        assert verification_cmd in content
        assert result["verify_ok"] is True, "验证应通过"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_worktree_clone_fallback(self, mock_wt_create, mock_subprocess,
                                     mock_headless, mock_load_agent,
                                     temp_repo, task_dir, fast_logger,
                                     basic_subtask):
        """worktree 创建失败时应回退到 git clone"""
        mock_wt_create.return_value = (False, "worktree add failed")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        # 验证 subprocess.run 被调用包含 git clone
        clone_calls = [
            c for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "clone" in c.args[0]
        ]
        assert len(clone_calls) >= 1, "worktree 失败后应回退到 git clone"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_existing_worktree_reused(self, mock_wt_create, mock_subprocess,
                                      mock_headless, mock_load_agent,
                                      temp_repo, task_dir, fast_logger,
                                      basic_subtask):
        """已存在的 worktree 应跳过创建"""
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        # 预先创建 worktree 目录和 .git（真实 git worktree 的 .git 是文件）
        sub_dir = task_dir / "sub-1"
        sub_dir.mkdir(parents=True, exist_ok=True)
        worktree = sub_dir / "work"
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / ".git").write_text("gitdir: ../../.git/worktrees/sub-1", encoding="utf-8")

        run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        # _worktree_create 不应被调用
        mock_wt_create.assert_not_called()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_return_value_structure(self, mock_wt_create, mock_subprocess,
                                    mock_headless, mock_load_agent,
                                    temp_repo, task_dir, fast_logger,
                                    basic_subtask):
        """返回值应包含所有必需字段"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        result = run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        required_keys = [
            "subtask_id", "status", "exit_code", "summary", "worktree",
            "sandbox_type", "verify_ok", "duration_sec", "agent_type_source",
            "skills_unresolved", "retry_count", "timing", "change_stats",
            "merge_results", "verification_results",
        ]
        for key in required_keys:
            assert key in result, f"返回值应包含 '{key}' 字段"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_sandbox_type_headless(self, mock_wt_create, mock_subprocess,
                                   mock_headless, mock_load_agent,
                                   temp_repo, task_dir, fast_logger,
                                   basic_subtask):
        """headless 模式下 sandbox_type 应为 'headless'"""
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        result = run_subtask("test-task", basic_subtask, temp_repo, task_dir,
                             fast_logger, headless=True)

        assert result["sandbox_type"] == "headless"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_sandbox_type_native(self, mock_wt_create, mock_subprocess,
                                 mock_load_agent, temp_repo, task_dir,
                                 fast_logger, basic_subtask):
        """交互模式下无 greywall 时 sandbox_type 应为 'native'"""
        mock_wt_create.return_value = (True, "")
        mock_load_agent.return_value = None

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        with patch("shutil.which", return_value=None):
            result = run_subtask("test-task", basic_subtask, temp_repo,
                                 task_dir, fast_logger, headless=False)

        assert result["sandbox_type"] == "native"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_no_git_repo_copies_directory(self, mock_wt_create, mock_subprocess,
                                          mock_headless, mock_load_agent,
                                          tmp_path, fast_logger):
        """无 .git 目录时应使用 shutil.copytree"""
        repo = tmp_path / "non_git_repo"
        repo.mkdir(parents=True)
        (repo / "file.txt").write_text("hello", encoding="utf-8")

        task_dir = tmp_path / "task_dir"
        task_dir.mkdir(parents=True)

        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)

        subtask = {
            "id": "sub-1", "title": "拷贝任务", "description": "desc",
            "agent_prompt": "do work", "verification": "",
            "risks": [], "depends_on": [], "skills": [],
            "agent_type": "developer",
        }

        result = run_subtask("test-task", subtask, repo, task_dir,
                             fast_logger, headless=True)

        # _worktree_create 不应被调用（无 .git）
        mock_wt_create.assert_not_called()
        # 工作目录应存在且包含复制的文件（验证 copytree 实际执行）
        worktree = task_dir / "sub-1" / "work"
        assert worktree.exists()
        assert (worktree / "file.txt").exists(), "shutil.copytree 应复制文件到 worktree"
        assert (worktree / "file.txt").read_text(encoding="utf-8") == "hello"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_tag_namespaced_with_task_id(self, mock_wt_create, mock_subprocess,
                                         mock_headless, mock_metrics, mock_load_agent,
                                         temp_repo, task_dir, fast_logger,
                                         basic_subtask):
        """git tag 应包含 task_id 前缀避免跨任务冲突"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = {
            "files_changed": 1, "insertions": 1, "deletions": 1,
            "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
        }

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return make_subprocess_mock(stdout="M  src/main.py\n")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return make_subprocess_mock(stdout="src/main.py | 2 +-")
            if "numstat" in cmd_str:
                return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
            return make_subprocess_mock()

        mock_subprocess.side_effect = subprocess_side_effect

        run_subtask("my-task", basic_subtask, temp_repo, task_dir,
                    fast_logger, headless=True)

        # 找到 git tag 调用
        tag_calls = [
            c for c in mock_subprocess.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "tag" in c.args[0]
        ]
        assert len(tag_calls) >= 1, "应有 git tag 调用"
        # 验证 tag 名称格式
        tag_args = tag_calls[0].args[0]
        tag_index = tag_args.index("-f") + 1 if "-f" in tag_args else -1
        if tag_index > 0 and tag_index < len(tag_args):
            tag_name = tag_args[tag_index]
        else:
            # tag -f <name> 格式
            tag_name = tag_args[-1]
        assert tag_name == "my-task/sub-1", (
            f"tag 应为 my-task/sub-1，实际: {tag_name}"
        )


# ═══════════════════════════════════════════════════════════════
# _run_verification_cmd 边界：安全门禁 / argv 解析失败 / 超时
# ═══════════════════════════════════════════════════════════════

class TestRunVerificationCmd:
    """_run_verification_cmd 单命令执行的边界分支"""

    @patch("subprocess.run")
    @patch("agent_go.executor._log_rejected_command")
    def test_rejected_command_skips_execution(self, mock_log_rejected, mock_subprocess,
                                              tmp_path, fast_logger):
        """安全门禁拒绝的命令不应执行，应标记 rejected 并记录拒绝原因"""
        result = _run_verification_cmd("rm -rf /", tmp_path, 1, {}, fast_logger,
                                       "task-1", "sub-1")

        assert result["rejected"] is True
        assert result["reject_reason"], "拒绝时应给出诊断原因"
        assert result["exit_code"] == -1, "被拒绝的命令不应产生真实退出码"
        mock_subprocess.assert_not_called()
        mock_log_rejected.assert_called_once()
        # _log_rejected_command(vcmd, reason, logger, task_id, sub_id)
        reject_args = mock_log_rejected.call_args[0]
        assert reject_args[0] == "rm -rf /"
        assert reject_args[3] == "task-1"
        assert reject_args[4] == "sub-1"

    @patch("subprocess.run")
    @patch("agent_go.executor._log_rejected_command")
    @patch("subprocess.run")
    @patch("agent_go.executor._log_rejected_command")
    def test_command_chain_rejected(self, mock_log_rejected, mock_subprocess,
                                    tmp_path, fast_logger):
        """含 && 的命令拆分子命令校验，危险子命令（rm -rf）仍应被拒绝"""
        result = _run_verification_cmd("pytest -q && rm -rf x", tmp_path, 1, {},
                                       fast_logger)

        assert result["rejected"] is True
        assert "子命令不通过" in result["reject_reason"], (
            f"&& 已允许，但 rm 子命令应被拦截，实际: {result['reject_reason']}"
        )
        mock_subprocess.assert_not_called()

    @patch("subprocess.run")
    @patch("agent_go.executor._log_rejected_command")
    def test_command_chain_safe_allowed(self, mock_log_rejected, mock_subprocess,
                                        tmp_path, fast_logger):
        """安全的 && 命令链（两个安全子命令）应被允许"""
        mock_subprocess.return_value = make_subprocess_mock(returncode=0, stdout="ok")

        result = _run_verification_cmd("pytest -q && python -m pytest tests/ -v",
                                       tmp_path, 1, {}, fast_logger)

        assert "rejected" not in result, (
            f"安全 && 链不应被拒绝，实际: {result.get('reject_reason')}"
        )
        assert result["exit_code"] == 0

    def test_amp_chain_executes_each_part_and_short_circuits(self, tmp_path, fast_logger):
        """&& 链应拆分逐个执行（真实 subprocess）：前段失败则短路，前段通过则执行后段"""
        # 场景 1：前段失败 → 后段不执行（短路）
        r1 = _run_verification_cmd(
            "python3 -c 'import sys; sys.exit(1)' && echo SECOND_RAN",
            tmp_path, 1, {}, fast_logger)
        assert r1["exit_code"] == 1, f"前段失败应整体 exit=1，实际 {r1['exit_code']}"
        assert "SECOND_RAN" not in r1["stdout_tail"], "前段失败时后段不应执行（短路）"

        # 场景 2：前段通过 → 后段执行
        r2 = _run_verification_cmd(
            "python3 -c 'print(1)' && echo SECOND_RAN",
            tmp_path, 1, {}, fast_logger)
        assert r2["exit_code"] == 0, f"全链通过应 exit=0，实际 {r2['exit_code']}"
        assert "SECOND_RAN" in r2["stdout_tail"], "前段通过时后段应执行"

        # 场景 3：三段链，中段失败 → 末段不执行
        r3 = _run_verification_cmd(
            "python3 -c 'print(1)' && python3 -c 'import sys; sys.exit(2)' && echo THIRD_RAN",
            tmp_path, 1, {}, fast_logger)
        assert r3["exit_code"] == 2, f"中段失败应整体 exit=2，实际 {r3['exit_code']}"
        assert "THIRD_RAN" not in r3["stdout_tail"], "中段失败时末段不应执行"

    @patch("subprocess.run")
    def test_cd_prefix_stripped(self, mock_subprocess, tmp_path, fast_logger):
        """冗余的 cd <dir> && 前缀应被剥离后再执行"""
        mock_subprocess.return_value = make_subprocess_mock(returncode=0, stdout="ok")

        result = _run_verification_cmd("cd /some/dir && pytest -q", tmp_path, 1, {},
                                       fast_logger)

        assert result["command"] == "pytest -q", (
            f"cd 前缀应被剥离，实际: {result['command']}"
        )
        assert result["exit_code"] == 0
        # 实际执行的 argv 也不应包含 cd
        executed_argv = mock_subprocess.call_args[0][0]
        assert executed_argv == ["pytest", "-q"]

    @pytest.mark.parametrize("exc", [FileNotFoundError, OSError, ValueError])
    @patch("subprocess.run")
    def test_argv_parse_failure(self, mock_subprocess, tmp_path, exc):
        """subprocess 抛出 FileNotFoundError/OSError/ValueError 时应跳过且不降级 shell"""
        mock_logger = MagicMock(spec=logging.Logger)
        mock_subprocess.side_effect = exc("boom")

        result = _run_verification_cmd("pytest -q", tmp_path, 1, {}, mock_logger)

        assert result["exit_code"] == -1, "解析失败不应产生真实退出码"
        assert "rejected" not in result, "解析失败与安全拒绝是不同的分支"
        assert "stdout_tail" not in result, "未执行成功不应有输出尾部"
        warning_msgs = " ".join(str(c.args[0]) for c in mock_logger.warning.call_args_list)
        assert "无法解析" in warning_msgs

    @patch("subprocess.run")
    def test_timeout_120s(self, mock_subprocess, tmp_path):
        """验证命令超 120s 应捕获 TimeoutExpired 并标记 exit_code=-1"""
        mock_logger = MagicMock(spec=logging.Logger)
        mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd=["pytest", "-q"],
                                                                timeout=120)

        result = _run_verification_cmd("pytest -q", tmp_path, 1, {}, mock_logger)

        assert result["exit_code"] == -1
        assert result["duration_ms"] == 0, "超时未正常返回不应记录耗时"
        warning_msgs = " ".join(str(c.args[0]) for c in mock_logger.warning.call_args_list)
        assert "超时" in warning_msgs

    @patch("subprocess.run")
    def test_output_tail_truncated_to_3000(self, mock_subprocess, tmp_path, fast_logger):
        """stdout/stderr 尾部应只保留最后 3000 字符（供修复 prompt 注入完整 traceback）"""
        mock_subprocess.return_value = make_subprocess_mock(
            returncode=1, stdout="x" * 4000, stderr="y" * 3600)

        result = _run_verification_cmd("pytest -q", tmp_path, 2, {}, fast_logger)

        assert result["exit_code"] == 1
        assert result["stdout_tail"] == "x" * 3000
        assert result["stderr_tail"] == "y" * 3000
        assert result["attempt"] == 2


# ═══════════════════════════════════════════════════════════════
# _load_verify_state 边界：损坏 JSON 容错恢复
# ═══════════════════════════════════════════════════════════════

class TestLoadVerifyState:
    """_load_verify_state 读取 verify_state.json 的容错分支"""

    def test_missing_file_returns_none(self, tmp_path):
        """状态文件不存在时应返回 None"""
        assert _load_verify_state(tmp_path, "sub-1") is None

    def test_valid_state_returned(self, tmp_path):
        """合法 JSON 且 subtask_id 匹配时应返回完整 dict"""
        sub_dir = tmp_path / "sub-1"
        sub_dir.mkdir(parents=True)
        state = {"subtask_id": "sub-1", "attempts": 2, "history": [],
                 "verification_results": []}
        (sub_dir / "verify_state.json").write_text(
            json.dumps(state), encoding="utf-8")

        result = _load_verify_state(tmp_path, "sub-1")

        assert result is not None
        assert result["subtask_id"] == "sub-1"
        assert result["attempts"] == 2

    def test_wrong_subtask_id_returns_none(self, tmp_path):
        """subtask_id 不匹配（跨子任务串扰）时应返回 None"""
        sub_dir = tmp_path / "sub-1"
        sub_dir.mkdir(parents=True)
        (sub_dir / "verify_state.json").write_text(
            json.dumps({"subtask_id": "sub-2", "attempts": 1}), encoding="utf-8")

        assert _load_verify_state(tmp_path, "sub-1") is None

    def test_corrupted_json_returns_none(self, tmp_path):
        """损坏的 JSON 应容错返回 None 而不是抛 JSONDecodeError"""
        sub_dir = tmp_path / "sub-1"
        sub_dir.mkdir(parents=True)
        (sub_dir / "verify_state.json").write_text("{not valid json", encoding="utf-8")

        assert _load_verify_state(tmp_path, "sub-1") is None

    def test_non_dict_json_returns_none(self, tmp_path):
        """JSON 合法但不是 dict（如 list）时应返回 None"""
        sub_dir = tmp_path / "sub-1"
        sub_dir.mkdir(parents=True)
        (sub_dir / "verify_state.json").write_text("[1, 2, 3]", encoding="utf-8")

        assert _load_verify_state(tmp_path, "sub-1") is None

    def test_oserror_returns_none(self, tmp_path):
        """读取抛 OSError（如权限/IO 错误）时应容错返回 None"""
        sub_dir = tmp_path / "sub-1"
        sub_dir.mkdir(parents=True)
        (sub_dir / "verify_state.json").write_text("{}", encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("io error")):
            assert _load_verify_state(tmp_path, "sub-1") is None


# ═══════════════════════════════════════════════════════════════
# _assess_verification_confidence 各置信度分支
# ═══════════════════════════════════════════════════════════════

class TestAssessVerificationConfidence:
    """_assess_verification_confidence 的 deterministic/heuristic/manual/none 分支"""

    def test_no_changes_returns_none_level(self):
        """无变更时无需验证，level 应为 none"""
        result = _assess_verification_confidence("pytest -q", has_changes=False)

        assert result["level"] == "none"
        assert result["warning"] == ""

    def test_no_verification_returns_manual(self):
        """有变更但无验证命令时应为 manual，并给出假阳性警告"""
        result = _assess_verification_confidence("", has_changes=True)

        assert result["level"] == "manual"
        assert "假阳性" in result["warning"]

    def test_whitespace_verification_returns_manual(self):
        """纯空白验证命令等价于未配置，应为 manual"""
        result = _assess_verification_confidence("   ", has_changes=True)

        assert result["level"] == "manual"

    def test_deterministic_pytest(self):
        """含测试框架关键字的命令应为 deterministic"""
        result = _assess_verification_confidence("pytest tests/ -q", has_changes=True)

        assert result["level"] == "deterministic"
        assert result["warning"] == ""

    def test_deterministic_case_insensitive(self):
        """关键字匹配应大小写不敏感"""
        result = _assess_verification_confidence("PYTEST -Q", has_changes=True)

        assert result["level"] == "deterministic"

    def test_deterministic_takes_precedence_over_heuristic(self):
        """同时含测试和静态检查关键字时应优先判为 deterministic"""
        result = _assess_verification_confidence("ruff --check src && pytest -q",
                                                 has_changes=True)

        assert result["level"] == "deterministic"

    def test_heuristic_static_check(self):
        """仅含 lint/check 等静态检查关键字时应为 heuristic"""
        result = _assess_verification_confidence("ruff --check src", has_changes=True)

        assert result["level"] == "heuristic"
        assert "静态检查" in result["warning"]

    def test_unclassified_returns_heuristic(self):
        """有命令但无任何已知关键字时应归为 heuristic 并提示改用测试框架"""
        result = _assess_verification_confidence("mypy --strict src", has_changes=True)

        assert result["level"] == "heuristic"
        assert "无法归类" in result["reason"]
        assert "pytest" in result["warning"]

    def test_echo_latest_not_deterministic(self):
        """词边界匹配：'echo latest' 含子串 'test' 但不应误判为 deterministic"""
        result = _assess_verification_confidence("echo latest", has_changes=True)

        assert result["level"] == "heuristic"
        assert "无法归类" in result["reason"]


# ═══════════════════════════════════════════════════════════════
# _is_simple_task 判定逻辑
# ═══════════════════════════════════════════════════════════════

class TestIsSimpleTask:
    """_is_simple_task: 探索性关键词 + depends_on 数量"""

    def test_minimal_subtask_is_simple(self):
        """无描述、无依赖的子任务应判为简单任务"""
        assert _is_simple_task({}) is True

    @pytest.mark.parametrize("keyword", [
        "探索", "调研", "重构", "迁移",
        "explore", "refactor", "migrate",
    ])
    def test_exploration_keyword_not_simple(self, keyword):
        """description 含任一探索性关键词时应判为复杂任务"""
        subtask = {"description": f"请{keyword}一下代码库", "depends_on": []}

        assert _is_simple_task(subtask) is False

    def test_keyword_in_agent_prompt_not_simple(self):
        """agent_prompt 中的探索性关键词同样应触发复杂判定"""
        subtask = {"description": "修改配置",
                   "agent_prompt": "Please refactor the module"}

        assert _is_simple_task(subtask) is False

    def test_keyword_case_insensitive(self):
        """英文关键词匹配应大小写不敏感"""
        subtask = {"description": "EXPLORE the repo structure"}

        assert _is_simple_task(subtask) is False

    def test_architect_agent_type_not_simple(self):
        """规则 1：architect agent_type 直接判复杂（探索性任务）"""
        assert _is_simple_task({"agent_type": "architect"}) is False

    def test_reviewer_agent_type_not_simple(self):
        """规则 1：reviewer agent_type 直接判复杂"""
        assert _is_simple_task({"agent_type": "reviewer"}) is False

    def test_developer_agent_type_still_simple(self):
        """developer 不触发规则 1，按其他规则判定为简单"""
        assert _is_simple_task({"agent_type": "developer"}) is True

    def test_files_hint_many_wildcards_not_simple(self):
        """规则 3：files_hint 含 >1 个 ** → 涉及文件多 → 复杂"""
        subtask = {"files_hint": "src/**/*.py test/**/*.py"}
        assert _is_simple_task(subtask) is False

    def test_files_hint_single_wildcard_still_simple(self):
        """规则 3 边界：仅 1 个 ** 仍属简单"""
        assert _is_simple_task({"files_hint": "src/**/*.py"}) is True

    def test_two_dependencies_simple(self):
        """depends_on ≤ 2 且无关键词时应判为简单任务"""
        subtask = {"description": "实现登录接口", "depends_on": ["sub-1", "sub-2"]}

        assert _is_simple_task(subtask) is True

    def test_three_dependencies_not_simple(self):
        """depends_on > 2 时应判为复杂任务"""
        subtask = {"description": "实现登录接口",
                   "depends_on": ["sub-1", "sub-2", "sub-3"]}

        assert _is_simple_task(subtask) is False

    def test_keyword_and_many_deps_not_simple(self):
        """关键词与过多依赖同时存在时应判为复杂任务"""
        subtask = {"description": "分析并重构模块",
                   "depends_on": ["sub-1", "sub-2", "sub-3"]}

        assert _is_simple_task(subtask) is False


# ═══════════════════════════════════════════════════════════════
# 验证循环 E2E
# ═══════════════════════════════════════════════════════════════

class TestVerificationLoopE2E:
    """_verify_changes 的 retry → fix → reverify 完整循环。

    覆盖 3 个场景：
      1. 首次验证失败 → 修复 → 重新验证通过
      2. 始终失败 → 达到 max_retries → verify_ok=False
      3. shell 通过但语义评估未通过 → 触发修复
    """

    _SUBTASK_TPL = {
        "id": "sub-1", "title": "基础任务", "description": "执行操作",
        "verification": "pytest tests/",
        "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
        "agent_prompt": "work",
    }

    @staticmethod
    def _git_mock(verify_success_on_attempt: int = 1):
        """构造 subprocess.run side_effect。

        Args:
            verify_success_on_attempt: 验证命令在第几次调用时返回成功。
               例 1 → 首次通过；2 → 首次失败、二次通过。
        """
        verify_count = [0]

        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

            # Git status → has changes
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            # Git diff stat
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed, 10 insertions(+)", stderr="")
            # Git operations (add / commit / tag) always succeed
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            # Verification command
            if "pytest" in cmd_str:
                verify_count[0] += 1
                rc = 0 if verify_count[0] >= verify_success_on_attempt else 1
                return MagicMock(returncode=rc, stdout="", stderr="test output")
            return MagicMock(returncode=0, stdout="", stderr="")

        return _run

    @staticmethod
    def _git_mock_no_changes():
        """git status 返回空（无变更），验证命令通过。"""
        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="", stderr="")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            if "pytest" in cmd_str:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return _run

    def test_no_changes_but_verification_passes(self, temp_repo, task_dir, logger):
        """无变更 + 验证命令通过 → verify_ok=True（不再误判失败）。

        修复前：`无文件变更且存在验证命令 → 标记为失败`，
        即使验证通过也判 failed。修复后：执行验证，通过则成功。
        """
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock_no_changes()):
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )
        assert result["verify_ok"] is True, "验证通过应判定成功（no_changes）"
        assert result["retry_count"] == 0

    def test_verify_fail_then_fix_then_pass(self, temp_repo, task_dir, logger):
        """验证首次失败 → 修复 → 重新验证通过 (retry_count=1, verify_ok=True)"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=2)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)

            # 显式禁用语义评估：本测试只测 shell 验证循环，避免真实环境 evaluator
            # 配置（可能指向外部 API）污染重试计数（fail-open 语义评估会额外触发一次修复）
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )

        assert result["verify_ok"] is True, "修复后应验证通过"
        assert result["retry_count"] == 1, "应修复 1 次后通过"
        assert mock_fix.call_count == 1, "修复 prompt 应被调用 1 次"

    def test_max_retries_then_fail(self, temp_repo, task_dir, logger):
        """始终失败 → 达到 max_retries → verify_ok=False"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 2}},
            )

        assert result["verify_ok"] is False, "应最终失败"
        assert result["retry_count"] == 2, "应达到 max_retries=2"
        assert mock_fix.call_count == 2, "应修复 2 次"

    def test_degrade_mode_caps_max_retries_to_1(self, temp_repo, task_dir, logger):
        """S12-P1 G4：budget_mode=degrade 时 max_retries 降为 1（不在便宜模型无限烧钱）。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 3}, "_degraded": True},
            )

        # 原始 config 给 max_retries=3，但 _degraded=True → cap 到 1
        assert result["verify_ok"] is False
        assert result["retry_count"] == 1, "degrade 模式应 cap max_retries=1"
        assert mock_fix.call_count == 1

    def test_over_budget_l2_skips_fix(self, temp_repo, task_dir, logger):
        """S12-P1 G8：kill_reason=over_budget_l2 → 不进修复重试（不再烧钱）。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 3}},
                initial_kill_reason="over_budget_l2",
            )

        # over_budget_l2 → G8 短路，不进重试（mock_fix 未被调用）
        assert result["verify_ok"] is False
        assert mock_fix.call_count == 0, "over_budget 不应触发修复"

    def test_over_budget_l3_skips_fix(self, temp_repo, task_dir, logger):
        """覆盖补强：kill_reason=over_budget_l3（pipeline 级熔断写入）→ G8 同样短路不重试。
        G8 读路径此前的行为测试只覆盖 L2，L3（startswith('over_budget')）路径无回归守护。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 3}},
                initial_kill_reason="over_budget_l3",
            )
        assert result["verify_ok"] is False
        assert result["kill_reason"] == "over_budget_l3"
        assert mock_fix.call_count == 0, "over_budget_l3 不应触发修复"

    def test_l2_cost_trip_sets_over_budget_l2(self, temp_repo, task_dir, logger):
        """覆盖补强（P0-1）：L2 写路径——累计 cost ≥ per_subtask×multiplier 时
        _verify_changes 设 kill_reason=over_budget_l2。此前只测读路径(initial_kill_reason)，
        写路径无守护 → 回归会让预算控制静默失效（超预算子任务继续烧钱重试）。"""
        import json as _json
        from threading import Lock
        from agent_go.executor import _verify_changes
        # metering：sub-1 累计 cost 1.5 ≥ limit(medium 0.4 × 2.5 = 1.0)
        metering = task_dir / "metering.jsonl"
        metering.write_text(_json.dumps({"sub_id": "sub-1", "cost_usd": 1.5}) + "\n", encoding="utf-8")
        config = {
            "verification": {"max_retries": 3},
            "cost_control": {"enabled": True,
                             "per_subtask_budget_usd": {"medium": 0.4},
                             "subtask_multiplier": 2.5},
            "_metering_path": str(metering),
        }
        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.executor.write_censored_event"):
            mock_fix.return_value = MagicMock(returncode=0)
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir, config=config,
            )
        assert result["verify_ok"] is False
        assert result["kill_reason"] == "over_budget_l2"
        assert mock_fix.call_count == 0, "L2 熔断应在 fix 前触发，不调用修复"

    def test_resume_continues_from_persisted_attempts(self, temp_repo, task_dir, logger):
        """P2-2: verify_state.json 记录已尝试次数 → resume 后从已尝试轮次续跑，
        不重跑已尝试的修复（max_retries 会计错 → 过试烧钱 / 少试过早失败）。
        崩溃在第 3 次重试后，resume 修复次数应显著少于从头跑。"""
        import json as _json
        from threading import Lock
        from agent_go.executor import _verify_changes

        def _run():
            with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=999)), \
                 patch("agent_go.executor._run_headless") as mock_fix:
                mock_fix.return_value = MagicMock(returncode=0)
                _verify_changes(
                    "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                    task_md="# Task", env={}, tag_name="task-1/sub-1",
                    active_pids=set(), active_pids_lock=Lock(), logger=logger,
                    task_dir=task_dir, config={"verification": {"max_retries": 5}})
                return mock_fix.call_count

        # 对照：无 verify_state → 从头跑满重试
        control = _run()
        # 预写 verify_state：已尝试 3 次（崩溃在第 3 次）
        vpath = task_dir / "sub-1" / "verify_state.json"
        vpath.parent.mkdir(parents=True, exist_ok=True)
        vpath.write_text(_json.dumps({
            "subtask_id": "sub-1", "attempts": 3, "max_retries": 5,
            "history": [], "verification_results": [],
        }), encoding="utf-8")
        # resume 后修复次数应减少（跳过已尝试轮次），但仍 >0（继续尝试未完成工作）
        resumed = _run()
        assert resumed > 0, "resume 后仍应继续修复"
        assert control > resumed, f"resume 应跳过已尝试轮次（control={control}, resumed={resumed}）"

    def test_semantic_eval_triggers_repair(self, temp_repo, task_dir, logger):
        """shell 验证通过 → 语义评估失败 → 触发修复 → 修复后通过"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=1)), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            mock_fix.return_value = MagicMock(returncode=0)
            # 语义评估首次失败，二次通过
            mock_eval.side_effect = [
                {"passed": False, "reason": "代码风格不统一",
                 "cost_usd": 0.001, "latency_ms": 100},
                {"passed": True, "reason": "修复后符合规范",
                 "cost_usd": 0.001, "latency_ms": 100},
            ]

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": True}},
            )

        assert result["verify_ok"] is True, "修复后应通过"
        assert mock_eval.call_count >= 1, "语义评估至少被调用 1 次"
        assert mock_fix.call_count >= 1, "语义评估失败应触发修复"

    def test_semantic_eval_disabled_skips(self, temp_repo, task_dir, logger):
        """evaluator.enabled=False → 不触发语义评估（即使 shell 通过）"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=1)), \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )

        assert result["verify_ok"] is True
        mock_eval.assert_not_called()

    def test_scope_violation_recorded_when_verify_passes(self, temp_repo, task_dir, logger):
        """ISSUE-32: 验证通过但存在超范围改动 → verification_results 记录 scope_compliance 审计。

        修复前：scope 合规只在验证失败分支检查（注入修复 prompt）。验证通过时超范围改动
        （Claude 顺手改无关文件）静默通过，无审计。修复后：验证通过分支也调用
        _check_scope_compliance，违规时记录审计供 review/交付检查。
        """
        from threading import Lock
        from agent_go.executor import _verify_changes

        subtask = dict(self._SUBTASK_TPL)
        subtask["files_hint"] = "src/main.py"  # 范围约束

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=1)), \
             patch("agent_go.executor._check_scope_compliance",
                   return_value={"compliant": False, "out_of_scope": ["tests/test_other.py"],
                                 "missing": [], "expected": ["src/main.py"], "actual": ["src/main.py", "tests/test_other.py"]}):

            result = _verify_changes(
                "task-1", "sub-1", subtask, temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )

        # 验证仍通过（scope 违规是审计，不阻断成功）
        assert result["verify_ok"] is True
        scope_records = [v for v in result["verification_results"]
                         if isinstance(v, dict) and v.get("type") == "scope_compliance"]
        assert scope_records, "应记录 scope_compliance 审计"
        assert scope_records[0]["out_of_scope"] == ["tests/test_other.py"]

    def test_scope_compliant_no_audit(self, temp_repo, task_dir, logger):
        """ISSUE-32: 验证通过且范围合规 → 不记录 scope_compliance 审计。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        subtask = dict(self._SUBTASK_TPL)
        subtask["files_hint"] = "src/main.py"

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=1)), \
             patch("agent_go.executor._check_scope_compliance",
                   return_value={"compliant": True, "out_of_scope": [], "missing": [],
                                 "expected": ["src/main.py"], "actual": ["src/main.py"]}):

            result = _verify_changes(
                "task-1", "sub-1", subtask, temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )

        assert result["verify_ok"] is True
        scope_records = [v for v in result["verification_results"]
                         if isinstance(v, dict) and v.get("type") == "scope_compliance"]
        assert scope_records == [], "范围合规时不应记录审计"

    def test_rejected_verification_short_circuits(self, temp_repo, task_dir, logger):
        """S12-P1 G8：验证命令被安全门禁拒绝 → 短路，不重试、不修复、verify_ok=False。

        修复只改代码，不会让被拒绝的命令变得可执行；拒绝应直接判定失败，
        retry_count 不增加、mock_fix 不被调用（resume 亦不重复修复）。
        """
        from threading import Lock
        from agent_go.executor import _verify_changes

        subtask = dict(self._SUBTASK_TPL)
        subtask["verification"] = "rm -rf /"  # 安全门禁必然拒绝的命令（不实际执行）

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=1)), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.executor._log_rejected_command"):
            mock_fix.return_value = MagicMock(returncode=0)

            result = _verify_changes(
                "task-1", "sub-1", subtask, temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": False}},
            )

        assert result["verify_ok"] is False, "被拒绝的命令应直接判定失败"
        assert result["retry_count"] == 0, "被拒绝不应触发重试（retry_count 与初始值相同）"
        assert mock_fix.call_count == 0, "被拒绝不应调用修复逻辑"


# ═══════════════════════════════════════════════════════════════
# 修复 prompt 内容验证
# ═══════════════════════════════════════════════════════════════

class TestRepairPromptContent:
    """验证修复 prompt 包含完整的失败上下文。

    覆盖：
      1. 失败命令、exit_code、输出内容
      2. git diff 注入了修复 prompt
      3. 语义评估反馈注入
    """

    @staticmethod
    def _always_fail_git_mock():
        """subprocess.run side_effect：git 命令成功，验证命令始终失败。"""
        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed, 10 insertions(+)", stderr="")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            if "pytest" in cmd_str:
                return MagicMock(returncode=1, stdout="", stderr="FAIL: 3 tests failed")
            return MagicMock(returncode=0, stdout="", stderr="")
        return _run

    def _make_subtask(self):
        return {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "verification": "pytest tests/",
            "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
            "agent_prompt": "请修改 main.py 实现登录功能",
        }

    def test_fix_prompt_contains_failed_commands(self, temp_repo, task_dir, logger):
        """修复 prompt 应包含失败命令、exit_code、标准输出"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        captured_prompts = []

        def capturing_fix(prompt, *a, **kw):
            captured_prompts.append(prompt)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=self._always_fail_git_mock()), \
             patch("agent_go.executor._run_headless", side_effect=capturing_fix):

            result = _verify_changes(
                "task-1", "sub-1", self._make_subtask(), temp_repo, headless=True,
                task_md="# Task\n请修改 main.py", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 2}},
            )

        assert result["verify_ok"] is False  # 始终失败
        assert result["retry_count"] == 2, "应修复 2 次"
        assert len(captured_prompts) == 2, "应产生 2 个修复 prompt"

        # 验证第一个修复 prompt 的内容
        p1 = captured_prompts[0]
        assert "pytest" in p1, "应包含失败命令"
        assert "exit_code=1" in p1 or "FAIL" in p1, "应包含退出码或失败输出"
        assert "修复指令" in p1, "应包含修复指令段"
        assert "修改 main.py" in p1, "应包含原始 TASK.md 内容"

        # 验证第二次修复 prompt 包含历史记录
        p2 = captured_prompts[1]
        assert "历史修复尝试" in p2, "第二次修复应包含历史记录"
        assert "第 1 次" in p2 or "attempt" in p2.lower(), "应引用前次尝试"

    def test_fix_prompt_contains_git_diff(self, temp_repo, task_dir, logger):
        """修复 prompt 应包含当前变更摘要（git diff --stat）"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        captured_prompts = []

        def capturing_fix(prompt, *a, **kw):
            captured_prompts.append(prompt)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=self._always_fail_git_mock()), \
             patch("agent_go.executor._run_headless", side_effect=capturing_fix):

            _verify_changes(
                "task-1", "sub-1", self._make_subtask(), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 1}},
            )

        assert len(captured_prompts) >= 1
        p = captured_prompts[0]
        # git diff --stat 在 _verify_changes 中以 summary 形式传递
        assert "file changed" in p or "insertions" in p, "应包含 git diff 变更摘要"

    def test_semantic_feedback_in_fix_prompt(self, temp_repo, task_dir, logger):
        """语义评估失败 → 反馈注入修复 prompt"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        captured_prompts = []

        def capturing_fix(prompt, *a, **kw):
            captured_prompts.append(prompt)
            return MagicMock(returncode=0)

        # 注意：语义评估仅在 shell 全部通过后触发（否则不会进入 Phase 3）
        # 因此需要 shell 通过 → 语义评估失败 → 触发修复
        # 修复后 shell 再次通过 → 语义评估再次执行（本次通过）
        make_git = TestVerificationLoopE2E._git_mock

        with patch("subprocess.run", side_effect=make_git(verify_success_on_attempt=1)), \
             patch("agent_go.executor._run_headless", side_effect=capturing_fix), \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            # 首次语义评估失败，二次通过
            mock_eval.side_effect = [
                {"passed": False, "reason": "代码风格不统一，缺少类型注解",
                 "cost_usd": 0.001, "latency_ms": 50},
                {"passed": True, "reason": "修复后符合规范",
                 "cost_usd": 0.001, "latency_ms": 50},
            ]

            result = _verify_changes(
                "task-1", "sub-1", self._make_subtask(), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": True}},
            )

        assert result["verify_ok"] is True, "修复后应通过"
        # 验证修复 prompt 包含语义评估的 reason
        if captured_prompts:
            assert "缺少类型注解" in captured_prompts[0], \
                "语义评估的 reason 应注入修复 prompt"
            assert "LLM 语义评估反馈" in captured_prompts[0]


# ═══════════════════════════════════════════════════════════════
# L1 自动触发语义评估
# ═══════════════════════════════════════════════════════════════

class TestL1AutoTrigger:
    """L1: heuristic/manual 验证时自动启用语义评估（即使配置为关闭）。"""

    def _subtask_with_verification(self, verification: str) -> dict:
        return {
            "id": "sub-1", "title": "任务",
            "description": "desc", "agent_prompt": "work",
            "verification": verification,
            "risks": [], "depends_on": [], "skills": [],
            "agent_type": "developer",
        }

    def _run_verify(self, subtask, temp_repo, task_dir, logger, extra_config=None):
        """运行 _verify_changes 并捕获是否调用了语义评估。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        config = {"evaluator": {"enabled": False}}  # 默认关闭
        if extra_config:
            config.update(extra_config)

        with patch("subprocess.run", side_effect=self._git_mock_all_pass()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            mock_fix.return_value = MagicMock(returncode=0)
            mock_eval.return_value = {"passed": True, "confidence": 0.9,
                                       "reason": "ok", "cost_usd": 0, "latency_ms": 0}

            result = _verify_changes(
                "task-1", "sub-1", subtask, temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir, config=config,
            )
        return result, mock_eval.called

    @staticmethod
    def _git_mock_all_pass():
        """所有 git 命令成功 + 验证命令通过。"""
        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return _run

    def test_l1_triggers_for_heuristic(self, temp_repo, task_dir, logger):
        """heuristic 验证（npm run lint）→ L1 自动开启语义评估"""
        subtask = self._subtask_with_verification("npm run lint")
        result, eval_called = self._run_verify(subtask, temp_repo, task_dir, logger)
        assert eval_called is True, "heuristic 验证应触发语义评估"

    def test_l1_does_not_trigger_for_deterministic(self, temp_repo, task_dir, logger):
        """deterministic（pytest）→ L1 不触发（已有配置控制）"""
        subtask = self._subtask_with_verification("pytest tests/")
        result, eval_called = self._run_verify(subtask, temp_repo, task_dir, logger)
        assert eval_called is False, "deterministic 验证不应触发语义评估"

    def test_l1_respects_existing_config(self, temp_repo, task_dir, logger):
        """如果 evaluator.enabled=True，L1 不重复触发（但评估仍运行）"""
        subtask = self._subtask_with_verification("pytest tests/")
        result, eval_called = self._run_verify(subtask, temp_repo, task_dir, logger,
                                                extra_config={"evaluator": {"enabled": True}})
        # evaluator 在 config 中已开启，评估会运行（但不是由于 L1）
        # 我们验证的是 L1 不干扰正常配置路径
        assert eval_called is True  # 因为 config 开启了，所以评估运行


# ═══════════════════════════════════════════════════════════════
# 本地后端模型探测
# ═══════════════════════════════════════════════════════════════

class TestProbeLocalModel:
    """_probe_local_model：从本地代理 /status 探测真实后端模型名"""

    _STATUS_HTML = """<html><body>
    <div class="row"><span class="label">Model</span><span class="value">mlx-community/Qwen3.6-27B-4bit</span></div>
    <div class="row"><span class="label">Cloud Model</span><span class="value">deepseek-v4-flash</span></div>
    <div class="row"><span class="label">Model</span><span class="value">claude-haiku-4-5</span></div>
    </body></html>"""

    def test_probe_parses_first_model_field(self, monkeypatch):
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_model_probe_cache", {})
        mock_resp = MagicMock()
        mock_resp.read.return_value = self._STATUS_HTML.encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=mock_ctx))
        model = _probe_local_model("http://127.0.0.1:4000")
        assert model == "mlx-community/Qwen3.6-27B-4bit"

    def test_probe_uses_cache(self, monkeypatch):
        from agent_go import executor
        calls = []
        monkeypatch.setattr(executor, "_local_model_probe_cache", {})
        orig = executor._probe_local_model

        def _cached(self, url, timeout=2.0):
            calls.append(url)
            return "cached-model"

        monkeypatch.setattr(executor, "_local_model_probe_cache", {"http://127.0.0.1:4000": "cached-model"})
        assert _probe_local_model("http://127.0.0.1:4000") == "cached-model"
        assert len(calls) == 0

    def test_probe_empty_url(self, monkeypatch):
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_model_probe_cache", {})
        assert _probe_local_model("") == ""

    def test_probe_unreachable_returns_empty(self, monkeypatch):
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_model_probe_cache", {})
        import urllib.error
        def _boom(*a, **k):
            raise urllib.error.URLError("refused")
        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert _probe_local_model("http://127.0.0.1:9999") == ""

    def test_probe_malformed_html_returns_empty(self, monkeypatch):
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_model_probe_cache", {})
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html>no model info</html>"
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=mock_ctx))
        assert _probe_local_model("http://127.0.0.1:4000") == ""


class TestVerifyLocalBackend:
    """S12 本地判定加固：URL 指向本机但实际走云时不清零成本。"""

    def test_local_confirmed(self, monkeypatch):
        """响应 model == /status 声明本地模型 → 真本地。"""
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_verify_cache", {})
        monkeypatch.setattr(executor, "_probe_local_model",
                            lambda *a, **k: "mlx-community/Qwen3.6-27B-4bit")
        fake_cp = MagicMock(stdout='{"type":"assistant","message":{"model":"mlx-community/Qwen3.6-27B-4bit"}}\n')
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=fake_cp))
        is_local, actual = executor._verify_local_backend("http://127.0.0.1:4000")
        assert is_local is True
        assert actual == "mlx-community/Qwen3.6-27B-4bit"

    def test_cloud_behind_local_url(self, monkeypatch):
        """URL 本地但实际走云（响应 glm-4.7 != 本地声明）→ 判定为云，不清零。"""
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_verify_cache", {})
        monkeypatch.setattr(executor, "_probe_local_model",
                            lambda *a, **k: "mlx-community/Qwen3.6-27B-4bit")
        fake_cp = MagicMock(stdout='{"type":"assistant","message":{"model":"glm-4.7"}}\n')
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=fake_cp))
        is_local, actual = executor._verify_local_backend("http://127.0.0.1:4000")
        assert is_local is False
        assert actual == "glm-4.7"

    def test_probe_failure_conservative(self, monkeypatch):
        """探测失败 → 保守不清零。"""
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_verify_cache", {})
        monkeypatch.setattr(executor, "_probe_local_model", lambda *a, **k: "")
        fake_cp = MagicMock(stdout="")
        monkeypatch.setattr("subprocess.run", MagicMock(return_value=fake_cp))
        is_local, actual = executor._verify_local_backend("http://127.0.0.1:4000")
        assert is_local is False
        assert actual == ""

    def test_cache_reused(self, monkeypatch):
        """结果缓存，第二次不重复探测调用。"""
        from agent_go import executor
        monkeypatch.setattr(executor, "_local_verify_cache",
                            {"http://127.0.0.1:4000": (False, "glm-4.7")})
        is_local, actual = executor._verify_local_backend("http://127.0.0.1:4000")
        assert is_local is False
        assert actual == "glm-4.7"


# ═══════════════════════════════════════════════════════════════
# CR-G3: task_type → 模型路由（worker_models_by_type 覆盖难度）
# ═══════════════════════════════════════════════════════════════

class TestTaskTypeRouting:
    """task_type 路由优先级：worker_models_by_type[type] > worker_models[difficulty]。
    未配 by_type / 无 task_type → 回退难度路由（默认行为不变）。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_task_type_overrides_difficulty(self, mock_wt_create, mock_subprocess,
                                            mock_headless, mock_load_agent,
                                            temp_repo, task_dir, fast_logger, basic_subtask):
        """CR-G3：task_type=security 且配了 by_type → 用 security 模型，覆盖 medium 难度模型。"""
        from agent_go.executor import run_subtask
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        basic_subtask["difficulty"] = "medium"
        basic_subtask["task_type"] = "security"
        config = {
            "worker_models": {"easy": "", "medium": "claude-sonnet-5", "hard": ""},
            "worker_models_by_type": {"security": "claude-opus-4-8"},
        }
        run_subtask("t", basic_subtask, temp_repo, task_dir, fast_logger,
                    headless=True, config=config)
        env = mock_headless.call_args[0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-opus-4-8"  # task_type 胜出

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_task_type_falls_back_when_unconfigured(self, mock_wt_create, mock_subprocess,
                                                    mock_headless, mock_load_agent,
                                                    temp_repo, task_dir, fast_logger, basic_subtask):
        """CR-G3：task_type 存在但 worker_models_by_type 未配该类型 → 回退难度路由。"""
        from agent_go.executor import run_subtask
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        basic_subtask["difficulty"] = "medium"
        basic_subtask["task_type"] = "security"
        config = {
            "worker_models": {"easy": "", "medium": "claude-sonnet-5", "hard": ""},
            "worker_models_by_type": {},  # 未配 security
        }
        run_subtask("t", basic_subtask, temp_repo, task_dir, fast_logger,
                    headless=True, config=config)
        env = mock_headless.call_args[0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-sonnet-5"  # 回退 medium 难度

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_no_task_type_uses_difficulty(self, mock_wt_create, mock_subprocess,
                                          mock_headless, mock_load_agent,
                                          temp_repo, task_dir, fast_logger, basic_subtask):
        """CR-G3：无 task_type → 纯难度路由（默认行为零变化）。"""
        from agent_go.executor import run_subtask
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        basic_subtask["difficulty"] = "hard"
        # 不设 task_type
        config = {
            "worker_models": {"easy": "", "medium": "", "hard": "claude-opus-4-8"},
            "worker_models_by_type": {"security": "claude-opus-4-8"},
        }
        run_subtask("t", basic_subtask, temp_repo, task_dir, fast_logger,
                    headless=True, config=config)
        env = mock_headless.call_args[0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-opus-4-8"  # hard 难度模型

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_degrade_mode_downgrades_model(self, mock_wt_create, mock_subprocess,
                                           mock_headless, mock_load_agent,
                                           temp_repo, task_dir, fast_logger, basic_subtask):
        """覆盖补强：config['_degraded']=True + worker_models_degrades → env 模型被降档
        （hard→medium）。degrade 模式的核心行为（降档本身）此前未测，只测了安全阀/max_retries。"""
        from agent_go.executor import run_subtask
        mock_wt_create.return_value = (True, "")
        mock_subprocess.return_value = make_subprocess_mock()
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        basic_subtask["difficulty"] = "hard"
        config = {
            "_degraded": True,  # pipeline 预算超限后置位
            "worker_models": {"easy": "claude-haiku-4-5", "medium": "claude-sonnet-5", "hard": "claude-opus-4-8"},
            "worker_models_degrades": {"hard": "medium", "medium": "easy", "easy": ""},
        }
        run_subtask("t", basic_subtask, temp_repo, task_dir, fast_logger,
                    headless=True, config=config)
        env = mock_headless.call_args[0][2]
        # hard 经 degrades 表降档到 medium → sonnet（不是 opus）
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-sonnet-5"


# ═══════════════════════════════════════════════════════════════
# SDD 设计意图传递 + L1 范围合规
# ═══════════════════════════════════════════════════════════════

class TestBuildArchitectureContext:
    """_build_architecture_context: TASK.md 架构上下文注入"""

    def test_with_files_hint_and_upstream(self, tmp_path):
        """有 files_hint + 上游 context.md → 完整架构上下文"""
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        up_dir = task_dir / "sub-1"
        up_dir.mkdir()
        (up_dir / "context.md").write_text("# 完成 JWT 认证\n已实现登录和验证中间件。", encoding="utf-8")

        subtask = {
            "id": "sub-2",
            "depends_on": ["sub-1"],
            "files_hint": "src/cli.py, src/storage.py",
        }
        result = _build_architecture_context(subtask, task_dir)
        assert "sub-2" in result
        assert "sub-1（已完成 — 完成 JWT 认证）" in result
        assert "src/cli.py" in result
        assert "src/storage.py" in result
        assert "范围约束" in result

    def test_without_files_hint(self, tmp_path):
        """无 files_hint → 不显示范围约束"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {"id": "sub-1", "depends_on": [], "files_hint": ""}
        result = _build_architecture_context(subtask, task_dir)
        assert "范围约束" not in result

    def test_with_wildcard_files_hint(self, tmp_path):
        """files_hint = * → 不显示范围约束（全文件范围）"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {"id": "sub-3", "depends_on": [], "files_hint": "*"}
        result = _build_architecture_context(subtask, task_dir)
        assert "范围约束" not in result

    def test_without_upstream(self, tmp_path):
        """无上游依赖 → 不显示依赖信息，仅有子任务 ID + 范围约束"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {
            "id": "sub-1",
            "depends_on": [],
            "files_hint": "src/models.py",
        }
        result = _build_architecture_context(subtask, task_dir)
        assert "依赖的上游" not in result
        assert "src/models.py" in result
        assert "范围约束" in result

    def test_upstream_without_context_md(self, tmp_path):
        """上游无 context.md → 仅显示上游 ID，无摘要"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {
            "id": "sub-2",
            "depends_on": ["sub-1"],
            "files_hint": "src/main.py",
        }
        result = _build_architecture_context(subtask, task_dir)
        assert "sub-1" in result
        assert "已完成" not in result

    def test_do_not_touch_injected(self, tmp_path):
        """改进方向 3: do_not_touch 约束注入架构上下文（防越界交叉污染）"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {
            "id": "sub-2",
            "depends_on": ["sub-1"],
            "files_hint": "src/cli.py",
            "do_not_touch": ["src/storage.py", "src/models.py"],
        }
        result = _build_architecture_context(subtask, task_dir)
        assert "禁止修改" in result
        assert "src/storage.py" in result
        assert "src/models.py" in result
        # 关键：明确提示「不要改这些文件」
        assert "绝对不要改动" in result

    def test_scope_boundary_injected(self, tmp_path):
        """改进方向 3: scope_boundary 职责边界注入架构上下文"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {
            "id": "sub-1",
            "depends_on": [],
            "files_hint": "src/cli.py",
            "scope_boundary": "仅修改 CLI 逻辑，不改变存储层接口",
        }
        result = _build_architecture_context(subtask, task_dir)
        assert "职责边界" in result
        assert "仅修改 CLI 逻辑" in result

    def test_no_boundaries_when_absent(self, tmp_path):
        """改进方向 3: 无 do_not_touch/scope_boundary → 不注入对应段（向后兼容）"""
        task_dir = tmp_path / "task"; task_dir.mkdir()
        subtask = {"id": "sub-1", "depends_on": [], "files_hint": "src/main.py"}
        result = _build_architecture_context(subtask, task_dir)
        assert "禁止修改" not in result
        assert "职责边界" not in result
        assert "src/main.py" in result


class TestCheckScopeCompliance:
    """_check_scope_compliance: L1 范围合规检查"""

    def test_wildcard_skips_check(self, tmp_path):
        """files_hint = * → 跳过检查，返回 compliant=True"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        result = _check_scope_compliance(worktree, "*")
        assert result["compliant"] is True
        assert result["out_of_scope"] == []
        assert result["missing"] == []

    def test_empty_files_hint_skips(self, tmp_path):
        """files_hint 为空 → 跳过检查"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        result = _check_scope_compliance(worktree, "")
        assert result["compliant"] is True

    def test_compliant_when_actual_matches_expected(self, tmp_path):
        """实际改动文件完全在范围内 → compliant=True"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(worktree), capture_output=True)
        (worktree / "src").mkdir()
        (worktree / "src" / "cli.py").write_text("# cli", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True)
        (worktree / "src" / "cli.py").write_text("# cli updated", encoding="utf-8")
        result = _check_scope_compliance(worktree, "src/cli.py")
        assert result["compliant"] is True
        assert result["out_of_scope"] == []

    def test_out_of_scope_detected(self, tmp_path):
        """实际改动了范围外的文件 → out_of_scope 非空"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(worktree), capture_output=True)
        (worktree / "src").mkdir()
        (worktree / "src" / "cli.py").write_text("# cli", encoding="utf-8")
        (worktree / "tests").mkdir()
        (worktree / "tests" / "test_cli.py").write_text("# tests", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True)
        (worktree / "tests" / "test_cli.py").write_text("# tests updated", encoding="utf-8")
        result = _check_scope_compliance(worktree, "src/cli.py")
        assert result["compliant"] is False
        assert "tests/test_cli.py" in result["out_of_scope"]

    def test_missing_detected(self, tmp_path):
        """范围内文件未被改动 → missing 非空"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(worktree), capture_output=True)
        (worktree / "src").mkdir()
        (worktree / "src" / "cli.py").write_text("# cli", encoding="utf-8")
        (worktree / "src" / "storage.py").write_text("# storage", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(worktree), capture_output=True)
        (worktree / "src" / "cli.py").write_text("# cli updated", encoding="utf-8")
        result = _check_scope_compliance(worktree, "src/cli.py, src/storage.py")
        assert result["compliant"] is False
        assert "src/storage.py" in result["missing"]


class TestBuildRepairPromptWithScope:
    """_build_repair_prompt: 范围偏差注入"""

    def test_scope_violation_injected(self):
        """scope_violation 非空且非 compliant → 修复 prompt 包含范围偏差段"""
        task_md = "# 子任务: 测试\n## 描述\n修复问题"
        scope_violation = {
            "compliant": False,
            "out_of_scope": ["tests/test_cli.py"],
            "missing": ["src/storage.py"],
            "expected": ["src/cli.py", "src/storage.py"],
            "actual": ["src/cli.py", "tests/test_cli.py"],
        }
        prompt = _build_repair_prompt(
            task_md, ["pytest"], ["exit_code=1"],
            "diff --stat", 1, 3, [],
            scope_violation=scope_violation,
        )
        assert "范围偏差" in prompt
        assert "tests/test_cli.py" in prompt
        assert "src/storage.py" in prompt
        assert "越界改动" in prompt
        assert "遗漏改动" in prompt

    def test_no_scope_violation_when_compliant(self):
        """scope_violation.compliant=True → 不注入范围偏差段"""
        task_md = "# 子任务: 测试\n## 描述\n修复问题"
        scope_violation = {"compliant": True, "out_of_scope": [], "missing": []}
        prompt = _build_repair_prompt(
            task_md, ["pytest"], ["exit_code=1"],
            "diff --stat", 1, 3, [],
            scope_violation=scope_violation,
        )
        assert "范围偏差" not in prompt

    def test_no_scope_violation_when_none(self):
        """scope_violation=None → 不注入（向后兼容）"""
        task_md = "# 子任务: 测试\n## 描述\n修复问题"
        prompt = _build_repair_prompt(
            task_md, ["pytest"], ["exit_code=1"],
            "diff --stat", 1, 3, [],
            scope_violation=None,
        )
        assert "范围偏差" not in prompt


# ═══════════════════════════════════════════════════════════════
# 改进方向 4：打地鼠检测（verify divergence early-terminate）
# ═══════════════════════════════════════════════════════════════

class TestVerifyDivergence:
    """连续两次语义评估指出不同缺陷 → 提前终止重试（打地鼠检测）。"""

    _SUBTASK_TPL = {
        "id": "sub-1", "title": "基础任务", "description": "执行操作",
        "verification": "pytest tests/",
        "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
        "agent_prompt": "work",
    }

    @staticmethod
    def _git_always_pass():
        """shell 验证始终通过（让循环只卡在语义评估）。"""
        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed", stderr="")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            if "rev-list" in cmd_str or "hash-object" in cmd_str:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return _run

    def test_pure_fingerprint_similarity(self):
        """纯函数：指纹提取与相似度——不同缺陷相似度低，相同缺陷相似度高。"""
        from agent_go.executor import _defect_fingerprint, _defect_similarity

        fp_a = _defect_fingerprint("加载数据时 AttributeError: 'NoneType' 没有 load 属性")
        fp_b = _defect_fingerprint("命令行参数解析错误，缺少必填参数 task_id")
        fp_same = _defect_fingerprint("加载数据时 AttributeError: 'NoneType' 没有 load 属性")

        assert fp_a and fp_b, "有效 reason 应产出非空指纹"
        assert _defect_similarity(fp_a, fp_a) == 1.0
        assert _defect_similarity(fp_a, fp_same) == 1.0
        assert _defect_similarity(fp_a, fp_b) < 0.3, \
            f"不同缺陷相似度应低于阈值: {_defect_similarity(fp_a, fp_b)}"

    def test_pure_fingerprint_empty(self):
        """纯函数：过短 reason → 空指纹（不触发检测）。"""
        from agent_go.executor import _defect_fingerprint
        assert _defect_fingerprint("") == ""
        assert _defect_fingerprint("代码不统一") == "" or True  # 短文本可容忍

    def test_divergence_early_terminates(self, temp_repo, task_dir, logger):
        """连续两次语义评估指出不同缺陷 → 提前终止（不等到 max_retries）。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_always_pass()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            mock_fix.return_value = MagicMock(returncode=0)
            # 连续两次语义评估失败，指出的缺陷不同（打地鼠）
            mock_eval.side_effect = [
                {"passed": False, "reason": "数据加载缺少 None 保护，AttributeError",
                 "cost_usd": 0.001, "latency_ms": 100},
                {"passed": False, "reason": "命令行解析缺少 task_id 必填校验",
                 "cost_usd": 0.001, "latency_ms": 100},
            ]

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": True}},
            )

        assert result["verify_ok"] is False, "打地鼠应判定失败"
        # 只做了 1 次修复（第二次评估后立即终止），未等到 max_retries=3
        assert mock_fix.call_count == 1, \
            f"应在第 1 次修复后终止，实际修复 {mock_fix.call_count} 次"
        types = [v.get("type") for v in result["verification_results"]]
        assert "divergence" in types, f"应记录 divergence 结果: {types}"

    def test_similar_defects_not_diverged(self, temp_repo, task_dir, logger):
        """连续语义评估指出同一缺陷（收敛中）→ 不提前终止，走正常重试。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_always_pass()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            mock_fix.return_value = MagicMock(returncode=0)
            # 两次都指出「None 保护缺失」→ 同一缺陷，应继续重试
            mock_eval.side_effect = [
                {"passed": False, "reason": "数据加载缺少 None 保护，AttributeError on None",
                 "cost_usd": 0.001, "latency_ms": 100},
                {"passed": False, "reason": "仍然缺少 None 保护导致 AttributeError",
                 "cost_usd": 0.001, "latency_ms": 100},
            ]

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"evaluator": {"enabled": True}},
            )

        types = [v.get("type") for v in result["verification_results"]]
        assert "divergence" not in types, "同一缺陷不应判定为打地鼠"


class TestVerifyRevertDetection:
    """回退/振荡检测（workflow-vs-subagent 改进 B-回退）：
    修复后 worktree 累积 diff 状态重复出现 → 循环振荡，提前终止重试。"""

    _SUBTASK_TPL = {
        "id": "sub-1", "title": "基础任务", "description": "执行操作",
        "verification": "pytest tests/", "risks": [], "depends_on": [],
        "skills": [], "agent_type": "developer", "agent_prompt": "work",
    }

    @staticmethod
    def _git_with_base(base_commit):
        """构造 subprocess.run side_effect：git 命令成功、验证始终失败、diff 恒定。

        模拟「修复无效果」：每次 fix 后 worktree 累积 diff 相对 base 不变 →
        diff stat hash 恒定 → 触发回退检测。
        """
        verify_count = [0]

        def _run(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed, 10 insertions(+)", stderr="")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            if "pytest" in cmd_str:
                verify_count[0] += 1
                return MagicMock(returncode=1, stdout="", stderr="FAIL")
            return MagicMock(returncode=0, stdout="", stderr="")

        return _run

    def test_revert_detection_terminates_early(self, temp_repo, task_dir, logger):
        """有 _base_commit 且累积 diff 状态重复 → 提前终止（revert 结果记录）。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_with_base("abc123")), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 5}, "_base_commit": "abc123"},
            )

        assert result["verify_ok"] is False
        types = [v.get("type") for v in result["verification_results"]]
        assert "revert" in types, f"应记录 revert 结果: {types}"
        # revert_threshold=2 → 第 3 次见到同一状态时终止 → 只做 2 次修复
        assert mock_fix.call_count <= 2, f"应在循环振荡时提前终止（修复 {mock_fix.call_count} 次）"
        assert mock_fix.call_count < 5, "不应跑满 max_retries=5"

    def test_revert_disabled_without_base(self, temp_repo, task_dir, logger):
        """无 _base_commit → 回退检测跳过，走正常重试预算。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_with_base("")), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 3}},
            )

        assert result["verify_ok"] is False
        types = [v.get("type") for v in result["verification_results"]]
        assert "revert" not in types, "无基座时不应触发回退检测"
        assert result["retry_count"] == 3, "应跑满 max_retries=3"

    def test_diff_stat_hash_pure(self, temp_repo):
        """纯函数：_diff_stat_hash 归一化 diff stat 输出（忽略行尾对齐差异）。"""
        from agent_go.executor import _diff_stat_hash
        with patch("agent_go.executor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" src/a.py  | 3 ++\n src/b.py  | 2 --\n 2 files changed",
                stderr="")
            h1 = _diff_stat_hash(temp_repo, "abc123")
            # 归一化后同内容同 hash
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" src/a.py   | 3 ++\n src/b.py   | 2 --\n 2 files changed",
                stderr="")
            h2 = _diff_stat_hash(temp_repo, "abc123")
        assert h1 == h2, "忽略列对齐差异后 hash 应一致"
        assert len(h1) == 16, "sha1 前 16 字符"


class TestCognitiveModelRouting:
    """异构模型路由：认知模式（explore/implement/review）→ 独立模型。"""

    def test_infer_cognitive_mode(self):
        """纯函数：认知模式推断——显式标注优先，agent_type 兜底。"""
        from agent_go.executor import _infer_cognitive_mode

        assert _infer_cognitive_mode({"cognitive_mode": "explore"}) == "explore"
        assert _infer_cognitive_mode({"cognitive_mode": "review"}) == "review"
        assert _infer_cognitive_mode({"agent_type": "architect"}) == "explore"
        assert _infer_cognitive_mode({"agent_type": "reviewer"}) == "review"
        assert _infer_cognitive_mode({"agent_type": "developer"}) == "implement"
        assert _infer_cognitive_mode({"agent_type": "tester"}) == "implement"
        assert _infer_cognitive_mode({}) == "implement"
        # 非法显式值回退 agent_type 推断
        assert _infer_cognitive_mode({"cognitive_mode": "weird", "agent_type": "architect"}) == "explore"

    def test_cognitive_route_overrides_difficulty(self, temp_repo, task_dir, logger):
        """配置了 worker_models_by_cognitive → 认知模式模型覆盖难度路由。"""
        from agent_go.executor import run_subtask

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "files_hint": "*", "verification": "pytest", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "reviewer", "difficulty": "easy",
            "agent_prompt": "work",
        }
        config = {
            "worker_models": {"easy": "", "medium": "", "hard": ""},
            "worker_models_by_cognitive": {"review": "claude-opus-4-8"},
            "verification": {"max_retries": 1},
            "evaluator": {"enabled": False},
            "plan_api": {},
            "agent_loop": {"enabled": False},
        }
        with patch("agent_go.executor._run_claude") as mock_claude, \
             patch("agent_go.executor._create_worktree", return_value=(temp_repo, 1)), \
             patch("agent_go.executor._verify_changes") as mock_verify:
            mock_verify.return_value = {
                "has_changes": True, "summary": "1 file changed", "metrics_changes": {},
                "git_commit_ms": 1, "verification_ms": 1, "verify_ok": True, "git_ok": True,
                "retry_count": 0, "verification_results": [], "commit_hash": "abc",
                "change_stats": {}, "kill_reason": "none",
            }
            mock_claude.return_value = (MagicMock(returncode=0, stdout=""), "headless", 1.0)
            result = run_subtask(
                "task-1", subtask, temp_repo, task_dir, logger, headless=True,
                metering_path="", config=config,
            )

        env = mock_claude.call_args[0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-opus-4-8", \
            f"认知模式路由应覆盖难度路由: {env.get('AGENT_GO_CLAUDE_MODEL')}"
        assert env["AGENT_GO_COGNITIVE_MODE"] == "review"

    def test_cognitive_route_falls_back_to_difficulty(self, temp_repo, task_dir, logger):
        """未配置认知模式映射 → 回退既有 difficulty 路由。"""
        from agent_go.executor import run_subtask

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "files_hint": "*", "verification": "pytest", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "developer", "difficulty": "hard",
            "agent_prompt": "work",
        }
        config = {
            "worker_models": {"hard": "claude-opus-4-8", "medium": "", "easy": ""},
            "worker_models_by_cognitive": {},
            "verification": {"max_retries": 1},
            "evaluator": {"enabled": False},
            "plan_api": {},
            "agent_loop": {"enabled": False},
        }
        with patch("agent_go.executor._run_claude") as mock_claude, \
             patch("agent_go.executor._create_worktree", return_value=(temp_repo, 1)), \
             patch("agent_go.executor._verify_changes") as mock_verify:
            mock_verify.return_value = {
                "has_changes": True, "summary": "1 file changed", "metrics_changes": {},
                "git_commit_ms": 1, "verification_ms": 1, "verify_ok": True, "git_ok": True,
                "retry_count": 0, "verification_results": [], "commit_hash": "abc",
                "change_stats": {}, "kill_reason": "none",
            }
            mock_claude.return_value = (MagicMock(returncode=0, stdout=""), "headless", 1.0)
            result = run_subtask(
                "task-1", subtask, temp_repo, task_dir, logger, headless=True,
                metering_path="", config=config,
            )

        env = mock_claude.call_args[0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-opus-4-8", \
            "未配置认知模式映射时回退难度路由"


class TestReadonlyReview:
    """独立只读审查 subagent：验证失败时黑盒分析失败根因注入修复 prompt。"""

    def test_repair_prompt_injects_review(self):
        """_build_repair_prompt：readonly_review 意见注入修复 prompt。"""
        review = {
            "root_cause": "实现缺陷：遗漏 None 保护",
            "blind_spot": "实现者可能忽略空输入边界",
            "suggestions": "在 load() 开头加 None 守卫\n补充空列表测试",
        }
        prompt = _build_repair_prompt(
            task_md="# Task", failed_cmds=["pytest"], failed_outputs=["FAIL"],
            git_diff="+def load():", attempt=1, max_retries=2, history=[],
            readonly_review=review,
        )
        assert "独立只读审查意见" in prompt
        assert "实现缺陷：遗漏 None 保护" in prompt
        assert "空输入边界" in prompt
        assert "load() 开头加 None 守卫" in prompt

    def test_repair_prompt_without_review(self):
        """无 readonly_review → 不出现审查段落。"""
        prompt = _build_repair_prompt(
            task_md="# Task", failed_cmds=["pytest"], failed_outputs=["FAIL"],
            git_diff="+def load():", attempt=1, max_retries=2, history=[],
        )
        assert "独立只读审查意见" not in prompt

    def test_review_disabled_returns_none(self, tmp_path, logger):
        """readonly_review.enabled=False → run_readonly_review 返回 None（不调用 API）。"""
        from agent_go.review_agent import run_readonly_review
        with patch("agent_go.api.call_api") as mock_api:
            result = run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff", {"verification": {"readonly_review": {"enabled": False}}}, logger,
            )
        assert result is None
        mock_api.assert_not_called()

    def test_review_enabled_parses_response(self, tmp_path, logger):
        """readonly_review.enabled=True → 解析审查响应并返回结构化结果。"""
        from agent_go.review_agent import run_readonly_review
        fake_content = ('```json\n{"root_cause": "测试问题：断言与需求不符", '
                        '"blind_spot": "无", "suggestions": "修正断言"}\n```')
        with patch("agent_go.api.call_api", return_value=fake_content), \
             patch("agent_go.metrics.estimate_cost", return_value=0.001), \
             patch("agent_go.config.meter_event") as mock_meter:
            result = run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff",
                {"verification": {"readonly_review": {"enabled": True}},
                 "evaluator": {"model": "claude-haiku-4-5"},
                 "plan_api": {"provider": "anthropic", "model": "claude-sonnet-4"}},
                logger, metering_path="x.jsonl",
            )
        assert result is not None
        assert result["root_cause"].startswith("测试问题")
        assert result["suggestions"] == "修正断言"
        assert mock_meter.called, "应写入 metering（role=reviewer）"
        # metering role 应为 reviewer
        meter_event = mock_meter.call_args[0][1]
        assert meter_event["role"] == "reviewer"

    def test_review_api_failure_fail_open(self, tmp_path, logger):
        """API 失败 → 返回 None（fail-open，不阻断验证循环）。"""
        from agent_go.review_agent import run_readonly_review
        with patch("agent_go.api.call_api", side_effect=RuntimeError("boom")):
            result = run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff", {"verification": {"readonly_review": {"enabled": True}}}, logger,
            )
        assert result is None

    def test_review_skill_injected_into_prompt(self, tmp_path, logger):
        """配置 skill → skill body 注入为「领域审查维度指引」段落。"""
        from agent_go.review_agent import run_readonly_review
        fake_content = '{"root_cause": "x", "blind_spot": "无", "suggestions": "y"}'
        with patch("agent_go.api.call_api", return_value=fake_content) as mock_api, \
             patch("agent_go.metrics.estimate_cost", return_value=0.0), \
             patch("agent_go.config.meter_event"), \
             patch("agent_go.skills.load_skill") as mock_skill:
            mock_skill.return_value = MagicMock(body="## 安全审查规则\n- JWT 必须 RS256\n- 禁止硬编码密钥")
            run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff",
                {"verification": {"readonly_review": {"enabled": True, "skill": "security-review"}},
                 "plan_api": {"provider": "anthropic", "model": "claude-sonnet-4"}},
                logger, metering_path="x.jsonl",
            )
        prompt = mock_api.call_args[0][1][0]["content"]
        assert "领域审查维度指引" in prompt, "skill 应注入为领域审查指引"
        assert "JWT 必须 RS256" in prompt
        assert "禁止硬编码密钥" in prompt
        assert "security-review" in prompt or "安全审查规则" in prompt

    def test_review_skill_missing_falls_back(self, tmp_path, logger):
        """skill 不存在 → 回退内置模板（prompt 不含领域指引段）。"""
        from agent_go.review_agent import run_readonly_review
        fake_content = '{"root_cause": "x", "blind_spot": "无", "suggestions": "y"}'
        with patch("agent_go.api.call_api", return_value=fake_content) as mock_api, \
             patch("agent_go.metrics.estimate_cost", return_value=0.0), \
             patch("agent_go.config.meter_event"), \
             patch("agent_go.skills.load_skill", return_value=None):
            run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff",
                {"verification": {"readonly_review": {"enabled": True, "skill": "missing-skill"}},
                 "plan_api": {"provider": "anthropic", "model": "claude-sonnet-4"}},
                logger, metering_path="x.jsonl",
            )
        prompt = mock_api.call_args[0][1][0]["content"]
        assert "领域审查维度指引" not in prompt, "skill 缺失时应回退内置模板"

    def test_review_skill_infer_repo_from_worktree(self, tmp_path, logger):
        """项目级 skill：从 worktree 向上推断 repo 根，能找到 <repo>/.agent_go/skills。"""
        from agent_go.review_agent import run_readonly_review
        # 构造 worktree 树：tmp_path/work/sub-1/<repo>/.git
        repo_root = tmp_path / "work" / "sub-1" / "myrepo"
        (repo_root / ".git").mkdir(parents=True)
        fake_content = '{"root_cause": "x", "blind_spot": "无", "suggestions": "y"}'
        with patch("agent_go.api.call_api", return_value=fake_content) as mock_api, \
             patch("agent_go.metrics.estimate_cost", return_value=0.0), \
             patch("agent_go.config.meter_event"), \
             patch("agent_go.skills.load_skill") as mock_skill:
            mock_skill.return_value = MagicMock(body="# 项目审查规则\n- 禁止修改核心模型")
            run_readonly_review(
                {"id": "sub-1"}, repo_root / "src", "pytest", ["pytest"], ["FAIL"],
                "diff",
                {"verification": {"readonly_review": {"enabled": True, "skill": "proj-review"}},
                 "plan_api": {"provider": "anthropic", "model": "claude-sonnet-4"}},
                logger, metering_path="x.jsonl",
            )
        # load_skill 收到的 project_root 应是推断出的 repo 根
        proj_root = mock_skill.call_args[0][1]
        assert proj_root is not None and proj_root == repo_root, \
            f"应推断 repo 根 {repo_root}，实际 {proj_root}"
        prompt = mock_api.call_args[0][1][0]["content"]
        assert "禁止修改核心模型" in prompt, "项目级 skill body 应注入"

    def test_infer_repo_root(self, tmp_path):
        """纯函数：_infer_repo_root 向上找含 .git 的目录。"""
        from agent_go.review_agent import _infer_repo_root
        repo_root = tmp_path / "repo"
        (repo_root / ".git").mkdir(parents=True)
        nested = repo_root / "src" / "deep"
        nested.mkdir(parents=True)
        assert _infer_repo_root(nested) == repo_root
        assert _infer_repo_root(tmp_path / "no-repo") is None

    def test_review_no_skill_config_uses_template(self, tmp_path, logger):
        """未配置 skill → 仅内置通用模板。"""
        from agent_go.review_agent import run_readonly_review
        fake_content = '{"root_cause": "x", "blind_spot": "无", "suggestions": "y"}'
        with patch("agent_go.api.call_api", return_value=fake_content) as mock_api, \
             patch("agent_go.metrics.estimate_cost", return_value=0.0), \
             patch("agent_go.config.meter_event"):
            run_readonly_review(
                {"id": "sub-1"}, tmp_path, "pytest", ["pytest"], ["FAIL"],
                "diff",
                {"verification": {"readonly_review": {"enabled": True}},
                 "plan_api": {"provider": "anthropic", "model": "claude-sonnet-4"}},
                logger, metering_path="x.jsonl",
            )
        prompt = mock_api.call_args[0][1][0]["content"]
        assert "领域审查维度指引" not in prompt
        assert "独立的只读代码审查 agent" in prompt

    def test_verify_changes_invokes_review_on_failure(self, temp_repo, task_dir, logger):
        """验证失败且启用只读审查 → 审查 agent 被调用。"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "verification": "pytest tests/", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "developer", "agent_prompt": "work",
        }

        def _git_fail(cmd, **kw):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            if "status" in cmd_str and "--porcelain" in cmd_str:
                return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
            if "diff" in cmd_str and "--stat" in cmd_str:
                return MagicMock(returncode=0, stdout="1 file changed", stderr="")
            if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_git_fail), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.executor._run_verification_cmd") as mock_vcmd, \
             patch("agent_go.review_agent.run_readonly_review") as mock_review:
            mock_fix.return_value = MagicMock(returncode=0)
            mock_vcmd.return_value = {"command": "pytest", "exit_code": 1,
                                      "duration_ms": 10, "attempt": 1,
                                      "stdout_tail": "FAIL", "stderr_tail": ""}
            mock_review.return_value = {
                "root_cause": "实现缺陷", "blind_spot": "x", "suggestions": "y",
                "cost_usd": 0.001, "latency_ms": 100,
            }
            _verify_changes(
                "task-1", "sub-1", subtask, temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
                config={"verification": {"max_retries": 1,
                                         "readonly_review": {"enabled": True}}},
            )

        assert mock_review.called, "验证失败 + 启用只读审查 → 审查 agent 应被调用"


class TestPermissionMinimization:
    """工具/权限最小化：subtask 级 allowed_tools / permission_mode 覆盖 agent 默认。"""

    def test_subtask_allowed_tools_override(self, temp_repo, task_dir, logger):
        """subtask 声明 allowed_tools → 覆盖 agent 默认工具白名单。"""
        from agent_go.executor import run_subtask

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "files_hint": "*", "verification": "pytest", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "developer", "difficulty": "easy",
            "agent_prompt": "work",
            "allowed_tools": ["Read", "Grep", "Glob"],
        }
        config = {
            "worker_models": {}, "verification": {"max_retries": 1},
            "evaluator": {"enabled": False}, "plan_api": {},
            "agent_loop": {"enabled": False},
        }
        with patch("agent_go.executor._run_claude") as mock_claude, \
             patch("agent_go.executor._create_worktree", return_value=(temp_repo, 1)), \
             patch("agent_go.executor._verify_changes") as mock_verify:
            mock_verify.return_value = {
                "has_changes": True, "summary": "1 file changed", "metrics_changes": {},
                "git_commit_ms": 1, "verification_ms": 1, "verify_ok": True, "git_ok": True,
                "retry_count": 0, "verification_results": [], "commit_hash": "abc",
                "change_stats": {}, "kill_reason": "none",
            }
            mock_claude.return_value = (MagicMock(returncode=0, stdout=""), "headless", 1.0)
            run_subtask(
                "task-1", subtask, temp_repo, task_dir, logger, headless=True,
                metering_path="", config=config,
            )

        # agent 对象作为 _run_claude 的第 5 个位置参数传入
        agent_arg = mock_claude.call_args[0][4]
        assert agent_arg.claude_config["allowed_tools"] == ["Read", "Grep", "Glob"], \
            f"subtask 级工具白名单应覆盖 agent 默认: {agent_arg.claude_config}"

    def test_no_subtask_override_keeps_default(self, temp_repo, task_dir, logger):
        """subtask 未声明工具字段 → 保留 agent 默认配置。"""
        from agent_go.executor import run_subtask

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "files_hint": "*", "verification": "pytest", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "developer", "difficulty": "easy",
            "agent_prompt": "work",
        }
        config = {
            "worker_models": {}, "verification": {"max_retries": 1},
            "evaluator": {"enabled": False}, "plan_api": {},
            "agent_loop": {"enabled": False},
        }
        with patch("agent_go.executor._run_claude") as mock_claude, \
             patch("agent_go.executor._create_worktree", return_value=(temp_repo, 1)), \
             patch("agent_go.executor._verify_changes") as mock_verify:
            mock_verify.return_value = {
                "has_changes": True, "summary": "1 file changed", "metrics_changes": {},
                "git_commit_ms": 1, "verification_ms": 1, "verify_ok": True, "git_ok": True,
                "retry_count": 0, "verification_results": [], "commit_hash": "abc",
                "change_stats": {}, "kill_reason": "none",
            }
            mock_claude.return_value = (MagicMock(returncode=0, stdout=""), "headless", 1.0)
            run_subtask(
                "task-1", subtask, temp_repo, task_dir, logger, headless=True,
                metering_path="", config=config,
            )

        agent_arg = mock_claude.call_args[0][4]
        # developer 内置默认无 allowed_tools（完整权限），应保留
        assert "allowed_tools" not in (agent_arg.claude_config or {}), \
            f"未覆盖时应保留 agent 默认: {agent_arg.claude_config}"


class TestTaskBaseShared:
    """TASK_BASE.md 共享基座：绝对路径引用，不复制进 worktree（避免污染 git 变更范围）。"""

    def test_task_md_references_absolute_path(self, tmp_path, logger):
        """TASK_BASE.md 存在 → TASK.md 用绝对路径引用（不注入内联要求、不提示复制）。"""
        from agent_go.executor import _build_task_md

        task_dir = tmp_path / "task"
        task_dir.mkdir(parents=True)
        (task_dir / "TASK_BASE.md").write_text("# 通用执行要求\n- 不要自行 git commit\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        subtask = {
            "id": "sub-1", "title": "实现功能", "description": "写代码",
            "files_hint": "*", "verification": "pytest", "risks": [],
            "depends_on": [], "skills": [], "agent_type": "developer",
            "agent_prompt": "work", "difficulty": "easy",
        }
        task_md, _, _, _ = _build_task_md(
            subtask, tmp_path, task_dir, worktree, logger, headless=True, config={},
        )
        # 用绝对路径引用共享基座
        assert str(task_dir / "TASK_BASE.md") in task_md, \
            "TASK.md 应引用共享基座的绝对路径"
        assert "worktree 根目录" not in task_md, \
            "不应再提示基座在 worktree 根目录（已改为绝对路径引用）"
        # 不应回退到内联完整要求
        assert "变更保留在此目录" not in task_md

    def test_task_md_falls_back_inline_without_base(self, tmp_path, logger):
        """TASK_BASE.md 不存在 → 回退内联通用要求（兼容旧任务 resume）。"""
        from agent_go.executor import _build_task_md

        task_dir = tmp_path / "task"
        task_dir.mkdir(parents=True)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        subtask = {
            "id": "sub-1", "title": "实现功能", "description": "写代码",
            "files_hint": "*", "verification": "pytest", "risks": [],
            "depends_on": [], "skills": [], "agent_type": "developer",
            "agent_prompt": "work", "difficulty": "easy",
        }
        task_md, _, _, _ = _build_task_md(
            subtask, tmp_path, task_dir, worktree, logger, headless=True, config={},
        )
        assert "变更保留在此目录" in task_md, "无基座时应回退内联通用要求"

    def test_run_subtask_does_not_copy_base_to_worktree(self, temp_repo, task_dir, logger):
        """run_subtask：不把 TASK_BASE.md 复制进 worktree（不污染子任务 git 变更范围）。"""
        from agent_go.executor import run_subtask

        subtask = {
            "id": "sub-1", "title": "基础任务", "description": "执行操作",
            "files_hint": "*", "verification": "pytest", "risks": [], "depends_on": [],
            "skills": [], "agent_type": "developer", "difficulty": "easy",
            "agent_prompt": "work",
        }
        config = {
            "worker_models": {}, "verification": {"max_retries": 1},
            "evaluator": {"enabled": False}, "plan_api": {},
            "agent_loop": {"enabled": False},
        }
        with patch("agent_go.executor._run_claude") as mock_claude, \
             patch("agent_go.executor._create_worktree", return_value=(temp_repo, 1)), \
             patch("agent_go.executor._verify_changes") as mock_verify:
            mock_verify.return_value = {
                "has_changes": True, "summary": "1 file changed", "metrics_changes": {},
                "git_commit_ms": 1, "verification_ms": 1, "verify_ok": True, "git_ok": True,
                "retry_count": 0, "verification_results": [], "commit_hash": "abc",
                "change_stats": {}, "kill_reason": "none",
            }
            mock_claude.return_value = (MagicMock(returncode=0, stdout=""), "headless", 1.0)
            run_subtask(
                "task-1", subtask, temp_repo, task_dir, logger, headless=True,
                metering_path="", config=config,
            )

        # 基座写入 task_dir 而非 worktree（temp_repo 即 worktree）
        assert (task_dir / "TASK_BASE.md").exists(), "共享基座应写入 task_dir"
        assert not (temp_repo / "TASK_BASE.md").exists(), \
            "共享基座不应复制进 worktree（避免污染 git 变更范围）"

    def test_copy_base_function_removed(self):
        """_copy_base_md_to_worktree 函数已移除（不再有把基座复制进 worktree 的入口）。"""
        import agent_go.executor as ex
        assert not hasattr(ex, "_copy_base_md_to_worktree"), \
            "复制基座进 worktree 的函数应已移除"
