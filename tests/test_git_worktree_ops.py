"""测试 git_utils.py — worktree 创建/删除/清理、gc.auto 控制

_all 中的内部函数：_worktree_create, _worktree_remove, _worktree_prune, _set_gc_auto
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.git_utils import (
    _worktree_create,
    _worktree_remove,
    _worktree_prune,
    _set_gc_auto,
    init_git_repo,
    resolve_project_id,
)
import subprocess


# ═══════════════════════════════════════════════════════════════
# _worktree_create
# ═══════════════════════════════════════════════════════════════

class TestWorktreeCreate:
    """测试 worktree 创建"""

    def test_create_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = repo / "worktrees" / "wt1"

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 0
            mock_run.return_value = m

            ok, err = _worktree_create(repo, "agent_go/t1/sub-1", wt_path)
        assert ok is True
        assert err == ""
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert args[1] == "worktree"
        assert args[2] == "add"
        assert args[3] == "-b"
        assert "agent_go/t1/sub-1" in args

    def test_create_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktrees" / "wt1"

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"fatal: branch already exists\n"
            mock_run.return_value = m

            ok, err = _worktree_create(repo, "existing-branch", wt_path)
        assert ok is False
        assert "branch already exists" in err

    def test_create_stderr_truncation(self, tmp_path):
        """错误消息被截断到 200 字符"""
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "wt1"

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"A" * 300
            mock_run.return_value = m

            ok, err = _worktree_create(repo, "b", wt_path)
        assert len(err) <= 200


# ═══════════════════════════════════════════════════════════════
# _worktree_remove
# ═══════════════════════════════════════════════════════════════

class TestWorktreeRemove:
    """测试 worktree 删除"""

    def test_remove_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktrees" / "wt1"
        wt_path.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 0
            mock_run.return_value = m

            ok, err = _worktree_remove(repo, wt_path)
        assert ok is True
        assert err == ""
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--force" in args

    def test_remove_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = tmp_path / "worktrees" / "wt1"
        wt_path.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"fatal: cannot remove locked worktree\n"
            mock_run.return_value = m

            ok, err = _worktree_remove(repo, wt_path)
        assert ok is False
        assert "locked" in err

    def test_remove_nonexistent_path_skips(self, tmp_path):
        """路径不存在时直接返回成功（幂等）"""
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_path = repo / "nonexistent_wt"

        ok, err = _worktree_remove(repo, wt_path)
        assert ok is True
        assert err == ""


# ═══════════════════════════════════════════════════════════════
# _worktree_prune
# ═══════════════════════════════════════════════════════════════

class TestWorktreePrune:
    """测试 worktree prune 清理"""

    def test_prune_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 0
            mock_run.return_value = m

            ok, err = _worktree_prune(repo)
        assert ok is True
        assert err == ""

    def test_prune_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 1
            m.stderr = b"error: could not prune\n"
            mock_run.return_value = m

            ok, err = _worktree_prune(repo)
        assert ok is False
        assert "could not prune" in err


# ═══════════════════════════════════════════════════════════════
# _set_gc_auto
# ═══════════════════════════════════════════════════════════════

class TestSetGcAuto:
    """测试 gc.auto 读写控制"""

    def test_disable_gc(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            call_count = [0]

            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一次调用：读取当前值
                    m.returncode = 0
                    m.stdout = "1\n"
                elif call_count[0] == 2:
                    # 第二次调用：设置新值
                    m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            original, ok, err = _set_gc_auto(repo, "0")
        assert original == "1"  # 原始值为 "1"
        assert ok is True
        assert err == ""

    def test_enable_gc_after_pipeline(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            call_count = [0]

            def side_effect(args, **kwargs):
                m = MagicMock()
                call_count[0] += 1
                if call_count[0] == 1:
                    m.returncode = 0
                    m.stdout = "0\n"  # 之前被禁用
                elif call_count[0] == 2:
                    m.returncode = 0  # 恢复
                return m
            mock_run.side_effect = side_effect

            original, ok, err = _set_gc_auto(repo, "1")
        assert original == "0"
        assert ok is True

    def test_no_prior_gc_config(self, tmp_path):
        """无历史 gc.auto 配置时，original 默认为 '1'"""
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            call_count = [0]

            def side_effect(args, **kwargs):
                m = MagicMock()
                call_count[0] += 1
                if call_count[0] == 1:
                    m.returncode = 1  # 读取失败
                    m.stdout = ""
                elif call_count[0] == 2:
                    m.returncode = 0  # 设置成功
                return m
            mock_run.side_effect = side_effect

            original, ok, _ = _set_gc_auto(repo, "0")
        assert original == "1"  # 默认为 "1"
        assert ok is True

    def test_set_failure(self, tmp_path):
        """设置 gc.auto 失败"""
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            call_count = [0]

            def side_effect(args, **kwargs):
                m = MagicMock()
                call_count[0] += 1
                if call_count[0] == 1:
                    m.returncode = 0
                    m.stdout = "1\n"
                elif call_count[0] == 2:
                    m.returncode = 1
                    m.stderr = b"error: permission denied\n"
                return m
            mock_run.side_effect = side_effect

            original, ok, err = _set_gc_auto(repo, "0")
        assert original == "1"
        assert ok is False
        assert "permission denied" in err

    def test_returns_tuple_format(self, tmp_path):
        """确认返回值格式：(original, success, error)"""
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 0
            m.stdout = "1\n"
            mock_run.return_value = m

            original, ok, err = _set_gc_auto(repo, "0")
        assert isinstance(original, str)
        assert isinstance(ok, bool)
        assert isinstance(err, str)


# ═══════════════════════════════════════════════════════════════
# init_git_repo（--auto-init 用）
# ═══════════════════════════════════════════════════════════════

class TestInitGitRepo:
    """测试 --auto-init 的 helper（真实 git，不 mock）"""

    def test_init_empty_dir_success(self, tmp_path):
        """空目录 → init 成功，自动建 .gitkeep 让 commit 通过"""
        repo = tmp_path / "repo"
        repo.mkdir()

        ok, err = init_git_repo(repo)

        assert ok is True, f"err: {err}"
        assert err == ""
        assert (repo / ".git").is_dir()
        assert (repo / ".gitkeep").is_file()  # 空目录自动建占位文件
        # 有 1 个 commit
        r = subprocess.run(["git", "log", "--oneline"], cwd=str(repo),
                           capture_output=True, text=True)
        assert r.returncode == 0
        assert "init (auto-created by agent_go)" in r.stdout

    def test_init_with_existing_files(self, tmp_path):
        """已有文件的目录 → init 成功，文件被 commit"""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (repo / "src").mkdir()
        (repo / "src" / "util.py").write_text("# util\n", encoding="utf-8")

        ok, err = init_git_repo(repo)

        assert ok is True, f"err: {err}"
        assert not (repo / ".gitkeep").exists()  # 有文件就不建占位
        # 文件已入版本库
        r = subprocess.run(["git", "ls-files"], cwd=str(repo),
                           capture_output=True, text=True)
        assert "main.py" in r.stdout
        assert "src/util.py" in r.stdout

    def test_init_branch_name(self, tmp_path):
        """init 后默认分支为 main"""
        repo = tmp_path / "repo"
        repo.mkdir()

        ok, err = init_git_repo(repo)
        assert ok is True

        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=str(repo), capture_output=True, text=True)
        assert r.stdout.strip() == "main"

    def test_init_does_not_set_global_config(self, tmp_path):
        """identity 通过 -c 内联，不写 repo-local config（不影响 --local 配置）"""
        repo = tmp_path / "repo"
        repo.mkdir()

        ok, err = init_git_repo(repo)
        assert ok is True

        # repo-local（--local）user.email 不应被写入；用 --local 严格隔离全局配置
        r = subprocess.run(["git", "config", "--local", "user.email"],
                           cwd=str(repo), capture_output=True, text=True)
        # 失败说明 repo-local 没设置（exit code 1）—— 符合预期
        assert r.returncode != 0, "identity 不应写入 repo-local config"

    def test_init_failure_git_not_found(self, tmp_path):
        """git 命令不存在时返回失败（mock subprocess.run 抛 FileNotFoundError）"""
        repo = tmp_path / "repo"
        repo.mkdir()

        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            ok, err = init_git_repo(repo)
        assert ok is False
        assert "git not found" in err


# ═══════════════════════════════════════════════════════════════
# resolve_project_id（marker 文件 + git + 目录路径兜底）
# ═══════════════════════════════════════════════════════════════

class TestResolveProjectId:
    """测试项目身份解析的三种优先级"""

    def test_marker_file_priority(self, tmp_path):
        """marker 文件优于 git remote"""
        repo = tmp_path / "project"
        repo.mkdir()
        (repo / ".agent-go-project").write_text("")
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "remote", "add", "origin", "git@github.com:user/repo.git"],
                       cwd=str(repo), capture_output=True)

        pid = resolve_project_id(repo)
        assert len(pid) == 16
        assert pid.isalnum()

    def test_git_remote_fallback(self, tmp_path):
        """无 marker 文件时使用 git remote"""
        repo = tmp_path / "project"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "remote", "add", "origin", "git@github.com:user/repo.git"],
                       cwd=str(repo), capture_output=True)

        pid = resolve_project_id(repo)
        assert len(pid) == 16
        assert pid.isalnum()

    def test_path_fallback_no_git(self, tmp_path):
        """非 git 目录且无 marker → 目录路径 sha256"""
        repo = tmp_path / "some-dir"
        repo.mkdir()

        pid = resolve_project_id(repo)
        assert len(pid) == 16
        assert pid.isalnum()

    def test_marker_upwards_resolution(self, tmp_path):
        """marker 在父目录时也能找到"""
        root = tmp_path / "monorepo"
        sub = root / "packages" / "sub-a"
        sub.mkdir(parents=True)
        (root / ".agent-go-project").write_text("")

        pid = resolve_project_id(sub)
        assert len(pid) == 16

    def test_stable_across_calls(self, tmp_path):
        """同一项目多次调用返回相同 ID"""
        repo = tmp_path / "stable"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "remote", "add", "origin", "git@github.com:user/repo.git"],
                       cwd=str(repo), capture_output=True)

        pid1 = resolve_project_id(repo)
        pid2 = resolve_project_id(repo)
        assert pid1 == pid2

    def test_different_remotes_different_ids(self, tmp_path):
        """不同 remote → 不同 ID"""
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        for r in (repo_a, repo_b):
            r.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=str(r), capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "remote", "add", "origin", "git@github.com:user/a.git"],
                       cwd=str(repo_a), capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "remote", "add", "origin", "git@github.com:user/b.git"],
                       cwd=str(repo_b), capture_output=True)

        assert resolve_project_id(repo_a) != resolve_project_id(repo_b)
