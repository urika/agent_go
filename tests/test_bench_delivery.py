"""N1 bench 交付闭环：apply_local_delivery + _apply_bench_delivery 测试。

使用真实临时 git 仓库覆盖：成功交付（不推进 target）、冲突归因为
delivery_failure、advance_target 语义、bench hook 的跳过条件。
"""

import json
import subprocess
from pathlib import Path

import pytest

from agent_go.bench import _apply_bench_delivery
from agent_go.delivery import apply_local_delivery, evaluate_accepted_delivery


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30,
    )


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.email", "bench@test.local")
    _git(repo, "config", "user.name", "bench")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    assert _git(repo, "commit", "-m", "init").returncode == 0
    return repo


def _make_delivery_branch(repo: Path, task_id: str = "task-1",
                          filename: str = "feature.py", content: str = "y = 2\n"):
    """从当前 main 建 delivery branch 并加一个 commit，返回 (branch, commit, base)。"""
    base = _rev(repo, "main")
    branch = f"agent_go/{task_id}/delivery"
    assert _git(repo, "branch", branch, base).returncode == 0
    tmp = repo.parent / f"wt-{task_id}"
    assert _git(repo, "worktree", "add", "--detach", str(tmp), branch).returncode == 0
    (tmp / filename).write_text(content, encoding="utf-8")
    _git(tmp, "add", ".")
    assert _git(tmp, "commit", "-m", "subtask commit").returncode == 0
    commit = _rev(tmp, "HEAD")
    assert _git(repo, "branch", "-f", branch, commit).returncode == 0
    assert _git(repo, "worktree", "remove", "--force", str(tmp)).returncode == 0
    return branch, commit, base


def _meta(repo: Path, branch: str, commit: str, base: str, **overrides):
    meta = {
        "task_id": "task-1",
        "status": "DELIVERY_READY",
        "status_schema_version": 2,
        "repo": str(repo),
        "base_commit": base,
        "target_branch": "main",
        "delivery_branch": branch,
        "results": [
            {"subtask_id": "1", "status": "completed", "verify_ok": True,
             "commit_hash": commit},
        ],
    }
    meta.update(overrides)
    return meta


class TestApplyLocalDelivery:
    def test_success_records_merge_commit_without_advancing_target(self, fixture_repo):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        meta = _meta(fixture_repo, branch, commit, base)

        result = apply_local_delivery(fixture_repo, meta)

        assert result["delivered"] is True
        assert result["merge_commit"]
        assert meta["explicit_merge_commit"] == result["merge_commit"]
        assert meta["delivery_attempted"] is True
        assert meta["delivery_failed"] is False
        assert meta["delivery_mode"] == "bench_local"
        assert meta["accepted_delivery"] is True
        assert meta["status"] == "ACCEPTED_DELIVERY"
        # 关键：target 引用不推进，fixture repeat 基线不被污染
        assert _rev(fixture_repo, "main") == base
        # merge commit 对象存在，Accepted Delivery 判定闭合
        verdict = evaluate_accepted_delivery(meta, fixture_repo)
        assert verdict["accepted_delivery"] is True
        assert verdict["accepted_delivery_reasons"] == []

    def test_delivered_content_matches_delivery_branch(self, fixture_repo):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        meta = _meta(fixture_repo, branch, commit, base)

        result = apply_local_delivery(fixture_repo, meta)

        tree = _git(fixture_repo, "show", f"{result['merge_commit']}:feature.py")
        assert tree.stdout == "y = 2\n"

    def test_conflict_marks_delivery_failed(self, fixture_repo):
        branch, commit, base = _make_delivery_branch(
            fixture_repo, filename="app.py", content="x = 2\n")
        # main 同行改动 → 冲突
        (fixture_repo / "app.py").write_text("x = 3\n", encoding="utf-8")
        _git(fixture_repo, "add", ".")
        assert _git(fixture_repo, "commit", "-m", "conflicting main change").returncode == 0
        meta = _meta(fixture_repo, branch, commit, base)

        result = apply_local_delivery(fixture_repo, meta)

        assert result["delivered"] is False
        assert result["conflicts"]
        assert meta["delivery_attempted"] is True
        assert meta["delivery_failed"] is True
        assert "冲突" in meta["delivery_error"]
        assert "explicit_merge_commit" not in meta
        verdict = evaluate_accepted_delivery(meta, fixture_repo)
        assert verdict["accepted_delivery"] is False
        assert verdict["delivery_failed"] is True

    def test_advance_target_updates_ref(self, fixture_repo):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        meta = _meta(fixture_repo, branch, commit, base)

        result = apply_local_delivery(fixture_repo, meta, advance_target=True)

        assert result["delivered"] is True
        assert meta["delivery_mode"] == "local_advance"
        assert _rev(fixture_repo, "main") == result["merge_commit"]

    def test_missing_delivery_branch_fails_closed(self, fixture_repo):
        meta = _meta(fixture_repo, "", "", "")
        result = apply_local_delivery(fixture_repo, meta)

        assert result["delivered"] is False
        assert meta["delivery_attempted"] is True
        assert meta["delivery_failed"] is True
        assert "delivery_branch" in meta["delivery_error"]

    def test_ahead_zero_uses_target_tip(self, fixture_repo):
        base = _rev(fixture_repo, "main")
        branch = "agent_go/task-1/delivery"
        assert _git(fixture_repo, "branch", branch, base).returncode == 0
        meta = _meta(fixture_repo, branch, "", base,
                     results=[{"subtask_id": "1", "status": "no_changes",
                               "verify_ok": True}])

        result = apply_local_delivery(fixture_repo, meta)

        assert result["delivered"] is True
        assert result["merge_commit"] == base


class TestApplyBenchDelivery:
    def _write_meta(self, td: Path, meta: dict) -> Path:
        td.mkdir(parents=True, exist_ok=True)
        meta_path = td / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta_path

    def test_hook_applies_delivery_and_persists(self, fixture_repo, tmp_path):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        td = tmp_path / "task-x"
        meta_path = self._write_meta(td, _meta(fixture_repo, branch, commit, base))

        _apply_bench_delivery(td, fixture_repo)

        saved = json.loads(meta_path.read_text(encoding="utf-8"))
        assert saved["explicit_merge_commit"]
        assert saved["status"] == "ACCEPTED_DELIVERY"
        assert saved["delivery_mode"] == "bench_local"

    def test_hook_skips_pr_path(self, fixture_repo, tmp_path):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        td = tmp_path / "task-x"
        meta_path = self._write_meta(
            td, _meta(fixture_repo, branch, commit, base,
                      pr_url="https://example.test/pr/1"))

        _apply_bench_delivery(td, fixture_repo)

        saved = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "explicit_merge_commit" not in saved

    def test_hook_skips_unsuccessful_task(self, fixture_repo, tmp_path):
        branch, commit, base = _make_delivery_branch(fixture_repo)
        td = tmp_path / "task-x"
        meta_path = self._write_meta(
            td, _meta(fixture_repo, branch, commit, base,
                      status="VERIFICATION_FAILED",
                      results=[{"subtask_id": "1", "status": "failed",
                                "verify_ok": False}]))

        _apply_bench_delivery(td, fixture_repo)

        saved = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "explicit_merge_commit" not in saved
        assert "delivery_attempted" not in saved

    def test_hook_skips_when_no_delivery_branch(self, fixture_repo, tmp_path):
        td = tmp_path / "task-x"
        meta_path = self._write_meta(
            td, _meta(fixture_repo, "", "", "", delivery_branch=""))

        _apply_bench_delivery(td, fixture_repo)

        saved = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "delivery_attempted" not in saved
