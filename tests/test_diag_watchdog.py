"""测试 executor._start_diag_watchdog — C4 轮级看门狗（检测+上报，不杀进程）"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go import executor
from agent_go import diag as diag_mod


def _env(tmp_path):
    return {
        "AGENT_GO_SESSION_KEY": diag_mod.session_key("task-wd", "sub-1"),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000/v1/messages",
        "AGENT_GO_METERING_PATH": str(tmp_path / "metering.jsonl"),
    }


def _config():
    return {"diag_watchdog": {"poll_interval_sec": 5, "dup_threshold": 3}}


def _wait_state(state, key, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state.get(key):
            return True
        time.sleep(0.1)
    return False


class TestDiagWatchdog:
    def test_no_session_key_noop(self, tmp_path, logger):
        stop, state = executor._start_diag_watchdog(_config(), {}, "t", "s", logger)
        stop()
        assert state["loop_detected"] is False

    def test_cloud_url_not_started(self, tmp_path, logger):
        env = _env(tmp_path)
        env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com/v1/messages"
        stop, state = executor._start_diag_watchdog(_config(), env, "t", "s", logger)
        stop()
        assert state["loop_detected"] is False

    def test_dup_queries_trigger_detection(self, tmp_path, logger):
        ledger = {"turns_seen": 10,
                  "dup_queries": [{"target": "github ansible pull", "count": 4, "last_turn": 9}]}
        with patch.object(diag_mod, "get_session_ledger", return_value=ledger):
            stop, state = executor._start_diag_watchdog(_config(), _env(tmp_path), "task-wd", "sub-1", logger)
            assert _wait_state(state, "loop_detected")
            stop()
        # metering 事件落地
        events = [json.loads(line) for line in
                  (tmp_path / "metering.jsonl").read_text(encoding="utf-8").splitlines()]
        diag_events = [e for e in events if e.get("role") == "worker_diag" and e.get("loop_detected")]
        assert diag_events and diag_events[0]["dup_count"] == 4

    def test_stable_ledger_no_detection(self, tmp_path, logger):
        ledger = {"turns_seen": 10,
                  "dup_queries": [{"target": "x", "count": 2, "last_turn": 3}]}
        with patch.object(diag_mod, "get_session_ledger", return_value=ledger):
            stop, state = executor._start_diag_watchdog(_config(), _env(tmp_path), "t", "s", logger)
            time.sleep(6)  # 至少一次轮询
            stop()
        assert state["loop_detected"] is False

    def test_ledger_404_fail_open(self, tmp_path, logger):
        with patch.object(diag_mod, "get_session_ledger", return_value=None):
            stop, state = executor._start_diag_watchdog(_config(), _env(tmp_path), "t", "s", logger)
            time.sleep(6)
            stop()
        assert state["loop_detected"] is False
        assert not (tmp_path / "metering.jsonl").exists()

    def test_ledger_exception_never_raises(self, tmp_path, logger):
        with patch.object(diag_mod, "get_session_ledger", side_effect=RuntimeError("boom")):
            stop, state = executor._start_diag_watchdog(_config(), _env(tmp_path), "t", "s", logger)
            time.sleep(6)
            stop()
        assert state["loop_detected"] is False
