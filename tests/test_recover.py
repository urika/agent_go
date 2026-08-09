"""测试 recover.py — worktree 状态恢复 + meta.json 重建

核心规则（commit 是边界）：
- 有 commit + verify pass → completed
- 有 commit + verify fail → failed
- 无 commit + 有 orphan → reset orphan（视为未完成）
- 无 commit + 无 orphan → no_changes（从未跑过）

测试策略：真实 git 操作（不 mock），用 tmp_path 构建隔离环境。
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_go.recover import (
    _run_git,
    _save_meta_atomic,
    _branch_has_commits,
    _has_verify_pass,
    reset_orphan_changes,
    scan_subtask_state,
    recover_task,
)


def _init_repo(path: Path, files: Optional[dict[str, str]] = None) -> None:
    """Helper: git init + first commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), capture_output=True)
    if files:
        for name, content in files.items():
            (path / name).write_text(content, encoding="utf-8")
    else:
        (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


def _build_task_dir(tmp_path: Path, task_id: str) -> Path:
    """Build a fake ~/.agent_go/task-xxx directory."""
    agent_go_dir = tmp_path / ".agent_go"
    task_dir = agent_go_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


# ═══════════════════════════════════════════════════════════════
# _run_git
# ═══════════════════════════════════════════════════════════════

class TestRunGit:
    def test_success_returns_rc_stdout_stderr(self, tmp_path):
        _init_repo(tmp_path)
        rc, stdout, stderr = _run_git(tmp_path, "log", "--oneline")
        assert rc == 0
        assert "init" in stdout
        assert stderr == ""

    def test_failure_returns_nonzero_rc(self, tmp_path):
        rc, stdout, stderr = _run_git(tmp_path, "this-command-does-not-exist")
        assert rc != 0

    def test_timeout_returns_minus_one(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
            rc, stdout, stderr = _run_git(tmp_path, "log")
        assert rc == -1
        assert "timed out" in stderr

    def test_file_not_found_returns_minus_one(self, tmp_path):
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            rc, stdout, stderr = _run_git(tmp_path, "log")
        assert rc == -1
        assert "git not found" in stderr


# ═══════════════════════════════════════════════════════════════
# _save_meta_atomic
# ═══════════════════════════════════════════════════════════════

class TestSaveMetaAtomic:
    def test_writes_meta_json(self, tmp_path):
        meta = {"status": "completed", "task_id": "t1"}
        _save_meta_atomic(meta, tmp_path)
        meta_path = tmp_path / "meta.json"
        assert meta_path.exists()
        assert json.loads(meta_path.read_text(encoding="utf-8")) == meta

    def test_tmp_file_cleaned_up(self, tmp_path):
        meta = {"status": "completed"}
        _save_meta_atomic(meta, tmp_path)
        assert not (tmp_path / "meta.json.tmp").exists()

    def test_atomic_write_does_not_corrupt_on_failure(self, tmp_path):
        meta_path = tmp_path / "meta.json"
        meta_path.write_text('{"original": true}', encoding="utf-8")
        tmp_path2 = tmp_path / "meta.json.tmp"
        tmp_path2.write_text("{corrupted", encoding="utf-8")

        _save_meta_atomic({"status": "clean"}, tmp_path)
        assert json.loads(meta_path.read_text(encoding="utf-8")) == {"status": "clean"}
        assert not tmp_path2.exists()

    def test_serializes_default_types(self, tmp_path):
        from datetime import datetime
        meta = {"ts": datetime(2026, 7, 26, 12, 0, 0)}
        _save_meta_atomic(meta, tmp_path)
        data = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
        assert "2026-07-26" in data["ts"]


# ═══════════════════════════════════════════════════════════════
# _branch_has_commits
# ═══════════════════════════════════════════════════════════════

class TestBranchHasCommits:
    def test_on_correct_branch_with_commits(self, tmp_path):
        _init_repo(tmp_path, {"a.txt": "a"})
        subprocess.run(["git", "checkout", "-b", "agent_go/t1/s1"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "extra"], cwd=str(tmp_path), capture_output=True)

        on_branch, count, branch = _branch_has_commits(tmp_path, "agent_go/t1/s1")
        assert on_branch is True
        assert count == 1
        assert branch == "agent_go/t1/s1"

    def test_wrong_branch_prefix(self, tmp_path):
        _init_repo(tmp_path, {"a.txt": "a"})
        subprocess.run(["git", "checkout", "-b", "other-branch"], cwd=str(tmp_path), capture_output=True)

        on_branch, count, branch = _branch_has_commits(tmp_path, "agent_go/t1/s1")
        assert on_branch is False
        assert branch == "other-branch"

    def test_zero_commits_on_branch(self, tmp_path):
        _init_repo(tmp_path, {"a.txt": "a"})
        subprocess.run(["git", "checkout", "-b", "agent_go/t1/s1"], cwd=str(tmp_path), capture_output=True)

        on_branch, count, branch = _branch_has_commits(tmp_path, "agent_go/t1/s1")
        assert on_branch is True
        assert count == 0

    def test_no_git_repo_returns_empty(self, tmp_path):
        on_branch, count, branch = _branch_has_commits(tmp_path, "agent_go/t1/s1")
        assert on_branch is False
        assert count == 0
        assert branch == ""


# ═══════════════════════════════════════════════════════════════
# _has_verify_pass
# ═══════════════════════════════════════════════════════════════

class TestHasVerifyPass:
    def _make_log(self, path: Path, lines: list[str]):
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_verify_passed(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-1] subtask_complete {"status": "completed"}',
        ])
        assert _has_verify_pass(log, "sub-1") is True

    def test_verify_failed(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-1] subtask_complete {"status": "failed"}',
        ])
        assert _has_verify_pass(log, "sub-1") is False

    def test_verify_no_changes(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-1] subtask_complete {"status": "no_changes"}',
        ])
        assert _has_verify_pass(log, "sub-1") is False

    def test_no_matching_sub_id(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-2] subtask_complete {"status": "completed"}',
        ])
        assert _has_verify_pass(log, "sub-1") is None

    def test_log_file_not_found(self, tmp_path):
        assert _has_verify_pass(tmp_path / "nonexistent.log", "sub-1") is None

    def test_both_success_and_fail_markers_fail_wins(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-1] subtask_complete {"status": "failed"}',
            '[sub-1] subtask_complete {"status": "completed"}',
        ])
        assert _has_verify_pass(log, "sub-1") is False

    def test_empty_log(self, tmp_path):
        log = tmp_path / "execution.log"
        log.write_text("", encoding="utf-8")
        assert _has_verify_pass(log, "sub-1") is None

    def test_only_success_marker_not_fail(self, tmp_path):
        log = tmp_path / "execution.log"
        self._make_log(log, [
            '[sub-1] subtask_complete {"status": "completed"}',
            '[sub-1] some_other_event',
        ])
        assert _has_verify_pass(log, "sub-1") is True


# ═══════════════════════════════════════════════════════════════
# reset_orphan_changes
# ═══════════════════════════════════════════════════════════════

class TestResetOrphanChanges:
    def test_clean_repo_returns_true(self, tmp_path):
        _init_repo(tmp_path)
        assert reset_orphan_changes(tmp_path) is True

    def test_resets_untracked_file(self, tmp_path):
        _init_repo(tmp_path, {"tracked.txt": "original"})
        (tmp_path / "untracked.txt").write_text("new", encoding="utf-8")
        assert reset_orphan_changes(tmp_path) is True
        assert not (tmp_path / "untracked.txt").exists()

    def test_resets_modified_tracked_file(self, tmp_path):
        _init_repo(tmp_path, {"tracked.txt": "original"})
        (tmp_path / "tracked.txt").write_text("modified", encoding="utf-8")
        assert reset_orphan_changes(tmp_path) is True
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "original"

    def test_resets_mixed_state(self, tmp_path):
        _init_repo(tmp_path, {"tracked.txt": "original"})
        (tmp_path / "tracked.txt").write_text("modified", encoding="utf-8")
        (tmp_path / "untracked.txt").write_text("new", encoding="utf-8")
        assert reset_orphan_changes(tmp_path) is True
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "original"
        assert not (tmp_path / "untracked.txt").exists()

    def test_only_untracked_no_tracked_files(self, tmp_path):
        """边缘 case：.git 存在但没有 tracked 文件，只有 untracked 文件。
        这是旧代码 checkout -- . 失败的唯一场景（index 为空）。
        """
        (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
        # 现在 repo 有 1 个 tracked 文件；删除它，让 index 为空，类似 init 后无 commit 的状态
        subprocess.run(["git", "rm", "-rf", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "empty"], cwd=str(tmp_path), capture_output=True)
        # 确认无 tracked 文件
        r = subprocess.run(["git", "ls-files"], cwd=str(tmp_path), capture_output=True, text=True)
        assert r.stdout.strip() == ""

        # 只加 untracked 文件
        (tmp_path / "orphan.py").write_text("# orphan\n", encoding="utf-8")
        (tmp_path / "scratch.json").write_text("{}\n", encoding="utf-8")

        assert reset_orphan_changes(tmp_path) is True
        assert not (tmp_path / "orphan.py").exists()
        assert not (tmp_path / "scratch.json").exists()

    def test_only_untracked_nested_dirs(self, tmp_path):
        """untracked 文件在嵌套子目录中 — clean -fd 需要递归清理。"""
        _init_repo(tmp_path, {"base.py": "# base\n"})
        nested = tmp_path / "src" / "utils"
        nested.mkdir(parents=True)
        (nested / "helper.py").write_text("# helper\n", encoding="utf-8")
        (tmp_path / "config.json").write_text("{}\n", encoding="utf-8")

        assert reset_orphan_changes(tmp_path) is True
        assert not (nested / "helper.py").exists()
        assert not (tmp_path / "config.json").exists()
        assert not (tmp_path / "src").exists()  # 整个 untracked 目录树被 clean -fd 删除
        assert (tmp_path / "base.py").exists()  # tracked 文件不受影响

    def test_errors_when_git_fails(self, tmp_path):
        with patch("agent_go.recover._run_git",
                   side_effect=[(0, " M f\n", ""), (-1, "", "error")]):
            assert reset_orphan_changes(tmp_path) is False


# ═══════════════════════════════════════════════════════════════
# scan_subtask_state
# ═══════════════════════════════════════════════════════════════

class TestScanSubtaskState:
    def test_no_worktree_dir(self, tmp_path):
        task_dir = _build_task_dir(tmp_path, "task-t1")
        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "no_worktree"
        assert result["subtask_id"] == "sub-1"
        assert result.get("recovered") is True

    def test_worktree_missing_git_link(self, tmp_path):
        task_dir = _build_task_dir(tmp_path, "task-t1")
        worktree = task_dir / "sub-1" / "work"
        worktree.mkdir(parents=True)
        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "no_git_link"

    def test_worktree_on_wrong_branch(self, tmp_path):
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "checkout", "-b", "wrong-branch"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "wrong-branch"],
                       cwd=str(main), capture_output=True)

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "wrong_branch"

    def test_no_commits_no_orphan_no_changes(self, tmp_path):
        """Worktree exists, on correct branch, but no extra commits → no_changes."""
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "branch", "agent_go/task-t1/sub-1"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "agent_go/task-t1/sub-1"],
                       cwd=str(main), capture_output=True)

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "no_changes"
        assert result["commits"] == 0
        assert result["orphan_reset"] is False

    def test_commits_no_verify_log_unverified(self, tmp_path):
        """Has commits but no execution.log → committed_unverified."""
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "checkout", "-b", "agent_go/task-t1/sub-1"], cwd=str(main), capture_output=True)
        (main / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "commit", "-m", "subtask work"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "agent_go/task-t1/sub-1"],
                       cwd=str(main), capture_output=True)

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "committed_unverified"
        assert result["commits"] >= 1

    def test_commits_verify_fail_failed(self, tmp_path):
        """Has commits + verify fail → failed."""
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "checkout", "-b", "agent_go/task-t1/sub-1"], cwd=str(main), capture_output=True)
        (main / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "commit", "-m", "subtask work"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "agent_go/task-t1/sub-1"],
                       cwd=str(main), capture_output=True)

        log_path = task_dir / "execution.log"
        log_path.write_text(f'[sub-1] subtask_complete {{"status": "failed"}}\n', encoding="utf-8")

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "failed"
        assert result["verify_ok"] is False

    def test_commits_verify_pass_completed(self, tmp_path):
        """Has commits + verify pass → completed."""
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "checkout", "-b", "agent_go/task-t1/sub-1"], cwd=str(main), capture_output=True)
        (main / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "commit", "-m", "subtask work"], cwd=str(main), capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "agent_go/task-t1/sub-1"],
                       cwd=str(main), capture_output=True)

        log_path = task_dir / "execution.log"
        log_path.write_text(f'[sub-1] subtask_complete {{"status": "completed"}}\n', encoding="utf-8")

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "completed"
        assert result["verify_ok"] is True

    def test_no_commits_with_orphan_reset_and_no_changes(self, tmp_path):
        """No commits + orphan exists → orphan is reset, status = no_changes."""
        main = tmp_path / "main"
        _init_repo(main, {"f.txt": "base"})
        subprocess.run(["git", "branch", "agent_go/task-t1/sub-1"], cwd=str(main), capture_output=True)

        task_dir = _build_task_dir(tmp_path, "task-t1")
        wt_path = task_dir / "sub-1" / "work"
        wt_path.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", str(wt_path), "agent_go/task-t1/sub-1"],
                       cwd=str(main), capture_output=True)

        (wt_path / "orphan.txt").write_text("should be reset", encoding="utf-8")
        assert (wt_path / "orphan.txt").exists()

        result = scan_subtask_state("task-t1", task_dir, "sub-1")
        assert result["status"] == "no_changes"
        assert result["orphan_reset"] is True
        assert not (wt_path / "orphan.txt").exists()


# ═══════════════════════════════════════════════════════════════
# recover_task
# ═══════════════════════════════════════════════════════════════

class TestRecoverTask:
    def test_task_dir_not_found(self, tmp_path):
        with patch("agent_go.recover.AGENT_GO_DIR", tmp_path):
            result = recover_task("task-nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    def test_no_subtasks_scans_sub_dirs(self, tmp_path):
        """meta.json has no subtasks → discovers from sub-* dirs."""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {"status": "running", "task_id": "task-t1"}
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        sub_dir = task_dir / "sub-1"
        sub_dir.mkdir(parents=True)
        wt = sub_dir / "work"
        wt.mkdir()

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert "overall_status" in result
        assert len(result["recovered"]) == 1
        assert result["recovered"][0]["subtask_id"] == "sub-1"

    def test_preserves_existing_real_results(self, tmp_path):
        """Existing non-recovered results should be kept as-is (not re-scanned)."""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [{"id": "sub-1", "title": "real work"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed"},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert len(result["recovered"]) == 1
        # Should have been kept as-is (no recovered flag since original had none)
        assert result["recovered"][0]["status"] == "completed"

    def test_overall_status_all_completed(self, tmp_path):
        """All subtasks completed → overall completed（交付就绪）。"""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [
                {"id": "sub-1", "title": "t1"},
                {"id": "sub-2", "title": "t2"},
            ],
            "results": [
                {"subtask_id": "sub-1", "status": "completed"},
                {"subtask_id": "sub-2", "status": "completed"},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        # M1-3: 全部 completed → 交付就绪（legacy 映射 completed），不降级为 interrupted
        assert result["overall_status"] == "completed"

    def test_overall_status_failed(self, tmp_path):
        """Any subtask failed → overall failed."""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [
                {"id": "sub-1", "title": "t1"},
                {"id": "sub-2", "title": "t2"},
            ],
            "results": [
                {"subtask_id": "sub-1", "status": "completed"},
                {"subtask_id": "sub-2", "status": "failed"},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "failed"

    def test_overall_status_interrupted_when_no_changes(self, tmp_path):
        """Any subtask with no_changes → interrupted."""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [
                {"id": "sub-1", "title": "t1"},
            ],
            "results": [
                {"subtask_id": "sub-1", "status": "no_changes", "recovered": True},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "interrupted"

    def test_overall_status_dirty_error_interrupted(self, tmp_path):
        """Any error status (no_worktree etc.) → interrupted."""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [
                {"id": "sub-1", "title": "t1"},
            ],
            "results": [
                {"subtask_id": "sub-1", "status": "no_worktree", "recovered": True},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "interrupted"

    def test_writes_meta_when_update_meta_true(self, tmp_path):
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [{"id": "sub-1", "title": "t1"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed"},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1", update_meta=True)

        assert result["meta_updated"] is True
        updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        # M1-3: 全部 completed → 交付就绪（legacy 映射 completed）
        assert updated["status"] == "completed"
        assert "recovered_at" in updated

    def test_does_not_write_when_update_meta_false(self, tmp_path):
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "running",
            "task_id": "task-t1",
            "subtasks": [{"id": "sub-1", "title": "t1"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed", "recovered": True},
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1", update_meta=False)

        assert result["meta_updated"] is False
        updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert updated.get("recovered_at") is None

    def test_corrupted_meta_json_fallback(self, tmp_path):
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        (task_dir / "meta.json").write_text("{corrupted", encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert "error" not in result

    def test_no_subtasks_status(self, tmp_path):
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        (task_dir / "meta.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "interrupted"


class TestRecoverDelivery:
    """M1-3: recover 交付状态保留。"""

    def test_recover_preserves_accepted_delivery(self, tmp_path):
        """已 ACCEPTED_DELIVERY 的任务 recover 后不降级。"""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "ACCEPTED_DELIVERY", "status_schema_version": 1,
            "task_id": "task-t1", "delivery_branch": "agent_go/task-t1/delivery",
            "pr_url": "https://example.test/pr/1",
            "subtasks": [{"id": "sub-1", "title": "t1"}],
            "results": [{"subtask_id": "sub-1", "status": "completed", "verify_ok": True}],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "ACCEPTED_DELIVERY"
        updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert updated["status"] == "ACCEPTED_DELIVERY"
        # delivery 字段保留
        assert updated["delivery_branch"] == "agent_go/task-t1/delivery"
        assert updated["pr_url"] == "https://example.test/pr/1"

    def test_recover_all_completed_becomes_delivery_ready(self, tmp_path):
        """全部 completed 但未交付 → DELIVERY_READY（不降级 EXECUTING）。"""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "DELIVERY_READY", "status_schema_version": 1,
            "task_id": "task-t1",
            "subtasks": [{"id": "sub-1", "title": "t1"}],
            "results": [{"subtask_id": "sub-1", "status": "completed", "verify_ok": True}],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "DELIVERY_READY"
        updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert updated["status"] == "DELIVERY_READY"

    def test_recover_committed_unverified_goes_executing(self, tmp_path):
        """有 committed_unverified 子任务 → 任务级 EXECUTING（resume 接力）。"""
        agent_go_dir = tmp_path / ".agent_go"
        task_dir = agent_go_dir / "task-t1"
        task_dir.mkdir(parents=True)
        meta = {
            "status": "EXECUTING", "status_schema_version": 1,
            "task_id": "task-t1",
            "subtasks": [{"id": "sub-1", "title": "t1"}],
            "results": [{"subtask_id": "sub-1", "status": "committed_unverified",
                         "verify_ok": None, "recovered": True}],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with patch("agent_go.recover.AGENT_GO_DIR", agent_go_dir):
            result = recover_task("task-t1")

        assert result["overall_status"] == "EXECUTING"
        updated = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert updated["status"] == "EXECUTING"
