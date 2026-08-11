"""M2.5 偏差反馈数据层测试（deviation.py）。"""

from agent_go.deviation import (
    DEVIATION_FILENAME,
    DeviationEvent,
    aggregate_deviations,
    classify_deviation,
    load,
    load_all,
    write,
)


class TestClassifyDeviation:
    def test_infrastructure_failure_not_capability_deviation(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={"failure_class": "infrastructure_failure", "verification_results": []},
        )
        assert evt.requires_approval is False
        assert evt.failure_class == "infrastructure_failure"

    def test_rejected_command_infrastructure(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={
                "failure_class": "verification_failure",
                "verification_results": [{"reject_reason": "python -c 含换行", "rejected": True}],
            },
        )
        assert evt.requires_approval is False

    def test_scope_violation_is_architecture_deviation(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={
                "failure_class": None,
                "verification_results": [{
                    "type": "scope_compliance", "passed": False,
                    "out_of_scope": ["src/bad.py"], "missing": ["src/expected.py"],
                }],
                "scope_violation": {"compliant": False,
                                    "out_of_scope": ["src/bad.py"],
                                    "missing": ["src/expected.py"]},
            },
        )
        assert evt.deviation_type == "architecture_deviation"
        assert evt.root_cause_category == "decomposition_error"
        assert evt.requires_approval is True

    def test_semantic_fail_is_acceptance_gap(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={
                "failure_class": "verification_failure",
                "verification_results": [{
                    "type": "semantic", "passed": False,
                    "reason": "未实现边界检查",
                }],
            },
        )
        assert evt.deviation_type == "acceptance_gap"
        assert evt.root_cause_category == "verification_insufficient"
        assert evt.failure_class == "verification_failure"

    def test_failed_cmd_is_implementation_error(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={
                "failure_class": "verification_failure",
                "verification_results": [],
                "failed_cmds": ["pytest -k test_core"],
            },
        )
        assert evt.deviation_type == "acceptance_gap"
        assert evt.root_cause_category == "implementation_error"

    def test_timeout_not_capability(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={"failure_class": "timeout", "verification_results": []},
        )
        assert evt.requires_approval is False

    def test_unknown_no_evidence(self):
        evt = classify_deviation(task_id="t1", subtask_id="s1", result={"failure_class": "verification_failure"})
        assert evt.deviation_type == "acceptance_gap"
        assert evt.root_cause_category == "unknown"
        assert evt.requires_approval is True

    def test_verify_revert_maps_to_no_progress_pattern(self):
        evt = classify_deviation(
            task_id="t1", subtask_id="s1",
            result={
                "failure_class": "verification_failure",
                "kill_reason": "verify_revert",
                "verification_results": [],
                "failed_cmds": ["pytest"],
            },
        )
        assert evt.failure_pattern == "no_progress"
        assert evt.root_cause_category == "implementation_error"


class TestDeviationPersistence:
    def test_write_and_load_roundtrip(self, tmp_path):
        evt = DeviationEvent(
            task_id="t1", subtask_id="s1",
            deviation_type="acceptance_gap",
            root_cause_category="implementation_error",
            summary="实现缺陷",
            failure_class="verification_failure",
            failure_pattern="shell_fail",
        )
        write(tmp_path / DEVIATION_FILENAME, evt)
        events = load(tmp_path)
        assert len(events) == 1
        assert events[0].task_id == "t1"
        assert events[0].deviation_type == "acceptance_gap"
        assert events[0].root_cause_category == "implementation_error"
        assert events[0].failure_pattern == "shell_fail"

    def test_load_missing_file(self, tmp_path):
        assert load(tmp_path) == []

    def test_load_corrupt_line_tolerated(self, tmp_path):
        (tmp_path / DEVIATION_FILENAME).write_text(
            '{"task_id": "t1"}\nnot-json\n{"task_id": "t2", "subtask_id": "s2"}\n',
            encoding="utf-8",
        )
        events = load(tmp_path)
        assert len(events) == 2

    def test_write_creates_parent(self, tmp_path):
        nested = tmp_path / "a" / "b" / DEVIATION_FILENAME
        write(nested, DeviationEvent(task_id="t1", subtask_id="s1", deviation_type="acceptance_gap"))
        assert nested.exists()

    def test_load_all_scans_task_dirs(self, tmp_path):
        d1 = tmp_path / "task-20260812-000001-000-aaaa"
        d2 = tmp_path / "task-20260812-000002-000-bbbb"
        d1.mkdir()
        d2.mkdir()
        write(d1 / DEVIATION_FILENAME, DeviationEvent(task_id="t1", subtask_id="s1", deviation_type="acceptance_gap"))
        write(d2 / DEVIATION_FILENAME, DeviationEvent(task_id="t2", subtask_id="s2", deviation_type="spec_deviation"))
        events = load_all(tmp_path)
        assert len(events) == 2


class TestAggregateDeviations:
    def test_empty(self):
        agg = aggregate_deviations([])
        assert agg["total"] == 0

    def test_aggregation(self, tmp_path):
        evts = [
            DeviationEvent(task_id="t1", subtask_id="s1", deviation_type="acceptance_gap",
                           root_cause_category="implementation_error", failure_class="verification_failure",
                           requires_approval=True, human_decision=""),
            DeviationEvent(task_id="t1", subtask_id="s2", deviation_type="architecture_deviation",
                           root_cause_category="decomposition_error", failure_class="verification_failure",
                           requires_approval=True, human_decision="approve"),
            DeviationEvent(task_id="t1", subtask_id="s3", deviation_type="acceptance_gap",
                           root_cause_category="implementation_error", failure_class="verification_failure",
                           requires_approval=True, human_decision="", spec_rewrite_required=True),
        ]
        agg = aggregate_deviations(evts)
        assert agg["total"] == 3
        assert agg["by_type"]["acceptance_gap"] == 2
        assert agg["by_type"]["architecture_deviation"] == 1
        assert agg["by_root_cause"]["implementation_error"] == 2
        assert agg["require_approval"] == 3
        assert agg["resolved"] == 1
        assert agg["spec_rewrite_pending"] == 1
