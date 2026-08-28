"""测试 metrics.py — 数据采集模块

全覆盖: collect_timing, collect_change_stats, collect_merge_result, extract_usage
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.metrics import (
    collect_timing,
    collect_change_stats,
    collect_merge_result,
    extract_usage,
)


class TestCollectTiming:
    """阶段耗时采集"""

    def test_all_fields_present(self):
        result = collect_timing(
            worktree_create_ms=123.4,
            merge_upstream_ms=45.6,
            claude_execute_ms=30000.0,
            verification_ms=1500.0,
            git_commit_ms=200.0,
        )
        assert result["worktree_create_ms"] == 123
        assert result["merge_upstream_ms"] == 46
        assert result["claude_execute_ms"] == 30000
        assert result["verification_ms"] == 1500
        assert result["git_commit_ms"] == 200

    def test_zero_values(self):
        result = collect_timing(0, 0, 0, 0, 0)
        assert all(v == 0 for v in result.values())

    def test_rounding(self):
        result = collect_timing(1.499, 2.4, 3.501, 4.999, 5.0)
        assert result["worktree_create_ms"] == 1
        assert result["merge_upstream_ms"] == 2
        assert result["claude_execute_ms"] == 4

    def test_returns_dict(self):
        result = collect_timing(1, 2, 3, 4, 5)
        assert isinstance(result, dict)
        assert len(result) == 5


class TestCollectChangeStats:
    """git 变更统计采集"""

    def test_with_changes(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = "5\t3\tsrc/main.py\n2\t0\tsrc/utils.py\n"
                elif "porcelain" in cmd:
                    m.stdout = "M  src/main.py\nA  src/new.py\n"
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        # files_changed = 2 (src/main.py + src/utils.py from numstat)
        # "A  src/new.py" in porcelain does NOT start with "??", so it's not counted as new
        assert result["files_changed"] == 2
        assert result["insertions"] == 7
        assert result["deletions"] == 3
        assert result["new_files"] == 0
        assert result["modified_files"] == 2

    def test_no_changes(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = ""
                elif "porcelain" in cmd:
                    m.stdout = ""
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        assert result["files_changed"] == 0
        assert result["insertions"] == 0
        assert result["deletions"] == 0
        assert result["new_files"] == 0

    def test_new_files_only(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = ""
                elif "porcelain" in cmd:
                    m.stdout = "?? new_file.py\n?? another.py\n"
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        assert result["files_changed"] == 2
        assert result["new_files"] == 2
        assert result["insertions"] == 0

    def test_negative_numstat_handled(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = "-\t-\tsrc/binary.bin\n"
                elif "porcelain" in cmd:
                    m.stdout = ""
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))
            assert result["insertions"] == 0
            assert result["deletions"] == 0
            assert result["files_changed"] == 1

    def test_subprocess_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            # FileNotFoundError 会传播出来，没有 try/except 包裹
            with pytest.raises(FileNotFoundError):
                collect_change_stats(Path("/fake/repo"))


class TestCollectMergeResult:
    """产物传递结果采集"""

    def test_success(self):
        result = collect_merge_result("sub-1", True)
        assert result["upstream"] == "sub-1"
        assert result["status"] == "success"

    def test_failure_no_conflict_files(self):
        result = collect_merge_result("sub-2", False)
        assert result["status"] == "conflict"
        assert "conflict_files" not in result

    def test_failure_with_conflict_files(self):
        result = collect_merge_result("sub-2", False, ["main.py", "utils.py"])
        assert result["status"] == "conflict"
        assert result["conflict_files"] == ["main.py", "utils.py"]

    def test_empty_conflict_list(self):
        result = collect_merge_result("sub-3", False, [])
        assert result["status"] == "conflict"
        # 空列表是 falsy，不会被添加到结果中
        assert "conflict_files" not in result


class TestExtractUsage:
    """API 用量提取"""

    def test_openai_response(self):
        api_resp = {
            "usage": {"input_tokens": 200, "output_tokens": 400}
        }
        result = extract_usage(api_resp, "openai", "gpt-4o")
        assert result["prompt_tokens"] == 200
        assert result["completion_tokens"] == 400
        assert result["model"] == "gpt-4o"
        assert result["provider"] == "openai"

    def test_anthropic_response(self):
        api_resp = {
            "usage": {"input_tokens": 150, "output_tokens": 300}
        }
        result = extract_usage(api_resp, "anthropic", "claude-sonnet-4")
        assert result["prompt_tokens"] == 150
        assert result["completion_tokens"] == 300

    def test_no_usage(self):
        api_resp = {}
        result = extract_usage(api_resp, "anthropic", "claude-sonnet-4")
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    def test_partial_usage(self):
        api_resp = {"usage": {"input_tokens": 100}}
        result = extract_usage(api_resp, "anthropic", "test")
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 0


# ═══════════════════════════════════════════════════════════════
# CR-P1-3：K12 MCP 工具调用成功率（PRD ≥95%，排除用户配置错误）
# ═══════════════════════════════════════════════════════════════

from agent_go.metrics import compute_mcp_tool_success_rate, MCP_TOOL_SUCCESS_THRESHOLD


def _evt(success, err=None):
    e = {"tool": "mcp__srv__t", "success": success}
    if err:
        e["error_type"] = err
    return e


def test_k12_passes_at_95_excluding_user_config():
    """95 成功 / 4 server_error / 1 user_config_error → 排除 user_config 后 95/99≥95%。"""
    events = [_evt(True) for _ in range(95)]
    events += [_evt(False, "server_error") for _ in range(4)]
    events += [_evt(False, "user_config_error")]
    r = compute_mcp_tool_success_rate(events)
    assert r["excluded_user_config"] == 1
    assert r["denominator"] == 99
    assert r["successes"] == 95
    assert r["success_rate"] >= MCP_TOOL_SUCCESS_THRESHOLD
    assert r["passes_threshold"] is True


def test_k12_below_threshold_fails():
    """94 成功 / 6 server_error → 0.94 < 0.95 → 不过阈值。"""
    events = [_evt(True) for _ in range(94)]
    events += [_evt(False, "server_error") for _ in range(6)]
    r = compute_mcp_tool_success_rate(events)
    assert r["success_rate"] == 0.94
    assert r["passes_threshold"] is False


def test_k12_no_exclude_user_config():
    """不排除 user_config_error → 计入分母：95/100 = 0.95（边界过）。"""
    events = [_evt(True) for _ in range(95)]
    events += [_evt(False, "server_error") for _ in range(4)]
    events += [_evt(False, "user_config_error")]
    r = compute_mcp_tool_success_rate(events, exclude_user_config_errors=False)
    assert r["denominator"] == 100
    assert r["success_rate"] == 0.95
    assert r["passes_threshold"] is True


def test_k12_all_user_config_errors():
    """全 user_config_error → 排除后 denominator=0 → rate None、不过阈值（无数据可判）。"""
    events = [_evt(False, "user_config_error") for _ in range(3)]
    r = compute_mcp_tool_success_rate(events)
    assert r["denominator"] == 0
    assert r["success_rate"] is None
    assert r["passes_threshold"] is False


def test_k12_empty():
    r = compute_mcp_tool_success_rate([])
    assert r["success_rate"] is None
    assert r["passes_threshold"] is False


# ═══ #49 信任指标 ═══════════════════════════════════════════════════════

import json as _json


def test_compute_trust_metrics_review_and_recurrence(tmp_path):
    """审查后修改率 + 复发可见率。"""
    from agent_go.metrics import compute_trust_metrics

    # task-1: 1 失败带 problem_id + review=changes_requested
    t1 = tmp_path / "task-1"
    t1.mkdir()
    (t1 / "meta.json").write_text(_json.dumps({
        "results": [{"subtask_id": "s1", "status": "failed", "problem_id": "p-1"}]}), encoding="utf-8")
    (t1 / "review.json").write_text(_json.dumps({"decision": "changes_requested"}), encoding="utf-8")
    # task-2: 1 失败无 problem_id + review=approved
    t2 = tmp_path / "task-2"
    t2.mkdir()
    (t2 / "meta.json").write_text(_json.dumps({
        "results": [{"subtask_id": "s1", "status": "failed"}]}), encoding="utf-8")
    (t2 / "review.json").write_text(_json.dumps({"decision": "approved"}), encoding="utf-8")
    # task-3: 无 meta（跳过）
    t3 = tmp_path / "task-3"
    t3.mkdir()

    r = compute_trust_metrics([t1, t2, t3])
    assert r["reviewed_tasks"] == 2
    assert r["review_modification_rate"] == 0.5  # 1/2
    assert r["failed_subtasks"] == 2
    assert r["recurrence_visibility_rate"] == 0.5  # 1/2
    assert r["blind_spot_hit_rate"] is None  # 待跨任务历史


def test_compute_trust_metrics_empty(tmp_path):
    from agent_go.metrics import compute_trust_metrics
    r = compute_trust_metrics([])
    assert r["review_modification_rate"] is None
    assert r["recurrence_visibility_rate"] is None
    assert r["failed_subtasks"] == 0


# ═══════════════════════════════════════════════════════════════
# #49 盲区命中率（compute_blind_spot_hit_rate / trust 接线）
# ═══════════════════════════════════════════════════════════════

def _mk_task(base: Path, name: str, meta: dict, review=None) -> Path:
    td = base / name
    td.mkdir()
    (td / "meta.json").write_text(_json.dumps(meta), encoding="utf-8")
    if review is not None:
        (td / "review.json").write_text(_json.dumps(review), encoding="utf-8")
    return td


def test_blind_spot_hit_weak_anchor_failed(tmp_path):
    """弱锚定标注 + 该子任务最终 failed → 命中。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    t = _mk_task(tmp_path, "task-1", {
        "status": "VERIFICATION_FAILED",
        "results": [{"subtask_id": "s1", "status": "failed"}],
        "blind_spots": {"weakly_anchored_subtasks": ["s1"]},
    })
    r = compute_blind_spot_hit_rate([t])
    assert r["blind_spot_items"] == 1
    assert r["blind_spot_hits"] == 1
    assert r["blind_spot_hit_rate"] == 1.0
    assert r["by_signal"]["weakly_anchored_subtasks"] == {"items": 1, "hits": 1, "pending": 0, "na": 0}


def test_blind_spot_no_hit_when_completed(tmp_path):
    """弱锚定标注但子任务 completed、任务交付、repo 可达有文件、观察期未满
    → 挂起（pending），不计命中也不进分母（ISSUE-54：「尚未出问题」≠「不出问题」）。"""
    import subprocess
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    t = _mk_task(tmp_path, "task-1", {
        "status": "ACCEPTED_DELIVERY",
        "repo": str(repo),
        "results": [{"subtask_id": "s1", "status": "completed",
                     "summary": "a.py | 2 ++"}],
        "blind_spots": {"weakly_anchored_subtasks": ["s1"]},
    }, review={"decision": "approved"})
    r = compute_blind_spot_hit_rate([t])
    assert r["blind_spot_items"] == 1
    assert r["blind_spot_hits"] == 0
    assert r["blind_spot_pending"] == 1
    assert r["blind_spot_na"] == 0
    assert r["blind_spot_hit_rate"] is None


def test_blind_spot_hit_inconclusive_review_rejected(tmp_path):
    """评估不确定标注 + review 被拒 → 命中（即使子任务未 failed）。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    t = _mk_task(tmp_path, "task-1", {
        "status": "DELIVERY_READY",
        "results": [{"subtask_id": "s1", "status": "completed"}],
        "blind_spots": {"inconclusive_evaluations": ["s1"]},
    }, review={"decision": "changes_requested"})
    r = compute_blind_spot_hit_rate([t])
    assert r["by_signal"]["inconclusive_evaluations"] == {"items": 1, "hits": 1, "pending": 0, "na": 0}


def test_blind_spot_hit_uncovered_acceptance_goal_low(tmp_path):
    """未覆盖验收 ID + goal_adherence=low（执行全过但漏验收）→ 命中。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    t = _mk_task(tmp_path, "task-1", {
        "status": "completed",
        "results": [{"subtask_id": "s1", "status": "completed"}],
        "goal_adherence": {"level": "low"},
        "blind_spots": {"uncovered_acceptance_ids": ["AC-1", "AC-2"]},
    })
    r = compute_blind_spot_hit_rate([t])
    assert r["by_signal"]["uncovered_acceptance_ids"] == {"items": 2, "hits": 2, "pending": 0, "na": 0}


def test_blind_spot_uncovered_acceptance_no_hit_when_delivered(tmp_path):
    """未覆盖验收 ID 但任务完成且 goal 合规 → 未命中。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    t = _mk_task(tmp_path, "task-1", {
        "status": "completed",
        "results": [{"subtask_id": "s1", "status": "completed"}],
        "goal_adherence": {"level": "full"},
        "blind_spots": {"uncovered_acceptance_ids": ["AC-1"]},
    })
    r = compute_blind_spot_hit_rate([t])
    assert r["blind_spot_hits"] == 0


def test_blind_spot_excludes_non_predictive_signals(tmp_path):
    """unattributed_failures（本身是失败）与 baseline_dirty（环境位）不计入分母。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    t = _mk_task(tmp_path, "task-1", {
        "status": "failed",
        "results": [{"subtask_id": "s1", "status": "failed"}],
        "blind_spots": {"unattributed_failures": ["s1"], "baseline_dirty": True},
    })
    r = compute_blind_spot_hit_rate([t])
    assert r["blind_spot_items"] == 0
    assert r["blind_spot_hit_rate"] is None


def test_trust_metrics_blind_spot_wired(tmp_path):
    """compute_trust_metrics 接入盲区命中率（不再是固定 None）。"""
    from agent_go.metrics import compute_trust_metrics
    t = _mk_task(tmp_path, "task-1", {
        "status": "VERIFICATION_FAILED",
        "results": [{"subtask_id": "s1", "status": "failed", "problem_id": "p-1"}],
        "blind_spots": {"weakly_anchored_subtasks": ["s1"]},
    })
    r = compute_trust_metrics([t])
    assert r["blind_spot_hit_rate"] == 1.0
    assert r["blind_spot_items"] == 1
    assert r["blind_spot_by_signal"]["weakly_anchored_subtasks"]["hits"] == 1


def test_trust_metrics_blind_spot_none_without_annotations(tmp_path):
    """无盲区标注 → 命中率为 None（样本不足语义，非 0）。"""
    from agent_go.metrics import compute_trust_metrics
    t = _mk_task(tmp_path, "task-1", {
        "status": "completed",
        "results": [{"subtask_id": "s1", "status": "completed"}],
    })
    r = compute_trust_metrics([t])
    assert r["blind_spot_hit_rate"] is None
    assert r["blind_spot_items"] == 0


# ISSUE-54：交付后观察证据（返工命中 / pending 挂起）
# 复用下文返工率测试的 _git / _mk_repo_with_file 助手（模块级，调用时解析）。

def _mk_blind_task(base: Path, name: str, repo: Path, mtime_ts: int,
                   blind: dict, results: list) -> Path:
    import os
    td = base / name
    td.mkdir()
    meta = {
        "task_id": name, "status": "ACCEPTED_DELIVERY", "repo": str(repo),
        "results": results, "blind_spots": blind,
    }
    mp = td / "meta.json"
    mp.write_text(_json.dumps(meta), encoding="utf-8")
    os.utime(mp, (mtime_ts, mtime_ts))
    return td


def test_blind_spot_hit_post_delivery_rework(tmp_path):
    """弱锚定子任务交付后 14d 内其文件被人工 commit 修改 → 命中（ISSUE-54）。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_blind_task(tmp_path, "task-a", repo, anchor,
                        blind={"weakly_anchored_subtasks": ["s1"]},
                        results=[{"subtask_id": "s1", "status": "completed",
                                  "summary": "a.py | 2 ++\n 1 file changed, 2 insertions(+)"}])
    # 交付 merge（近似锚点丢弃最旧）+ 人工返工
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge agent work", date_ts=anchor + _DAY)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "human fix", date_ts=anchor + 2 * _DAY)
    r = compute_blind_spot_hit_rate([td], now=anchor + 20 * _DAY)
    assert r["blind_spot_hits"] == 1
    assert r["blind_spot_judged"] == 1
    assert r["blind_spot_pending"] == 0
    assert r["blind_spot_hit_rate"] == 1.0


def test_blind_spot_no_rework_after_window_judged_non_hit(tmp_path):
    """观察期满且无返工 → 判定为非命中（进分母，rate=0.0）。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_blind_task(tmp_path, "task-a", repo, anchor,
                        blind={"weakly_anchored_subtasks": ["s1"]},
                        results=[{"subtask_id": "s1", "status": "completed",
                                  "summary": "a.py | 2 ++"}])
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge agent work", date_ts=anchor + _DAY)
    r = compute_blind_spot_hit_rate([td], now=anchor + 20 * _DAY)
    assert r["blind_spot_hits"] == 0
    assert r["blind_spot_judged"] == 1
    assert r["blind_spot_hit_rate"] == 0.0


def test_blind_spot_window_not_elapsed_pending(tmp_path):
    """观察期未满 → pending（不进分母，rate=None）。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_blind_task(tmp_path, "task-a", repo, anchor,
                        blind={"weakly_anchored_subtasks": ["s1"]},
                        results=[{"subtask_id": "s1", "status": "completed",
                                  "summary": "a.py | 2 ++"}])
    r = compute_blind_spot_hit_rate([td], now=anchor + 5 * _DAY)
    assert r["blind_spot_pending"] == 1
    assert r["blind_spot_judged"] == 0
    assert r["blind_spot_hit_rate"] is None


def test_blind_spot_rework_item_level_file_isolation(tmp_path):
    """返工命中按标注项关联文件判定：别人子任务的文件被返工不算本项命中。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = _mk_repo_with_file(tmp_path, _BASE)  # a.py 已在 base commit
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add b", date_ts=_BASE)
    anchor = _BASE + _DAY
    td = _mk_blind_task(tmp_path, "task-a", repo, anchor,
                        blind={"weakly_anchored_subtasks": ["s1"]},
                        results=[
                            {"subtask_id": "s1", "status": "completed",
                             "summary": "b.py | 2 ++"},
                            {"subtask_id": "s2", "status": "completed",
                             "summary": "a.py | 2 ++"},
                        ])
    (repo / "a.py").write_text("x = 9\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "human fix a only", date_ts=anchor + 2 * _DAY)
    r = compute_blind_spot_hit_rate([td], now=anchor + 20 * _DAY)
    # s1 的交付文件是 b.py，未被返工 → 非命中（近似锚点丢弃最旧=该 commit 恰为
    # 唯一触碰 commit 时按交付 merge 处理，两种解释下均不命中 s1）
    assert r["blind_spot_hits"] == 0
    assert r["blind_spot_judged"] == 1


def test_blind_spot_repo_gone_after_window_na(tmp_path):
    """repo 已删（bench/smoke 临时目录常见终局）→ N/A 终态排除（死挂起终态化，
    2026-08-29）：不可观察 ≠ 挂起，永久挂起只会稀释观察、误导排查。"""
    from agent_go.metrics import compute_blind_spot_hit_rate
    td = _mk_blind_task(tmp_path, "task-a", tmp_path / "no-such-repo",
                        _BASE, blind={"inconclusive_evaluations": ["s1"]},
                        results=[{"subtask_id": "s1", "status": "completed",
                                  "summary": "a.py | 2 ++"}])
    r = compute_blind_spot_hit_rate([td], now=_BASE + 30 * _DAY)
    assert r["blind_spot_na"] == 1
    assert r["blind_spot_pending"] == 0
    assert r["blind_spot_items"] == 0
    assert r["blind_spot_hit_rate"] is None


def test_blind_spot_na_no_associated_files(tmp_path):
    """repo 可达但关联文件全集为空（no_changes/空 diffstat）→ N/A 终态排除。"""
    import subprocess
    from agent_go.metrics import compute_blind_spot_hit_rate
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    t = _mk_task(tmp_path, "task-1", {
        "status": "completed",
        "repo": str(repo),
        "results": [{"subtask_id": "s1", "status": "completed", "summary": ""}],
        "goal_adherence": {"level": "full"},
        "blind_spots": {"uncovered_acceptance_ids": ["AC-1"]},
    })
    r = compute_blind_spot_hit_rate([t])
    assert r["blind_spot_na"] == 1
    assert r["blind_spot_items"] == 0
    assert r["blind_spot_pending"] == 0


# ═══════════════════════════════════════════════════════════════
# 交付后返工率（compute_post_delivery_rework，审查行为入流）
# ═══════════════════════════════════════════════════════════════

def _git(repo: Path, *args, date_ts=None):
    import os
    import subprocess
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    if date_ts is not None:
        env["GIT_AUTHOR_DATE"] = f"@{date_ts}"
        env["GIT_COMMITTER_DATE"] = f"@{date_ts}"
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, env=env)


def _mk_repo_with_file(tmp_path: Path, base_ts: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base", date_ts=base_ts)
    return repo


def _mk_delivered_task(base: Path, name: str, repo: Path, mtime_ts: int,
                       merge_commit: str = "", status: str = "completed") -> Path:
    import os
    td = base / name
    td.mkdir()
    meta = {
        "task_id": name, "status": status, "repo": str(repo),
        "results": [{"subtask_id": "s1", "status": "completed",
                     "summary": "a.py | 2 ++\n 1 file changed, 2 insertions(+)"}],
    }
    if merge_commit:
        meta["explicit_merge_commit"] = merge_commit
    mp = td / "meta.json"
    mp.write_text(_json.dumps(meta), encoding="utf-8")
    os.utime(mp, (mtime_ts, mtime_ts))
    return td


_BASE = 1755000000  # 2025-08-12 左右
_DAY = 86400


def test_rework_detected_after_approx_anchor(tmp_path):
    """近似锚点（meta mtime）：丢弃最旧触碰 commit（交付 merge），其余计返工。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_delivered_task(tmp_path, "task-a", repo, anchor)
    # 交付 merge（anchor+1d）+ 人工返工（anchor+2d）
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge agent work", date_ts=anchor + _DAY)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "human fix", date_ts=anchor + 2 * _DAY)
    now = anchor + 20 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["rework_eligible_tasks"] == 1
    assert r["reworked_tasks"] == 1
    assert r["post_delivery_rework_rate"] == 1.0
    assert r["reworked"][0]["commits"] == 1


def test_rework_only_merge_commit_not_counted(tmp_path):
    """近似锚点：仅交付 merge 一个触碰 commit → 不算返工。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_delivered_task(tmp_path, "task-a", repo, anchor)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge agent work", date_ts=anchor + _DAY)
    now = anchor + 20 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["rework_eligible_tasks"] == 1
    assert r["reworked_tasks"] == 0
    assert r["post_delivery_rework_rate"] == 0.0


def test_rework_explicit_merge_anchor_counts_all_after(tmp_path):
    """显式锚点（explicit_merge_commit）：锚点后的触碰 commit 全部计入（含第一个）。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "explicit delivery merge", date_ts=anchor)
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    td = _mk_delivered_task(tmp_path, "task-a", repo, anchor, merge_commit=merge_sha)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "human tweak", date_ts=anchor + _DAY)
    now = anchor + 20 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["reworked_tasks"] == 1
    assert r["reworked"][0]["commits"] == 1


def test_rework_agent_self_commit_excluded(tmp_path):
    """消息含 task_id 的 commit（agent 自身）不计返工。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_delivered_task(tmp_path, "task-a", repo, anchor)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge", date_ts=anchor + _DAY)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "follow-up for task-a", date_ts=anchor + 2 * _DAY)
    now = anchor + 20 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["reworked_tasks"] == 0


def test_rework_window_not_elapsed_excluded(tmp_path):
    """观察期不足 → 不进分母（rate=None）。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    anchor = _BASE + _DAY
    td = _mk_delivered_task(tmp_path, "task-a", repo, anchor)
    now = anchor + 5 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["rework_eligible_tasks"] == 0
    assert r["post_delivery_rework_rate"] is None


def test_rework_missing_repo_excluded(tmp_path):
    """仓库已删 → 不进分母（fail-open）。"""
    from agent_go.metrics import compute_post_delivery_rework
    td = _mk_delivered_task(tmp_path, "task-a", tmp_path / "ghost", _BASE)
    r = compute_post_delivery_rework([td], window_days=14, now=_BASE + 30 * _DAY)
    assert r["rework_eligible_tasks"] == 0
    assert r["post_delivery_rework_rate"] is None


def test_rework_non_delivered_status_excluded(tmp_path):
    """非交付态（failed/PAUSED）不进分母。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    td = _mk_delivered_task(tmp_path, "task-a", repo, _BASE, status="failed")
    r = compute_post_delivery_rework([td], window_days=14, now=_BASE + 30 * _DAY)
    assert r["rework_eligible_tasks"] == 0


def test_rework_dirname_anchor_preferred_over_mtime(tmp_path):
    """目录名时间戳锚点优先于 mtime（元数据迁移刷新 mtime 后窗口判定仍正确）。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    # 目录名时间戳 = 2025-08-12 12:00:00；mtime 故意设为「现在」（模拟元数据迁移刷新）
    import time
    anchor = _BASE + _DAY
    td = _mk_delivered_task(tmp_path, "task-20250812-120000-000-abcd", repo,
                            time.time())
    # 对齐：目录名时间戳应解析为 2025-08-12 12:00:00 ≈ anchor 附近
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "merge agent work", date_ts=anchor + _DAY)
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "human fix", date_ts=anchor + 2 * _DAY)
    # 若错误使用 mtime（现在），窗口会判「不足」→ eligible=0；
    # 正确使用目录名锚点 → eligible=1 且检出返工
    now = anchor + 20 * _DAY
    r = compute_post_delivery_rework([td], window_days=14, now=now)
    assert r["rework_eligible_tasks"] == 1
    assert r["reworked_tasks"] == 1


# ═══════════════════════════════════════════════════════════════
# D-0 观察窗口（select_recent_task_dirs / recent_window 口径）
# ═══════════════════════════════════════════════════════════════

def test_select_recent_task_dirs_by_dirname_ts(tmp_path):
    """按目录名时间戳取最近 N 个任务。"""
    from agent_go.metrics import select_recent_task_dirs
    td_old = _mk_task(tmp_path, "task-20250101-000000-000-aaaa", {"status": "completed"})
    td_mid = _mk_task(tmp_path, "task-20250601-000000-000-bbbb", {"status": "completed"})
    td_new = _mk_task(tmp_path, "task-20251201-000000-000-cccc", {"status": "completed"})
    recent = select_recent_task_dirs([td_old, td_mid, td_new], window=2)
    assert recent == [td_mid, td_new]
    recent1 = select_recent_task_dirs([td_old, td_mid, td_new], window=1)
    assert recent1 == [td_new]


def test_select_recent_task_dirs_mtime_fallback(tmp_path):
    """无目录名时间戳时回退 meta.json mtime。"""
    from agent_go.metrics import select_recent_task_dirs
    import time
    t1 = _mk_task(tmp_path, "task-1", {"status": "completed"})
    time.sleep(0.01)
    t2 = _mk_task(tmp_path, "task-2", {"status": "completed"})
    recent = select_recent_task_dirs([t1, t2], window=1)
    assert recent == [t2]


def test_select_recent_task_dirs_no_window(tmp_path):
    """window=None/<=0 返回全量（升序）。"""
    from agent_go.metrics import select_recent_task_dirs
    td_old = _mk_task(tmp_path, "task-20250101-000000-000-aaaa", {"status": "completed"})
    td_new = _mk_task(tmp_path, "task-20251201-000000-000-cccc", {"status": "completed"})
    assert len(select_recent_task_dirs([td_old, td_new], window=None)) == 2
    assert len(select_recent_task_dirs([td_old, td_new], window=0)) == 2


def test_trust_metrics_recent_window_filters_old(tmp_path):
    """recent_window 只统计最近 N 个任务（旧任务不稀释新信号）。"""
    from agent_go.metrics import compute_trust_metrics
    td_old = _mk_task(tmp_path, "task-20250101-000000-000-aaaa", {
        "results": [{"subtask_id": "s1", "status": "failed", "problem_id": "p-1"}]})
    td_new = _mk_task(tmp_path, "task-20251201-000000-000-cccc", {
        "results": [{"subtask_id": "s1", "status": "failed"}]})
    r = compute_trust_metrics([td_old, td_new], recent_window=1)
    assert r["failed_subtasks"] == 1
    assert r["recurrence_visibility_rate"] == 0.0  # 只有新任务（无 problem_id）
    r_all = compute_trust_metrics([td_old, td_new], recent_window=None)
    assert r_all["failed_subtasks"] == 2
    assert r_all["recurrence_visibility_rate"] == 0.5


def test_rework_recent_window_filters_old(tmp_path):
    """rework 的 recent_window 同样只统计最近 N 个任务。"""
    from agent_go.metrics import compute_post_delivery_rework
    repo = _mk_repo_with_file(tmp_path, _BASE)
    td_old = _mk_delivered_task(tmp_path, "task-20250101-000000-000-aaaa", repo, _BASE)
    td_new = _mk_delivered_task(tmp_path, "task-20251201-000000-000-cccc", repo, _BASE)
    # 两个都已满观察期（now 覆盖两个目录名锚点）；window=1 时只统计较新的一个
    now = _BASE + 200 * _DAY
    r = compute_post_delivery_rework([td_old, td_new], window_days=14, now=now,
                                     recent_window=1)
    assert r["rework_eligible_tasks"] == 1
