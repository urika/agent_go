"""补强 subtask.py 的超时与看门狗覆盖（PRD S2/S3 验证循环）。

覆盖：
  - hard_timeout（retry_timeout 硬超时）：到点即 kill，记 headless_hard_timeout 事件
  - idle 超时（IDLE_TIMEOUT）：长时间无事件 → kill
  - goal 看门狗：goal 循环超时 / 轮数超限 → kill
  - env 注入的 goal 配置覆盖磁盘 config

依赖 time.time / time.sleep 被 mock 以快速触发分支，避免真实等待。
"""

import json

from unittest.mock import patch, MagicMock

from agent_go.subtask import _run_headless


def _make_proc(stdout_lines=(), stderr_lines=(), returncode=0, pid=42000):
    """构造 mock claude 进程：stdout/stderr 逐行吐出后 EOF，poll 立即返回。"""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.returncode = returncode
    out = list(stdout_lines)
    err = list(stderr_lines)
    mock_proc.stdout.readline.side_effect = lambda: out.pop(0) if out else ""
    mock_proc.stderr.readline.side_effect = lambda: err.pop(0) if err else ""
    mock_proc.poll.return_value = returncode
    return mock_proc


def _make_alive_proc(stdout_lines=(), pid=43000):
    """构造存活进程：poll 一直返回 None（除非被 kill 标记）。

    用于让看门狗/超时循环有机会跑分支。stdout 行消费完后继续返回空行（进程仍存活）。
    wait() 立即返回 0 避免 join 后阻塞。
    """
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.returncode = None
    out = list(stdout_lines)
    mock_proc.stdout.readline.side_effect = lambda: out.pop(0) if out else ""
    mock_proc.stderr.readline.side_effect = lambda: ""
    mock_proc.poll.return_value = None
    mock_proc.wait.return_value = 0
    return mock_proc


def _time_factory(t0=100.0, warmup=6):
    """生成一个 time.time() mock：前 warmup 次返回 t0，之后线性递增。

    线性递增保证：无论 _run_one 在 run_start 之前采样几次 time.time()，
    只要 warmup 足够大，run_start/last_ts/goal_start_ts 都落在 t0 基线上；
    之后每次采样 +1，第 warmup+k 次调用返回 t0+k，差值随迭代次数增长，
    必然越过任何有限阈值（hard_timeout / IDLE_TIMEOUT / GOAL_TIMEOUT）。
    """
    state = {"n": 0}

    def _now():
        state["n"] += 1
        if state["n"] <= warmup:
            return t0
        return t0 + (state["n"] - warmup)
    return _now


# ═══════════════════════════════════════════════════════════════
# hard_timeout（PRD S2 retry_timeout 硬超时）
# ═══════════════════════════════════════════════════════════════

class TestHardTimeout:
    """hard_timeout: 到点即 kill，不依赖事件活动。"""

    @patch("subprocess.Popen")
    def test_hard_timeout_kills_process(self, mock_popen, logger, tmp_path):
        """run_start 后 time.time() 越过 hard_timeout → kill + 记事件"""
        proc = _make_alive_proc()
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.subtask.time.time",
                   side_effect=_time_factory()):
            _run_headless(
                "task", tmp_path, {}, logger, "sub-hard",
                hard_timeout=10,
            )

        proc.kill.assert_called()
        assert mock_popen.call_count == 1  # 超时不重试

    @patch("subprocess.Popen")
    def test_hard_timeout_zero_no_kill(self, mock_popen, logger, tmp_path):
        """hard_timeout=0（默认）→ 不触发硬超时分支，走正常 result 事件结束"""
        result_event = json.dumps({"type": "result", "subtype": "success",
                                   "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n"
        proc = _make_proc([result_event])
        mock_popen.return_value = proc

        result = _run_headless("task", tmp_path, {}, logger, "sub-nohard")
        assert result.returncode == 0
        proc.kill.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# env 注入的 goal 看门狗配置覆盖磁盘 config
# ═══════════════════════════════════════════════════════════════

class TestGoalWatchdogConfigFromEnv:
    """AGENT_GO_GOAL_* env 优先于磁盘 config（CLI 覆盖生效）。"""

    @patch("subprocess.Popen")
    def test_env_disables_goal_watchdog(self, mock_popen, logger, tmp_path):
        """AGENT_GO_GOAL_ENABLED=0 → GOAL_WATCHDOG_ENABLED=False（env 覆盖磁盘）"""
        result_event = json.dumps({"type": "result", "subtype": "success",
                                   "usage": {"input_tokens": 1}}) + "\n"
        mock_popen.return_value = _make_proc([result_event])

        # 即使磁盘 load_config 返回 enabled=True，env=0 也要覆盖
        with patch("agent_go.config.load_config",
                   return_value={"goal": {"enabled": True, "max_turns": 5}}):
            _run_headless("task", tmp_path,
                          {"AGENT_GO_GOAL_ENABLED": "0"}, logger, "sub-env")
        # 仅断言不抛错（env 解析路径已执行）；proc 正常结束
        assert mock_popen.called

    @patch("subprocess.Popen")
    def test_env_max_turns_invalid_falls_back(self, mock_popen, logger, tmp_path):
        """AGENT_GO_GOAL_MAX_TURNS 非整数 → 静默保留默认（不抛 ValueError）"""
        result_event = json.dumps({"type": "result", "subtype": "success",
                                   "usage": {"input_tokens": 1}}) + "\n"
        mock_popen.return_value = _make_proc([result_event])

        _run_headless("task", tmp_path,
                      {"AGENT_GO_GOAL_MAX_TURNS": "not-a-number",
                       "AGENT_GO_GOAL_TIMEOUT": "also-bad"},
                      logger, "sub-env-bad")
        assert mock_popen.called

    @patch("subprocess.Popen")
    def test_load_config_failure_silent(self, mock_popen, logger, tmp_path):
        """load_config 抛异常 → 静默使用默认看门狗配置"""
        result_event = json.dumps({"type": "result", "subtype": "success",
                                   "usage": {"input_tokens": 1}}) + "\n"
        mock_popen.return_value = _make_proc([result_event])

        with patch("agent_go.config.load_config", side_effect=RuntimeError("boom")):
            _run_headless("task", tmp_path, {}, logger, "sub-cfgfail")
        assert mock_popen.called


# ═══════════════════════════════════════════════════════════════
# idle 超时（IDLE_TIMEOUT）与 goal 看门狗：通过时间 mock 触发
# ═══════════════════════════════════════════════════════════════

class TestIdleTimeoutKill:
    """last_ts 长期不更新（无事件）→ idle 超过 IDLE_TIMEOUT → kill。"""

    @patch("subprocess.Popen")
    def test_idle_timeout_kills(self, mock_popen, logger, tmp_path):
        """进程存活 + 无事件 + idle > IDLE_TIMEOUT → kill

        time.time 线性递增，差值最终 > IDLE_TIMEOUT(600) → 触发 idle kill。
        """
        proc = _make_alive_proc()
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.subtask.time.time",
                   side_effect=_time_factory()):
            _run_headless("task", tmp_path, {}, logger, "sub-idle")
        proc.kill.assert_called()


class TestGoalWatchdogTimeout:
    """goal 看门狗：elapsed > GOAL_TIMEOUT 或 goal_turn_count >= MAX_GOAL_TURNS → kill。"""

    @patch("subprocess.Popen")
    def test_goal_timeout_kills(self, mock_popen, logger, tmp_path):
        """goal_start_ts 初值=run_start；time.time 越过 GOAL_TIMEOUT → kill

        配置 env AGENT_GO_GOAL_ENABLED=1 + AGENT_GO_GOAL_TIMEOUT=10（很短）。
        线性递增的 time.time 让 goal 看门狗分支在 idle 超时前命中。
        """
        proc = _make_alive_proc()
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.subtask.time.time",
                   side_effect=_time_factory()):
            _run_headless(
                "task", tmp_path,
                {"AGENT_GO_GOAL_ENABLED": "1", "AGENT_GO_GOAL_TIMEOUT": "10"},
                logger, "sub-goal-to",
            )
        proc.kill.assert_called()


# ═══════════════════════════════════════════════════════════════
# B（2026-08-23）：goal turn 计数语义——只数「验证循环轮数」（Bash 且命令
# 命中 AGENT_GO_VERIFY_HINT 的 token 交集 ≥2），非验证工具调用不消耗预算；
# hint 为空回退旧口径（全部工具调用计数）；GOAL_TIMEOUT 按难度缩放。
# ═══════════════════════════════════════════════════════════════

def _tool_use_lines(tool_name: str, command: str = ""):
    """构造一次工具调用的 stream-json 事件行（start → input delta → stop）。"""
    lines = [json.dumps({"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"name": tool_name}}})]
    if command:
        lines.append(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta",
                      "partial_json": json.dumps({"command": command})}}}))
    lines.append(json.dumps({"type": "stream_event", "event": {
        "type": "content_block_stop"}}))
    return lines


_VERIFY_HINT = "python -m pytest tests/test_cache.py -q"
_GOAL_ENV = {"AGENT_GO_GOAL_ENABLED": "1", "AGENT_GO_GOAL_MAX_TURNS": "3",
             "AGENT_GO_VERIFY_HINT": _VERIFY_HINT}


class TestGoalWatchdogVerifyRounds:
    """验证轮计数：Bash 执行验证命令才消耗 max_turns 预算。"""

    @patch("subprocess.Popen")
    def test_verify_bash_counts_round(self, mock_popen, logger, tmp_path):
        """3 次 Bash 验证调用达到 MAX_GOAL_TURNS=3 → goal_turns_exceeded kill。"""
        events = []
        for _ in range(3):
            events += _tool_use_lines("Bash", "python -m pytest tests/test_cache.py -q")
        proc = _make_alive_proc(events)
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.config.load_config", return_value={}), \
             patch("agent_go.subtask.log_event") as mock_log:
            _run_headless("task", tmp_path, dict(_GOAL_ENV), logger, "sub-vr")

        proc.kill.assert_called()
        assert any(c.args[1] == "goal_turns_exceeded" for c in mock_log.call_args_list)

    @patch("subprocess.Popen")
    def test_non_verify_tools_not_counted(self, mock_popen, logger, tmp_path):
        """Read / 非验证 Bash 调用不消耗轮数预算 → 正常结束不 kill。"""
        events = []
        for _ in range(6):  # 远超 MAX_GOAL_TURNS=3，但都不是验证调用
            events += _tool_use_lines("Read")
            events += _tool_use_lines("Bash", "ls -la src/")
        events.append(json.dumps({"type": "result", "subtype": "success",
                                  "usage": {"input_tokens": 1}}))
        proc = _make_proc(events)
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.config.load_config", return_value={}), \
             patch("agent_go.subtask.log_event") as mock_log:
            _run_headless("task", tmp_path, dict(_GOAL_ENV), logger, "sub-nvr")

        proc.kill.assert_not_called()
        assert not any(c.args[1] == "goal_turns_exceeded" for c in mock_log.call_args_list)

    @patch("subprocess.Popen")
    def test_no_hint_fallback_counts_all(self, mock_popen, logger, tmp_path):
        """无 VERIFY_HINT → 回退旧口径：任意工具调用都计数（防死循环兜底）。"""
        events = []
        for _ in range(3):
            events += _tool_use_lines("Read")  # Read 也计
        proc = _make_alive_proc(events)
        mock_popen.return_value = proc
        env = {"AGENT_GO_GOAL_ENABLED": "1", "AGENT_GO_GOAL_MAX_TURNS": "3"}

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.config.load_config", return_value={}), \
             patch("agent_go.subtask.log_event") as mock_log:
            _run_headless("task", tmp_path, env, logger, "sub-nohint")

        proc.kill.assert_called()
        assert any(c.args[1] == "goal_turns_exceeded" for c in mock_log.call_args_list)

    @patch("subprocess.Popen")
    def test_goal_timeout_scales_with_difficulty(self, mock_popen, logger, tmp_path):
        """GOAL_TIMEOUT 按 AGENT_GO_DIFFICULTY 缩放：10s × hard(2.5) → limit=25。"""
        proc = _make_alive_proc()
        mock_popen.return_value = proc

        with patch("agent_go.subtask.time.sleep", lambda s: None), \
             patch("agent_go.subtask.time.time", side_effect=_time_factory()), \
             patch("agent_go.config.load_config", return_value={}), \
             patch("agent_go.subtask.log_event") as mock_log:
            _run_headless(
                "task", tmp_path,
                {"AGENT_GO_GOAL_ENABLED": "1", "AGENT_GO_GOAL_TIMEOUT": "10",
                 "AGENT_GO_DIFFICULTY": "hard"},
                logger, "sub-goal-scale",
            )

        proc.kill.assert_called()
        timeout_events = [c for c in mock_log.call_args_list
                          if c.args[1] == "goal_timeout"]
        assert timeout_events, "应触发 goal_timeout"
        assert timeout_events[0].args[2]["limit"] == 25  # 10 × 2.5
