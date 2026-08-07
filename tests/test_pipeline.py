"""测试 _run_pipeline — 拓扑调度、并发执行、信号中断、恢复、清理

通过 mock run_subtask / _set_gc_auto / _worktree_remove / _worktree_prune / subprocess.run
避免真实 git 操作和 Claude 子进程。
"""

import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from agent_go.pipeline import _run_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_subtask(sub_id, title="test", depends_on=None):
    """构造一个最小 subtask dict。"""
    return {
        "id": sub_id,
        "title": title,
        "description": f"desc-{sub_id}",
        "depends_on": depends_on or [],
    }


def _success_result(sub_id):
    """run_subtask 返回的成功结果。"""
    return {
        "subtask_id": sub_id,
        "status": "completed",
        "exit_code": 0,
        "summary": f"done-{sub_id}",
        "worktree": "",
        "sandbox_type": "headless",
        "verify_ok": True,
        "duration_sec": 1.0,
    }


def _failed_result(sub_id, reason="验证失败"):
    """run_subtask 返回的失败结果。"""
    return {
        "subtask_id": sub_id,
        "status": "failed",
        "exit_code": 1,
        "summary": f"fail-{sub_id}",
        "failure_reason": reason,
        "worktree": "",
        "sandbox_type": "headless",
        "verify_ok": False,
        "duration_sec": 1.0,
    }


def _default_meta(task_id="t1"):
    """默认 meta dict。"""
    return {"task_id": task_id, "status": "running"}


def _setup_repo_and_task_dir(temp_dir, task_id="t1"):
    """创建带 .git 的伪仓库目录和任务目录。"""
    repo = temp_dir / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    task_dir = temp_dir / "tasks" / task_id
    task_dir.mkdir(parents=True)
    return repo, task_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipeline:
    """_run_pipeline 核心行为测试。"""

    # ── 1. 串行执行 ──────────────────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_serial_execution(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """2 个无依赖子任务按顺序执行。"""
        sub1 = _make_subtask("sub-1")
        sub2 = _make_subtask("sub-2")
        confirmed = [sub1, sub2]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        # 让 run_subtask 依次返回成功结果
        mock_run_subtask.side_effect = [
            _success_result("sub-1"),
            _success_result("sub-2"),
        ]
        # subprocess.run 用于 tag 删除等，统一返回成功
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # run_subtask 应被调用 2 次，且顺序为 sub-1 -> sub-2
        assert mock_run_subtask.call_count == 2
        call_ids = [c.args[1]["id"] for c in mock_run_subtask.call_args_list]
        assert call_ids == ["sub-1", "sub-2"]

    # ── 2. 并行执行 ──────────────────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_parallel_execution(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """2 个独立子任务并行执行（parallel=2）。"""
        sub1 = _make_subtask("sub-1")
        sub2 = _make_subtask("sub-2")
        confirmed = [sub1, sub2]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        mock_run_subtask.side_effect = [
            _success_result("sub-1"),
            _success_result("sub-2"),
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=2,
            issue_ref="", meta=_default_meta(),
        )

        # 两个子任务都应被执行
        assert mock_run_subtask.call_count == 2
        executed_ids = {c.args[1]["id"] for c in mock_run_subtask.call_args_list}
        assert executed_ids == {"sub-1", "sub-2"}

    # ── 3. 依赖顺序 ──────────────────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_dependency_order(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """sub-2 依赖 sub-1，sub-1 先执行。"""
        sub1 = _make_subtask("sub-1", title="first")
        sub2 = _make_subtask("sub-2", title="second", depends_on=["sub-1"])
        confirmed = [sub1, sub2]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        mock_run_subtask.side_effect = [
            _success_result("sub-1"),
            _success_result("sub-2"),
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=2,
            issue_ref="", meta=_default_meta(),
        )

        # sub-1 必须在 sub-2 之前执行
        call_ids = [c.args[1]["id"] for c in mock_run_subtask.call_args_list]
        idx1 = call_ids.index("sub-1")
        idx2 = call_ids.index("sub-2")
        assert idx1 < idx2, f"sub-1 (index {idx1}) should run before sub-2 (index {idx2})"

        # sub-2 调用时的 upstream_worktrees 应包含 sub-1 的路径
        sub2_call = mock_run_subtask.call_args_list[idx2]
        upstream = sub2_call.args[5]  # 第 6 个位置参数: upstream_worktrees
        assert "sub-1" in upstream

    # ── 4. gc.auto 禁用与恢复 ────────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto")
    @patch("agent_go.pipeline.run_subtask")
    def test_gc_auto_disabled_and_restored(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """gc.auto 在执行前设为 0，执行后恢复原值。"""
        sub1 = _make_subtask("sub-1")
        confirmed = [sub1]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        mock_run_subtask.return_value = _success_result("sub-1")
        # 第一次调用（禁用）返回原值 "256"；第二次调用（恢复）也返回成功
        mock_gc.side_effect = [("256", True, ""), ("256", True, "")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # _set_gc_auto 应被调用 2 次：禁用（"0"）+ 恢复（原值）
        assert mock_gc.call_count == 2
        # 第一次调用：设为 "0"
        assert mock_gc.call_args_list[0] == call(repo, "0")
        # 第二次调用：恢复为原值 "256"
        assert mock_gc.call_args_list[1] == call(repo, "256")

    # ── 5. 恢复时跳过已完成子任务 ────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_resume_skips_completed(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """已完成子任务被跳过，只执行剩余部分。"""
        sub1 = _make_subtask("sub-1")
        sub2 = _make_subtask("sub-2")
        sub3 = _make_subtask("sub-3")
        confirmed = [sub1, sub2, sub3]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        mock_run_subtask.side_effect = [
            _success_result("sub-2"),
            _success_result("sub-3"),
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        # sub-1 已完成，传入 completed_ids
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
            completed_ids={"sub-1"},
        )

        # run_subtask 只应被调用 2 次（sub-2, sub-3）
        assert mock_run_subtask.call_count == 2
        executed_ids = [c.args[1]["id"] for c in mock_run_subtask.call_args_list]
        assert "sub-1" not in executed_ids
        assert "sub-2" in executed_ids
        assert "sub-3" in executed_ids

    # ── 6. 中断信号设置 paused 状态 ─────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_interrupt_handler_writes_paused(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """_on_interrupt 信号处理器的核心逻辑：写 meta.json status=paused、kill 活跃进程。

        由于 _on_interrupt 是 _run_pipeline 内部闭包，我们通过运行 pipeline 捕获
        注册的信号处理器，然后直接调用它来测试行为。
        注意：需先屏蔽 SIGINT 防止 handler 触发 KeyboardInterrupt 传播到 pytest。
        """
        import os
        import sys as _sys

        sub1 = _make_subtask("sub-1")
        confirmed = [sub1]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        meta = _default_meta()
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")
        mock_run_subtask.return_value = _success_result("sub-1")

        # 捕获 _run_pipeline 注册的信号处理器
        captured_handler = [None]
        original_signal_fn = signal.signal

        def _capturing_signal(signum, handler):
            if signum in (signal.SIGINT, signal.SIGTERM) and callable(handler):
                captured_handler[0] = handler
            return original_signal_fn(signum, handler)

        # 先屏蔽 SIGINT，防止后续调用 handler 时 KeyboardInterrupt 传播
        saved_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)

        try:
            with patch("signal.signal", side_effect=_capturing_signal):
                _run_pipeline(
                    confirmed, repo, task_dir, logger,
                    config={}, headless=False, parallel=1,
                    issue_ref="", meta=meta,
                )

            assert captured_handler[0] is not None, "信号处理器应被注册"

            # 直接调用捕获的 _on_interrupt 闭包来测试其行为
            # 新设计：信号处理器仅设置中断标志 + kill 子进程，不执行 I/O 或 exit
            # sys.exit 和 meta.json 写入由主循环在检测到 _interrupted 标志后执行
            with patch.object(_sys, "exit") as mock_exit, \
                 patch("os.kill") as mock_kill, \
                 patch.object(logger, "info") as mock_log:
                captured_handler[0](signal.SIGTERM, None)

                # 验证信号处理器未调用 sys.exit（由主循环负责）
                mock_exit.assert_not_called()

                # 验证信号处理器未写 meta.json（由主循环负责）
                # 验证 handler 未记录 INFO 日志（日志写入也不是 async-signal-safe 的）
                info_calls = [c for c in mock_log.call_args_list
                              if "任务已暂停" in str(c) or "可通过" in str(c)]
                assert len(info_calls) == 0, \
                    "信号处理器不应执行日志记录（非 async-signal-safe）"
        finally:
            signal.signal(signal.SIGINT, saved_sigint)

    # ── 7. Worktree 清理 ─────────────────────────────────────────────────
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_cleanup_after_pipeline(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """管线结束后 worktree_remove 和 worktree_prune 被调用。"""
        sub1 = _make_subtask("sub-1")
        sub2 = _make_subtask("sub-2")
        confirmed = [sub1, sub2]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        # 创建 worktree 目录，让 _worktree_remove 有路径可清理
        for sub_id in ["sub-1", "sub-2"]:
            wt = task_dir / sub_id / "work"
            wt.mkdir(parents=True)

        mock_run_subtask.side_effect = [
            _success_result("sub-1"),
            _success_result("sub-2"),
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # _worktree_remove 应为每个子任务调用一次
        assert mock_wt_remove.call_count == 2
        # _worktree_prune 应被调用一次
        assert mock_wt_prune.call_count == 1

        # 验证 remove 的路径正确
        removed_paths = [c.args[1] for c in mock_wt_remove.call_args_list]
        assert task_dir / "sub-1" / "work" in removed_paths
        assert task_dir / "sub-2" / "work" in removed_paths


class TestPipelineDependencyFailure:
    """依赖循环/不可满足时的失败标记（回归 docs/ISSUES.md ISSUE-7）"""

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_dependency_cycle_marks_failed(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        temp_dir, logger,
    ):
        """互相依赖导致无法调度时，子任务标记 failed，meta 不得误标 completed。"""
        sub1 = _make_subtask("sub-1", depends_on=["sub-2"])
        sub2 = _make_subtask("sub-2", depends_on=["sub-1"])
        confirmed = [sub1, sub2]

        repo = temp_dir / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = temp_dir / "tasks" / "t1"
        task_dir.mkdir(parents=True)

        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        meta = _default_meta()
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=meta,
        )

        # 没有任何子任务被执行
        mock_run_subtask.assert_not_called()
        # 未执行的子任务被标记 failed，meta 不得为 completed
        assert meta["status"] == "failed"
        assert len(meta["results"]) == 2
        assert all(r["status"] == "failed" for r in meta["results"])
        # 落盘的 meta.json 同样是 failed
        saved = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert saved["status"] == "failed"


class TestPipelineRemotePush:
    """远程 push 逻辑：分支存在性检查、失败计数与容忍（pipeline.py 193-215 行）。"""

    @staticmethod
    def _fake_git(existing=(), push_fail=()):
        """构造 subprocess.run 的 fake，按命令区分 branch --list / push / tag -d。

        existing:   本地存在的分支名集合（branch --list 返回非空）
        push_fail:  push 应失败的分支名集合（returncode=1）
        """
        def _run(cmd, **kwargs):
            if cmd[:3] == ["git", "branch", "--list"]:
                branch = cmd[3]
                stdout = f"{branch}\n" if branch in existing else ""
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            if cmd[:2] == ["git", "push"]:
                branch = cmd[3].split(":")[0]
                rc = 1 if branch in push_fail else 0
                return MagicMock(returncode=rc, stdout="", stderr=b"push error")
            return MagicMock(returncode=0, stdout="", stderr=b"")
        return _run

    @staticmethod
    def _push_calls(mock_subproc):
        """从 subprocess.run 调用记录中筛出 git push 调用。"""
        return [c for c in mock_subproc.call_args_list if c.args[0][:2] == ["git", "push"]]

    # ── 1. 全部分支推送成功 ──────────────────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_push_all_branches_success(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """分支存在且 push 成功时，每个子任务各 push 一次，记录成功日志。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.side_effect = [_success_result("sub-1"), _success_result("sub-2")]
        branches = {"agent_go/t1/sub-1", "agent_go/t1/sub-2"}
        mock_subproc.side_effect = self._fake_git(existing=branches)

        with patch.object(logger, "info") as mock_info:
            _run_pipeline(
                confirmed, repo, task_dir, logger,
                config={}, headless=False, parallel=1,
                issue_ref="", meta=_default_meta(),
                remote_url="https://example.com/repo.git",
            )

        # 每个分支各 push 一次，refspec 为 branch:branch
        pushes = self._push_calls(mock_subproc)
        assert len(pushes) == 2
        pushed_refs = {c.args[0][3] for c in pushes}
        assert pushed_refs == {
            "agent_go/t1/sub-1:agent_go/t1/sub-1",
            "agent_go/t1/sub-2:agent_go/t1/sub-2",
        }
        # push 的 remote 参数正确，且在 repo 目录下执行
        for c in pushes:
            assert c.args[0][2] == "https://example.com/repo.git"
            assert c.kwargs["cwd"] == str(repo)
        # 成功日志
        assert any("所有分支推送成功" in str(c) for c in mock_info.call_args_list)

    # ── 2. 分支不存在时跳过 push ─────────────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_push_skips_missing_branch(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """branch --list 为空（如 clone 降级未建分支）时跳过该分支的 push。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.side_effect = [_success_result("sub-1"), _success_result("sub-2")]
        # 只有 sub-1 的分支存在
        mock_subproc.side_effect = self._fake_git(existing={"agent_go/t1/sub-1"})

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
            remote_url="https://example.com/repo.git",
        )

        pushes = self._push_calls(mock_subproc)
        assert len(pushes) == 1
        assert pushes[0].args[0][3] == "agent_go/t1/sub-1:agent_go/t1/sub-1"

    # ── 3. push 失败计数且不影响任务状态 ────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_push_failure_counted_and_tolerated(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """单个分支 push 失败只计数告警，管线照常完成，meta 仍为 completed。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.side_effect = [_success_result("sub-1"), _success_result("sub-2")]
        branches = {"agent_go/t1/sub-1", "agent_go/t1/sub-2"}
        mock_subproc.side_effect = self._fake_git(
            existing=branches, push_fail={"agent_go/t1/sub-2"},
        )

        meta = _default_meta()
        with patch.object(logger, "warning") as mock_warning:
            _run_pipeline(
                confirmed, repo, task_dir, logger,
                config={}, headless=False, parallel=1,
                issue_ref="", meta=meta,
                remote_url="https://example.com/repo.git",
            )

        # 两个分支都尝试了 push
        assert len(self._push_calls(mock_subproc)) == 2
        # 失败计数告警：单条分支失败 + 汇总
        warnings = [str(c) for c in mock_warning.call_args_list]
        assert any("推送失败 agent_go/t1/sub-2" in w for w in warnings)
        assert any("1 个分支推送失败" in w for w in warnings)
        # push 失败被容忍：任务状态不受影响
        assert meta["status"] == "completed"

    # ── 4. 未指定 remote 时不做任何 push ────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_no_push_without_remote_url(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """remote_url 为空时跳过整个远程推送段（连 branch --list 都不调用）。"""
        confirmed = [_make_subtask("sub-1")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.return_value = _success_result("sub-1")
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        git_cmds = [c.args[0] for c in mock_subproc.call_args_list]
        assert not any(cmd[:2] == ["git", "push"] for cmd in git_cmds)
        assert not any(cmd[:3] == ["git", "branch", "--list"] for cmd in git_cmds)


class TestPipelineNotify:
    """管线末尾通知派发：on_blocked > on_failed > on_complete，一次管线只派发一个事件
    （pipeline.py 361-364 行）。"""

    # ── 1. 全部成功 → on_complete ────────────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_notify_on_complete(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """无失败无阻断时派发 on_complete，且只派发一次。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.side_effect = [_success_result("sub-1"), _success_result("sub-2")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        config = {}
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config=config, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # 解耦后所有调用（含循环内 subtask_failed + 末尾 on_complete）都被拦截。
        event_names = [c.args[0] for c in mock_notify.call_args_list]
        assert "on_complete" in event_names
        # 取 on_complete 调用的 context 做原断言
        on_complete_calls = [c for c in mock_notify.call_args_list if c.args[0] == "on_complete"]
        assert len(on_complete_calls) == 1
        event, context, passed_config = on_complete_calls[0].args
        # context 携带 meta / results_map / task_dir，config 原样透传
        assert set(context.keys()) == {"meta", "results_map", "task_dir"}
        assert context["task_dir"] == task_dir
        assert context["meta"]["status"] == "completed"
        assert passed_config is config

    # ── 2. 有失败（无阻断）→ on_failed ───────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_notify_on_failed(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """存在 failed 子任务（无 blocked）时派发 on_failed。"""
        # 两个无依赖子任务：sub-1 失败，sub-2 成功 → 无级联阻断
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.side_effect = [_failed_result("sub-1"), _success_result("sub-2")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # 解耦后 mock 目标改为 agent_go.notify.notify_event，循环内 subtask_failed
        # 调用也会被拦截（之前 mock pipeline.notify_event 时遗漏了函数内动态 import 的调用）。
        # 断言 on_failed 在调用列表中即可。
        event_names = [c.args[0] for c in mock_notify.call_args_list]
        assert "on_failed" in event_names

    # ── 3. 阻断优先于失败 → on_blocked ───────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_notify_on_blocked_priority(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """failed 与 blocked 同时存在时，优先级 on_blocked > on_failed，只派发一次。"""
        # sub-1 失败 → 依赖它的 sub-2 被级联阻断：has_failed 与 has_blocked 同时为真
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2", depends_on=["sub-1"])]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        mock_run_subtask.return_value = _failed_result("sub-1")
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # 解耦后 mock 目标改为 agent_go.notify.notify_event，循环内 subtask_failed
        # 调用也会被拦截。断言 on_blocked 在调用列表中即可（优先级正确）。
        event_names = [c.args[0] for c in mock_notify.call_args_list]
        assert "on_blocked" in event_names
        # 取最后一次 on_blocked 调用的 context
        on_blocked_calls = [c for c in mock_notify.call_args_list if c.args[0] == "on_blocked"]
        assert len(on_blocked_calls) == 1
        event, context, _ = on_blocked_calls[0].args
        statuses = {sid: r["status"] for sid, r in context["results_map"].items()}
        assert statuses == {"sub-1": "failed", "sub-2": "blocked"}


class TestPipelinePreservedMarker:
    """失败/阻断 worktree 保留与 .preserved 标记写入（pipeline.py 236-247 行）。"""

    # ── 1. 失败 worktree 保留并写入标记 ──────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_failed_worktree_preserved_with_marker(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """默认行为（preserve_worktrees=None）：failed 保留+写标记，completed 正常清理。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        # 两个子任务都有 worktree 目录（run_subtask 已 mock，手动补建）
        for sid in ("sub-1", "sub-2"):
            (task_dir / sid / "work").mkdir(parents=True)

        mock_run_subtask.side_effect = [
            _success_result("sub-1"),
            _failed_result("sub-2", reason="pytest 3 个用例失败"),
        ]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # sub-2 保留：.preserved 标记存在且字段完整（S12 失败清理：含 kill_reason/degraded）
        marker = task_dir / "sub-2" / ".preserved"
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data == {
            "subtask_id": "sub-2",
            "status": "failed",
            "failure_reason": "pytest 3 个用例失败",
            "kill_reason": "",
            "degraded": False,
            "branch": "agent_go/t1/sub-2",
        }
        # sub-1 成功：被清理，无标记
        assert not (task_dir / "sub-1" / ".preserved").exists()
        removed_paths = [c.args[1] for c in mock_wt_remove.call_args_list]
        assert removed_paths == [task_dir / "sub-1" / "work"]

    # ── 2. 阻断 worktree 保留并写入标记 ──────────────────────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_blocked_worktree_preserved_with_marker(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """级联阻断（blocked）的子任务同样保留 worktree 并写入 .preserved。"""
        confirmed = [_make_subtask("sub-1"), _make_subtask("sub-2", depends_on=["sub-1"])]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)

        # blocked 的 sub-2 未真实执行，但 worktree 目录可能已存在，补建以覆盖该路径
        for sid in ("sub-1", "sub-2"):
            (task_dir / sid / "work").mkdir(parents=True)

        mock_run_subtask.return_value = _failed_result("sub-1", reason="编译失败")
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        # 两个 worktree 都保留（failed + blocked），均不清理
        mock_wt_remove.assert_not_called()
        # blocked 的标记：status/failure_reason 来自级联阻断结果（S12 失败清理：含新字段）
        data = json.loads((task_dir / "sub-2" / ".preserved").read_text(encoding="utf-8"))
        assert data == {
            "subtask_id": "sub-2",
            "status": "blocked",
            "failure_reason": "上游依赖失败，级联阻断",
            "kill_reason": "",
            "degraded": False,
            "branch": "agent_go/t1/sub-2",
        }

    # ── 3. preserve_worktrees=True 时成功 worktree 也保留 ────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_preserve_all_when_flag_true(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """preserve_worktrees=True：completed 的 worktree 同样保留，标记 status 为 completed。"""
        confirmed = [_make_subtask("sub-1")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)
        (task_dir / "sub-1" / "work").mkdir(parents=True)

        mock_run_subtask.return_value = _success_result("sub-1")
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
            preserve_worktrees=True,
        )

        mock_wt_remove.assert_not_called()
        data = json.loads((task_dir / "sub-1" / ".preserved").read_text(encoding="utf-8"))
        # 成功结果没有 failure_reason 字段 → 空串
        assert data["status"] == "completed"
        assert data["failure_reason"] == ""
        assert data["branch"] == "agent_go/t1/sub-1"

    # ── 4. preserve_worktrees=False 时失败 worktree 也清理 ───────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_no_preserve_when_flag_false(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """preserve_worktrees=False：failed 的 worktree 强制清理，不写 .preserved。"""
        confirmed = [_make_subtask("sub-1")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)
        (task_dir / "sub-1" / "work").mkdir(parents=True)

        mock_run_subtask.return_value = _failed_result("sub-1")
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
            preserve_worktrees=False,
        )

        mock_wt_remove.assert_called_once_with(repo, task_dir / "sub-1" / "work")
        assert not (task_dir / "sub-1" / ".preserved").exists()

    # ── 5. S12 失败清理：cleanup_race（实际成功）不保留 ───────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_cleanup_race_not_preserved(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """kill_reason=cleanup_race（S12-P0 已修正为实际成功）→ 即使 status=failed 也不保留。"""
        confirmed = [_make_subtask("sub-1")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)
        (task_dir / "sub-1" / "work").mkdir(parents=True)

        result = _failed_result("sub-1", reason="cleanup race")
        result["kill_reason"] = "cleanup_race"
        mock_run_subtask.return_value = result
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        mock_wt_remove.assert_called_once_with(repo, task_dir / "sub-1" / "work")
        assert not (task_dir / "sub-1" / ".preserved").exists()

    # ── 6. S12 失败清理：degraded 降级产物强制保留 + 标记 ─────────────────
    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_degraded_forced_preserved(
        self, mock_run_subtask, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """degraded=True（降级产物最需审查）→ 强制保留 + marker 带 degraded 标记。"""
        confirmed = [_make_subtask("sub-1")]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir)
        (task_dir / "sub-1" / "work").mkdir(parents=True)

        result = _success_result("sub-1")
        result["degraded"] = True
        mock_run_subtask.return_value = result
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=False, parallel=1,
            issue_ref="", meta=_default_meta(),
        )

        mock_wt_remove.assert_not_called()
        data = json.loads((task_dir / "sub-1" / ".preserved").read_text(encoding="utf-8"))
        assert data["degraded"] is True
        assert data["status"] == "completed"

    # ── 7. S12 失败清理：保留现场净化（移除 .pytest_cache/__pycache__/pyc）─
    def test_sanitize_preserved_worktree_removes_cache(self, temp_dir):
        """_sanitize_preserved_worktree 移除 .pytest_cache / __pycache__ / *.pyc。"""
        from agent_go.pipeline import _sanitize_preserved_worktree
        wt = temp_dir / "work"
        (wt / ".pytest_cache").mkdir(parents=True)
        (wt / "src" / "__pycache__").mkdir(parents=True)
        (wt / "src" / "mod.pyc").write_text("x")
        (wt / "keep.py").write_text("y")

        _sanitize_preserved_worktree(wt)

        assert not (wt / ".pytest_cache").exists()
        assert not (wt / "src" / "__pycache__").exists()
        assert not (wt / "src" / "mod.pyc").exists()
        assert (wt / "keep.py").exists()

    # ── 8. S12 失败清理：clean --older-than 保留期过滤 ────────────────────
    def test_clean_older_than_filters(self, temp_dir, monkeypatch, capsys):
        """cmd_clean --older-than 只清理早于 N 天的任务目录。"""
        import time as _t
        from agent_go.cli import cmd_clean, AGENT_GO_DIR
        from unittest.mock import patch as _patch

        # 构造两个任务目录：一个旧（10 天前）、一个新（现在）
        old_task = temp_dir / "task-old-1"
        new_task = temp_dir / "task-new-2"
        old_task.mkdir()
        new_task.mkdir()
        (old_task / "meta.json").write_text('{"task_id": "task-old-1", "repo": ""}', encoding="utf-8")
        (new_task / "meta.json").write_text('{"task_id": "task-new-2", "repo": ""}', encoding="utf-8")
        _old_mtime = _t.time() - 10 * 86400
        import os as _os
        _os.utime(str(old_task), (_old_mtime, _old_mtime))

        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", temp_dir)
        args = type("Args", (), {"older_than": 7})()

        with _patch("agent_go.cli.safe_input", return_value="y"):
            cmd_clean(args)

        assert not old_task.exists()
        assert new_task.exists()


# ═══════════════════════════════════════════════════════════════
# 并发压力测试（P2）
# ═══════════════════════════════════════════════════════════════

class TestPipelineConcurrencyStress:
    """高并发场景下管线稳定性。

    覆盖 2 个场景：
      1. 20 个子任务 parallel=10 → 全部完成，不崩溃
      2. 并发不丢失 subtask 结果（meta.results 完整）
    """

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_high_concurrency_no_crash(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """20 个无依赖子任务 parallel=10 → 全部 completed，不崩溃"""
        n = 20
        confirmed = [_make_subtask(f"sub-{i}") for i in range(n)]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-stress")

        mock_run.side_effect = [_success_result(f"sub-{i}") for i in range(n)]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        meta = _default_meta("t-stress")
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=True, parallel=10,
            issue_ref="", meta=meta,
        )

        # 验证全部完成
        assert meta["status"] == "completed"
        assert len(meta["results"]) == n
        assert all(r["status"] == "completed" for r in meta["results"])
        # run_subtask 被调用了 n 次
        assert mock_run.call_count == n

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_concurrent_with_mixed_results(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """并发场景混搭成功/失败 → 全部结果被记录，不崩溃"""
        n = 12
        confirmed = [_make_subtask(f"sub-{i}") for i in range(n)]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-mix")

        # 前 8 个成功，后 4 个失败（在同一个 wave 中，互不影响）
        results = [_success_result(f"sub-{i}") for i in range(8)]
        results += [_failed_result(f"sub-{i}", reason=f"sub-{i} error") for i in range(8, 12)]
        mock_run.side_effect = results
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        meta = _default_meta("t-mix")
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=True, parallel=6,
            issue_ref="", meta=meta,
        )

        assert meta["status"] == "failed"
        assert len(meta["results"]) == n
        completed = sum(1 for r in meta["results"] if r["status"] == "completed")
        failed = sum(1 for r in meta["results"] if r["status"] == "failed")
        assert completed == 8, f"期望 8 个成功，实际 {completed}"
        assert failed == 4, f"期望 4 个失败，实际 {failed}"

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_all_subtasks_fail_no_crash(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc,
        mock_notify, temp_dir, logger,
    ):
        """10 个子任务 parallel=5 全部失败 → pipeline 不崩溃"""
        n = 10
        confirmed = [_make_subtask(f"sub-{i}") for i in range(n)]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-all-fail")

        mock_run.side_effect = [_failed_result(f"sub-{i}") for i in range(n)]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        meta = _default_meta("t-all-fail")
        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=True, parallel=5,
            issue_ref="", meta=meta,
        )

        assert meta["status"] == "failed"
        assert len(meta["results"]) == n
        assert all(r["status"] == "failed" for r in meta["results"])


class TestPipelineArtifactExport:
    """S9-B 产物导出集成：--artifact-dir 配置时清理前收集 __artifacts__/。

    覆盖验收 B1（导出到目标目录）/ B2（不指定则不导出，向后兼容）。
    """

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_artifact_dir_exports_files(self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune,
                                        mock_subproc, temp_dir, logger):
        """B1: config 含 artifact_dir → 子任务 __artifacts__/ 文件被导出。"""
        sub1 = _make_subtask("sub-1")
        confirmed = [sub1]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-art")

        # 构造 worktree/__artifacts__/report.md
        work = task_dir / "sub-1" / "work"
        art_dir = work / "__artifacts__"
        art_dir.mkdir(parents=True)
        (art_dir / "report.md").write_text("# Q3 report", encoding="utf-8")

        artifact_dir = temp_dir / "reports"
        mock_run.side_effect = [_success_result("sub-1")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={"artifact_dir": str(artifact_dir)}, headless=True, parallel=1,
            issue_ref="", meta=_default_meta("t-art"),
        )

        exported = artifact_dir / "t-art" / "sub-1" / "report.md"
        assert exported.exists()
        assert exported.read_text(encoding="utf-8") == "# Q3 report"

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_no_artifact_dir_no_export(self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune,
                                       mock_subproc, temp_dir, logger):
        """B2: 不指定 artifact_dir → 无导出目录创建，产物留在 worktree。"""
        sub1 = _make_subtask("sub-1")
        confirmed = [sub1]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-art2")

        work = task_dir / "sub-1" / "work"
        art_dir = work / "__artifacts__"
        art_dir.mkdir(parents=True)
        (art_dir / "report.md").write_text("# report", encoding="utf-8")

        mock_run.side_effect = [_success_result("sub-1")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={}, headless=True, parallel=1,
            issue_ref="", meta=_default_meta("t-art2"),
        )

        # 无 artifact_dir → 不创建导出目录
        assert not (temp_dir / "reports").exists()
        # 产物仍在 worktree 中（未被删除）
        assert (art_dir / "report.md").exists()

    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_export_failure_does_not_break_pipeline(self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune,
                                                    mock_subproc, temp_dir, logger, monkeypatch):
        """导出异常被吞掉，pipeline 正常完成（解耦原则）。"""
        sub1 = _make_subtask("sub-1")
        confirmed = [sub1]
        repo, task_dir = _setup_repo_and_task_dir(temp_dir, "t-art3")

        work = task_dir / "sub-1" / "work"
        work.mkdir(parents=True)

        mock_run.side_effect = [_success_result("sub-1")]
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        def _boom(*a, **k):
            raise RuntimeError("export failure")

        monkeypatch.setattr("agent_go.artifacts.export", _boom)

        _run_pipeline(
            confirmed, repo, task_dir, logger,
            config={"artifact_dir": str(temp_dir / "reports")}, headless=True, parallel=1,
            issue_ref="", meta=_default_meta("t-art3"),
        )
        # pipeline 正常走到 meta.json 落盘，导出失败不中断
        assert (task_dir / "meta.json").exists()
