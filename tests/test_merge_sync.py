"""merge 工作区同步（_sync_checked_out_worktree）测试。"""
import subprocess
from pathlib import Path

import pytest
from agent_go.cli import _sync_checked_out_worktree


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main", "-q")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("v1")
    _git(r, "add", ".")
    _git(r, "commit", "-m", "init")
    _git(r, "checkout", "-b", "feature", "-q")
    (r / "a.txt").write_text("v2")
    _git(r, "add", ".")
    _git(r, "commit", "-m", "feature work")
    return r


def _simulate_merge_on_main(repo: Path) -> None:
    """模拟 cmd_merge 的 update-ref：main checkout 状态下推进 main 到 feature commit。"""
    _git(repo, "checkout", "main", "-q")
    feat_head = _git(repo, "rev-parse", "feature")
    _git(repo, "update-ref", "refs/heads/main", feat_head)


class _FakeConsole:
    def __init__(self):
        self.lines = []

    def warning(self, msg):
        self.lines.append(msg)

    def success(self, msg):
        self.lines.append(msg)

    def error(self, msg):
        self.lines.append(msg)

    def print(self, *args):
        self.lines.append(" ".join(str(a) for a in args))


class TestSyncWorktree:
    def test_checkout_other_branch_noop(self, repo, monkeypatch):
        """当前 checkout 在非 target 分支 → 不 reset、不警告。"""
        _git(repo, "checkout", "feature", "-q")
        fc = _FakeConsole()
        monkeypatch.setattr("agent_go.cli.console", fc)
        _sync_checked_out_worktree(str(repo), "main", merge_start_clean=True)
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
        assert fc.lines == []

    def test_checkout_target_clean_merge_resets(self, repo, monkeypatch):
        """merge 前工作区干净 + 当前在 target → reset 同步（M/D 失配消除）。"""
        _simulate_merge_on_main(repo)
        assert _git(repo, "status", "--porcelain") != ""  # update-ref 引入的失配
        fc = _FakeConsole()
        monkeypatch.setattr("agent_go.cli.console", fc)
        _sync_checked_out_worktree(str(repo), "main", merge_start_clean=True)
        assert _git(repo, "status", "--porcelain") == ""
        assert (repo / "a.txt").read_text() == "v2"
        assert "同步" in fc.lines[-1]

    def test_checkout_target_dirty_before_merge_warns(self, repo, monkeypatch):
        """merge 前工作区已有改动 → 仅警告不 reset（防丢改动）。"""
        _simulate_merge_on_main(repo)
        (repo / "wip.txt").write_text("wip")  # merge 后新增的未提交文件
        fc = _FakeConsole()
        monkeypatch.setattr("agent_go.cli.console", fc)
        _sync_checked_out_worktree(str(repo), "main", merge_start_clean=False)
        assert "未自动同步" in fc.lines[-1]
        assert (repo / "wip.txt").exists()  # 改动保留
