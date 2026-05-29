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


def _default_meta(task_id="t1"):
    """默认 meta dict。"""
    return {"task_id": task_id, "status": "running"}


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
            # mock sys.exit 和 os.kill 以避免真实退出/杀进程
            with patch.object(_sys, "exit") as mock_exit, \
                 patch("os.kill") as mock_kill:
                captured_handler[0](signal.SIGTERM, None)

                # 验证 sys.exit(0) 被调用
                mock_exit.assert_called_once_with(0)

                # 验证 meta.json 写入 paused
                meta_file = task_dir / "meta.json"
                assert meta_file.exists(), "meta.json 应被写入"
                saved = json.loads(meta_file.read_text(encoding="utf-8"))
                assert saved["status"] == "paused", f"status 应为 paused，实际: {saved['status']}"
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
