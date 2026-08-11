"""M1.4 SDD 治理闭环测试：traceability + architecture review + compliance 输出。"""

import logging

from unittest.mock import patch

from agent_go.governance import (
    assess_traceability,
    build_traceability_matrix,
    extract_spec_requirements,
    _extract_json_decision,
    _canonical_id,
)


def _meta(**overrides):
    meta = {
        "status": "EXECUTING",
        "subtasks": [
            {
                "id": "sub-1",
                "title": "实现功能",
                "requirement_ids": ["REQ-001"],
                "acceptance_criteria_ids": ["AC-001"],
                "verification": "pytest tests/",
                "verification_results": [{"passed": True}],
                "commit_hash": "abc123",
            }
        ],
        "plan_quality": {"requirement_ids": ["REQ-001"], "acceptance_criteria_ids": ["AC-001"]},
        "delivery_branch": "agent_go/task-1/delivery",
        "accepted_delivery": True,
    }
    meta.update(overrides)
    return meta


# ---------------------------------------------------------------- extract_spec_requirements
class TestExtractSpecRequirements:
    def test_extracts_named_ids(self):
        r = extract_spec_requirements(
            "REQ-001 增加函数\nREQ-002 处理错误\n验收标准 AC-001 单测通过\nAC-002 边界测试")
        assert r["requirement_ids"] == ["REQ-001", "REQ-002"]
        assert r["acceptance_criteria_ids"] == ["AC-001", "AC-002"]
        assert r["count"] == 4

    def test_normalizes_variant_writings(self):
        r = extract_spec_requirements("req1 功能\nreq2 校验\nac1 测试\nac2 覆盖")
        assert r["requirement_ids"] == ["REQ-001", "REQ-002"]
        assert r["acceptance_criteria_ids"] == ["AC-001", "AC-002"]

    def test_fallback_numbered_requirements(self):
        r = extract_spec_requirements("要求：支持导入。\n1. 要求 校验输入\n2. 必须 处理异常")
        assert r["requirement_ids"], "编号条款应兜底生成 REQ ID"
        assert r["count"] >= 1

    def test_empty_task_returns_empty(self):
        r = extract_spec_requirements("")
        assert r["requirement_ids"] == []
        assert r["acceptance_criteria_ids"] == []
        assert r["count"] == 0


# ---------------------------------------------------------------- _canonical_id
class TestCanonicalId:
    def test_passthrough_canonical(self):
        assert _canonical_id("REQ-001") == "REQ-001"
        assert _canonical_id("AC-100") == "AC-100"

    def test_normalizes_variants(self):
        assert _canonical_id("req1") == "REQ-001"
        assert _canonical_id("ac_2") == "AC-002"
        assert _canonical_id("REQ_3") == "REQ-003"

    def test_non_id_passthrough(self):
        assert _canonical_id("foo") == "foo"


# ---------------------------------------------------------------- assess_traceability
class TestAssessTraceability:
    def test_complete_traceability(self):
        a = assess_traceability(_meta())
        assert a["status"] == "complete"
        assert a["missing_requirement_ids"] == []
        assert a["verification_coverage"] == 1.0
        assert a["delivery_coverage"] is True

    def test_incomplete_missing_requirement(self):
        meta = _meta(subtasks=[
            {"id": "sub-1", "title": "实现", "requirement_ids": ["REQ-999"],
             "verification": "pytest", "verification_results": []},
        ])
        a = assess_traceability(meta)
        assert a["status"] == "incomplete"
        assert "REQ-001" in a["missing_requirement_ids"]

    def test_unmapped_subtask_marked(self):
        meta = _meta(subtasks=[
            {"id": "sub-1", "title": "实现", "requirement_ids": ["REQ-001"],
             "verification": "pytest", "verification_results": [{"passed": True}]},
            {"id": "sub-2", "title": "无映射", "verification": "pytest",
             "verification_results": [{"passed": True}]},
        ])
        a = assess_traceability(meta)
        assert "sub-2" in a["unmapped_subtask_ids"]
        assert a["status"] == "incomplete"

    def test_no_spec_ids_not_failure(self):
        meta = _meta(plan_quality={}, subtasks=[
            {"id": "sub-1", "title": "实现", "verification": "pytest",
             "verification_results": [{"passed": True}]},
        ])
        a = assess_traceability(meta)
        assert a["status"] == "no_spec_ids"

    def test_incomplete_verification(self):
        meta = _meta(subtasks=[
            {"id": "sub-1", "title": "实现", "requirement_ids": ["REQ-001"],
             "verification": "", "verification_results": []},
        ])
        a = assess_traceability(meta)
        assert a["status"] == "incomplete"
        assert any("验证覆盖不完整" in i for i in a["issues"])

    def test_no_delivery_marked_incomplete(self):
        meta = _meta(delivery_branch="", pr_url="", explicit_merge_commit="",
                     accepted_delivery=False)
        a = assess_traceability(meta)
        assert a["delivery_coverage"] is False
        assert any("无交付记录" in i for i in a["issues"])


# ---------------------------------------------------------------- build_traceability_matrix
class TestBuildTraceabilityMatrix:
    def test_matrix_structure(self):
        report = build_traceability_matrix(_meta())
        tr = report["traceability"]
        assert len(tr["requirements"]) >= 2  # REQ-001 + AC-001
        assert tr["subtasks"][0]["id"] == "sub-1"
        assert tr["subtasks"][0]["verification_passed"] is True
        assert tr["delivery"]["delivery_branch"] == "agent_go/task-1/delivery"
        assert tr["delivery"]["accepted_delivery"] is True

    def test_arch_not_reviewed_when_missing(self):
        report = build_traceability_matrix(_meta())
        arch = report["architecture_compliance"]
        assert arch["reviewed"] is False
        assert arch["decision"] == "not_reviewed"

    def test_arch_review_surface(self):
        meta = _meta(architecture_review={
            "decision": "approved",
            "summary": "边界清晰",
            "constraints": ["不修改 storage.py"],
            "risks": ["并发写风险"],
        })
        arch = build_traceability_matrix(meta)["architecture_compliance"]
        assert arch["reviewed"] is True
        assert arch["decision"] == "approved"
        assert arch["constraints"] == ["不修改 storage.py"]

    def test_assessment_embedded(self):
        report = build_traceability_matrix(_meta())
        assert "assessment" in report
        assert report["assessment"]["status"] in ("complete", "incomplete", "no_spec_ids")


# ---------------------------------------------------------------- architecture_review
class TestArchitectureReview:
    def _subtasks(self):
        return [{
            "id": "sub-1", "title": "实现", "files": ["a.py"],
            "scope_boundary": "只改 a.py", "do_not_touch": ["b.py"],
            "requirement_ids": ["REQ-001"], "depends_on": [],
        }]

    def test_disabled_returns_none(self):
        from agent_go.governance import architecture_review
        result = architecture_review(
            "task", self._subtasks(),
            {"architecture_review": {"enabled": False}}, logging.getLogger("t"))
        assert result is None

    def test_api_failure_fails_open(self):
        from agent_go.governance import architecture_review
        with patch("agent_go.api.call_api", side_effect=RuntimeError("api down")):
            result = architecture_review(
                "task", self._subtasks(),
                {"architecture_review": {"enabled": True}, "plan_api": {}},
                logging.getLogger("t"))
        assert result is None

    def test_parsed_decision_returned(self):
        from agent_go.governance import architecture_review
        fake = ('{"decision": "approved", "summary": "边界清晰", "boundaries": ["a.py"], '
                '"dependency_direction": [], "constraints": ["不改 b.py"], "risks": []}')
        with patch("agent_go.api.call_api", return_value=fake):
            result = architecture_review(
                "task", self._subtasks(),
                {"architecture_review": {"enabled": True}, "plan_api": {}},
                logging.getLogger("t"))
        assert result["decision"] == "approved"
        assert result["constraints"] == ["不改 b.py"]
        assert result["_source"] == "llm"

    def test_unparseable_output_fails_open(self):
        from agent_go.governance import architecture_review
        with patch("agent_go.api.call_api", return_value="no json"):
            result = architecture_review(
                "task", self._subtasks(),
                {"architecture_review": {"enabled": True}, "plan_api": {}},
                logging.getLogger("t"))
        assert result is None


# ---------------------------------------------------------------- _extract_json_decision
class TestExtractJsonDecision:
    def test_parses_clean_json(self):
        d = _extract_json_decision(
            '{"decision": "approved", "summary": "ok", "boundaries": ["a"], '
            '"dependency_direction": [], "constraints": [], "risks": []}')
        assert d["decision"] == "approved"
        assert d["summary"] == "ok"

    def test_tolerates_fence_and_prefix(self):
        raw = ('先分析：\n```json\n{"decision": "changes_requested", "summary": "需补充", '
               '"boundaries": [], "dependency_direction": [], "constraints": [], "risks": []}\n```')
        d = _extract_json_decision(raw)
        assert d["decision"] == "changes_requested"

    def test_rejects_invalid_decision(self):
        assert _extract_json_decision(
            '{"decision": "maybe", "summary": "x", "boundaries": [], '
            '"dependency_direction": [], "constraints": [], "risks": []}') is None

    def test_rejects_non_json(self):
        assert _extract_json_decision("no json here") is None
        assert _extract_json_decision("") is None
