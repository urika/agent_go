"""规则集管线 P0 测试：受限 DSL / 清单 / 候选导入 / 离线复算（零网络、零 runtime）。"""
import json
from pathlib import Path

import pytest

from agent_go.rule_set import (
    RuleSyntaxError,
    append_decisions,
    candidate_to_rule,
    check_fields,
    eval_condition,
    generate_candidates,
    import_candidates,
    load_labeled_states,
    load_rules,
    main,
    make_rule,
    parse_condition,
    promote_rule,
    replay_report,
    retire_rule,
    rule_fires,
    rule_sha,
    save_rules,
    shadow_evaluate,
    upsert_rule,
    validate_rule,
    verify_frozen,
)


# ---------------------------------------------------------------------------
# DSL：解析
# ---------------------------------------------------------------------------

def test_parse_precedence_not_and_or():
    ast = parse_condition("not a == true and b >= 2 or c == 'x'")
    assert ast[0] == "or"
    left, right = ast[1]
    assert left[0] == "and"
    assert left[1][0][0] == "not"          # not 绑定最紧
    assert right == ("cmp", "c", "==", "x")


def test_parse_parens_override():
    ast = parse_condition("a == true and (b >= 2 or c == false)")
    assert ast[0] == "and"
    assert ast[1][1][0] == "or"


@pytest.mark.parametrize("bad", ["", "a", "a ==", "== 1", "(a == 1", "a == 1)", "a == 1 and", "'x' == a"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(RuleSyntaxError):
        parse_condition(bad)


def test_parse_accepts_dotted_paths_and_literals():
    assert parse_condition("change_stats.files_changed == 0")[1] == "change_stats.files_changed"
    assert parse_condition("kill_reason == 'infra'")[3] == "infra"
    assert parse_condition("degraded == true")[3] is True
    assert parse_condition("x != null")[3] is None


# ---------------------------------------------------------------------------
# DSL：三值求值（fail-open）
# ---------------------------------------------------------------------------

def test_eval_numeric_and_dotted_path():
    state = {"retry_count": 3, "change_stats": {"files_changed": 0}}
    assert eval_condition("retry_count >= 2", state) == (True, "")
    assert eval_condition("change_stats.files_changed == 0", state) == (True, "")
    assert eval_condition("retry_count < 2", state) == (False, "")


def test_eval_missing_field_is_unknown():
    result, reason = eval_condition("missing_field == 1", {"a": 1})
    assert result is None and "missing field" in reason


def test_eval_type_mismatch_is_unknown():
    result, reason = eval_condition("retry_count >= 2", {"retry_count": "many"})
    assert result is None and reason == "type mismatch"
    result, reason = eval_condition("retry_count == 2", {"retry_count": True})
    assert result is None and reason == "type mismatch"


def test_eval_three_valued_logic():
    state = {"a": True}
    assert eval_condition("a == true and b == 1", state)[0] is None     # 真 and 未知 = 未知
    assert eval_condition("a == true or b == 1", state)[0] is True      # 真 or 未知 = 真
    assert eval_condition("a == false and b == 1", state)[0] is False   # 假 and 未知 = 假
    assert eval_condition("a == false or b == 1", state)[0] is None     # 假 or 未知 = 未知
    assert eval_condition("not (b == 1)", state)[0] is None
    assert eval_condition("not (a == false)", state)[0] is True


def test_eval_null_and_string_ordering():
    assert eval_condition("x == null", {"x": None})[0] is True
    assert eval_condition("x != null", {"x": None})[0] is False
    result, reason = eval_condition("kill_reason > 'infra'", {"kill_reason": "stuck"})
    assert result is None and "ordering on strings" in reason


def test_eval_fail_open_on_bad_input():
    assert eval_condition("a == 1", None)[0] is None          # state 非 dict
    assert eval_condition("a ===", {"a": 1})[0] is None       # 语法错误不抛
    assert eval_condition("a == 1", {"a": object()})[0] is None  # 异常类型不抛


def test_rule_fires_only_on_true():
    rule = {"condition": "a == 1"}
    assert rule_fires(rule, {"a": 1})[0] is True
    assert rule_fires(rule, {"a": 2})[0] is False
    assert rule_fires(rule, {})[0] is False and "missing field" in rule_fires(rule, {})[1]


# ---------------------------------------------------------------------------
# 清单：sha / 校验 / 读写 / 流转
# ---------------------------------------------------------------------------

def test_rule_sha_stable_and_condition_sensitive():
    a = make_rule(rule_id="r-1", condition="a == 1")
    b = make_rule(rule_id="r-1", condition="a == 1")
    assert rule_sha(a) == rule_sha(b)
    c = make_rule(rule_id="r-1", condition="a == 2")
    assert rule_sha(a) != rule_sha(c)


def test_validate_rule_problems():
    assert validate_rule(make_rule(rule_id="ok-rule", condition="a == 1")) == []
    assert validate_rule({"rule_id": "X", "version": 0, "status": "bogus",
                          "stage": "nope", "condition": "a ==="}) != []
    bad = make_rule(rule_id="ok-rule-2", condition="a == 1")
    bad["cover"] = "not-an-object"
    assert any("cover" in p for p in validate_rule(bad))


def test_verify_frozen_detects_tamper():
    rule = make_rule(rule_id="r-tamper", condition="a == 1")
    assert verify_frozen(rule) is True
    rule["condition"] = "a == 2"
    assert verify_frozen(rule) is False


def test_load_save_round_trip_and_order(tmp_path):
    path = tmp_path / "rules.jsonl"
    rules = [make_rule(rule_id="b-rule", condition="b == 1"),
             make_rule(rule_id="a-rule", condition="a == 1")]
    save_rules(rules, path)
    loaded = load_rules(path)
    assert [r["rule_id"] for r in loaded] == ["a-rule", "b-rule"]
    save_rules(loaded, path)
    assert load_rules(path) == loaded
    assert load_rules(tmp_path / "missing.jsonl") == []


def test_upsert_dedup_and_version_bump():
    rules = [make_rule(rule_id="r-up", condition="a == 1")]
    assert upsert_rule(rules, make_rule(rule_id="r-up", condition="a == 1")) == "unchanged"
    assert len(rules) == 1
    assert upsert_rule(rules, make_rule(rule_id="r-up", condition="a == 2")) == "updated"
    assert rules[0]["version"] == 2 and rules[0]["status"] == "candidate"
    assert upsert_rule(rules, make_rule(rule_id="r-new", condition="b == 1")) == "added"


def test_promote_gate_blocks_until_holdout_ready():
    rule = make_rule(rule_id="r-gate", condition="a == 1")
    rules = [rule]
    ok, msg = promote_rule(rules, "r-gate", "active")
    assert ok is False and "holdout_n" in msg
    rule["metrics"] = {"holdout_n": 99, "holdout_sha": "x", "regression_ok": True}
    assert promote_rule(rules, "r-gate", "active")[0] is False
    rule["metrics"] = {"holdout_n": 100, "holdout_sha": "x", "regression_ok": False}
    assert promote_rule(rules, "r-gate", "active")[0] is False
    rule["metrics"] = {"holdout_n": 100, "holdout_sha": "x", "regression_ok": True}
    assert promote_rule(rules, "r-gate", "active") == (True, "ok")
    assert rules[0]["status"] == "active"
    assert promote_rule(rules, "r-gate", "shadow") == (True, "ok")
    assert promote_rule(rules, "nope", "shadow")[0] is False


def test_promote_to_active_requires_sha_updated():
    rule = make_rule(rule_id="r-gate-2", condition="a == 1",
                     metrics={"holdout_n": 120, "holdout_sha": "abc", "regression_ok": True})
    rules = [rule]
    assert promote_rule(rules, "r-gate-2", "active")[0] is True
    assert verify_frozen(rule) is True  # 状态流转后哈希同步刷新


def test_retire_records_reason():
    rules = [make_rule(rule_id="r-ret", condition="a == 1")]
    assert retire_rule(rules, "r-ret", "overfit") == (True, "ok")
    assert rules[0]["status"] == "retired"
    assert rules[0]["evidence"]["retire_reason"] == "overfit"
    assert retire_rule(rules, "nope")[0] is False


# ---------------------------------------------------------------------------
# 候选：导入 / 生成
# ---------------------------------------------------------------------------

def test_candidate_to_rule_from_feature_and_from_condition():
    r1 = candidate_to_rule({"feature": "retry_count", "op": ">=", "threshold": 2,
                            "precision": 0.8, "recall": 0.5, "cover_n": 4})
    assert r1["condition"] == "retry_count >= 2" and r1["status"] == "candidate"
    assert r1["evidence"]["cover_n"] == 4
    r2 = candidate_to_rule({"condition": "a == true", "rule_id": "my-rule"})
    assert r2["rule_id"] == "my-rule"
    with pytest.raises(ValueError):
        candidate_to_rule({"feature": "a", "op": "LIKE", "threshold": 1})


def test_import_candidates_counts(tmp_path):
    cand_path = tmp_path / "cand.jsonl"
    cand_path.write_text("\n".join([
        json.dumps({"condition": "a == 1"}),
        json.dumps({"condition": "a == 1"}),           # 去重
        json.dumps({"condition": "b == 1"}),
        json.dumps({"feature": "c", "op": "LIKE"}),    # 拒绝
        "not-json",
    ]) + "\n", encoding="utf-8")
    rules = []
    summary = import_candidates(cand_path, rules)
    assert summary == {"added": 2, "updated": 0, "unchanged": 1, "rejected": 2}
    assert len(rules) == 2


def test_generate_candidates_finds_planted_rule_and_respects_thresholds():
    labeled = []
    for i in range(6):
        labeled.append(("pos-%d" % i, {"files_changed": 0, "retry_count": 1}, "content_fix"))
    for i in range(4):
        labeled.append(("neg-%d" % i, {"files_changed": 3, "retry_count": 0}, "infra_or_process"))
    labeled.append(("und-0", {"files_changed": 0}, "undecidable"))  # 不得进入生成
    cands = generate_candidates(labeled, min_cover=3, min_precision=0.9)
    conditions = [c["condition"] for c in cands]
    assert "files_changed == 0" in conditions
    best = [c for c in cands if c["condition"] == "files_changed == 0"][0]
    assert best["precision"] == 1.0 and best["tp"] == 6 and best["cover_n"] == 6
    assert generate_candidates([], min_cover=1) == []
    # 确定性
    assert generate_candidates(labeled, min_cover=3, min_precision=0.9) == cands


# ---------------------------------------------------------------------------
# 离线复算报告
# ---------------------------------------------------------------------------

def _write_dataset(tmp: Path, states, labels):
    sdir = tmp / "state"
    sdir.mkdir(exist_ok=True)
    for ref, state in states.items():
        (sdir / ("%s.json" % ref)).write_text(json.dumps(state), encoding="utf-8")
    lpath = tmp / "labels.jsonl"
    lpath.write_text("\n".join(json.dumps({"run_ref": ref, "label": lb, "origin": "human"})
                               for ref, lb in labels.items()) + "\n", encoding="utf-8")
    return sdir, lpath


def test_load_labeled_states_filters_non_human(tmp_path):
    sdir, lpath = _write_dataset(tmp_path,
                                 {"r1": {"a": 1}, "r2": {"a": 2}},
                                 {"r1": "content_fix"})
    with lpath.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_ref": "r2", "label": "infra_or_process",
                             "origin": "prelabel"}) + "\n")
    rows = load_labeled_states(sdir, lpath)
    assert [(ref, lb) for ref, _s, lb in rows] == [("r1", "content_fix"), ("r2", "")]


def test_replay_report_exact_numbers_and_determinism(tmp_path):
    states = {f"p{i}": {"x": 0} for i in range(3)}
    states.update({f"n{i}": {"x": 5} for i in range(2)})
    states["u0"] = {"x": 0}
    labels = {f"p{i}": "content_fix" for i in range(3)}
    labels.update({f"n{i}": "infra_or_process" for i in range(2)})
    labels["u0"] = "undecidable"
    sdir, lpath = _write_dataset(tmp_path, states, labels)
    rule = make_rule(rule_id="r-replay", condition="x == 0")
    report = replay_report([rule], load_labeled_states(sdir, lpath))
    assert report["samples"] == 6
    row = report["rules"][0]
    assert (row["tp"], row["fp"], row["fn"], row["tn"]) == (3, 0, 0, 2)
    assert row["precision"] == 1.0 and row["recall"] == 1.0 and row["fired"] == 4
    assert report["union"]["tp"] == 3
    again = replay_report([rule], load_labeled_states(sdir, lpath))
    assert json.dumps({k: v for k, v in report.items() if k != "generated_at"}, sort_keys=True) == \
           json.dumps({k: v for k, v in again.items() if k != "generated_at"}, sort_keys=True)


def test_replay_excludes_undecidable_and_reports_unknown_fields(tmp_path):
    states = {"a": {"x": 0}, "b": {"y": 1}}
    labels = {"a": "content_fix", "b": "undecidable"}
    sdir, lpath = _write_dataset(tmp_path, states, labels)
    rule = make_rule(rule_id="r-unknown", condition="x == 0 or z == 9")
    report = replay_report([rule], load_labeled_states(sdir, lpath))
    assert report["rules"][0]["labeled"] == 1
    assert "missing field" in report["unknown_field_hits"]


def test_check_fields_reports_unreachable():
    rule = make_rule(rule_id="r-fields", condition="a == 1 and b.c == 2")
    states = [{"a": 1}, {"a": 2, "b": {"c": 3}}]
    assert check_fields(rule, states) == []
    assert check_fields(make_rule(rule_id="r-fields-2", condition="nope == 1"), states) == ["nope"]


# ---------------------------------------------------------------------------
# 影子执行（P1 接入点；P0 纯函数）
# ---------------------------------------------------------------------------

def test_shadow_evaluate_filters_stage_and_status_and_appends(tmp_path):
    rules = [make_rule(rule_id="s-1", condition="a == 1", stage="review", status="shadow"),
             make_rule(rule_id="s-2", condition="a == 1", stage="plan", status="shadow"),
             make_rule(rule_id="s-3", condition="a == 1", stage="review", status="candidate")]
    decisions = shadow_evaluate(rules, {"a": 1}, "review", now="2026-10-05T00:00:00")
    assert [d["rule_id"] for d in decisions] == ["s-1"] and decisions[0]["result"] is True
    path = append_decisions(tmp_path, decisions)
    assert path is not None and path.name == "rule_decisions.jsonl"
    assert json.loads(path.read_text(encoding="utf-8").strip())["rule_id"] == "s-1"
    assert append_decisions(tmp_path, []) is None


# ---------------------------------------------------------------------------
# CLI（P0 形态：python3 -m agent_go.rule_set）
# ---------------------------------------------------------------------------

def test_cli_list_validate_and_promote_gate(tmp_path, capsys):
    path = tmp_path / "rules.jsonl"
    save_rules([make_rule(rule_id="cli-rule", condition="a == 1")], path)
    assert main(["--rules", str(path), "list"]) == 0
    assert "cli-rule" in capsys.readouterr().out
    assert main(["--rules", str(path), "validate"]) == 0
    assert main(["--rules", str(path), "promote", "cli-rule", "--to", "active"]) == 1
    assert main(["--rules", str(path), "promote", "cli-rule", "--to", "shadow"]) == 0
    assert main(["--rules", str(path), "show", "nope"]) == 1
    assert main(["--rules", str(path), "retire", "cli-rule", "--reason", "test"]) == 0
    assert load_rules(path)[0]["status"] == "retired"


def test_cli_validate_catches_tamper(tmp_path):
    path = tmp_path / "rules.jsonl"
    save_rules([make_rule(rule_id="t-rule", condition="a == 1")], path)
    rule = load_rules(path)[0]
    rule["condition"] = "a == 2"  # 冻结后手改，sha 不再匹配
    path.write_text(json.dumps(rule) + "\n", encoding="utf-8")
    assert main(["--rules", str(path), "validate"]) == 1


def test_cli_generate_and_replay(tmp_path):
    states = {"p1": {"x": 0}, "p2": {"x": 0}, "p3": {"x": 0}, "n1": {"x": 9}}
    labels = {"p1": "content_fix", "p2": "content_fix", "p3": "content_fix",
              "n1": "infra_or_process"}
    sdir, lpath = _write_dataset(tmp_path, states, labels)
    cand_out = tmp_path / "cand.jsonl"
    assert main(["generate", "--states-dir", str(sdir), "--labels", str(lpath),
                 "--out", str(cand_out), "--min-cover", "3", "--min-precision", "0.9"]) == 0
    cands = [json.loads(line) for line in cand_out.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert any(c["condition"] == "x == 0" for c in cands)
    rules_path = tmp_path / "rules.jsonl"
    assert main(["--rules", str(rules_path), "import-candidates", "--candidates", str(cand_out)]) == 0
    report_out = tmp_path / "report.json"
    assert main(["--rules", str(rules_path), "replay", "--states-dir", str(sdir),
                 "--labels", str(lpath), "--out", str(report_out)]) == 0
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["samples"] == 4 and report["rules"]
