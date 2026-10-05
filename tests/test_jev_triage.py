"""jev 复核排序仪器（薄版）测试——零外发、合成数据。

冻结纪律：题面 hash 被钉死——任何改动 FROZEN_QUESTIONS 的行为都会让本文件变红
（对应需求文档 §6.2/§17：改题面＝重开一轮）。
"""
import json
from pathlib import Path

from tools.jev_triage import (
    FROZEN_QUESTIONS,
    WHITELIST_TOP_KEYS,
    _extract_answers,
    build_state,
    cluster_bootstrap,
    coarse_hit,
    control_base_rate,
    effort_at_recall,
    lint_questions,
    main,
    precision_at_k,
    questions_sha256,
    rank_entries,
    run_ref_for,
    sanitize,
    spearman_mean_rank,
)

# 2026-10-05 v1.1 冻结值（含 Q3 noul 第二排序器）：改动题面即红（重开一轮）
FROZEN_QUESTIONS_SHA256 = "700e07ea111bd0a90e8fe8da1b362ed2241d082d0f7f591c537e9406e38bc667"


# ---------------------------------------------------------------------------
# 冻结题面与指纹
# ---------------------------------------------------------------------------

def test_frozen_questions_hash_pinned():
    assert questions_sha256() == FROZEN_QUESTIONS_SHA256


def test_frozen_questions_english_and_structured():
    blob = json.dumps(FROZEN_QUESTIONS, ensure_ascii=False)
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in blob)
    criteria = FROZEN_QUESTIONS["review_class"]["criteria"]
    assert set(criteria) == {"content_fix", "infra_or_process", "insufficient_evidence"}
    for spec in criteria.values():
        assert "what" in spec and "not_for" in spec
    assert set(FROZEN_QUESTIONS["control_change_nonempty"]["criteria"]) == {"true", "false"}
    # v1.1：Q3 第二排序器（noul，英文，true/false criteria）
    assert set(FROZEN_QUESTIONS) == {"review_class", "control_change_nonempty", "ranking_noul"}
    q3 = FROZEN_QUESTIONS["ranking_noul"]
    assert q3["type"] == "noul" and set(q3["criteria"]) == {"true", "false"}
    assert "root cause" in q3["instructions"]["question"]


def test_lint_accepts_frozen_questions():
    assert lint_questions(FROZEN_QUESTIONS) == []


def test_lint_rejects_cjk_and_missing_not_for():
    bad = json.loads(json.dumps(FROZEN_QUESTIONS))
    bad["review_class"]["criteria"]["content_fix"]["what"] = "根因在内容"
    assert any("语言门" in p for p in lint_questions(bad))
    bad2 = json.loads(json.dumps(FROZEN_QUESTIONS))
    del bad2["review_class"]["criteria"]["content_fix"]["not_for"]
    assert any("not_for" in p for p in lint_questions(bad2))


# ---------------------------------------------------------------------------
# 脱敏与 state
# ---------------------------------------------------------------------------

def test_sanitize_paths_urls_hex_email():
    text = ("see /Users/bob/repo/src/a.py and /tmp/x.log "
            "http://example.com/a mailto bob@example.com "
            "commit 0123456789abcdef0123456789abcdef01234567")
    out = sanitize(text)
    assert "/Users/bob" not in out and "/tmp/" not in out
    assert "example.com" not in out and "bob@example.com" not in out
    assert "0123456789abcdef0123456789abcdef01234567" not in out
    assert "<path>" in out and "<url>" in out and "<sha>" in out


def test_sanitize_replaces_repo_worktree_task():
    out = sanitize("at /custom/repo/wt/sub and task-abc",
                   repo_path="/custom/repo", worktree="/custom/repo/wt", task_id="task-abc")
    assert "/custom/repo" not in out and "task-abc" not in out


def _sample_result(**over):
    base = {
        "subtask_id": "sub-1", "status": "failed", "exit_code": 1, "verify_ok": False,
        "retry_count": 1, "kill_reason": None, "degraded": False, "loop_detected": False,
        "crash_but_verified": False, "duration_sec": 12.5,
        "timing": {"claude_execute_ms": 12000, "verification_ms": 500},
        "change_stats": {"files_changed": 2, "insertions": 10, "deletions": 1,
                         "new_files": 0, "modified_files": 2},
        "verification_confidence": {"level": "deterministic", "anchoring": "file",
                                    "warning": "see /Users/bob/warn"},
        "verification_results": [
            {"command": "python -m pytest /tmp/t.py -q", "exit_code": 1, "attempt": 1,
             "stdout_tail": "FAILED at /Users/bob/t.py", "stderr_tail": ""}
        ],
        "failure_reason": "assert failed at /Users/bob/repo/a.py",
    }
    base.update(over)
    return base


def test_build_state_keys_subset_whitelist_and_control():
    state = build_state(_sample_result(), run_ref="abc123", signals=None,
                        repo_path="/Users/bob/repo", worktree="/Users/bob/repo/wt", task_id="task-abc")
    assert set(state) <= WHITELIST_TOP_KEYS
    assert state["control"]["change_nonempty"] is True
    blob = json.dumps(state)
    assert "/Users/" not in blob and "task-abc" not in blob


def test_build_state_kill_reason_normalized():
    state = build_state(_sample_result(kill_reason="stuck"), run_ref="r", signals=None,
                        repo_path=None, worktree=None, task_id=None)
    assert state["kill_reason"] == "stuck"
    state2 = build_state(_sample_result(kill_reason="wild_new_reason"), run_ref="r", signals=None,
                         repo_path=None, worktree=None, task_id=None)
    assert state2["kill_reason"] == "other"


def test_build_state_trajectory_counts():
    signals = {"path_violations": ["/a", "/b"], "repeated_edits": [{"file": "x", "count": 4}],
               "file": "x", "count": 4, "steps": 9, "tool_calls": 3, "tool_errors": 1,
               "mutations": 2, "mutation_without_worktree_change": False}
    state = build_state(_sample_result(), run_ref="r", signals=signals,
                        repo_path=None, worktree=None, task_id=None)
    assert state["trajectory"]["path_violation_n"] == 2
    assert state["trajectory"]["repeated_edit_max"] == 4
    assert state["trajectory"]["repeated_edit_files_n"] == 1


def test_run_ref_stable():
    a = run_ref_for("task-1", "sub-1")
    assert a == run_ref_for("task-1", "sub-1") and len(a) == 16


# ---------------------------------------------------------------------------
# 规则与统计
# ---------------------------------------------------------------------------

def test_coarse_hit_rule():
    assert coarse_hit({"loop_detected": True})
    assert coarse_hit({"retry_count": 2})
    assert coarse_hit({"kill_reason": "hard_timeout"})
    assert coarse_hit({"change_stats": {"files_changed": 0}})
    assert not coarse_hit({"retry_count": 1, "kill_reason": "none", "change_stats": {"files_changed": 3}})


def test_control_base_rate():
    states = [{"control": {"change_nonempty": v}} for v in (True, False, True)]
    assert control_base_rate(states) == (2, 1)


def test_effort_at_recall_handcalc():
    ranked = ["a", "b", "c", "d"]
    targets = {"a": 0, "b": 1, "c": 0, "d": 1}
    assert effort_at_recall(ranked, targets, 0.5) == 2
    assert effort_at_recall(ranked, targets, 1.0) == 4
    assert effort_at_recall(ranked, {"a": 0}, 0.5) is None


def test_precision_at_k():
    ranked = ["a", "b", "c"]
    targets = {"a": 1, "b": 0, "c": 1}
    assert precision_at_k(ranked, targets, 2) == 0.5


def test_spearman_mean_rank_ties():
    assert spearman_mean_rank([1, 2, 3], [1, 2, 3]) == 1.0
    assert spearman_mean_rank([1, 2, 3], [3, 2, 1]) == -1.0
    # 并列取平均秩后仍应接近 0.5
    rho = spearman_mean_rank([1, 1, 2, 3], [1, 2, 3, 4])
    assert rho is not None and -1.0 <= rho <= 1.0


def test_cluster_bootstrap_deterministic():
    clusters = [[1.0, 0.0], [1.0], [0.0, 0.0]]
    metric = lambda seq: sum(seq) / len(seq)  # noqa: E731
    a = cluster_bootstrap(clusters, metric, rounds=200, seed=7)
    b = cluster_bootstrap(clusters, metric, rounds=200, seed=7)
    assert a == b and abs(a[0] - 0.4) < 1e-9  # 展平 2/5


def test_rank_entries_order_abstain_and_tau():
    results = [
        {"run_ref": "b", "response": {"result": {"structuredContent": {"answers": {
            "review_class": {"choice": "content_fix",
                             "probabilities": {"content_fix": 0.7, "infra_or_process": 0.2,
                                               "insufficient_evidence": 0.1}}}}}}},
        {"run_ref": "a", "response": {"result": {"structuredContent": {"answers": {
            "review_class": {"choice": "content_fix",
                             "probabilities": {"content_fix": 0.7, "infra_or_process": 0.2,
                                               "insufficient_evidence": 0.1}}}}}}},
        {"run_ref": "c", "response": {"result": {"structuredContent": {"answers": {
            "review_class": {"choice": "insufficient_evidence",
                             "probabilities": {"content_fix": 0.2, "infra_or_process": 0.3,
                                               "insufficient_evidence": 0.5}}}}}}},
        {"run_ref": "d", "response": {"result": {"structuredContent": {"answers": {
            "review_class": {"choice": "infra_or_process",
                             "probabilities": {"content_fix": 0.3, "infra_or_process": 0.4,
                                               "insufficient_evidence": 0.3}}}}}}},
    ]
    rows = rank_entries(results, {}, tau=0.6)
    assert [r["run_ref"] for r in rows] == ["a", "b", "c", "d"]  # 并列按 ref；弃权垫底（按 ref 稳定）
    assert rows[2]["abstain"] is True  # c: 显式弃权
    assert rows[3]["abstain"] is True  # d: P_max 0.4 < 0.6
    assert _extract_answers(results[0]["response"])["answers"]["review_class"]["choice"] == "content_fix"
    assert rows[0]["noul_score"] is None  # 该 fixture 无 Q3 → 不臆造分数


def test_rank_entries_extracts_noul_second_ranker():
    results = [
        {"run_ref": "a", "response": {"result": {"structuredContent": {"answers": {
            "review_class": {"choice": "content_fix", "probabilities": {
                "content_fix": 0.5, "infra_or_process": 0.4, "insufficient_evidence": 0.1}},
            "ranking_noul": {"noul": 0.83}}}}}},
    ]
    rows = rank_entries(results, {}, tau=0.6)
    assert rows[0]["noul_score"] == 0.83


# ---------------------------------------------------------------------------
# 端到端（合成批 → build → check → packets → record → analyze → queue）
# ---------------------------------------------------------------------------

def _write_task(tmp: Path, idx: int, files_changed: int) -> Path:
    task_dir = tmp / f"task-20261005-0000{idx}-000-{idx:04d}"
    (task_dir / f"sub-{idx}").mkdir(parents=True)
    repo = tmp / "fixture-repo"
    result = _sample_result(
        subtask_id=f"sub-{idx}",
        change_stats={"files_changed": files_changed, "insertions": 3, "deletions": 0,
                      "new_files": 0, "modified_files": files_changed},
        worktree="",
    )
    meta = {"task_id": task_dir.name, "base_commit": "", "repo": str(repo), "results": [result]}
    (task_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (task_dir / f"sub-{idx}" / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return task_dir


def _mcp_response(p_content: float, noul: float, noul_rank: float = None) -> dict:
    return {"result": {"structuredContent": {
        "model": "jev-1.13.0",
        "answers": {
            "review_class": {"type": "choice", "choice": "content_fix", "confidence": 0.8,
                             "probabilities": {"content_fix": p_content,
                                               "infra_or_process": max(0.0, 0.9 - p_content),
                                               "insufficient_evidence": 0.1}},
            "control_change_nonempty": {"type": "noul", "noul": noul},
            "ranking_noul": {"type": "noul",
                             "noul": p_content if noul_rank is None else noul_rank},
        }}}}


def test_end_to_end_pilot(tmp_path):
    out = tmp_path / "pilot"
    # 6 条 failed：3 条有变更、3 条空变更（对照基率两类各 ≥3）
    bench = tmp_path / "results_batch.jsonl"
    lines = []
    for i, fc in enumerate([3, 2, 1, 0, 0, 0], start=1):
        td = _write_task(tmp_path, i, fc)
        lines.append(json.dumps({"task_dir": str(td), "task_id": td.name, "binary_pass": False}))
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--build", "--out", str(out), "--results", str(bench), "--stage", "pilot"]) == 0
    assert main(["--check", "--out", str(out), "--stage", "pilot"]) == 0
    assert main(["--packets", "--out", str(out)]) == 0

    pool = [json.loads(x) for x in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(pool) == 6
    # state 不含禁入项（build 已脱敏）
    for entry in pool:
        blob = (out / "state" / f"{entry['run_ref']}.json").read_text(encoding="utf-8")
        assert "/Users/" not in blob

    # 记录调用回包：正变更组 P(content)=0.9/0.8/0.7；空变更组 0.3/0.2/0.1
    positive_refs, negative_refs = [], []
    for entry in pool:
        state = json.loads((out / "state" / f"{entry['run_ref']}.json").read_text(encoding="utf-8"))
        (positive_refs if state["control"]["change_nonempty"] else negative_refs).append(entry["run_ref"])
    scores = {}
    for pos_ref, pos_p in zip(positive_refs, [0.9, 0.8, 0.7]):
        scores[pos_ref] = (pos_p, 0.9)
    for neg_ref, neg_p in zip(negative_refs, [0.3, 0.2, 0.1]):
        scores[neg_ref] = (neg_p, 0.1)
    for ref, (p, noul) in scores.items():
        resp_file = tmp_path / f"resp_{ref}.json"
        resp_file.write_text(json.dumps(_mcp_response(p, noul)), encoding="utf-8")
        assert main(["--record", ref, "--response", str(resp_file), "--out", str(out)]) == 0

    # 人工标签：3 内容型 + 3 环境型
    labels = []
    for ref in positive_refs:
        labels.append({"run_ref": ref, "label": "content_fix", "origin": "human", "minutes": 6})
    for ref in negative_refs:
        labels.append({"run_ref": ref, "label": "infra_or_process", "origin": "human", "minutes": 6})
    (out / "labels.jsonl").write_text("\n".join(json.dumps(x) for x in labels) + "\n", encoding="utf-8")

    assert main(["--analyze", "--out", str(out), "--stage", "pilot"]) == 0
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    gates = analysis["pilot_gates"]
    assert gates["h6"] is True and gates["target_variance"]["pass"] is True
    assert gates["alignment"]["pass"] is True and gates["coarse_selectivity"]["pass"] is True
    assert analysis["verdict"] == "M0.5-进入 M1"

    assert main(["--analyze", "--out", str(out), "--stage", "full"]) == 0
    full = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    effort = full["metrics"]["effort_at_recall_0.5"]
    assert effort["jev"] == 2 and effort["coarse"] == 5  # jev 优于粗规则
    assert full["verdict"].startswith("Go")
    # v1.1：Q3 noul 第二排序器（同覆盖口径、只作探索）
    noul = full["exploratory"]["noul_ranker"]
    assert noul["available"] == 6 and noul["effort_at_recall_0.5"] == 2
    assert noul["spearman_noul_vs_label"] is not None and noul["spearman_noul_vs_label"] > 0

    assert main(["--queue", "--out", str(out)]) == 0
    queue_md = (out / "review-queue.md").read_text(encoding="utf-8")
    assert "| 1 |" in queue_md and positive_refs[0] in queue_md


def test_check_fails_on_tampered_state(tmp_path):
    out = tmp_path / "pilot"
    bench = tmp_path / "results_batch.jsonl"
    lines = []
    for i, fc in enumerate([1, 1, 1, 0, 0, 0], start=1):
        td = _write_task(tmp_path, i, fc)
        lines.append(json.dumps({"task_dir": str(td), "task_id": td.name, "binary_pass": False}))
    bench.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["--build", "--out", str(out), "--results", str(bench), "--stage", "pilot"]) == 0
    state_file = sorted((out / "state").glob("*.json"))[0]
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["failure_reason"] = "leaked /Users/bob/private"
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    assert main(["--check", "--out", str(out), "--stage", "pilot"]) == 1


def test_analyze_refuses_incomplete_calls(tmp_path):
    out = tmp_path / "pilot"
    bench = tmp_path / "results_batch.jsonl"
    td = _write_task(tmp_path, 1, 1)
    bench.write_text(json.dumps({"task_dir": str(td), "task_id": td.name, "binary_pass": False}) + "\n",
                     encoding="utf-8")
    assert main(["--build", "--out", str(out), "--results", str(bench), "--stage", "pilot"]) == 0
    assert main(["--analyze", "--out", str(out), "--stage", "pilot"]) == 3  # 调用 0/1 → 拒绝出判据
