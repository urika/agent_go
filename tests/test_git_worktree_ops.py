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
)


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
