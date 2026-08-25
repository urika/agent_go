"""测试 subtask.py — git merge 上游产物和 headless 子进程运行

全覆盖: _git_merge_upstream（冲突/成功/headless）, _run_headless（交互检测/超时/重试），
stream-json 事件解析（stream_event/assistant/user/result）、goal watchdog 计数与 usage/cost 聚合
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
    def test_worker_metering_written(self, mock_popen, logger, tmp_path):
        """result 事件中的 usage/cost 被聚合并写入 metering.jsonl（worker 角色）"""
        metering = tmp_path / "metering.jsonl"
        result_event = json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 1500, "output_tokens": 300},
            "duration_ms": 5000, "num_turns": 4,
        })
        mock_proc = MagicMock()
        mock_proc.pid = 12350
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = [result_event + "\n", "", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        result = _run_headless(
            "task content", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering), "AGENT_GO_TASK_ID": "task-t1"},
            logger, "sub-1"
        )

        assert result.returncode == 0
        lines = metering.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["role"] == "worker"
        assert ev["actual_provider"] == "claude-code"
        assert ev["prompt_tokens"] == 1500
        assert ev["completion_tokens"] == 300
        assert ev["cost_usd"] == 0.0123
        assert ev["task_id"] == "task-t1"
        assert ev["subtask_id"] == "sub-1"
        assert ev["result"] == "success"

    @patch("subprocess.Popen")
    def test_worker_metering_skipped_without_path(self, mock_popen, logger, tmp_path):
        """未设置 AGENT_GO_METERING_PATH 时不写计量文件"""
        result_event = json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        mock_proc = MagicMock()
        mock_proc.pid = 12351
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = [result_event + "\n", "", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        _run_headless("task content", Path("/tmp/work"), {}, logger, "sub-1")

        assert not (tmp_path / "metering.jsonl").exists()

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
    @patch("agent_go.subtask.subprocess.run", return_value=MagicMock(stdout=""))
    def test_idle_timeout_kills_process(self, mock_run, mock_popen, logger):
        """多维活性全静默 + grace 复检确认 stuck 后应被 kill"""
        mock_proc = MagicMock()
        mock_proc.pid = 12347
        # poll 返回 None（进程运行中）；循环需覆盖 600s idle + 120s grace 复检
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 30:
                return 0  # 被 kill 后进程退出
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = -9
        mock_popen.return_value = mock_proc

        # 模拟 time.time：单调递增步长 1000。last_ts 读到某值后，后续 time 与它相差
        # >1000（> IDLE_TIMEOUT=600）触发 grace（suspected=下一值），再差 1000（> grace 120s）
        # 复检确认 stuck → kill。不依赖精确调用次数（兼容多维活性初始化）。
        import itertools as _it
        time_gen = _it.count(0, 1000)

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                result = _run_headless(
                    "task", Path("/tmp/work"), {},
                    logger, "sub-3"
                )

        mock_proc.kill.assert_called_once()

    @patch("subprocess.Popen")
    @patch("agent_go.subtask.subprocess.run", return_value=MagicMock(stdout=""))
    def test_idle_timeout_records_kill_reason(self, mock_run, mock_popen, logger, tmp_path):
        """S12-P0 G1：IDLE_TIMEOUT 杀进程时，_run_headless 返回对象携带 kill_reason=stuck，
        并写 kill_state 事件到 metering.jsonl（运行时 kill 分类贯穿）。"""
        mock_proc = MagicMock()
        mock_proc.pid = 12348
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 30:
                return 0
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = -9
        mock_popen.return_value = mock_proc

        # idle > 600s 触发 grace，再推进 120s 完成复检确认 stuck。
        # 单调递增步长 1000，兼容多维活性初始化 + _record_kill 内 meter_event 消耗 time.time。
        import itertools as _it
        time_gen = _it.count(0, 1000)
        meter_path = tmp_path / "metering.jsonl"

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                result = _run_headless(
                    "task", Path("/tmp/work"),
                    {"AGENT_GO_METERING_PATH": str(meter_path), "AGENT_GO_TASK_ID": "task-kr",
                     "AGENT_GO_DIFFICULTY": "hard"},
                    logger, "sub-kr", config={"goal": {"enabled": False}}
                )

        mock_proc.kill.assert_called_once()
        assert getattr(result, "kill_reason", None) == "stuck"
        # kill_state 事件已写入 metering
        assert meter_path.exists()
        lines = meter_path.read_text(encoding="utf-8").strip().split("\n")
        kill_events = [json.loads(l) for l in lines if json.loads(l).get("event") == "kill_state"]
        assert len(kill_events) >= 1
        assert kill_events[0]["kill_reason"] == "stuck"
        assert kill_events[0]["sub_id"] == "sub-kr"

    @patch("subprocess.Popen")
    def test_hard_timeout_kills_process(self, mock_popen, logger):
        """hard_timeout 到点即 kill（修复重试的 retry_timeout 控制，不依赖事件活动）"""
        mock_proc = MagicMock()
        mock_proc.pid = 12352
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 3:
                return 0
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = -9
        mock_popen.return_value = mock_proc

        # run_start 读取某个递增 time 值，循环后续 time 更大 → time.time()-run_start > hard_timeout。
        # 步长 1000（> hard_timeout=300），不依赖精确的 time.time 调用次数（兼容多维活性初始化）。
        import itertools as _it
        _counter = _it.count(0, 1000)
        time_gen = _counter

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                _run_headless(
                    "task", Path("/tmp/work"), {},
                    logger, "sub-ht", hard_timeout=300
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


# ═══════════════════════════════════════════════════════════════
# stream-json 事件解析辅助
# ═══════════════════════════════════════════════════════════════

def _stream_event(inner: dict) -> str:
    """包装一条 stream_event 事件为 JSON 行"""
    return json.dumps({"type": "stream_event", "event": inner}) + "\n"


def _tool_start(name: str) -> str:
    """content_block_start（工具调用开始）事件行"""
    return _stream_event({
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": name},
    })


def _tool_stop() -> str:
    """content_block_stop（工具调用结束）事件行——goal 轮数在 stop 处计数（B 语义）。"""
    return _stream_event({"type": "content_block_stop"})


def _tool_call(name: str) -> list:
    """一次完整工具调用（start + stop）：goal watchdog 计数的最小事件对。"""
    return [_tool_start(name), _tool_stop()]


def _make_proc(stdout_lines=(), stderr_lines=(), returncode=0, pid=42000):
    """构造 mock claude 进程：stdout/stderr 逐行吐出给定内容后 EOF，poll 立即返回。"""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.returncode = returncode
    out = list(stdout_lines)
    err = list(stderr_lines)
    mock_proc.stdout.readline.side_effect = lambda: out.pop(0) if out else ""
    mock_proc.stderr.readline.side_effect = lambda: err.pop(0) if err else ""
    mock_proc.poll.return_value = returncode
    return mock_proc


def _make_watchdog_proc(stdout_lines, consumed, pid=43000):
    """构造 mock 进程：poll 在 stdout 行被消费完之前返回 None（进程存活），
    消费完后再返回一次 None，保证看门狗检查循环至少跑一轮。"""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.returncode = 0
    out = list(stdout_lines)

    def readline():
        line = out.pop(0) if out else ""
        if line == "":
            # EOF 时此前所有事件行均已被 parse_and_log 处理完
            consumed.set()
        return line

    mock_proc.stdout.readline.side_effect = readline
    mock_proc.stderr.readline.side_effect = lambda: ""
    after_consumed = [0]

    def polling():
        if not consumed.is_set():
            return None
        after_consumed[0] += 1
        return None if after_consumed[0] <= 1 else 0

    mock_proc.poll.side_effect = polling
    return mock_proc


# ═══════════════════════════════════════════════════════════════
# stream-json 事件解析分支（parse_and_log）
# ═══════════════════════════════════════════════════════════════

class TestStreamEventParsing:
    """stream_event / assistant / user / 非 JSON 行的解析分支"""

    @patch("subprocess.Popen")
    def test_content_block_start_tool_logged(self, mock_popen, logger, caplog):
        """content_block_start 带工具名时记录工具调用日志"""
        mock_popen.return_value = _make_proc([_tool_start("Read")])
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev1")
        assert result.returncode == 0
        assert "[Read]" in caplog.text

    @patch("subprocess.Popen")
    def test_content_block_start_without_name_ignored(self, mock_popen, logger, caplog):
        """content_block_start 无工具名（文本块）时不计入工具状态，
        后续 content_block_stop 也不应记录工具完成日志"""
        lines = [
            _stream_event({"type": "content_block_start",
                           "content_block": {"type": "text"}}),
            _stream_event({"type": "content_block_stop"}),
        ]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev2")
        assert "完成" not in caplog.text

    @patch("subprocess.Popen")
    def test_text_delta_appended_to_output(self, mock_popen, logger):
        """text_delta 非空白文本进入输出行"""
        lines = [_stream_event({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "你好世界"},
        })]
        mock_popen.return_value = _make_proc(lines)
        result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev3")
        assert "你好世界" in result.stdout

    @patch("subprocess.Popen")
    def test_text_delta_blank_skipped(self, mock_popen, logger):
        """text_delta 纯空白文本被跳过，未知 delta 类型被忽略"""
        lines = [
            _stream_event({"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": "   "}}),
            _stream_event({"type": "content_block_delta",
                           "delta": {"type": "signature_delta", "signature": "sig"}}),
        ]
        mock_popen.return_value = _make_proc(lines)
        result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev4")
        # 只剩 attempt 分隔行，不含任何事件文本
        body = [ln for ln in result.stdout.split("\n") if "attempt" not in ln]
        assert body == []

    @patch("subprocess.Popen")
    def test_input_json_delta_and_block_stop(self, mock_popen, logger, caplog):
        """input_json_delta 累积工具输入，content_block_stop 复位工具状态"""
        lines = [
            _tool_start("Write"),
            _stream_event({"type": "content_block_delta",
                           "delta": {"type": "input_json_delta",
                                     "partial_json": '{"file_path":'}}),
            _stream_event({"type": "content_block_delta",
                           "delta": {"type": "input_json_delta",
                                     "partial_json": '"/tmp/x"}'}}),
            _stream_event({"type": "content_block_stop"}),
            # 多余的 stop（无对应 start）不应再次记录完成
            _stream_event({"type": "content_block_stop"}),
        ]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev5")
        # 完成日志应附带工具输入预览
        assert '[Write] 完成: {"file_path":"/tmp/x"}' in caplog.text
        assert caplog.text.count("完成") == 1

    @patch("subprocess.Popen")
    def test_assistant_text_and_tool_use(self, mock_popen, logger, caplog):
        """assistant 事件：text 块进入输出，tool_use 块记录日志，空白 text 跳过"""
        lines = [json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "分析结果"},
                {"type": "tool_use", "name": "Grep"},
                {"type": "text", "text": "  "},
            ]},
        }) + "\n"]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev6")
        assert "分析结果" in result.stdout
        assert "[tool_use] Grep" in caplog.text

    @patch("subprocess.Popen")
    def test_assistant_non_dict_blocks_ignored(self, mock_popen, logger):
        """assistant content 中的非 dict 块被忽略，不抛异常"""
        lines = [json.dumps({
            "type": "assistant",
            "message": {"content": ["plain string", 42, None]},
        }) + "\n"]
        mock_popen.return_value = _make_proc(lines)
        result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev7")
        assert result.returncode == 0

    @patch("subprocess.Popen")
    def test_user_tool_result_logged(self, mock_popen, logger, caplog):
        """user 事件的 tool_result 字符串内容记录 INFO 日志"""
        lines = [json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "content": "file created ok"},
            ]},
        }) + "\n"]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev8")
        assert "[tool_result] file created ok" in caplog.text

    @patch("subprocess.Popen")
    def test_user_tool_result_truncated(self, mock_popen, logger, caplog):
        """tool_result 超长内容截断到 200 字符"""
        lines = [json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "content": "x" * 500},
            ]},
        }) + "\n"]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev9")
        assert "x" * 200 in caplog.text
        assert "x" * 201 not in caplog.text

    @patch("subprocess.Popen")
    def test_user_tool_result_non_string_skipped(self, mock_popen, logger, caplog):
        """tool_result 非字符串（list）或空白内容被跳过"""
        lines = [json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result",
                 "content": [{"type": "text", "text": "inner"}]},
                {"type": "tool_result", "content": "  "},
                "not-a-dict",
            ]},
        }) + "\n"]
        mock_popen.return_value = _make_proc(lines)
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev10")
        assert "[tool_result]" not in caplog.text

    @patch("subprocess.Popen")
    def test_unknown_event_type_ignored(self, mock_popen, logger):
        """未知事件类型轻量跳过，不抛异常"""
        lines = [
            json.dumps({"type": "system", "subtype": "init"}) + "\n",
            json.dumps({"type": "rate_limit_event"}) + "\n",
        ]
        mock_popen.return_value = _make_proc(lines)
        result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev11")
        assert result.returncode == 0

    @patch("subprocess.Popen")
    def test_non_json_lines_recorded(self, mock_popen, logger):
        """非 JSON 的 stdout/stderr 行直接记录进输出"""
        mock_popen.return_value = _make_proc(
            ["plain output line\n"], ["some warning\n"]
        )
        result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev12")
        assert "plain output line" in result.stdout
        assert "some warning" in result.stdout

    @patch("subprocess.Popen")
    def test_stderr_interaction_pattern_flagged(self, mock_popen, logger, caplog):
        """stderr 命中交互模式时记录 ⚠️ 交互（退出码 0 则不触发重试）"""
        mock_popen.return_value = _make_proc([], ["请确认是否继续 [y/n]\n"])
        with caplog.at_level(logging.DEBUG, logger="test_logger"):
            result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-ev13")
        assert "⚠️ 交互" in caplog.text
        assert result.returncode == 0
        assert mock_popen.call_count == 1


# ═══════════════════════════════════════════════════════════════
# Goal watchdog 计数与超时
# ═══════════════════════════════════════════════════════════════

class _ListHandler(logging.Handler):
    """内存日志收集器 — 比 caplog 更可靠地抓取守护线程日志。

    caplog 的 LogCaptureHandler 在 with 块期间附加，但 _run_headless 的读线程
    生命周期与 caplog 捕获窗口存在竞争（线程日志可能落在捕获区间外）。
    直接 addHandler 到 logger，handler 生命周期与 logger 一致，无窗口问题。
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


class TestGoalWatchdog:
    """goal 工具轮数统计、轮数超限/超时 kill、开关禁用

    用 _ListHandler（而非 caplog）抓取线程日志，消除 caplog 与守护线程的捕获窗口竞争。
    """

    @patch("subprocess.Popen")
    def test_goal_turn_count_logged_every_5(self, mock_popen, logger):
        """每 5 次工具调用记录一次 goal turn count"""
        env = {"AGENT_GO_GOAL_ENABLED": "1",
               "AGENT_GO_GOAL_MAX_TURNS": "100",
               "AGENT_GO_GOAL_TIMEOUT": "600"}
        mock_popen.return_value = _make_proc(
            [line for _ in range(10) for line in _tool_call("Bash")])
        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            result = _run_headless("task", Path("/tmp/work"), env, logger, "sub-g1")
        finally:
            logger.removeHandler(handler)
        log_text = "\n".join(handler.records)
        assert result.returncode == 0
        assert "goal turn count: 5/100" in log_text
        assert "goal turn count: 10/100" in log_text

    @patch("subprocess.Popen")
    def test_goal_max_turns_exceeded_kills(self, mock_popen, logger):
        """goal 轮数达到上限时强制 kill"""
        env = {"AGENT_GO_GOAL_ENABLED": "1",
               "AGENT_GO_GOAL_MAX_TURNS": "3",
               "AGENT_GO_GOAL_TIMEOUT": "600"}
        consumed = threading.Event()
        mock_popen.return_value = _make_watchdog_proc(
            [line for _ in range(5) for line in _tool_call("Bash")], consumed
        )
        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            with patch("time.sleep"):
                _run_headless("task", Path("/tmp/work"), env, logger, "sub-g2")
        finally:
            logger.removeHandler(handler)
        mock_popen.return_value.kill.assert_called_once()
        assert any("轮数超限" in r or "goal turn count" in r for r in handler.records)

    @patch("subprocess.Popen")
    def test_goal_timeout_kills(self, mock_popen, logger):
        """goal 循环超时（elapsed > GOAL_TIMEOUT）时强制 kill"""
        env = {"AGENT_GO_GOAL_ENABLED": "1",
               "AGENT_GO_GOAL_MAX_TURNS": "100",
               "AGENT_GO_GOAL_TIMEOUT": "1"}
        mock_proc = _make_proc([], [])
        # 进程保持存活直到被 kill
        poll_count = [0]

        def polling():
            poll_count[0] += 1
            return None if poll_count[0] <= 20 else 0

        mock_proc.poll.side_effect = polling
        mock_popen.return_value = mock_proc

        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            # 时间从 1000 开始，每次 sleep 前进 5s：idle(5s) 远低于 IDLE_TIMEOUT(600s)，
            # 但 goal elapsed(5s) 超过 GOAL_TIMEOUT(1s)，可区分两种 kill 原因
            now = [1000.0]
            with patch("time.time", side_effect=lambda: now[0]):
                with patch("time.sleep", side_effect=lambda s: now.__setitem__(0, now[0] + 5)):
                    _run_headless("task", Path("/tmp/work"), env, logger, "sub-g3")
        finally:
            logger.removeHandler(handler)
        mock_proc.kill.assert_called_once()
        assert "goal 循环超时" in "\n".join(handler.records)

    @patch("subprocess.Popen")
    def test_goal_watchdog_disabled_no_kill(self, mock_popen, logger):
        """AGENT_GO_GOAL_ENABLED=0 时不统计轮数也不 kill"""
        env = {"AGENT_GO_GOAL_ENABLED": "0",
               "AGENT_GO_GOAL_MAX_TURNS": "1",
               "AGENT_GO_GOAL_TIMEOUT": "600"}
        consumed = threading.Event()
        mock_proc = _make_watchdog_proc([_tool_start("Bash") for _ in range(3)], consumed)
        mock_popen.return_value = mock_proc
        handler = _ListHandler()
        logger.addHandler(handler)
        try:
            with patch("time.sleep"):
                result = _run_headless("task", Path("/tmp/work"), env, logger, "sub-g4")
        finally:
            logger.removeHandler(handler)
        log_text = "\n".join(handler.records)
        assert result.returncode == 0
        mock_proc.kill.assert_not_called()
        assert "goal turn count" not in log_text
        assert "轮数超限" not in log_text

    @patch("subprocess.Popen")
    def test_s12p3_grace_recheck_finds_activity_resets(self, mock_popen, logger):
        """S12-P3：事件静默超时进入 grace，复检发现 S2/S3 活性（慢工具在干活）→ 不 kill"""
        mock_proc = MagicMock()
        mock_proc.pid = 12360
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 40:
                return 0
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # 单调递增 time：进入 grace 后，复检阶段 S2 返回有变更（模拟 build 写产物）→ 复位
        import itertools as _it
        time_gen = _it.count(0, 1000)

        # 用 side_effect 控制 subprocess.run：git status 每次返回递增内容（模拟 build
        # 持续写产物，S2 活性持续变化）→ grace 复检总发现活性 → 永不 kill。
        # ps 调用（第偶数次）返回空行表（_process_cpu_ticks 解析为无 CPU）。
        _run_call = [0]

        def _rr(*_a, **_k):
            _run_call[0] += 1
            if _run_call[0] % 2 == 1:  # git status（奇数调用）
                return MagicMock(stdout=f" M file{_run_call[0]}\n")
            return MagicMock(stdout="")  # ps（偶数调用）→ 无 CPU 行
        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                with patch("agent_go.subtask.subprocess.run", side_effect=_rr):
                    result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-p3", config={"goal": {"enabled": False}})

        # 复检发现文件活性 → 复位，不 kill；进程最终自然退出
        mock_proc.kill.assert_not_called()

    def test_s12p3_mtime_detects_single_file_rewrite(self, logger, tmp_path):
        """CR-M2 回归：单个 dirty 文件被改写（git status 集合恒定、仅 mtime 变）→
        快照 mtime 段变化 → grace 复检判为活性 → 不 kill。
        修复前 _st.stdout.strip() 吃掉 porcelain 首行前导空格，_line[3:] 偏移错位 →
        首文件路径解析失败、mtime 静默丢失 → 复检误判全静默 → 误杀。"""
        import os as _os
        import subprocess as _sp
        # 真实 git worktree + 一个已提交文件（dirty）——必须在 mock 之前执行
        wt = tmp_path / "work"
        wt.mkdir()
        _sp.run(["git", "init", "-q"], cwd=wt)
        _sp.run(["git", "config", "user.email", "t@t.t"], cwd=wt)
        _sp.run(["git", "config", "user.name", "t"], cwd=wt)
        f = wt / "existing.py"
        f.write_text("a=1\n")
        _sp.run(["git", "add", "-A"], cwd=wt)
        _sp.run(["git", "commit", "-qm", "init"], cwd=wt)
        f.write_text("a=2\n")  # worktree dirty → git status 恒为 " M existing.py"

        mock_proc = MagicMock()
        mock_proc.pid = 12370
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 40:
                return 0
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0

        import itertools as _it
        time_gen = _it.count(0, 1000)

        _run_call = [0]

        def _rr(*_a, **_k):
            _run_call[0] += 1
            if _run_call[0] % 2 == 1:  # git status（奇数次：baseline + recheck）
                # 模拟"改写既有文件"：每次采样前刷新 mtime（git status 集合保持不变）
                f.write_text(f"a={_run_call[0]}\n")
                _os.utime(f, (_run_call[0], _run_call[0]))
                # 返回恒定 porcelain（集合不变）—— 修复前 mtime 段会因偏移丢失
                return MagicMock(stdout=" M existing.py\n")
            return MagicMock(stdout="")  # ps（偶数次）→ 无 CPU 行

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                with patch("agent_go.subtask.subprocess.run", side_effect=_rr):
                    with patch("subprocess.Popen", return_value=mock_proc):
                        _run_headless("task", wt, {}, logger, "sub-mtime", config={"goal": {"enabled": False}})

        # mtime 被检出 → 复检判活性 → 复位 → 进程自然退出，不 kill
        mock_proc.kill.assert_not_called()

    @patch("subprocess.Popen")
    def test_s12p3_all_signals_dead_confirms_stuck(self, mock_popen, logger):
        """S12-P3：事件/文件/CPU 全静默经 grace 复检确认 stuck → kill"""
        mock_proc = MagicMock()
        mock_proc.pid = 12361
        call_count = [0]

        def polling():
            call_count[0] += 1
            if call_count[0] > 40:
                return 0
            return None

        mock_proc.poll.side_effect = polling
        mock_proc.stdout.readline.side_effect = ["", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = -9
        mock_popen.return_value = mock_proc

        import itertools as _it
        time_gen = _it.count(0, 1000)

        # 全部信号无活性：git status 恒定空串、ps 恒定 -1（循环供应防 StopIteration）
        run_results = [
            MagicMock(stdout=""),
            MagicMock(stdout=""),
            MagicMock(stdout=""),
            MagicMock(stdout=""),
        ]
        _rr_iter = iter(run_results)

        def _rr(*_a, **_k):
            try:
                return next(_rr_iter)
            except StopIteration:
                return MagicMock(stdout="")

        with patch("time.time", side_effect=lambda: next(time_gen)):
            with patch("time.sleep"):
                with patch("agent_go.subtask.subprocess.run", side_effect=_rr):
                    result = _run_headless("task", Path("/tmp/work"), {}, logger, "sub-p3b", config={"goal": {"enabled": False}})

        mock_proc.kill.assert_called_once()
        assert getattr(result, "kill_reason", None) == "stuck"


# ═══════════════════════════════════════════════════════════════
# usage/cost 聚合与 metering
# ═══════════════════════════════════════════════════════════════

class TestUsageAggregation:
    """result 事件的 token/cost 聚合及 metering.jsonl 写入"""

    def _read_metering(self, path: Path) -> dict:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        return json.loads(lines[0])

    @patch("subprocess.Popen")
    def test_multiple_result_events_aggregated(self, mock_popen, logger, tmp_path):
        """多个 result 事件的 token/cost/duration/turns 累加"""
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({"type": "result", "subtype": "success",
                        "total_cost_usd": 0.01,
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "duration_ms": 1000, "num_turns": 2}) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "total_cost_usd": 0.005,
                        "usage": {"input_tokens": 200, "output_tokens": 100},
                        "duration_ms": 500, "num_turns": 1}) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        result = _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering)},
            logger, "sub-u1"
        )
        assert result.returncode == 0
        ev = self._read_metering(metering)
        assert ev["prompt_tokens"] == 300
        assert ev["completion_tokens"] == 150
        assert ev["cost_usd"] == 0.015
        assert ev["latency_ms"] == 1500
        assert ev["num_turns"] == 3
        # 未注入路由模型时记录默认执行器名
        assert ev["actual_model"] == "claude-code-executor"

    @patch("subprocess.Popen")
    def test_usage_fallback_keys(self, mock_popen, logger, tmp_path):
        """usage 缺 input_tokens/output_tokens 时回退 prompt_tokens/completion_tokens；
        input_tokens 存在时优先于 prompt_tokens"""
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({"type": "result", "subtype": "success",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "usage": {"input_tokens": 100, "prompt_tokens": 999,
                                  "output_tokens": 40}}) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering)},
            logger, "sub-u2"
        )
        ev = self._read_metering(metering)
        assert ev["prompt_tokens"] == 110
        assert ev["completion_tokens"] == 45
        assert ev["cost_usd"] == 0.0

    @patch("subprocess.Popen")
    def test_result_without_usage_no_metering(self, mock_popen, logger, tmp_path):
        """result 事件无 token 且无 cost 时不聚合（仅 duration/turns 不够），不写计量"""
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({"type": "result", "subtype": "success"}) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "total_cost_usd": 0.0,
                        "duration_ms": 9999, "num_turns": 9}) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering)},
            logger, "sub-u3"
        )
        assert not metering.exists()

    @patch("subprocess.Popen")
    def test_metering_failed_result(self, mock_popen, logger, tmp_path):
        """子进程非 0 退出（非交互）时 metering result 为 failed"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "error",
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 500, "output_tokens": 80},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events, returncode=1)
        result = _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering)},
            logger, "sub-u4"
        )
        assert result.returncode == 1
        assert mock_popen.call_count == 1  # 非交互失败不重试
        ev = self._read_metering(metering)
        assert ev["result"] == "failed"
        assert ev["prompt_tokens"] == 500

    @patch("subprocess.Popen")
    def test_metering_actual_model_and_difficulty(self, mock_popen, logger, tmp_path):
        """路由模型与难度从 env 注入 metering 记录"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.03,
            "usage": {"input_tokens": 800, "output_tokens": 120},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-opus-4",
             "AGENT_GO_DIFFICULTY": "hard"},
            logger, "sub-u5"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "claude-opus-4"
        assert ev["difficulty"] == "hard"

    @patch("subprocess.Popen")
    def test_metering_resolves_real_model_from_assistant(self, mock_popen, logger, tmp_path):
        """assistant 事件的 message.model 覆盖路由名：记录实际请求的模型

        --model claude-haiku-4-5 被 claude 解析为 deepseek-v4-flash 后，
        metering 应记录实际模型 deepseek-v4-flash，同时保留 routed_model。
        """
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-flash", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.03,
                "usage": {"input_tokens": 800, "output_tokens": 120},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5",
             "AGENT_GO_DIFFICULTY": "easy"},
            logger, "sub-u6"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "deepseek-v4-flash"
        assert ev["routed_model"] == "claude-haiku-4-5"
        assert ev["difficulty"] == "easy"

    @patch("subprocess.Popen")
    def test_metering_no_assistant_model_falls_back_to_routed(self, mock_popen, logger, tmp_path):
        """无 assistant 事件（或 message.model 缺失）时回退路由名"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.03,
            "usage": {"input_tokens": 800, "output_tokens": 120},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-sonnet-4-6"},
            logger, "sub-u7"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "claude-sonnet-4-6"
        assert ev["routed_model"] == "claude-sonnet-4-6"

    @patch("subprocess.Popen")
    def test_metering_local_backend_zero_cost(self, mock_popen, logger, tmp_path):
        """AGENT_GO_IS_LOCAL=1 时：成本清零，actual_model 用 AGENT_GO_LOCAL_MODEL

        本地后端（如 4000 代理 → Qwen3.6-27B-4bit）时，claude 响应的 model
        （deepseek-v4-flash）是内置映射不代表真实后端，应被本地模型名覆盖。
        """
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-flash", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.03,
                "usage": {"input_tokens": 800, "output_tokens": 120},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5",
             "AGENT_GO_IS_LOCAL": "1",
             "AGENT_GO_LOCAL_MODEL": "Qwen3.6-27B-4bit"},
            logger, "sub-u8"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "Qwen3.6-27B-4bit"
        assert ev["routed_model"] == "claude-haiku-4-5"
        assert ev["cost_usd"] == 0.0
        assert ev["is_local"] is True

    @patch("subprocess.Popen")
    def test_metering_local_backend_fallback_to_routed(self, mock_popen, logger, tmp_path):
        """本地后端但未配置 AGENT_GO_LOCAL_MODEL 时，actual_model 回退路由名"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 800, "output_tokens": 120},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5",
             "AGENT_GO_IS_LOCAL": "1"},
            logger, "sub-u9"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "claude-haiku-4-5"
        assert ev["cost_usd"] == 0.0
        assert ev["is_local"] is True

    @patch("subprocess.Popen")
    def test_metering_local_zero_tokens_still_written(self, mock_popen, logger, tmp_path):
        """2026-08-12 修复：本地 worker 即使 token/cost 全 0 也写 metering 事件。

        mlx/Qwen 本地代理可能不返回 usage tokens（claude -p 解析失败 → 全 0）。
        此前 658 行条件要求 token/cost 非零，导致本地 worker 事件缺失 →
        analyze_cost 无法识别本地事件折算 TCO。AGENT_GO_IS_LOCAL=1 时强制写。
        """
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-sonnet-4-6",
             "AGENT_GO_IS_LOCAL": "1",
             "AGENT_GO_LOCAL_MODEL": "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit"},
            logger, "sub-u10"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit"
        assert ev["cost_usd"] == 0.0
        assert ev["is_local"] is True

    @patch("subprocess.Popen")
    def test_metering_legacy_local_models_config(self, mock_popen, logger, tmp_path):
        """兼容旧配置：AGENT_GO_LOCAL_MODELS 列出的模型成本清零"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 800, "output_tokens": 120},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5",
             "AGENT_GO_LOCAL_MODELS": "claude-haiku-4-5,qwen-local"},
            logger, "sub-u10"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "claude-haiku-4-5"
        assert ev["cost_usd"] == 0.0
        assert ev["is_local"] is True

    @patch("subprocess.Popen")
    def test_metering_cost_recomputed_with_deepseek_pricing(self, mock_popen, logger, tmp_path):
        """实际模型为 deepseek-v4-flash 时，成本按 DeepSeek 定价重算，而非 claude 的 Anthropic 定价。

        claude 返回 total_cost_usd=0.0571（按 claude-haiku-4-5 的 $1/$5 定价），
        但实际模型 deepseek-v4-flash 定价 $0.14/$0.28，重算后成本大幅降低。
        """
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-flash", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.0571,
                "usage": {"input_tokens": 36229, "output_tokens": 688},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5"},
            logger, "sub-u11"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "deepseek-v4-flash"
        # 重算: 36229/1e6*0.14 + 688/1e6*0.28 = 0.005072 + 0.000193 = 0.005265
        assert abs(ev["cost_usd"] - 0.005265) < 0.0001

    @patch("subprocess.Popen")
    def test_metering_cost_recomputed_pro_pricing(self, mock_popen, logger, tmp_path):
        """deepseek-v4-pro 定价重算成本。"""
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-pro", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.2486,
                "usage": {"input_tokens": 35128, "output_tokens": 583},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-opus-4-7"},
            logger, "sub-u12"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "deepseek-v4-pro"
        # 重算: 35128/1e6*0.435 + 583/1e6*0.87 = 0.015281 + 0.000507 = 0.015788
        assert abs(ev["cost_usd"] - 0.015788) < 0.0001

    @patch("subprocess.Popen")
    def test_metering_cost_unknown_model_falls_back(self, mock_popen, logger, tmp_path):
        """未知模型（无定价）时回退 claude 返回的成本。"""
        metering = tmp_path / "metering.jsonl"
        events = [json.dumps({
            "type": "result", "subtype": "success",
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 1500, "output_tokens": 300},
        }) + "\n"]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering)},
            logger, "sub-u13"
        )
        ev = self._read_metering(metering)
        assert ev["cost_usd"] == 0.0123

    @patch("subprocess.Popen")
    def test_metering_cost_reduction_range_flash(self, mock_popen, logger, tmp_path):
        """真实 DeepSeek 验证固化：claude-haiku-4-5 → deepseek-v4-flash 重算降幅在 82-92%。

        使用本次真实验证的数据（input=41335, output=203）：
        - claude 原始 total_cost_usd=$0.04961（按 claude-haiku-4-5 的 $1/$5 定价）
        - 重算 = 41335/1e6*0.14 + 203/1e6*0.28 = $0.00584
        - 降幅 = 1 - 0.00584/0.04961 ≈ 88.2%
        """
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-flash", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.04961,
                "usage": {"input_tokens": 41335, "output_tokens": 203},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5"},
            logger, "sub-u14"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "deepseek-v4-flash"
        recomputed = ev["cost_usd"]
        # 重算 = 41335/1e6*0.14 + 203/1e6*0.28
        expected = 41335 / 1e6 * 0.14 + 203 / 1e6 * 0.28
        assert abs(recomputed - expected) < 0.0001
        # 降幅必须在 82-92% 范围（防回归：若重算失效会退回 claude 原始成本，降幅≈0）
        reduction = 1 - recomputed / 0.04961
        assert 0.82 <= reduction <= 0.92, f"降幅 {reduction:.1%} 超出 82-92% 范围"

    @patch("subprocess.Popen")
    def test_metering_cost_reduction_range_pro(self, mock_popen, logger, tmp_path):
        """真实验证固化：claude-opus-4-7 → deepseek-v4-pro 重算降幅在 82-92%。

        用 v2 bench 实测数据（input=35128, output=583）：
        - claude 原始 total_cost_usd=$0.2486（按 claude-opus-4-7 的 $5/$25 定价）
        - 重算 = 35128/1e6*0.435 + 583/1e6*0.87 = $0.01579
        - 降幅 = 1 - 0.01579/0.2486 ≈ 93.6%（略超 92% 上限，因 output 占比高）
        本测试验证 pro 路径的降幅 ≥82%（防回归下限）。
        """
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "deepseek-v4-pro", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.2486,
                "usage": {"input_tokens": 35128, "output_tokens": 583},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-opus-4-7"},
            logger, "sub-u15"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "deepseek-v4-pro"
        recomputed = ev["cost_usd"]
        expected = 35128 / 1e6 * 0.435 + 583 / 1e6 * 0.87
        assert abs(recomputed - expected) < 0.0001
        reduction = 1 - recomputed / 0.2486
        assert reduction >= 0.82, f"降幅 {reduction:.1%} 低于 82% 下限"

    @patch("subprocess.Popen")
    def test_metering_cloud_actual_model_priced(self, mock_popen, logger, tmp_path):
        """S12 云后端透传：AGENT_GO_ACTUAL_MODEL（如 glm-4.7，URL 本地但实际走云）
        优先计价——成本按实际模型而非清零，is_local=False。"""
        metering = tmp_path / "metering.jsonl"
        events = [
            json.dumps({
                "type": "assistant",
                "message": {"model": "glm-4.7", "content": [
                    {"type": "text", "text": "ok"},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success",
                "total_cost_usd": 0.0409,
                "usage": {"input_tokens": 40064, "output_tokens": 107},
            }) + "\n",
        ]
        mock_popen.return_value = _make_proc(events)
        _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(metering),
             "AGENT_GO_CLAUDE_MODEL": "claude-haiku-4-5",
             "AGENT_GO_ACTUAL_MODEL": "glm-4.7"},
            logger, "sub-cloud"
        )
        ev = self._read_metering(metering)
        assert ev["actual_model"] == "glm-4.7"
        assert ev["is_local"] is False
        # glm-4.7 定价 $0.5556/$2.2222 重算
        expected = 40064 / 1e6 * 0.5556 + 107 / 1e6 * 2.2222
        assert abs(ev["cost_usd"] - expected) < 0.0001
        assert ev["cost_usd"] > 0  # 不再清零


# ═══════════════════════════════════════════════════════════════
# CR-H2 回归守护：_parse_cpu_time（模块级，ps 时间格式解析）
# ═══════════════════════════════════════════════════════════════

def test_parse_cpu_time_linux_ticks():
    """Linux 纯 ticks → float。"""
    from agent_go.subtask import _parse_cpu_time
    assert _parse_cpu_time("1234") == 1234.0
    assert _parse_cpu_time("0") == 0.0


def test_parse_cpu_time_macos_minutes_seconds():
    """macOS M:SS.cc 格式 → 秒。CR-H2 修复前 float() 直接解析会 ValueError。"""
    from agent_go.subtask import _parse_cpu_time
    assert _parse_cpu_time("2:33.26") == 153.26
    assert _parse_cpu_time("11:26.55") == 686.55
    assert _parse_cpu_time("0:00.02") == 0.02


def test_parse_cpu_time_macos_hours_and_days():
    """macOS H:MM:SS 与 D-HH:MM:SS 格式。"""
    from agent_go.subtask import _parse_cpu_time
    assert _parse_cpu_time("1:02:03") == 3723.0          # 1h2m3s
    assert _parse_cpu_time("1-02:03:04") == 93784.0      # 1天2h3m4s


def test_parse_cpu_time_invalid_raises():
    """非法输入抛 ValueError（调用方跳过该行）。"""
    from agent_go.subtask import _parse_cpu_time
    import pytest
    for bad in ("", "   ", "abc", "x:y"):
        with pytest.raises(ValueError):
            _parse_cpu_time(bad)


@patch("subprocess.Popen")
@patch("agent_go.subtask.subprocess.run", return_value=MagicMock(stdout=""))
def test_kill_state_written_before_proc_kill(mock_run, mock_popen, logger, tmp_path):
    """P2-2: kill_state metering 必须写在 proc.kill() 之前——SIGKILL 后事件可能丢失，
    顺序颠倒会导致 kill_reason 分类丢失（G1 持久化时机契约）。"""
    import agent_go.config as config_mod
    mock_proc = MagicMock()
    mock_proc.pid = 12349
    call_count = [0]

    def polling():
        call_count[0] += 1
        if call_count[0] > 30:
            return 0
        return None

    mock_proc.poll.side_effect = polling
    mock_proc.stdout.readline.side_effect = ["", ""]
    mock_proc.stderr.readline.side_effect = ["", ""]
    mock_proc.returncode = -9
    mock_popen.return_value = mock_proc

    import itertools as _it
    time_gen = _it.count(0, 1000)
    meter_path = tmp_path / "metering.jsonl"

    order = []
    real_meter = config_mod.meter_event

    def _meter_wrapper(mp, ev, **kw):
        order.append(("meter", ev.get("event", "?")))
        return real_meter(mp, ev, **kw)

    real_kill = mock_proc.kill

    def _kill_wrapper(*a, **k):
        order.append(("kill", None))
        return real_kill(*a, **k)

    mock_proc.kill = _kill_wrapper
    with patch("time.time", side_effect=lambda: next(time_gen)), \
         patch("time.sleep"), \
         patch.object(config_mod, "meter_event", side_effect=_meter_wrapper):
        from agent_go.subtask import _run_headless
        _run_headless("task", Path("/tmp/work"),
                      {"AGENT_GO_METERING_PATH": str(meter_path), "AGENT_GO_TASK_ID": "t",
                       "AGENT_GO_DIFFICULTY": "hard"},
                      logger, "sub-ord")

    kill_idx = next((i for i, (kind, _e) in enumerate(order) if kind == "kill"), None)
    meter_kill_idxs = [i for i, (kind, ev) in enumerate(order) if kind == "meter" and ev == "kill_state"]
    assert meter_kill_idxs, "kill_state 事件应已写入"
    assert kill_idx is not None, "proc.kill 应被调用"
    assert meter_kill_idxs[0] < kill_idx, "kill_state 必须先于 proc.kill() 落盘"


@patch("subprocess.Popen")
@patch("agent_go.subtask.subprocess.run", return_value=MagicMock(stdout=""))
def test_stuck_grace_env_override_accepted(mock_run, mock_popen, logger, tmp_path):
    """CR-TD：AGENT_GO_STUCK_GRACE_SEC 环境变量可覆盖 grace 窗（参数化后默认 120s 行为不变，
    env 覆盖被接受、kill_reason=stuck 仍正确）。"""
    mock_proc = MagicMock()
    mock_proc.pid = 12377
    call_count = [0]

    def polling():
        call_count[0] += 1
        if call_count[0] > 30:
            return 0
        return None

    mock_proc.poll.side_effect = polling
    mock_proc.stdout.readline.side_effect = ["", ""]
    mock_proc.stderr.readline.side_effect = ["", ""]
    mock_proc.returncode = -9
    mock_popen.return_value = mock_proc
    import itertools as _it
    time_gen = _it.count(0, 1000)
    meter_path = tmp_path / "metering.jsonl"
    with patch("time.time", side_effect=lambda: next(time_gen)), \
         patch("time.sleep"):
        from agent_go.subtask import _run_headless
        result = _run_headless(
            "task", Path("/tmp/work"),
            {"AGENT_GO_METERING_PATH": str(meter_path), "AGENT_GO_TASK_ID": "t",
             "AGENT_GO_DIFFICULTY": "hard", "AGENT_GO_STUCK_GRACE_SEC": "30"},
            logger, "sub-grace", config={"goal": {"enabled": False}})
    mock_proc.kill.assert_called_once()
    assert getattr(result, "kill_reason", None) == "stuck"
