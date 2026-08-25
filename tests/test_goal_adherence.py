"""M4 goal 回溯：compute_goal_adherence 测试。

核心场景：「执行全过但漏了验收标准」的任务被标记为合规度不足
（needs_human_review=True），而不是静默 completed。合规度与 status 正交。
"""

from agent_go.planning import compute_goal_adherence, refresh_goal_adherence


def _meta(goal_contract=None, results=None, status="DELIVERY_READY", **overrides):
    meta = {
        "status": status,
        "goal_contract": goal_contract if goal_contract is not None else {
            "goal_description": "实现功能 X",
            "acceptance_criteria_ids": [],
            "completion_evidence": ["python -m pytest -q"],
            "constraints": [],
            "missing_verification_subtasks": [],
            "delivery_required": True,
        },
        "results": results if results is not None else [
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "python -m pytest -q", "exit_code": 0, "attempt": 1},
             ]},
        ],
        "accepted_delivery": True,
        "traceability": {"missing_requirement_ids": []},
    }
    meta.update(overrides)
    return meta


class TestNoContract:
    def test_unknown_without_goal_contract(self):
        result = compute_goal_adherence({"status": "completed", "results": []})
        assert result["level"] == "unknown"
        assert result["score"] is None
        assert result["needs_human_review"] is False
        assert result["gaps"] == []


class TestFullAdherence:
    def test_full_when_all_evidence_passed_and_delivered(self):
        result = compute_goal_adherence(_meta())
        assert result["level"] == "full"
        assert result["score"] == 1.0
        assert result["gaps"] == []
        assert result["needs_human_review"] is False

    def test_retry_eventually_passes_counts_as_covered(self):
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "python -m pytest -q", "exit_code": 1, "attempt": 1},
                 {"command": "python -m pytest -q", "exit_code": 0, "attempt": 2},
             ]},
        ])
        result = compute_goal_adherence(meta)
        assert result["level"] == "full"
        assert result["detail"]["evidence"]["passed"] == 1

    def test_whitespace_and_cd_prefix_tolerant_matching(self):
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "cd /repo && python -m pytest -q", "exit_code": 0, "attempt": 1},
             ]},
        ])
        result = compute_goal_adherence(meta)
        assert result["level"] == "full"


class TestSilentAcceptanceMiss:
    """M4 核心：执行全过但漏验收 → 合规度不足 + needs_human_review。"""

    def test_evidence_not_executed_marks_insufficient(self):
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "python -m pytest tests/test_other.py", "exit_code": 0, "attempt": 1},
             ]},
        ])
        result = compute_goal_adherence(meta)
        # 证据未执行 → 合规度不足（delivery 达成使 score=0.5 → partial，仍非 full）
        assert result["level"] == "partial"
        assert result["score"] < 1.0
        assert result["needs_human_review"] is True
        assert any(g["type"] == "evidence_not_executed" for g in result["gaps"])
        assert result["detail"]["evidence"]["not_executed"] == ["python -m pytest -q"]

    def test_rejected_evidence_counts_as_unverified(self):
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "python -m pytest -q", "exit_code": -1,
                  "rejected": True, "reject_reason": "不在白名单", "attempt": 1},
             ]},
        ])
        result = compute_goal_adherence(meta)
        assert any(g["type"] == "evidence_rejected" for g in result["gaps"])
        assert result["needs_human_review"] is True

    def test_evidence_failed_finally(self):
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": [
                 {"command": "python -m pytest -q", "exit_code": 1, "attempt": 1},
             ]},
        ])
        result = compute_goal_adherence(meta)
        assert any(g["type"] == "evidence_failed" for g in result["gaps"])

    def test_silent_pass_subtask_without_verification(self):
        meta = _meta(
            goal_contract={
                "goal_description": "两子任务",
                "acceptance_criteria_ids": [],
                "completion_evidence": ["python -m pytest -q"],
                "constraints": [],
                "missing_verification_subtasks": ["2"],
                "delivery_required": True,
            },
            results=[
                {"subtask_id": "1", "status": "completed", "verify_ok": True,
                 "verification_results": [
                     {"command": "python -m pytest -q", "exit_code": 0, "attempt": 1},
                 ]},
                {"subtask_id": "2", "status": "completed", "verify_ok": True,
                 "verification_results": []},
            ],
        )
        result = compute_goal_adherence(meta)
        assert any(g["type"] == "silent_pass_without_verification" for g in result["gaps"])
        assert result["detail"]["silent_pass_subtasks"] == ["2"]
        assert result["needs_human_review"] is True

    def test_uncovered_acceptance_criterion(self):
        meta = _meta(
            goal_contract={
                "goal_description": "spec 任务",
                "acceptance_criteria_ids": ["AC-001", "AC-002"],
                "completion_evidence": ["python -m pytest -q"],
                "constraints": [],
                "missing_verification_subtasks": [],
                "delivery_required": True,
            },
            traceability={"missing_requirement_ids": ["AC-002"]},
        )
        result = compute_goal_adherence(meta)
        assert any(g["type"] == "acceptance_criterion_uncovered" for g in result["gaps"])
        assert result["detail"]["acceptance_criteria"]["uncovered"] == ["AC-002"]
        assert result["level"] == "partial"

    def test_delivery_unmet_when_goal_requires_delivery(self):
        meta = _meta(accepted_delivery=False)
        result = compute_goal_adherence(meta)
        assert any(g["type"] == "delivery_unmet" for g in result["gaps"])
        assert result["needs_human_review"] is True


class TestOrthogonality:
    """合规度与 status 正交：失败任务不需要人工补验收标记。"""

    def test_failed_task_not_flagged_for_human_review(self):
        meta = _meta(
            status="VERIFICATION_FAILED",
            accepted_delivery=False,
            results=[
                {"subtask_id": "1", "status": "failed", "verify_ok": False,
                 "verification_results": []},
            ],
        )
        result = compute_goal_adherence(meta)
        assert result["gaps"]  # 缺口仍记录（可审计）
        assert result["needs_human_review"] is False

    def test_paused_task_not_flagged(self):
        meta = _meta(status="PAUSED", accepted_delivery=False)
        result = compute_goal_adherence(meta)
        assert result["needs_human_review"] is False

    def test_delivery_not_required_no_delivery_gap(self):
        meta = _meta(
            accepted_delivery=False,
            goal_contract={
                "goal_description": "本地任务",
                "acceptance_criteria_ids": [],
                "completion_evidence": ["python -m pytest -q"],
                "constraints": [],
                "missing_verification_subtasks": [],
                "delivery_required": False,
            },
        )
        result = compute_goal_adherence(meta)
        assert not any(g["type"] == "delivery_unmet" for g in result["gaps"])
        assert result["level"] == "full"


class TestRefreshGoalAdherence:
    """ISSUE-52：交付状态变更后重算，消除 delivery_unmet 时序假阳性。"""

    def test_refresh_clears_stale_delivery_unmet_after_delivery(self):
        # pipeline 结束时：交付未达成，delivery_unmet 缺口落盘
        meta = _meta(accepted_delivery=False)
        stale = compute_goal_adherence(meta)
        assert any(g["type"] == "delivery_unmet" for g in stale["gaps"])
        assert stale["needs_human_review"] is True
        meta["goal_adherence"] = stale

        # 交付成功（merge / pr / bench --with-delivery 路径）后重算
        meta["accepted_delivery"] = True
        meta["status"] = "ACCEPTED_DELIVERY"
        refresh_goal_adherence(meta)

        ga = meta["goal_adherence"]
        assert ga["level"] == "full"
        assert ga["gaps"] == []
        assert ga["needs_human_review"] is False

    def test_refresh_keeps_real_gaps(self):
        # 交付达成但验收证据未执行：真缺口不被重算抹掉
        meta = _meta(results=[
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "verification_results": []},
        ])
        refresh_goal_adherence(meta)
        ga = meta["goal_adherence"]
        assert any(g["type"] == "evidence_not_executed" for g in ga["gaps"])
        assert ga["needs_human_review"] is True

    def test_refresh_is_fail_open(self, monkeypatch):
        def _boom(_meta):
            raise RuntimeError("boom")

        monkeypatch.setattr("agent_go.planning.compute_goal_adherence", _boom)
        meta = _meta()
        refresh_goal_adherence(meta)  # 不抛异常（观测层不阻断交付）
        assert "goal_adherence" not in meta
