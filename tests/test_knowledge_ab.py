"""pytest 单测：agent_go.knowledge_ab A/B 判定分析"""

import json

import pytest

from agent_go.knowledge_ab import (
    _has_eliminable_knowledge,
    _summarize,
    analyze_ab,
    load_results,
)


def _mk(accepted_delivery: bool, total_cost: float, pass_rate: float = 0.7) -> dict:
    return {
        "task_id": "t",
        "accepted_delivery": accepted_delivery,
        "total_cost_usd": total_cost,
        "pass_rate": pass_rate,
    }


def test_load_results_skips_bad_lines(tmp_path):
    f = tmp_path / "r.jsonl"
    f.write_text(
        json.dumps({"accepted_delivery": True, "total_cost_usd": 1.0})
        + "\n{not json\n"
        + json.dumps({"accepted_delivery": False, "total_cost_usd": 2.0})
        + "\n"
    )
    assert len(load_results(f)) == 2


def test_summarize_basic():
    s = _summarize([
        _mk(True, 1.0),
        _mk(False, 2.0),
        _mk(True, 1.0),
    ])
    assert s["n"] == 3
    assert s["adr"] == pytest.approx(2 / 3)
    assert s["pass_rate"] == pytest.approx(0.7)
    assert s["avg_cost_usd"] == pytest.approx(4 / 3)
    assert s["dollar_per_ad"] == pytest.approx(2.0)


def test_summarize_empty():
    assert _summarize([]) == {"n": 0}


def test_summarize_zero_accepted_dollar_per_ad_inf():
    s = _summarize([_mk(False, 5.0)])
    assert s["dollar_per_ad"] == float("inf")


def test_analyze_ab_adr_up_pass():
    ctl = [_mk(True, 1.0), _mk(False, 1.0)]
    inj = [_mk(True, 1.0), _mk(True, 1.0)]
    r = analyze_ab(ctl, inj)
    assert r["verdicts"]["adr_up"] is True
    assert r["verdicts"]["cost_not_worse"] is True
    assert r["conclusion"] == "PRODUCTIZE"


def test_analyze_ab_cost_over_tolerance_rollback():
    ctl = [_mk(True, 1.0), _mk(False, 1.0)]
    inj = [_mk(True, 2.5), _mk(True, 2.5)]
    r = analyze_ab(ctl, inj)
    assert r["verdicts"]["adr_up"] is True
    assert r["verdicts"]["cost_not_worse"] is False
    assert r["conclusion"] == "ROLLBACK"


def test_analyze_ab_adr_not_up_rollback():
    ctl = [_mk(True, 1.0), _mk(True, 1.0)]
    inj = [_mk(True, 1.0), _mk(False, 1.0)]
    r = analyze_ab(ctl, inj)
    assert r["verdicts"]["adr_up"] is False
    assert r["conclusion"] == "ROLLBACK"


def test_analyze_ab_empty_arms():
    r = analyze_ab([], [])
    assert r["verdicts"]["adr_up"] is False
    assert r["conclusion"] == "ROLLBACK"


def test_analyze_ab_cost_tolerance_respected():
    ctl = [_mk(True, 1.0), _mk(False, 1.0)]
    inj = [_mk(True, 2.12), _mk(True, 2.12)]
    assert analyze_ab(ctl, inj, cost_tolerance=0.10)["conclusion"] == "PRODUCTIZE"
    assert analyze_ab(ctl, inj, cost_tolerance=0.05)["conclusion"] == "ROLLBACK"


def test_has_eliminable_knowledge_dormant(tmp_path):
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    old_seen = (now - _dt.timedelta(days=40)).isoformat()
    p = tmp_path / "problems.jsonl"
    p.write_text(json.dumps({
        "id": "problem-1",
        "status": "opened",
        "last_seen_at": old_seen,
        "stale_after_days": 30,
    }) + "\n")
    ok, detail = _has_eliminable_knowledge(p)
    assert ok is True
    assert "1 条可淘汰" in detail


def test_has_eliminable_knowledge_suppressed(tmp_path):
    p = tmp_path / "problems.jsonl"
    p.write_text(json.dumps({"id": "problem-2", "suppressed_ids": ["problem-x"]}) + "\n")
    ok, _ = _has_eliminable_knowledge(p)
    assert ok is True


def test_has_eliminable_knowledge_none(tmp_path):
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    recent_seen = (now - _dt.timedelta(days=2)).isoformat()
    p = tmp_path / "problems.jsonl"
    p.write_text(json.dumps({
        "id": "problem-3",
        "status": "opened",
        "last_seen_at": recent_seen,
        "stale_after_days": 30,
    }) + "\n")
    ok, _ = _has_eliminable_knowledge(p)
    assert ok is False


def test_has_eliminable_knowledge_missing_file():
    ok, detail = _has_eliminable_knowledge("/nonexistent/problems.jsonl")
    assert ok is False
    assert "不存在" in detail


def test_has_eliminable_knowledge_none_path():
    ok, _ = _has_eliminable_knowledge(None)
    assert ok is False
