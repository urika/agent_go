"""测试 problems.py — Problem 实体（B4）+ 知识生命周期（H3 谦逊层）。"""

from datetime import datetime, timedelta

from agent_go.problems import (
    Problem,
    aggregate,
    load,
    make_problem_id,
    mark_analyzed,
    mark_resolved,
    record,
)


def _p(tmp_path):
    return tmp_path / "problems.jsonl"


class TestRecord:
    def test_new_pattern_creates_opened(self, tmp_path):
        p = record(_p(tmp_path), failure_pattern="shell_fail",
                   failure_class="verification_failure", task_id="task-1",
                   subtask_id="sub-1")
        assert p is not None
        assert p.status == "opened"
        assert p.occurrence_count == 1
        assert p.id == make_problem_id("shell_fail")

    def test_recurrence_increments(self, tmp_path):
        path = _p(tmp_path)
        record(path, failure_pattern="shell_fail", task_id="task-1")
        p2 = record(path, failure_pattern="shell_fail", task_id="task-2")
        assert p2.occurrence_count == 2
        assert p2.status == "opened"
        problems = load(path)
        assert len(problems) == 1  # 去重，不新建

    def test_resolved_reopens_on_recurrence(self, tmp_path):
        """B4 复发重开：resolved 复发 → opened，保留葬礼记录为历史证据。"""
        path = _p(tmp_path)
        record(path, failure_pattern="shell_fail", task_id="task-1")
        pid = make_problem_id("shell_fail")
        mark_resolved(path, pid, resolved_by="commit-x",
                      resolution_summary="worktree 未继承 venv；TASK.md 注明 source venv 修复")
        p = record(path, failure_pattern="shell_fail", task_id="task-2")
        assert p.status == "opened"  # 复发重开
        assert p.occurrence_count == 2  # 1(初现) + 1(复发)
        assert p.resolution_summary.startswith("worktree")  # 历史证据保留

    def test_empty_pattern_returns_none(self, tmp_path):
        assert record(_p(tmp_path), failure_pattern="") is None


class TestLifecycle:
    def test_mark_analyzed(self, tmp_path):
        path = _p(tmp_path)
        record(path, failure_pattern="diverge", task_id="task-1")
        p = mark_analyzed(path, make_problem_id("diverge"),
                          root_cause="两个缺陷交替出现", root_cause_category="verification_insufficient")
        assert p is not None and p.status == "analyzed"
        assert p.root_cause == "两个缺陷交替出现"

    def test_mark_resolved_funeral(self, tmp_path):
        """H3 葬礼：resolved 必须记录 resolution_summary（KnowledgeStore 输入）。"""
        path = _p(tmp_path)
        record(path, failure_pattern="shell_fail", task_id="task-1")
        p = mark_resolved(path, make_problem_id("shell_fail"),
                          resolved_by="task-2/commit-abc",
                          resolution_summary="为何曾重要：未继承 venv 导致 3 次空转；"
                                             "如何修：TASK.md 注入 source venv；可复用：所有 python 任务")
        assert p.status == "resolved"
        assert p.resolved_by == "task-2/commit-abc"
        assert "未继承 venv" in p.resolution_summary

    def test_mark_missing_id_returns_none(self, tmp_path):
        assert mark_resolved(_p(tmp_path), "p-nonexistent",
                             resolved_by="x", resolution_summary="y") is None
        assert mark_analyzed(_p(tmp_path), "p-nonexistent", root_cause="r") is None


class TestHalfLife:
    def test_is_dormant_after_stale(self, tmp_path):
        now = datetime(2026, 8, 14, 12, 0, 0)
        p = Problem(id="p-1", failure_pattern="x",
                    first_seen_at="2026-01-01T00:00:00", last_seen_at="2026-01-01T00:00:00")
        assert p.is_dormant(now) is True

    def test_not_dormant_when_recent(self):
        p = Problem(id="p-1", failure_pattern="x",
                    last_seen_at=(datetime.now() - timedelta(days=1)).isoformat())
        assert p.is_dormant() is False

    def test_resolved_never_dormant(self):
        p = Problem(id="p-1", failure_pattern="x", status="resolved",
                    first_seen_at="2026-01-01T00:00:00", last_seen_at="2026-01-01T00:00:00")
        assert p.is_dormant(datetime(2026, 8, 14)) is False

    def test_custom_stale_after_days(self):
        p = Problem(id="p-1", failure_pattern="x", stale_after_days=7,
                    first_seen_at="2026-08-01T00:00:00", last_seen_at="2026-08-01T00:00:00")
        assert p.is_dormant(datetime(2026, 8, 14, 12)) is True


class TestAggregate:
    def test_aggregate_basic(self, tmp_path):
        path = _p(tmp_path)
        record(path, failure_pattern="shell_fail", task_id="t1")
        record(path, failure_pattern="shell_fail", task_id="t2")
        record(path, failure_pattern="diverge", task_id="t3")
        mark_resolved(path, make_problem_id("diverge"), resolved_by="c1", resolution_summary="s")
        a = aggregate(load(path))
        assert a["total"] == 2
        assert a["status_counts"] == {"opened": 1, "analyzed": 0, "resolved": 1}
        assert a["recurrence_count"] == 1  # shell_fail 复发过
        assert a["total_occurrences"] == 3
        assert a["top_patterns"][0][0] == "shell_fail"

    def test_aggregate_empty(self):
        a = aggregate([])
        assert a["total"] == 0
        assert a["top_patterns"] == []


class TestPersistence:
    def test_load_skips_bad_lines(self, tmp_path):
        path = _p(tmp_path)
        path.write_text('{"broken": "json"\n' + 'not json at all\n', encoding="utf-8")
        assert load(path) == []

    def test_load_roundtrip(self, tmp_path):
        path = _p(tmp_path)
        record(path, failure_pattern="shell_fail", evidence="pytest: command not found",
               task_id="task-9", subtask_id="sub-2")
        problems = load(path)
        assert len(problems) == 1
        assert problems[0].failure_pattern == "shell_fail"
        assert problems[0].evidence == "pytest: command not found"
        assert problems[0].id == make_problem_id("shell_fail")

    def test_make_problem_id_stable_and_unique(self):
        assert make_problem_id("shell_fail") == make_problem_id("shell_fail")
        assert make_problem_id("shell_fail") != make_problem_id("diverge")
        assert len(make_problem_id("x")) == len("p-") + 12
