"""Problem ↔ GitHub Issue 联动（M5 后续，ADR-015）测试——全部 mock `gh`，零外发。"""
import json
import subprocess

from agent_go.issue_link import (
    ISSUE_LABEL,
    issue_body,
    issue_title,
    needs_sync,
    scrubbed,
    sync_problem,
    sync_problems,
    tracking_enabled,
)
from agent_go.problems import Problem, load, record


def _problem(**kw):
    base = dict(id="p-abc123", failure_pattern="verification_failure:pytest -q",
                failure_class="verification_failure", task_id="task-x", subtask_id="sub-1",
                summary="验证失败 3 次", occurrence_count=1, status="opened")
    base.update(kw)
    return Problem(**base)


class _FakeGh:
    """记录 gh 调用；按动作返回预设结果。"""

    def __init__(self, fail_label=False, fail_all=False):
        self.calls = []
        self.fail_label = fail_label
        self.fail_all = fail_all

    def __call__(self, args, capture_output=True, text=True, timeout=30):
        self.calls.append(list(args))
        sub = " ".join(args[1:3])   # args[0] == "gh"
        if self.fail_all:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if self.fail_label and "--label" in args:
            return subprocess.CompletedProcess(args, 1, "", "could not add label: not found")
        if sub == "issue create":
            return subprocess.CompletedProcess(args, 0, "https://github.com/o/r/issues/42\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def actions(self):
        """去掉 argv[0]=gh 前缀，返回动作序列（如 issue create / issue comment）。"""
        return [" ".join(c[1:3]) for c in self.calls]


def test_tracking_default_off_and_config_switch():
    assert tracking_enabled(None) is False
    assert tracking_enabled({}) is False
    assert tracking_enabled({"issues": {"enabled": False}}) is False
    assert tracking_enabled({"issues": {"enabled": True}}) is True


def test_needs_sync_state_machine():
    p = _problem()
    assert needs_sync(p) == "create"
    p.github_issue = "https://github.com/o/r/issues/42"
    p.issue_synced = {"occurrence_count": 1, "status": "opened"}
    assert needs_sync(p) == ""                      # 已同步
    p.occurrence_count = 3
    assert needs_sync(p) == "comment"               # 复发
    p.status = "resolved"
    assert needs_sync(p) == "close"                 # 修复关闭（优先于复发）
    p.issue_synced = {"occurrence_count": 3, "status": "resolved"}
    assert needs_sync(p) == ""


def test_body_scrubs_local_paths_and_omits_evidence_by_default():
    p = _problem(evidence="leaked /Users/bob/proj/.agent_go/task-1/output.log",
                 summary="失败于 /Users/bob/proj/src/a.py")
    body = issue_body(p, with_evidence=False)
    assert "evidence" not in body and "/Users/" not in body and ".agent_go" not in body
    assert "<home>" in body and p.id in body
    with_ev = issue_body(p, with_evidence=True)
    assert "evidence" in with_ev and "/Users/" not in with_ev
    assert scrubbed("/Users/x/y /home/z") == "<home>/y <home>"
    assert issue_title(p).startswith("[agent_go] verification_failure:")


def test_sync_creates_issue_and_writes_back(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    record(path, failure_pattern="verification_failure:pytest -q", task_id="task-x")
    fake = _FakeGh()
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr("agent_go.cli.load_config", lambda **_k: {"issues": {"enabled": True}})

    summary = sync_problems(path, config={"issues": {"enabled": True}}, task_id="task-x")
    assert summary["created"] == 1 and summary["failed"] == 0
    assert fake.actions() == ["issue create"]
    create_args = fake.calls[0]
    assert "--label" in create_args and ISSUE_LABEL in create_args
    saved = load(path)[0]
    assert saved.github_issue == "https://github.com/o/r/issues/42"
    assert saved.issue_synced["number"] == "42" and saved.issue_synced["status"] == "opened"
    # 幂等：再同步不产生新调用
    assert sync_problems(path, config={"issues": {"enabled": True}}, task_id="task-x")["considered"] == 0
    assert len(fake.calls) == 1


def test_sync_comments_on_recurrence_and_closes_on_resolution(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    p = record(path, failure_pattern="shell_fail:cmd", task_id="task-x")
    assert p is not None
    p.github_issue = "https://github.com/o/r/issues/7"
    p.issue_synced = {"occurrence_count": 1, "status": "opened", "number": "7"}
    p.occurrence_count = 2
    path.write_text(json.dumps(p.__dict__, ensure_ascii=False) + "\n", encoding="utf-8")

    fake = _FakeGh()
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")
    assert sync_problems(path, config={"issues": {"enabled": True}})["commented"] == 1
    assert fake.actions() == ["issue comment"]

    # resolved → close
    rows = load(path)
    rows[0].status = "resolved"
    rows[0].resolution_summary = "换用 shlex 解析后通过"
    path.write_text(json.dumps(rows[0].__dict__, ensure_ascii=False) + "\n", encoding="utf-8")
    assert sync_problems(path, config={"issues": {"enabled": True}})["closed"] == 1
    assert fake.actions() == ["issue comment", "issue close"]


def test_sync_label_missing_falls_back_without_label(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    record(path, failure_pattern="p-x", task_id="t")
    fake = _FakeGh(fail_label=True)
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")
    summary = sync_problems(path, config={"issues": {"enabled": True}})
    assert summary["created"] == 1
    assert len(fake.calls) == 2 and "--label" not in fake.calls[1]


def test_sync_dry_run_and_disabled_make_no_calls(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    record(path, failure_pattern="p-y", task_id="t")
    fake = _FakeGh()
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")

    off = sync_problems(path, config={"issues": {"enabled": False}})
    assert off["enabled"] is False and fake.calls == [] and load(path)[0].github_issue == ""

    dry = sync_problems(path, config={"issues": {"enabled": True}}, dry_run=True)
    assert dry["created"] == 0 and dry["details"][0]["action"] == "create" and fake.calls == []
    assert load(path)[0].github_issue == ""


def test_sync_fail_open_when_gh_missing_or_failing(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    record(path, failure_pattern="p-z", task_id="t")
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: None)
    res = sync_problems(path, config={"issues": {"enabled": True}})
    assert res["failed"] == 1 and "gh CLI" in res["details"][0]["detail"]
    assert load(path)[0].github_issue == ""          # 失败不写标记，下次重试

    fake = _FakeGh(fail_all=True)
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")
    res = sync_problems(path, config={"issues": {"enabled": True}})
    assert res["failed"] == 1 and load(path)[0].github_issue == ""

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("gh", 30)
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", _timeout)
    res = sync_problems(path, config={"issues": {"enabled": True}})
    assert res["failed"] == 1 and "超时" in res["details"][0]["detail"]


def test_sync_filters_by_task_and_limit(tmp_path, monkeypatch):
    path = tmp_path / "problems.jsonl"
    for i in range(3):
        record(path, failure_pattern=f"p-{i}", task_id="task-x" if i < 2 else "task-other")
    fake = _FakeGh()
    monkeypatch.setattr("agent_go.issue_link.subprocess.run", fake)
    monkeypatch.setattr("agent_go.issue_link.shutil.which", lambda _n: "/usr/bin/gh")
    res = sync_problems(path, config={"issues": {"enabled": True}}, task_id="task-x", limit=1)
    assert res["considered"] == 1 and len(fake.calls) == 1


def test_sync_problem_reports_already_synced(tmp_path):
    p = _problem(github_issue="https://github.com/o/r/issues/1",
                 issue_synced={"occurrence_count": 1, "status": "opened"})
    ok, action, detail = sync_problem(p, config={"issues": {"enabled": True}})
    assert (ok, action, detail) == (True, "", "已同步")


def test_cmd_issues_yes_is_the_human_gate(tmp_path):
    """`issues sync --yes` 本身即人闸门（ADR-015）：不要求先改 config；默认 dry-run。"""
    import io
    import contextlib
    from argparse import Namespace
    from unittest.mock import patch

    from agent_go.cli import cmd_issues

    seen = {}

    def fake_sync(path, **kw):
        seen.update(kw)
        return {"enabled": True, "considered": 0, "created": 0, "commented": 0, "closed": 0,
                "failed": 0, "details": []}

    def _args(**kw):
        base = dict(dry_run=False, issues_yes=False, task="", limit=5, repo="",
                    include_evidence=False, json_mode=False)
        base.update(kw)
        return Namespace(**base)

    with patch("agent_go.issue_link.sync_problems", side_effect=fake_sync):
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_issues(_args(issues_yes=True))
    assert seen["dry_run"] is False and seen["config"]["issues"]["enabled"] is True

    seen.clear()
    with patch("agent_go.issue_link.sync_problems", side_effect=fake_sync):
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_issues(_args())                       # 未给 --yes → dry-run
    assert seen["dry_run"] is True
    assert not seen["config"].get("issues", {}).get("enabled", False)

    seen.clear()
    with patch("agent_go.issue_link.sync_problems", side_effect=fake_sync):
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_issues(_args(issues_yes=True, include_evidence=True))
    assert seen["config"]["issues"]["include_evidence"] is True
