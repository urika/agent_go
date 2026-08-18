"""决策辅助 M6.1：evidence.py 证据物化 + insight 解析/校验测试。"""
import json
from pathlib import Path

import pytest

from agent_go.evidence import (
    materialize_evidence, evidence_to_prompt_context, EvidenceError,
)


def _mk_batch(tmp_path: Path, source_batch: str = "b1") -> Path:
    bd = tmp_path / source_batch
    bd.mkdir(parents=True)
    results = [
        {"task_id": "t1", "binary_pass": True, "model": "m1", "failure_class": "",
         "elapsed_sec": 100, "total_cost_usd": 0.1},
        {"task_id": "t2", "binary_pass": False, "model": "m1", "failure_class": "verification_failure",
         "failure_reason": "测试失败", "elapsed_sec": 200, "total_cost_usd": 0.2},
        {"task_id": "t3", "binary_pass": False, "model": "m1", "failure_class": "timeout",
         "failure_reason": "超时", "elapsed_sec": 300, "total_cost_usd": 0.3},
    ]
    (bd / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in results), encoding="utf-8")
    (bd / "summary.json").write_text(json.dumps({
        "task_count": 3, "metrics": {"pass_rate_diagnostic": 0.333,
                                     "dollar_per_pass_diagnostic_usd": 0.6,
                                     "valid_cost_usd": 0.6},
    }), encoding="utf-8")
    (bd / "manifest.json").write_text(json.dumps({
        "bench_schema_version": 1, "source_batch": source_batch,
        "suite": "canonical", "task_ids": ["t1", "t2", "t3"], "repeat_values": [1],
        "immutable": True,
    }), encoding="utf-8")
    return bd


class TestMaterializeEvidence:
    def test_basic(self, tmp_path, monkeypatch):
        bd = _mk_batch(tmp_path)
        monkeypatch.setattr("agent_go.config.load_config", lambda: {"router": {"enabled": False}})
        monkeypatch.setattr("agent_go.problems.load", lambda p: [])
        ev = materialize_evidence(bd)
        assert ev["source_batch"] == "b1"
        assert ev["record_count"] == 3
        assert ev["metrics"]["pass_rate_diagnostic"] == 0.333
        # 失败模式聚合
        cls = ev["failure_modes"]["by_failure_class"]
        assert cls == {"verification_failure": 1, "timeout": 1}
        assert len(ev["failure_modes"]["by_task"]) == 2
        assert ev["evidence_hash"]

    def test_missing_manifest(self, tmp_path):
        bd = tmp_path / "empty"
        bd.mkdir()
        with pytest.raises(EvidenceError, match="manifest"):
            materialize_evidence(bd)

    def test_missing_results(self, tmp_path):
        bd = tmp_path / "b"
        bd.mkdir()
        (bd / "manifest.json").write_text('{"bench_schema_version":1,"source_batch":"b"}', encoding="utf-8")
        with pytest.raises(EvidenceError, match="results.jsonl"):
            materialize_evidence(bd)

    def test_prompt_context_truncation(self):
        ev = {"source_batch": "b", "suite": "canonical", "record_count": 2,
              "metrics": {"pass_rate_diagnostic": 1.0, "accepted_delivery_rate": 1.0,
                          "dollar_per_pass_usd": 0.1},
              "failure_modes": {"by_failure_class": {}, "by_task": {},
                                "by_model": {}, "failed_records": []},
              "per_task": [],
              "environment": {"plan_model": "m1", "goal_policy": "force"},
              "problems_history": []}
        out = evidence_to_prompt_context(ev, max_chars=50)
        assert len(out) <= 50
        assert "截断" in out


class TestInsightParsing:
    """cmd_insight 的 LLM 输出解析与校验（不依赖真实 LLM）。"""

    def test_parse_suggestions_valid(self):
        from agent_go.eval import _parse_insight_suggestions
        content = json.dumps({"suggestions": [
            {"problem": "p1", "cause_hypothesis": "c1", "action": "a1",
             "expected_impact": "e1", "cost_risk": "r1", "confidence": 0.8,
             "requires_approval": True, "evidence_refs": ["metrics/pass_rate"]},
        ]})
        out = _parse_insight_suggestions(content)
        assert len(out) == 1
        assert out[0]["problem"] == "p1"

    def test_parse_suggestions_markdown_wrapped(self):
        from agent_go.eval import _parse_insight_suggestions
        content = '```json\n{"suggestions": [{"problem": "p", "cause_hypothesis": "c", "action": "a", "expected_impact": "e", "confidence": 0.5, "evidence_refs": []}]}\n```'
        out = _parse_insight_suggestions(content)
        assert len(out) == 1

    def test_parse_suggestions_invalid(self):
        from agent_go.eval import _parse_insight_suggestions
        assert _parse_insight_suggestions("not json") == []
        assert _parse_insight_suggestions('{"suggestions": "not-list"}') == [{"suggestions": "not-list"}]

    def test_validate_evidence_refs(self):
        from agent_go.eval import _validate_suggestion_evidence
        evidence = {"metrics": {"pass_rate_diagnostic": 1.0},
                    "failure_modes": {"by_failure_class": {"verification_failure": 1}}}
        good = {"problem": "p", "cause_hypothesis": "c", "action": "a", "expected_impact": "e",
                "confidence": 0.8, "requires_approval": True,
                "evidence_refs": ["metrics/pass_rate_diagnostic"]}
        missing = _validate_suggestion_evidence(good, evidence)
        assert missing == []
        # 无效引用 → 返回缺失列表
        bad = dict(good, evidence_refs=["metrics/nonexistent_xyz"])
        missing2 = _validate_suggestion_evidence(bad, evidence)
        assert missing2 != []

    def test_validate_missing_refs_listed(self):
        from agent_go.eval import _validate_suggestion_evidence
        evidence = {"metrics": {}, "environment": {},
                    "failure_modes": {"by_failure_class": {}}, "per_task": []}
        bad = {"problem": "p", "cause_hypothesis": "c", "action": "a", "expected_impact": "e",
               "confidence": 0.5, "evidence_refs": ["metrics/xyz", "batch"]}
        missing = _validate_suggestion_evidence(bad, evidence)
        # batch 直接通过；metrics/xyz 缺失被列出
        assert missing == ["metrics/xyz"]
