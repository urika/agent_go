"""测试 A3 未提交基线处理 — get_dirty_files / commit_baseline

worktree 从 HEAD 创建，看不到主工作区未提交改动；A3 在 run 启动时检测 dirty 并提供
commit 基线 / 显式允许 / 中止三种处理。这里测 git_utils 层的检测与基线 commit。
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.git_utils import get_dirty_files, commit_baseline


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _init_repo_with_commit(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    (repo / "a.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-m", "init").returncode == 0


class TestGetDirtyFiles:
    def test_not_a_git_repo_returns_empty(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert get_dirty_files(plain) == []

    def test_clean_repo_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        assert get_dirty_files(repo) == []

    def test_modified_and_untracked_detected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        (repo / "a.py").write_text("print('changed')\n", encoding="utf-8")  # 已跟踪修改
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")            # 未跟踪
        files = get_dirty_files(repo)
        assert "a.py" in files
        assert "new.py" in files

    def test_staged_file_detected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        (repo / "staged.py").write_text("y = 2\n", encoding="utf-8")
        _git(repo, "add", "staged.py")
        assert "staged.py" in get_dirty_files(repo)

    def test_rename_uses_new_path(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        _git(repo, "mv", "a.py", "b.py")
        files = get_dirty_files(repo)
        assert "b.py" in files
        assert "a.py" not in files

    def test_git_failure_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            mock_run.return_value = m
            assert get_dirty_files(repo) == []


class TestCommitBaseline:
    def test_not_a_git_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        ok, commit_hash, err = commit_baseline(plain)
        assert ok is False
        assert commit_hash == ""
        assert "not a git repo" in err

    def test_no_changes_is_idempotent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        ok, commit_hash, err = commit_baseline(repo)
        assert ok is True
        assert err == ""
        assert commit_hash == head_before  # 无改动 → 不产生新 commit
        # 仍然没有 dirty
        assert get_dirty_files(repo) == []

    def test_commits_dirty_changes(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        (repo / "a.py").write_text("print('changed')\n", encoding="utf-8")
        (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        ok, commit_hash, err = commit_baseline(repo)
        assert ok is True
        assert err == ""
        assert commit_hash != head_before  # 产生了新 commit
        # 工作区已 clean
        assert get_dirty_files(repo) == []
        # 新 commit 包含两个文件
        names = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert "a.py" in names and "new.py" in names

    def test_custom_message(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo_with_commit(repo)
        (repo / "a.py").write_text("print('changed')\n", encoding="utf-8")
        ok, _, _ = commit_baseline(repo, message="chore(baseline): custom")
        assert ok is True
        subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "chore(baseline): custom"
