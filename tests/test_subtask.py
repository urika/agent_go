"""测试 subtask.py — git merge 上游产物和 headless 子进程运行

全覆盖: _git_merge_upstream（冲突/成功/headless）, _run_headless（交互检测/超时/重试）
"""

import os
import sys
import json
import time
import signal
import logging
import threading
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.subtask import _git_merge_upstream, _run_headless


# ═══════════════════════════════════════════════════════════════
# _git_merge_upstream
# ═══════════════════════════════════════════════════════════════

class TestGitMergeUpstream:
    """上游产物合并测试"""

    def test_merge_success(self, tmp_path, logger):
        """merge 成功时提交并记录"""
        src = tmp_path / "src_worktree"
        dst = tmp_path / "dst_worktree"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        call_log = []

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            call_log.append(cmd_str)
            m = MagicMock()
            if "merge" in cmd_str:
                m.returncode = 0
            elif "commit" in cmd_str:
                m.returncode = 0
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=subprocess_side_effect):
            _git_merge_upstream(src, dst, "test-tag/sub-1", logger)

        # 验证 merge 被调用
        assert any("merge test-tag/sub-1" in c for c in call_log), (
            f"应调用 merge, 实际: {call_log}"
        )
        # 验证 commit 被调用
        assert any("commit" in c and "merge upstream" in c for c in call_log)

    def test_merge_conflict_no_headless(self, tmp_path, logger):
        """冲突时创建 .MERGE_CONFLICT 并 abort"""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        call_log = []

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            call_log.append(cmd_str[:60])
            m = MagicMock()
            if "merge" in cmd_str and "--abort" not in cmd_str:
                m.returncode = 1
                m.stderr = "CONFLICT in main.py"
            elif "diff" in cmd_str and "U" in cmd_str:
                m.returncode = 0
                m.stdout = "main.py\nutils.py\n"
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=subprocess_side_effect):
            _git_merge_upstream(src, dst, "test-tag/sub-1", logger, headless=False)

        # 验证 .MERGE_CONFLICT 被创建
        conflict_file = dst / ".MERGE_CONFLICT"
        assert conflict_file.exists()
        content = conflict_file.read_text()
        assert "main.py" in content
        assert "utils.py" in content
        # 验证 merge --abort 被调用
        assert any("merge --abort" in c for c in call_log)

    def test_merge_conflict_headless(self, tmp_path, logger):
        """headless 模式保留冲突标记"""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        call_log = []

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            call_log.append(cmd_str[:60])
            m = MagicMock()
            if "merge" in cmd_str and "--abort" not in cmd_str:
                m.returncode = 1
                m.stderr = "CONFLICT"
            elif "diff" in cmd_str and "U" in cmd_str:
                m.returncode = 0
                m.stdout = "main.py\n"
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=subprocess_side_effect):
            _git_merge_upstream(src, dst, "test-tag/sub-1", logger, headless=True)

        # headless: 保留冲突标记，不 abort
        assert not any("abort" in c for c in call_log), "headless 不应 abort"

    def test_unknown_conflict_no_files(self, tmp_path, logger):
        """diff --diff-filter=U 为空时记录未知冲突"""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        def subprocess_side_effect(args, **kwargs):
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            m = MagicMock()
            if "merge" in cmd_str and "--abort" not in cmd_str:
                m.returncode = 1
                m.stderr = "CONFLICT"
            elif "diff" in cmd_str and "U" in cmd_str:
                m.returncode = 0
                m.stdout = ""  # 空输出：无法识别冲突文件
            else:
                m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=subprocess_side_effect):
            _git_merge_upstream(src, dst, "tag", logger, headless=False)

        conflict_file = dst / ".MERGE_CONFLICT"
        assert conflict_file.exists()
        assert "未知冲突" in conflict_file.read_text()

    def test_commit_failure_logged(self, tmp_path, logger):
        """merge commit 失败时记录 warning 但不抛异常"""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)

        call_count = [0]

        def subprocess_side_effect(args, **kwargs):
            m = MagicMock()
            cmd_str = " ".join(args) if isinstance(args, list) else str(args)
            if "merge" in cmd_str:
                m.returncode = 0
            elif "commit" in cmd_str:
                call_count[0] += 1
                if call_count[0] == 1:
                    m.returncode = 1
                    m.stderr = b"commit failed"
                else:
                    m.returncode = 0
            else:
                m.returncode = 0
            return m

        # 不应抛出异常
        with patch("subprocess.run", side_effect=subprocess_side_effect):
            _git_merge_upstream(src, dst, "tag", logger)


# ═══════════════════════════════════════════════════════════════
# _run_headless
# ═══════════════════════════════════════════════════════════════

class TestRunHeadless:
    """headless 子进程运行测试"""

    @patch("subprocess.Popen")
    def test_basic_execution(self, mock_popen, logger):
        """正常执行路径"""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = ["", ""]  # EOF immediately
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = _run_headless(
            "task content", Path("/tmp/work"), {"KEY": "val"},
            logger, "sub-1"
        )

        # 验证了 Popen 被调用
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert "claude" in args[0], f"应调用 claude, 实际: {args[0]}"
        assert kwargs["env"]["KEY"] == "val"
        assert result.returncode == 0

    @patch("subprocess.Popen")
    def test_interaction_detected(self, mock_popen, logger):
        """检测到交互模式时应重试"""
        mock_proc = MagicMock()
        mock_proc.pid = 12346
        mock_proc.returncode = 130  # SIGINT = interaction detected
        # 第一次返回 130（交互），第二次返回 0
        mock_proc.poll.side_effect = [None, None, 130, None, None, 0]
        stdout_lines = iter([
            '',
        ])
        stderr_lines = iter([
            'waiting for input',
            '',
        ])
        mock_proc.stdout.readline.side_effect = lambda: next(stdout_lines, '')
        mock_proc.stderr.readline.side_effect = lambda: next(stderr_lines, '')
        mock_popen.return_value = mock_proc

        result = _run_headless(
            "task content", Path("/tmp/work"), {},
            logger, "sub-2"
        )

        # 应被调用两次（重试）
        assert mock_popen.call_count == 2

    @patch("subprocess.Popen")
    def test_idle_timeout_kills_process(self, mock_popen, logger):
        """超时应被 kill"""
        mock_proc = MagicMock()
        mock_proc.pid = 12347
        # poll 返回 None（进程运行中）
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 3:
                return 0  # 被 kill 后进程退出
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = -9
        mock_popen.return_value = mock_proc

        # 模拟 time.time: 前几次返回 100, 之后返回 701+ (idle > 600s)
        time_values = [100, 100, 100, 100, 701, 701, 701]

        def time_side():
            while True:
                for v in time_values:
                    yield v
                yield 701  # 无限供应
        time_gen = time_side()

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                result = _run_headless(
                    "task", Path("/tmp/work"), {},
                    logger, "sub-3"
                )

        mock_proc.kill.assert_called_once()

    @patch("subprocess.Popen")
    def test_non_interaction_failure_no_retry(self, mock_popen, logger):
        """非交互原因失败不重试"""
        mock_proc = MagicMock()
        mock_proc.pid = 12348
        mock_proc.returncode = 1  # 普通错误，非 SIGINT
        mock_proc.poll.side_effect = [None, None, 1, None, None, 0]
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_popen.return_value = mock_proc

        result = _run_headless(
            "task", Path("/tmp/work"), {},
            logger, "sub-4"
        )

        # 只应被调用一次（不重试）
        assert mock_popen.call_count == 1

    @patch("subprocess.Popen")
    def test_retry_suffix_added(self, mock_popen, logger):
        """重试时注入催促指令后缀"""
        mock_proc = MagicMock()
        mock_proc.pid = 12349
        mock_proc.returncode = 130  # 交互导致 SIGINT
        mock_proc.poll.side_effect = [None, None, 130, None, None, 0]
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            _run_headless(
                "original task", Path("/tmp/work"), {},
                logger, "sub-5"
            )

        # 第二次调用的 prompt 参数应包含 RETRY_SUFFIX
        assert mock_popen.call_count == 2
        second_call_args = mock_popen.call_args_list[1]
        # Popen 调用参数: ["claude", "-p", prompt, ...], prompt 在 args[0][2]
        cmd_list = second_call_args[0][0]
        prompt_arg = cmd_list[2]  # 第三个元素是 prompt
        assert "系统指令" in prompt_arg, "重试时应包含催促指令"

    @patch("subprocess.Popen")
    def test_active_pids_tracking(self, mock_popen, logger):
        """PID 应被注册和清理"""
        active_pids = set()
        lock = threading.Lock()

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        _run_headless(
            "task", Path("/tmp/work"), {},
            logger, "sub-6",
            active_pids=active_pids, active_pids_lock=lock
        )

        # PID 应在完成后被清理
        assert 99999 not in active_pids

    @patch("subprocess.Popen")
    def test_max_attempts(self, mock_popen, logger):
        """最多重试 MAX_ATTEMPTS（2）次"""
        mock_proc = MagicMock()
        mock_proc.pid = 12350
        mock_proc.returncode = 130  # 每次都是交互失败
        mock_proc.poll.side_effect = [None, None, 130, None, None, 130]
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_popen.return_value = mock_proc

        with patch("time.sleep"):
            _run_headless(
                "task", Path("/tmp/work"), {},
                logger, "sub-7"
            )

        # 最多 2 次尝试
        assert mock_popen.call_count == 2

    def test_exit_code_constants(self):
        """验证退出码常量正确"""
        from agent_go.subtask import _run_headless as _rh
        # 只是验证模块级常量存在
        import agent_go.subtask as m
        assert hasattr(m, "EXIT_CODE_INTERACTION")
