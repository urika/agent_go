"""S-1 P0 离线审计脚本测试——合成数据、零外发。"""
import json

from tools.s1_coverage_audit import (
    artifact_checks,
    audit,
    by_file,
    by_status,
    coverage_split,
    lint_hypothesis,
    load_records,
    main,
    mantel_haenszel,
    stratified_by_difficulty,
    stratified_contrast,
    warning_mediation,
)


def _rec(**over):
    base = {
        "binary_pass": True, "plan_quality_status": "passed", "plan_warning_count": 0,
        "plan_conflict_count": 0, "lint_errors": 1, "risk_types": ["r1"],
        "difficulty": "easy", "total_retries": 0,
    }
    base.update(over)
    return base


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_coverage_split_counts_present_vs_gap():
    records = [
        _rec(), _rec(binary_pass=False),                     # present, 1 fail
        _rec(plan_quality_status=None, total_retries=2),     # gap, pass
        _rec(plan_quality_status=None, binary_pass=False, total_retries=2),  # gap, fail
    ]
    out = coverage_split(records)
    assert out["rules_present"] == {"n": 2, "fails": 1, "fail_pct": 50.0, "mean_retries": 0.0}
    assert out["rule_gap"]["n"] == 2 and out["rule_gap"]["fail_pct"] == 50.0
    assert out["rule_gap"]["mean_retries"] == 2.0


def test_by_status_marks_blocked_mechanical():
    records = [_rec(), _rec(plan_quality_status="blocked", binary_pass=False)]
    out = by_status(records)
    assert out["blocked"]["mechanical"] is True and out["passed"]["mechanical"] is False
    assert out["blocked"]["fail_pct"] == 100.0


def test_by_file_respects_min_thresholds():
    records = []
    for i in range(12):
        records.append(_rec(_file="big.jsonl", binary_pass=(i % 2 == 0)))
        records.append(_rec(_file="big.jsonl", plan_quality_status="warning", binary_pass=(i % 3 == 0)))
    records += [_rec(_file="small.jsonl")]
    for r in records:
        r.setdefault("_file", "big.jsonl")
    out = by_file(records, min_n=20, min_cell_n=10)
    assert "small.jsonl" not in out
    assert "big.jsonl" in out and set(out["big.jsonl"]) == {"passed", "warning"}
    out2 = by_file(records, min_n=5, min_cell_n=10)
    assert "big.jsonl" in out2  # 24 条 ≥5，两个单元各 12 条


def test_stratified_by_difficulty():
    records = [
        _rec(difficulty="easy", binary_pass=False),  # present/easy fail
        _rec(difficulty="easy"),                     # present/easy pass
        _rec(plan_quality_status=None, difficulty="easy", binary_pass=False),  # gap/easy fail
    ]
    out = stratified_by_difficulty(records)
    assert out["rules_present"]["tiers"]["easy"] == {"n": 2, "fail_pct": 50.0}
    assert out["rule_gap"]["tiers"]["easy"] == {"n": 1, "fail_pct": 100.0}
    assert out["rule_gap"]["difficulty_dist"] == {"easy": 1}


def test_artifact_checks_flag_lint_anomaly_and_empty_fields():
    records = [
        _rec(lint_errors=0, binary_pass=False),
        _rec(lint_errors=3),
        _rec(plan_quality_status=None, plan_warning_count=None, risk_types=[], difficulty="hard"),
    ]
    checks = artifact_checks(records)
    assert checks["lint_errors_zero"]["n"] == 1
    assert "定案" in checks["lint_errors_zero"]["verdict"]
    assert checks["plan_warning_buckets"]["1-2"]["n"] == 0
    gaps = checks["rule_field_gaps"]
    assert gaps["total"] == 3
    assert "plan_acceptance_coverage" in gaps["never_emitted"]  # 合成集里从未出现


def test_mantel_haenszel_pools_odds_ratio():
    # 单层 (9,1,1,9)：OR = 81，p 很小
    out = mantel_haenszel([(9, 1, 1, 9)])
    assert out["or"] == 81.0 and out["p"] < 0.01 and out["strata"] == 1
    # 空表/退化表返回 None
    assert mantel_haenszel([])["or"] is None
    assert mantel_haenszel([(1, 0, 0, 0)])["p"] is None  # n=1 被跳过


def test_stratified_contrast_controls_model():
    records = []
    for model in ("m1", "m2"):
        for i in range(10):  # 每层：缺口组 8 失败/2 通过；有输出组 2 失败/8 通过
            records.append(_rec(model=model, plan_quality_status=None, binary_pass=(i >= 8)))
            records.append(_rec(model=model, plan_quality_status="passed", binary_pass=(i >= 2)))
    out = stratified_contrast(
        records, lambda r: str(r.get("model")),
        lambda r: r.get("plan_quality_status") is None,
        lambda r: r.get("plan_quality_status") == "passed", min_cell=5)
    assert out["pooled"]["kept_strata"] == 2
    assert out["pooled"]["or"] > 1 and out["pooled"]["p"] < 0.05


def test_lint_hypothesis_resolves_early_failure_artifact():
    zero = [_rec(lint_errors=0, completed=0, total_subtasks=0, binary_pass=False) for _ in range(8)]
    zero += [_rec(lint_errors=0, completed=1, total_subtasks=1) for _ in range(2)]
    nonzero = [_rec(lint_errors=2, completed=1, total_subtasks=2) for _ in range(5)]
    out = lint_hypothesis(zero + nonzero)
    assert out["lint_zero"]["completed_missing_or_0_pct"] == 80.0
    assert "伪迹成立" in out["verdict"]


def test_warning_mediation_detects_floor_file_mix():
    floor = [_rec(_file="floor.jsonl", plan_warning_count=3, binary_pass=True) for _ in range(20)]
    other = [_rec(_file="other.jsonl", plan_warning_count=3, binary_pass=False) for _ in range(20)]
    out = warning_mediation(floor + other, top_k=1)
    assert out["top_files_w3"] == {"floor.jsonl": 20}
    assert out["all"]["3+"]["fail_pct"] == 50.0
    assert out["excluding_top_files"]["3+"]["fail_pct"] == 100.0


def test_load_records_skips_files_without_schema(tmp_path):
    _write(tmp_path, "junk.jsonl", [{"foo": 1}])
    good = _write(tmp_path, "results_x.jsonl", [_rec()])
    records, files = load_records([str(tmp_path)])
    assert len(records) == 1 and files == [str(good)]


def test_main_json_mode(tmp_path, capsys):
    _write(tmp_path, "results_a.jsonl", [_rec(), _rec(binary_pass=False, plan_quality_status=None)])
    assert main(["--input", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dataset"]["records"] == 2
    assert report["coverage_split"]["rule_gap"]["n"] == 1


def test_main_text_mode_and_empty_input(tmp_path, capsys):
    _write(tmp_path, "results_a.jsonl", [_rec()])
    assert main(["--input", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "S-1 P0 离线审计" in text and "规则有输出" in text
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--input", str(empty)]) == 2


def test_audit_integration_shape():
    records = [_rec(_file="f.jsonl"), _rec(_file="f.jsonl", binary_pass=False)]
    report = audit(records, ["f.jsonl"], min_n=1, min_cell_n=1)
    assert set(report) == {"dataset", "coverage_split", "by_status", "by_file",
                           "stratified_by_difficulty", "stratified",
                           "lint_hypothesis", "warning_mediation", "artifact_checks"}
