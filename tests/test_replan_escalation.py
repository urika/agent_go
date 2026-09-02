"""AG-3 确定性决策层测试：TaskCircuitBreaker + decide_escalation（EscalationDecision 契约）。"""

import time

from agent_go.llama_contracts import CONTRACT_VERSION, EscalationDecision
from agent_go.replan import (
    ESCALATION_ACTIONS,
    TaskCircuitBreaker,
    build_reload_context,
    decide_escalation,
)


class TestTaskCircuitBreaker:
    def test_closed_by_default(self):
        b = TaskCircuitBreaker()
        assert b.can_execute("verify_revert") is True
        assert b.status("verify_revert")["open"] is False

    def test_opens_after_threshold(self):
        b = TaskCircuitBreaker(threshold=3, cooldown_s=300)
        for _ in range(3):
            b.record_failure("verify_revert")
        assert b.can_execute("verify_revert") is False
        st = b.status("verify_revert")
        assert st["open"] is True
        assert st["remaining_cooldown_s"] > 0

    def test_below_threshold_stays_closed(self):
        b = TaskCircuitBreaker(threshold=3)
        b.record_failure("x")
        b.record_failure("x")
        assert b.can_execute("x") is True

    def test_success_resets_count(self):
        b = TaskCircuitBreaker(threshold=2)
        b.record_failure("x")
        b.record_success("x")
        b.record_failure("x")
        assert b.can_execute("x") is True

    def test_cooldown_expiry_closes(self):
        b = TaskCircuitBreaker(threshold=1, cooldown_s=0)
        b.record_failure("x")
        time.sleep(0.01)
        assert b.can_execute("x") is True  # 冷却 0s → 立即关闭
        assert b.status("x")["open"] is False

    def test_classes_independent(self):
        b = TaskCircuitBreaker(threshold=1)
        b.record_failure("a")
        assert b.can_execute("a") is False
        assert b.can_execute("b") is True


class TestDecideEscalation:
    def test_no_progress_signal_gives_split(self):
        d = decide_escalation("s1", "verify_revert", attempt=0, max_retries=3)
        assert isinstance(d, EscalationDecision)
        assert d["action"] == "split"
        assert d["reason"] == "verify_revert"
        assert d["contract_version"] == CONTRACT_VERSION

    def test_all_no_progress_triggers_split(self):
        for reason in ("verify_revert", "verify_divergence", "failure_pattern_repeat"):
            assert decide_escalation("s1", reason, 0, 3)["action"] == "split"

    def test_default_retry(self):
        d = decide_escalation("s1", "test_failed", attempt=1, max_retries=3)
        assert d["action"] == "retry"
        assert d["reason"] == "test_failed"

    def test_max_retries_gives_human(self):
        d = decide_escalation("s1", "verify_revert", attempt=3, max_retries=3)
        assert d["action"] == "human"
        assert d["reason"] == "max_retries_exceeded"

    def test_breaker_open_gives_human(self):
        b = TaskCircuitBreaker(threshold=1)
        b.record_failure("verify_revert")
        d = decide_escalation("s1", "verify_revert", attempt=0, max_retries=3,
                              breaker=b)
        assert d["action"] == "human"
        assert d["reason"] == "circuit_breaker_open"

    def test_breaker_priority_over_max_retries(self):
        # 熔断打开优先于幂等闸（决策表顺序）
        b = TaskCircuitBreaker(threshold=1)
        b.record_failure("x")
        d = decide_escalation("s1", "x", attempt=5, max_retries=3, breaker=b)
        assert d["reason"] == "circuit_breaker_open"

    def test_failure_class_defaults_to_reason(self):
        d = decide_escalation("s1", "verify_divergence", 0, 3)
        assert d["signals_at_decision"]["failure_class"] == "verify_divergence"

    def test_signals_recorded(self):
        d = decide_escalation("s1", "test_failed", attempt=2, max_retries=3)
        sig = d["signals_at_decision"]
        assert sig["reason"] == "test_failed"
        assert sig["attempt"] == 2
        assert sig["max_retries"] == 3

    def test_action_vocabulary(self):
        # AG-4/5 已启用 reload，动作子集扩展为 retry/reload/split/human
        assert set(ESCALATION_ACTIONS) == {"retry", "reload", "split", "human"}

    def test_task_context_enabled_gives_reload(self):
        cfg = {"task_context": {"enabled": True}}
        for reason in ("verify_revert", "verify_divergence", "failure_pattern_repeat"):
            d = decide_escalation("s1", reason, attempt=0, max_retries=3, config=cfg)
            assert d["action"] == "reload", f"{reason} 应触发 reload"
            assert d["reason"] == reason

    def test_task_context_disabled_falls_back_to_split(self):
        # 默认未开启 task_context，无进展信号仍走 split
        for reason in ("verify_revert", "verify_divergence", "failure_pattern_repeat"):
            assert decide_escalation("s1", reason, 0, 3)["action"] == "split"

    def test_reload_respects_circuit_breaker_and_max_retries(self):
        cfg = {"task_context": {"enabled": True}}
        # 熔断打开时 reload 仍被拦截为 human
        b = TaskCircuitBreaker(threshold=1)
        b.record_failure("verify_revert")
        assert decide_escalation("s1", "verify_revert", 0, 3, breaker=b, config=cfg)["action"] == "human"
        # 达到 max_retries 时 reload 被拦截为 human
        assert decide_escalation("s1", "verify_revert", 3, 3, config=cfg)["action"] == "human"

    def test_non_no_progress_reason_never_reload(self):
        cfg = {"task_context": {"enabled": True}}
        d = decide_escalation("s1", "some_other_failure", 0, 3, config=cfg)
        assert d["action"] == "retry"


class TestBuildReloadContext:
    def test_missing_session_key(self):
        r = build_reload_context({"title": "t"}, "", {})
        assert r["context_bundle"] is None
        assert r["task_context_error"] == "missing_session_key"

    def test_missing_base_url(self):
        r = build_reload_context({"title": "t"}, "sess-key", {})
        assert r["context_bundle"] is None
        assert r["task_context_error"] == "missing_base_url"

    def test_empty_bundle_fail_open(self):
        from unittest.mock import patch
        with patch("agent_go.diag.get_task_context", return_value={"context_bundle": None}):
            r = build_reload_context(
                {"title": "t"}, "sess-key",
                {"task_context": {"enabled": True, "base_url": "http://127.0.0.1:4000"}})
        assert r["context_bundle"] is None
        assert r["task_context_fetched"] is True
        assert r["task_context_error"] == "empty_bundle"

    def test_bundle_with_pin_anchors(self):
        from unittest.mock import patch
        resp = {
            "context_bundle": {"manifest": [{"id": "m1"}]},
            "pin_anchors": ["orig:abc", "manifest:m1"],
        }
        with patch("agent_go.diag.get_task_context", return_value=resp):
            r = build_reload_context(
                {"title": "t"}, "sess-key",
                {"task_context": {"enabled": True, "base_url": "http://127.0.0.1:4000"}})
        assert r["context_bundle"]["manifest"][0]["id"] == "m1"
        assert r["pin_anchors"] == ["orig:abc", "manifest:m1"]

    def test_exception_fail_open(self):
        from unittest.mock import patch
        with patch("agent_go.diag.get_task_context", side_effect=RuntimeError("boom")):
            r = build_reload_context(
                {"title": "t"}, "sess-key",
                {"task_context": {"enabled": True, "base_url": "http://127.0.0.1:4000"}})
        assert r["context_bundle"] is None
        assert "boom" in r["task_context_error"]
