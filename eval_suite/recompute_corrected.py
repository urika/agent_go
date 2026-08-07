#!/usr/bin/env python3
"""离线重算：对现有 bench 结果套用 S12-P0 修正口径，验证度量修复的预期效果。

这是 S12-P0 的前置验证（Step 0）——不改任何产品代码，只用现有 v2/v3/v4 数据
推演"修了 _collect_result 之后 KPI 会变成什么"，把"v3 34%→~67%"这类论断用真实
数据坐实。若重算结果与设计文档预期不符，S12-P0 方向需先调整。

修正口径（对应 design/bench-metric-validity-2026-08-06.md 的缺陷 1/3/4）：
  - cleanup_race 计为通过：per_subtask 全部 completed+verify_ok 即算任务成功
    （即便顶层 pass_rate 因收尾超时被 zeroed）。
  - binary_pass 修 all([])：无子任务时不再 vacuously True。
  - pass_rate 分母/分子用 per_subtask 真实状态重算（completed+verified / total）。
  - $/pass 用修正后 pass_rate 作分母。
  - kill_reason 离线推导（尽力，受限于历史数据字段）。

用法： python3 eval_suite/recompute_corrected.py
"""
import json
from pathlib import Path

EVAL = Path(__file__).parent
FILES = {
    "v2": EVAL / "results_v2.jsonl",
    "v3": EVAL / "results_v3.jsonl",
    "v4_calib": EVAL / "results_v4_calib.jsonl",
}


def load(f):
    return [json.loads(l) for l in open(f) if l.strip()]


def correct_record(r):
    """返回单条记录的修正口径指标 + 推导的 kill_reason。"""
    ps = r.get("per_subtask") or []
    has_ps = bool(ps)
    # per_subtask 真实状态
    verified = [p for p in ps if p.get("status") == "completed" and p.get("verify_ok")]
    sub_all_done_verified = has_ps and len(verified) == len(ps) and len(ps) > 0
    any_done = len(verified) > 0

    # 修正 pass_rate（用 per_subtask；v2 无 per_subtask 时回退原值）
    if has_ps:
        corr_pr = (len(verified) / len(ps)) if ps else 0.0
    else:
        corr_pr = r.get("pass_rate", 0) or 0.0

    # 任务级"成功"（cleanup_race 计入）
    headline_pass = (r.get("pass_rate", 0) or 0) > 0
    corrected_pass = headline_pass or sub_all_done_verified

    # 修正 binary_pass（修 all([])：空集→False）
    if has_ps:
        corr_binary = sub_all_done_verified  # 全完成已验证才算
    else:
        corr_binary = headline_pass  # 无 per_subtask 只能回退

    # kill_reason 离线推导
    timed_out = bool(r.get("timed_out"))
    cost = r.get("total_cost_usd") or 0
    if corrected_pass:
        kr = "cleanup_race" if (timed_out and sub_all_done_verified and not headline_pass) else "none"
    elif timed_out:
        kr = "stuck_or_hardtimeout"  # 历史数据无法区分二者
    elif cost == 0:
        kr = "infra"  # cost=0 多为 API/本地故障
    else:
        kr = "interrupted_or_unknown"

    return {
        "has_ps": has_ps,
        "headline_pass": headline_pass,
        "corrected_pass": corrected_pass,
        "corr_pr": corr_pr,
        "corr_binary": corr_binary,
        "kill_reason": kr,
    }


def agg(records):
    from collections import Counter
    n = len(records)
    if not n:
        return None
    corrs = [correct_record(r) for r in records]
    headline = sum(c["headline_pass"] for c in corrs)
    corrected = sum(c["corrected_pass"] for c in corrs)
    corr_pr_sum = sum(c["corr_pr"] for c in corrs)
    cost_sum = sum((r.get("total_cost_usd") or 0) for r in records)
    kr = Counter(c["kill_reason"] for c in corrs)
    headline_dpp = cost_sum / sum((r.get("pass_rate") or 0) for r in records) if sum((r.get("pass_rate") or 0) for r in records) > 0 else None
    corr_dpp = cost_sum / corr_pr_sum if corr_pr_sum > 0 else None
    return {
        "n": n,
        "headline_pass_pct": headline / n,
        "corrected_pass_pct": corrected / n,
        "delta_pp": (corrected - headline) / n * 100,
        "headline_dpp": headline_dpp,
        "corrected_dpp": corr_dpp,
        "kill_reason": dict(kr),
        "correctable": sum(c["has_ps"] for c in corrs),  # 有 per_subtask 才能修正
    }


def main():
    print("=" * 100)
    print("S12-P0 Step 0 离线验证：修正口径对 KPI 的影响（v2/v3/v4）")
    print("=" * 100)
    for ver, f in FILES.items():
        if not f.exists():
            continue
        recs = load(f)
        by_model = {}
        for r in recs:
            by_model.setdefault(r.get("model", "?"), []).append(r)
        print(f"\n### {ver}  (n={len(recs)})")
        print(f"{'model':<20}{'n':>4}{'headline':>10}{'corrected':>11}{'Δpp':>7}"
              f"{'$/pass旧':>10}{'$/pass新':>10}{'可修正':>7}")
        for m in sorted(by_model):
            a = agg(by_model[m])
            if not a:
                continue
            hd = f"${a['headline_dpp']:.4f}" if a['headline_dpp'] else "  n/a "
            cd = f"${a['corrected_dpp']:.4f}" if a['corrected_dpp'] else "  n/a "
            print(f"{m:<20}{a['n']:>4}{a['headline_pass_pct']*100:>9.0f}%"
                  f"{a['corrected_pass_pct']*100:>10.0f}%{a['delta_pp']:>+7.1f}"
                  f"{hd:>10}{cd:>10}{a['correctable']:>4}/{a['n']}")
        # 版本汇总 + kill_reason 分布
        tot = agg(recs)
        print(f"  {'合计':<18}{tot['n']:>4}{tot['headline_pass_pct']*100:>9.0f}%"
              f"{tot['corrected_pass_pct']*100:>10.0f}%{tot['delta_pp']:>+7.1f}"
              f"{(''+ ('${:.4f}'.format(tot['headline_dpp']) if tot['headline_dpp'] else 'n/a')):>10}"
              f"{(''+ ('${:.4f}'.format(tot['corrected_dpp']) if tot['corrected_dpp'] else 'n/a')):>10}"
              f"{tot['correctable']:>4}/{tot['n']}")
        print(f"  kill_reason 分布: {tot['kill_reason']}")

    # 核心论断校验
    print("\n" + "=" * 100)
    print("核心论断校验")
    print("=" * 100)
    v3 = load(FILES["v3"])
    a = agg(v3)
    verdict = "✅ 成立" if a["corrected_pass_pct"] >= 0.60 and a["delta_pp"] >= 25 else "❌ 不符预期，需复查"
    print(f"  v3 headline 通过率 {a['headline_pass_pct']:.0%} → 修正后 {a['corrected_pass_pct']:.0%} "
          f"(Δ +{a['delta_pp']:.0f}pp)  预期 ~67%  → {verdict}")


if __name__ == "__main__":
    main()
