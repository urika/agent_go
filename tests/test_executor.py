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

    def test_verify_fail_then_fix_then_pass(self, temp_repo, task_dir, logger):
        """验证首次失败 → 修复 → 重新验证通过 (retry_count=1, verify_ok=True)"""
        from threading import Lock
        from agent_go.executor import _verify_changes

        with patch("subprocess.run", side_effect=self._git_mock(verify_success_on_attempt=2)), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)

            result = _verify_changes(
                "task-1", "sub-1", dict(self._SUBTASK_TPL), temp_repo, headless=True,
                task_md="# Task", env={}, tag_name="task-1/sub-1",
                active_pids=set(), active_pids_lock=Lock(), logger=logger,
                task_dir=task_dir,
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
