"""S2 验证循环全链路验收测试

对照 docs/design/verification-agent-goal-spec.md 审计缺口的修复验证：
- fix prompt 注入 stdout/stderr + diff --stat（全量失败反馈）
- 运行时 config 贯通（max_retries 经参数生效，不读磁盘）
- goal 设置经 env 传给 watchdog
- pipeline block_on_failure 开关 + blocked_by 字段
- eval Q3 口径修正 + Q10_avg_retries
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_go.executor import run_subtask
from agent_go.pipeline import _run_pipeline
from agent_go.eval import analyze_quality


# ═══════════════════════════════════════════════════════════════
# 共享 helpers（与 tests/test_executor.py 同风格）
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
    d = tmp_path / ".agent_go" / "task-s2"
    d.mkdir(parents=True)
    return d


def _mock_cp(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _subtask(verification="pytest tests/"):
    return {
        "id": "sub-1", "title": "S2 验收任务", "description": "d",
        "agent_prompt": "do work", "verification": verification,
        "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
    }


def _git_side_effect(pytest_results):
    """pytest_results: list of (rc, stdout, stderr)，按调用顺序消费。"""
    calls = list(pytest_results)

    def side_effect(args, **kwargs):
        cmd_str = " ".join(args) if isinstance(args, list) else str(args)
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return _mock_cp(stdout="M  src/main.py\n")
        if "diff" in cmd_str and "--stat" in cmd_str:
            return _mock_cp(stdout="src/main.py | 2 +-")
        if "pytest" in cmd_str:
            rc, out, err = calls.pop(0) if calls else (0, "", "")
            return _mock_cp(returncode=rc, stdout=out, stderr=err)
        return _mock_cp()
    return side_effect


# ═══════════════════════════════════════════════════════════════
# 修复 2：fix prompt 全量失败反馈
# ═══════════════════════════════════════════════════════════════

class TestRepairPromptFeedback:
    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_fix_prompt_contains_stdout_stderr_and_diffstat(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """验证失败 → 修复 prompt 必须含 exit code、stdout/stderr 尾部、diff --stat"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        # 第一次 pytest 失败（带输出），修复后第二次通过
        mock_subprocess.side_effect = _git_side_effect([
            (1, "FAILED tests/test_a.py::test_x", "AssertionError: expected 200 got 403"),
            (0, "1 passed", ""),
        ])

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True, config={"goal": {"enabled": False}})

        # 第 2 次 _run_headless 调用是修复 prompt
        assert mock_headless.call_count == 2
        fix_prompt = mock_headless.call_args_list[1][0][0]
        assert "exit_code=1" in fix_prompt
        assert "FAILED tests/test_a.py::test_x" in fix_prompt       # stdout 尾部
        assert "AssertionError: expected 200 got 403" in fix_prompt  # stderr 尾部
        assert "src/main.py | 2 +-" in fix_prompt                   # diff --stat


# ═══════════════════════════════════════════════════════════════
# 修复 1：运行时 config 贯通
# ═══════════════════════════════════════════════════════════════

class TestRuntimeConfigPropagation:
    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_max_retries_from_config_param(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """config 参数中的 max_retries=2 生效：首次执行 + 2 次修复 = 3 次 _run_headless"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        # pytest 永远失败
        mock_subprocess.side_effect = _git_side_effect([(1, "", "boom")] * 10)

        result = run_subtask("test-task", _subtask(), temp_repo, task_dir,
                             logger, headless=True,
                             config={"verification": {"max_retries": 2},
                                     "goal": {"enabled": False}})

        assert mock_headless.call_count == 3  # 1 次执行 + 2 次修复
        assert result["verify_ok"] is False
        assert result["retry_count"] == 2

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_max_retries_zero_disables_repair(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """max_retries=0：验证失败不修复，_run_headless 只调 1 次"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_side_effect([(1, "", "boom")] * 5)

        result = run_subtask("test-task", _subtask(), temp_repo, task_dir,
                             logger, headless=True,
                             config={"verification": {"max_retries": 0},
                                     "goal": {"enabled": False}})

        assert mock_headless.call_count == 1
        assert result["status"] == "failed"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_goal_config_propagated_via_env(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """config 的 goal 设置经 env 传给 subtask watchdog"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.side_effect = _git_side_effect([(0, "ok", "")])

        run_subtask("test-task", _subtask(), temp_repo, task_dir,
                    logger, headless=True,
                    config={"goal": {"enabled": False, "max_turns": 7, "timeout_seconds": 123}})

        env = mock_headless.call_args_list[0][0][2]
        assert env["AGENT_GO_GOAL_ENABLED"] == "0"
        assert env["AGENT_GO_GOAL_MAX_TURNS"] == "7"
        assert env["AGENT_GO_GOAL_TIMEOUT"] == "123"


# ═══════════════════════════════════════════════════════════════
# 修复 7/8：block_on_failure 开关 + blocked_by 字段
# ═══════════════════════════════════════════════════════════════

def _pipe_subtask(sub_id, depends_on=None):
    return {"id": sub_id, "title": f"t-{sub_id}", "description": "d",
            "depends_on": depends_on or []}


class TestBlockOnFailure:
    def _run(self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune,
             mock_subproc, tmp_path, logger, config):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = tmp_path / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        confirmed = [_pipe_subtask("sub-1"), _pipe_subtask("sub-2", depends_on=["sub-1"])]
        meta = {"task_id": "t1", "status": "running"}

        def side_effect(task_id, st, *a, **kw):
            if st["id"] == "sub-1":
                return {"subtask_id": "sub-1", "status": "failed", "exit_code": 1,
                        "summary": "验证失败", "failure_reason": "pytest exit=1",
                        "worktree": "", "sandbox_type": "headless",
                        "verify_ok": False, "duration_sec": 1.0}
            return {"subtask_id": st["id"], "status": "completed", "exit_code": 0,
                    "summary": "ok", "worktree": "", "sandbox_type": "headless",
                    "verify_ok": True, "duration_sec": 1.0}
        mock_run_subtask.side_effect = side_effect

        _run_pipeline(confirmed, repo, task_dir, logger,
                      config=config, headless=True, parallel=1,
                      issue_ref="", meta=meta)
        return meta

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_default_blocks_downstream_with_blocked_by(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune,
        mock_subproc, tmp_path, logger,
    ):
        """默认阻断：上游 failed → 下游 blocked 且带 blocked_by 字段"""
        meta = self._run(mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune,
                         mock_subproc, tmp_path, logger, config={})
        assert mock_run_subtask.call_count == 1  # 只执行 sub-1
        sub2 = next(r for r in meta["results"] if r["subtask_id"] == "sub-2")
        assert sub2["status"] == "blocked"
        assert sub2["blocked_by"] == ["sub-1"]

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_no_verify_block_lets_downstream_run(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune,
        mock_subproc, tmp_path, logger,
    ):
        """block_on_failure=False：上游失败不阻断，下游照常执行"""
        meta = self._run(mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune,
                         mock_subproc, tmp_path, logger,
                         config={"verification": {"block_on_failure": False}})
        assert mock_run_subtask.call_count == 2  # sub-1 / sub-2 都执行
        sub2 = next(r for r in meta["results"] if r["subtask_id"] == "sub-2")
        assert sub2["status"] == "completed"


# ═══════════════════════════════════════════════════════════════
# 修复 9：eval Q3 口径 + Q10_avg_retries
# ═══════════════════════════════════════════════════════════════

class TestEvalQualityMetrics:
    def test_q3_requires_verify_ok(self):
        """retry_count=0 但验证失败的不计入首次通过"""
        meta = {
            "task_id": "t1", "status": "failed",
            "subtasks": [],
            "results": [
                {"subtask_id": "s1", "status": "completed", "retry_count": 0, "verify_ok": True},
                {"subtask_id": "s2", "status": "failed", "retry_count": 0, "verify_ok": False},
                {"subtask_id": "s3", "status": "completed", "retry_count": 2, "verify_ok": True},
            ],
        }
        q = analyze_quality(meta)
        assert q["Q3_first_pass_rate"] == 33          # 仅 s1
        assert q["Q10_avg_retries"] == 0.67            # (0+0+2)/3
        assert q["Q8_retry_success_rate"] == 100       # s3 重试后完成
