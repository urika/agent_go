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
"""

import os, json, logging
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from agent_go.executor import run_subtask


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

        assert result["status"] == "failed", (
            f"非零退出码应为 failed，实际: {result['status']}"
        )
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

        with patch("shutil.which", return_value=None):
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
