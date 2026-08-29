"""pytest 单测：agent_go.attribution_watch（P2 opt-in 盲区归因监视）"""

import json
import subprocess
from pathlib import Path

import pytest

import agent_go.attribution_watch as aw


@pytest.fixture
def watch_env(tmp_path: Path, monkeypatch):
    """隔离环境：AGENT_GO_DIR + WATCH_INDEX_PATH/HOOK_SCRIPT_PATH 重定向。"""
    ag_dir = tmp_path / "agent_go"
    ag_dir.mkdir()
    (ag_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(aw, "AGENT_GO_DIR", ag_dir)
    monkeypatch.setattr(aw, "WATCH_INDEX_PATH", ag_dir / "attribution_watch.json")
    monkeypatch.setattr(aw, "HOOK_SCRIPT_PATH", ag_dir / "hooks" / "agent_go_attribution_stop.py")
    return ag_dir


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _mk_delivered_task(ag_dir: Path, repo: Path, task_id: str,
                       files_summary: str = "a.py | 2 ++\n 1 file changed") -> Path:
    td = ag_dir / task_id
    td.mkdir(parents=True)
    (td / "meta.json").write_text(json.dumps({
        "task_id": task_id, "task": "t", "status": "DELIVERY_READY",
        "repo": str(repo), "created": "2026-08-29T10:00:00",
        "subtasks": [], "results": [
            {"subtask_id": "sub-1", "status": "completed", "summary": files_summary}],
        "blind_spots": {"weakly_anchored_subtasks": ["sub-1"]},
    }), encoding="utf-8")
    return td


class TestInstallUninstall:
    def test_not_a_git_repo_rejected(self, watch_env, tmp_path):
        ok, msg = aw.install_hook(tmp_path / "no-git")
        assert ok is False
        assert "不是 git 仓库" in msg

    def test_install_merges_existing_hooks(self, watch_env, git_repo):
        settings = git_repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "model": "sonnet",
            "hooks": {"Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "echo user-hook"}]}]},
        }), encoding="utf-8")
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000001-001-aaaa")
        ok, msg = aw.install_hook(git_repo)
        assert ok is True
        assert "1 个交付任务" in msg
        d = json.loads(settings.read_text(encoding="utf-8"))
        assert d["model"] == "sonnet"
        stops = d["hooks"]["Stop"]
        assert len(stops) == 2
        assert "echo user-hook" in stops[0]["hooks"][0]["command"]
        assert aw.HOOK_MARK in stops[1]["hooks"][0]["command"]

    def test_install_idempotent(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000002-002-bbbb")
        aw.install_hook(git_repo)
        aw.install_hook(git_repo)
        d = json.loads((git_repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(d["hooks"]["Stop"]) == 1

    def test_uninstall_removes_only_ours(self, watch_env, git_repo):
        settings = git_repo / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "hooks": {"Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "echo user-hook"}]}]},
        }), encoding="utf-8")
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000003-003-cccc")
        aw.install_hook(git_repo)
        ok, msg = aw.uninstall_hook(git_repo)
        assert ok is True
        d = json.loads(settings.read_text(encoding="utf-8"))
        stops = d.get("hooks", {}).get("Stop", [])
        assert len(stops) == 1
        assert "echo user-hook" in stops[0]["hooks"][0]["command"]

    def test_uninstall_without_install(self, watch_env, git_repo):
        ok, msg = aw.uninstall_hook(git_repo)
        assert ok is False
        assert "未开启过监视" in msg


class TestStopHookReport:
    def test_hit_outputs_prefilled_command(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000004-004-dddd")
        aw.install_hook(git_repo)
        (git_repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        report = aw.stop_hook_report(git_repo)
        assert "task-20260829-000004-004-dddd" in report
        assert "agent_go trust --annotate" in report
        assert "weakly_anchored_subtasks:sub-1" in report

    def test_miss_returns_empty(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000005-005-eeee")
        aw.install_hook(git_repo)
        (git_repo / "other.py").write_text("y = 1\n", encoding="utf-8")
        assert aw.stop_hook_report(git_repo) == ""

    def test_clean_tree_returns_empty(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000006-006-ffff")
        aw.install_hook(git_repo)
        assert aw.stop_hook_report(git_repo) == ""

    def test_not_watching_returns_empty(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000007-007-a1a1")
        aw.install_hook(git_repo)
        aw.uninstall_hook(git_repo)
        (git_repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert aw.stop_hook_report(git_repo) == ""

    def test_hook_script_executes_silently_when_no_hit(self, watch_env, git_repo):
        _mk_delivered_task(watch_env, git_repo, "task-20260829-000008-008-b2b2")
        ok, _ = aw.install_hook(git_repo)
        assert ok is True
        (git_repo / "other.py").write_text("z = 1\n", encoding="utf-8")
        rv = subprocess.run(
            ["python3", str(watch_env / "hooks" / "agent_go_attribution_stop.py"),
             "--repo", str(git_repo)],
            input="{}", capture_output=True, text=True, timeout=30,
            cwd=str(git_repo))
        assert rv.returncode == 0
        assert rv.stdout == ""
