#!/usr/bin/env python3
"""S-1 P0：规则输出覆盖 × 结果 的离线审计（零外发、零依赖）。

用途（需求文档 §18 / 概念设计 §10 的 P0 交付物）：
    回答"规则缺口（规则没跑到/没覆盖）是否对应后来更常出问题"——只用历史 bench
    记录里已存在的 plan 期规则信号与结果，不调用任何外部服务、不产生任何外发。

数据面：eval_suite 下的 bench 结果 JSONL（含 ``binary_pass`` 字段的记录）。
判读纪律：本脚本只出**分层关联**与**伪迹体检**，不出因果结论；方向反常识的项一律标
"疑似伪迹、不采信"，须先做靶/基线体检（范式文档设计纪律 2）。

用法：
    python3 tools/s1_coverage_audit.py                    # 默认扫 <repo>/eval_suite
    python3 tools/s1_coverage_audit.py --input eval_suite # 指定目录或文件
    python3 tools/s1_coverage_audit.py --json             # 机器可读
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_PATTERNS = ("*.jsonl", "*/*.jsonl", "*/*/*.jsonl")
RULE_FIELDS = (
    "plan_quality_status", "plan_warning_count", "plan_conflict_count",
    "plan_acceptance_coverage", "plan_requirement_coverage", "risk_types",
)

Row = Dict[str, Any]


def load_records(inputs: Sequence[str]) -> Tuple[List[Row], List[str]]:
    """加载 bench 结果 JSONL：目录按 DEFAULT_PATTERNS 展开，文件直接读；只收带 binary_pass 的。"""
    paths: List[str] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            for pat in DEFAULT_PATTERNS:
                paths.extend(sorted(glob.glob(str(p / pat))))
        elif p.is_file():
            paths.append(str(p))
    records: List[Row] = []
    files: List[str] = []
    for fp in dict.fromkeys(paths):  # 去重保序
        try:
            lines = [json.loads(line) for line in open(fp, encoding="utf-8") if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        if not lines or "binary_pass" not in lines[0]:
            continue
        files.append(fp)
        for row in lines:
            row["_file"] = os.path.basename(fp)
            records.append(row)
    return records, files


def is_fail(row: Row) -> bool:
    return row.get("binary_pass") is not True


def rate(rows: Sequence[Row]) -> Tuple[int, int, float]:
    n = len(rows)
    fails = sum(1 for r in rows if is_fail(r))
    return n, fails, (100.0 * fails / n if n else 0.0)


def _mean_retries(rows: Sequence[Row]) -> float:
    if not rows:
        return 0.0
    return sum(float(r.get("total_retries") or 0) for r in rows) / len(rows)


def _status_of(row: Row) -> str:
    value = row.get("plan_quality_status")
    return "None" if value in (None, "") else str(value)


def coverage_split(records: Sequence[Row]) -> Dict[str, Dict[str, Any]]:
    """A. 规则有输出 vs 缺口（None）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for label, pred in (
        ("rules_present", lambda r: _status_of(r) not in ("None",)),
        ("rule_gap", lambda r: _status_of(r) == "None"),
    ):
        rows = [r for r in records if pred(r)]
        n, fails, pct = rate(rows)
        out[label] = {"n": n, "fails": fails, "fail_pct": round(pct, 1),
                      "mean_retries": round(_mean_retries(rows), 2)}
    return out


def by_status(records: Sequence[Row]) -> Dict[str, Dict[str, Any]]:
    """B. 按 plan_quality_status 分层（blocked 为机械必然，单列标注）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for status in ("passed", "warning", "blocked", "None"):
        rows = [r for r in records if _status_of(r) == status]
        n, fails, pct = rate(rows)
        out[status] = {"n": n, "fails": fails, "fail_pct": round(pct, 1),
                       "mean_retries": round(_mean_retries(rows), 2),
                       "mechanical": status == "blocked"}
    return out


def by_file(records: Sequence[Row], min_n: int, min_cell_n: int) -> Dict[str, Dict[str, Any]]:
    """C. 批次内一致性：每个文件按状态分层（只报达标单元）。"""
    by: Dict[str, List[Row]] = defaultdict(list)
    for r in records:
        by[r["_file"]].append(r)
    out: Dict[str, Dict[str, Any]] = {}
    for fn, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < min_n:
            continue
        cells: Dict[str, Any] = {}
        for status in ("passed", "warning", "None"):
            cell = [r for r in rows if _status_of(r) == status]
            if len(cell) >= min_cell_n:
                n, _, pct = rate(cell)
                cells[status] = {"n": n, "fail_pct": round(pct, 1)}
        if len(cells) >= 2:
            out[fn] = cells
    return out


def stratified_by_difficulty(records: Sequence[Row]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """D. 混杂控制：组 × 难度 的失败率与难度分布（回答"缺口组是不是题更难"）。"""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    groups = {
        "rules_present": [r for r in records if _status_of(r) not in ("None", "blocked")],
        "rule_gap": [r for r in records if _status_of(r) == "None"],
    }
    for label, rows in groups.items():
        dist = Counter(str(r.get("difficulty")) for r in rows)
        cell: Dict[str, Any] = {"n": len(rows), "difficulty_dist": dict(dist), "tiers": {}}
        for tier in ("easy", "medium", "hard"):
            tier_rows = [r for r in rows if r.get("difficulty") == tier]
            n, _, pct = rate(tier_rows)
            if n:
                cell["tiers"][tier] = {"n": n, "fail_pct": round(pct, 1)}
        out[label] = cell
    return out


def mantel_haenszel(tables: Sequence[Tuple[float, float, float, float]]) -> Dict[str, Any]:
    """分层 2×2（a=暴露&失败, b=暴露&通过, c=非暴露&失败, d=非暴露&通过）的 MH 合并。

    返回 pooled OR、chi2（1 df，连续性校正）与 p（errfc 精确式）。空表返回 None 值。
    """
    num = den = 0.0
    sum_a = sum_e = sum_v = 0.0
    for a, b, c, d in tables:
        n = a + b + c + d
        if n <= 1:
            continue
        num += a * d / n
        den += b * c / n
        n1, n0 = a + b, c + d
        m1, m0 = a + c, b + d
        sum_a += a
        sum_e += n1 * m1 / n
        sum_v += n1 * n0 * m1 * m0 / (n * n * (n - 1))
    or_ = (num / den) if den else None
    if sum_v <= 0:
        return {"or": or_, "chi2": None, "p": None, "strata": len(tables)}
    chi2 = (abs(sum_a - sum_e) - 0.5) ** 2 / sum_v
    if chi2 < 0:
        chi2 = 0.0
    p = math.erfc(math.sqrt(chi2 / 2.0))
    return {"or": round(or_, 3) if or_ is not None else None,
            "chi2": round(chi2, 2), "p": round(p, 4), "strata": len(tables)}


def stratified_contrast(records: Sequence[Row], strata_key, exposure, unexposed,
                       min_cell: int = 5) -> Dict[str, Any]:
    """同一分层内比较"暴露组 vs 非暴露组"的失败率，再 MH 合并（控制分层变量）。"""
    strata: Dict[str, List[Row]] = defaultdict(list)
    for r in records:
        if exposure(r) or unexposed(r):
            strata[strata_key(r)].append(r)
    tables: List[Tuple[float, float, float, float]] = []
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for key, rows in sorted(strata.items()):
        e_rows = [r for r in rows if exposure(r)]
        u_rows = [r for r in rows if unexposed(r)]
        if min(len(e_rows), len(u_rows)) < min_cell:
            dropped += 1
            continue
        a = sum(1 for r in e_rows if is_fail(r))
        b = len(e_rows) - a
        c = sum(1 for r in u_rows if is_fail(r))
        d = len(u_rows) - c
        if 0 in (a, b, c, d):
            a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        tables.append((float(a), float(b), float(c), float(d)))
        kept.append({"stratum": key, "exp_n": len(e_rows), "exp_fail_pct": round(100 * a / (a + b), 1),
                     "unexp_n": len(u_rows), "unexp_fail_pct": round(100 * c / (c + d), 1)})
    pooled = mantel_haenszel(tables)
    pooled.update({"kept_strata": len(kept), "dropped_strata_small": dropped})
    return {"pooled": pooled, "detail": kept[:8]}


def lint_hypothesis(records: Sequence[Row]) -> Dict[str, Any]:
    """伪迹定案：lint_errors=0 是否＝"早失败 ⇒ lint 未运行"（而非"无 lint 错误"）。"""
    def shares(rows: Sequence[Row]) -> Dict[str, Any]:
        n = len(rows)
        if not n:
            return {"n": 0}
        return {
            "n": n,
            "completed_missing_or_0_pct": round(100 * sum(1 for r in rows if not r.get("completed")) / n, 1),
            "total_subtasks_missing_or_0_pct": round(100 * sum(1 for r in rows if not r.get("total_subtasks")) / n, 1),
            "timed_out_pct": round(100 * sum(1 for r in rows if r.get("timed_out")) / n, 1),
            "failure_class_timeout_pct": round(100 * sum(1 for r in rows if r.get("failure_class") == "timeout") / n, 1),
        }
    zero = [r for r in records if (r.get("lint_errors") or 0) == 0]
    nonzero = [r for r in records if (r.get("lint_errors") or 0) > 0]
    verdict = (
        "伪迹成立：lint=0 组绝大多数从未 completed ⇒ 不是'无 lint 错误'而是'lint 根本没跑到'；"
        "该字段不得作为规则信号使用"
        if zero and shares(zero)["completed_missing_or_0_pct"] >= 50
        else "未定案：lint=0 与'未完成'的关联不足，需继续分层"
    )
    return {"lint_zero": shares(zero), "lint_nonzero": shares(nonzero), "verdict": verdict}


def warning_mediation(records: Sequence[Row], top_k: int = 3) -> Dict[str, Any]:
    """warnings 3+ 失败率回落的构成检验：是否由低失败"地板批次"混合造成。"""
    w3 = [r for r in records if isinstance(r.get("plan_warning_count"), int) and r["plan_warning_count"] >= 3]
    top = [fn for fn, _ in Counter(r["_file"] for r in w3).most_common(top_k)]
    def buckets(rows: Sequence[Row]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for label, lo, hi in (("0", 0, 0), ("1-2", 1, 2), ("3+", 3, 10 ** 9)):
            cell = [r for r in rows if isinstance(r.get("plan_warning_count"), int) and lo <= r["plan_warning_count"] <= hi]
            out[label] = {"n": len(cell), "fail_pct": round(rate(cell)[2], 1)}
        return out
    return {
        "all": buckets(records),
        "excluding_top_files": buckets([r for r in records if r["_file"] not in top]),
        "top_files_w3": {fn: sum(1 for r in w3 if r["_file"] == fn) for fn in top},
        "note": "回落若在剔除 top 贡献批次后消失/减弱 ⇒ 混合效应（地板批次），非 3+ 本身的属性",
    }


def artifact_checks(records: Sequence[Row]) -> Dict[str, Any]:
    """E. 伪迹体检：方向反常识的项单列并标"不采信"；空字段＝靶缺口。"""
    checks: Dict[str, Any] = {}

    lint_zero = [r for r in records if (r.get("lint_errors") or 0) == 0]
    n, _, pct = rate(lint_zero)
    checks["lint_errors_zero"] = {
        "n": n, "fail_pct": round(pct, 1),
        "top_files": dict(Counter(r.get("_file", "?") for r in lint_zero).most_common(5)),
        "verdict": "疑似伪迹（方向反常识）；定案见 lint_hypothesis 段",
    }

    buckets = {"0": [], "1-2": [], "3+": []}
    for r in records:
        wc = r.get("plan_warning_count")
        if not isinstance(wc, int):
            continue
        key = "0" if wc == 0 else ("1-2" if wc <= 2 else "3+")
        buckets[key].append(r)
    checks["plan_warning_buckets"] = {
        key: {"n": len(rows), "fail_pct": round(rate(rows)[2], 1)}
        for key, rows in buckets.items()
    }

    risk_buckets = {"0": [], "1-2": [], "3+": []}
    for r in records:
        rt = r.get("risk_types")
        if not isinstance(rt, list):
            continue
        key = "0" if len(rt) == 0 else ("1-2" if len(rt) <= 2 else "3+")
        risk_buckets[key].append(r)
    checks["risk_types_buckets"] = {
        key: {"n": len(rows), "fail_pct": round(rate(rows)[2], 1)}
        for key, rows in risk_buckets.items()
    }

    empty_fields: Dict[str, int] = {}
    for field in RULE_FIELDS:
        filled = sum(1 for r in records if r.get(field) not in (None, "", []))
        empty_fields[field] = len(records) - filled
    checks["rule_field_gaps"] = {
        "total": len(records),
        "empty_counts": empty_fields,
        "never_emitted": [f for f, miss in empty_fields.items() if miss == len(records)],
    }
    return checks


def audit(records: Sequence[Row], files: Sequence[str], min_n: int, min_cell_n: int) -> Dict[str, Any]:
    without_blocked = [r for r in records if _status_of(r) != "blocked"]
    return {
        "dataset": {"records": len(records), "files": len(files)},
        "coverage_split": coverage_split(records),
        "by_status": by_status(records),
        "by_file": by_file(records, min_n, min_cell_n),
        "stratified_by_difficulty": stratified_by_difficulty(records),
        "stratified": {
            "gap_vs_present_by_model": stratified_contrast(
                without_blocked, lambda r: str(r.get("model")),
                lambda r: _status_of(r) == "None",
                lambda r: _status_of(r) in ("passed", "warning"),
                min_cell=min_cell_n),
            "warning_vs_passed_by_model": stratified_contrast(
                without_blocked, lambda r: str(r.get("model")),
                lambda r: _status_of(r) == "warning",
                lambda r: _status_of(r) == "passed",
                min_cell=min_cell_n),
        },
        "lint_hypothesis": lint_hypothesis(records),
        "warning_mediation": warning_mediation(records),
        "artifact_checks": artifact_checks(records),
    }


def render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    ds = report["dataset"]
    lines.append("# S-1 P0 离线审计（规则输出覆盖 × 结果）")
    lines.append(f"数据面：{ds['records']} 条记录 / {ds['files']} 个文件（零外发）")
    lines.append("")
    lines.append("## A. 规则输出 vs 缺口")
    for key, label in (("rules_present", "规则有输出"), ("rule_gap", "规则无输出（缺口）")):
        c = report["coverage_split"][key]
        lines.append(f"- {label}: n={c['n']} 失败={c['fails']} ({c['fail_pct']}%) "
                     f"平均retry={c['mean_retries']}")
    lines.append("")
    lines.append("## B. 按 plan_quality_status")
    for status in ("passed", "warning", "blocked", "None"):
        c = report["by_status"][status]
        flag = "  ← 机械必然（阻断即不执行）" if c.get("mechanical") else ""
        lines.append(f"- {status:<8} n={c['n']:>4} 失败={c['fails']:>4} ({c['fail_pct']}%) "
                     f"平均retry={c['mean_retries']}{flag}")
    lines.append("")
    lines.append("## C. 批次内一致性（同文件分层）")
    for fn, cells in report["by_file"].items():
        parts = "  ".join(f"{k}={v['fail_pct']}%(n{v['n']})" for k, v in cells.items())
        lines.append(f"- {fn}: {parts}")
    lines.append("")
    lines.append("## D. 难度分层（混杂控制）")
    for label in ("rules_present", "rule_gap"):
        c = report["stratified_by_difficulty"][label]
        tiers = "  ".join(f"{t}={v['fail_pct']}%(n{v['n']})" for t, v in c["tiers"].items())
        lines.append(f"- {label}: n={c['n']} 难度分布={c['difficulty_dist']}")
        lines.append(f"    {tiers}")
    lines.append("")
    lines.append("## E. 伪迹体检 / 靶缺口")
    lint = report["artifact_checks"]["lint_errors_zero"]
    lines.append(f"- lint_errors=0: n={lint['n']} 失败={lint['fail_pct']}% —— {lint['verdict']}")
    for name, key in (("warnings", "plan_warning_buckets"), ("risk_types", "risk_types_buckets")):
        parts = "  ".join(f"{k}={v['fail_pct']}%(n{v['n']})"
                          for k, v in report["artifact_checks"][key].items())
        lines.append(f"- {name} 分桶: {parts}")
    gaps = report["artifact_checks"]["rule_field_gaps"]
    lines.append(f"- 规则字段缺口：从未产出={gaps['never_emitted'] or '无'}")
    lines.append("")
    lines.append("## F. 分层配对（MH 合并，控制模型；排除 blocked）")
    for key, title in (("gap_vs_present_by_model", "缺口 vs 有输出"),
                       ("warning_vs_passed_by_model", "warning vs passed")):
        c = report["stratified"][key]
        p = c["pooled"]
        lines.append(f"- {title}（按 model 分层）：OR={p['or']} chi2={p['chi2']} p={p['p']}"
                     f" 有效层={p['kept_strata']}（小层丢弃 {p['dropped_strata_small']}）")
        for d in c["detail"][:4]:
            lines.append(f"    层 {str(d['stratum'])[:28]:<28} 暴露 {d['exp_fail_pct']}%(n{d['exp_n']})"
                         f"  vs  非暴露 {d['unexp_fail_pct']}%(n{d['unexp_n']})")
    lines.append("")
    lines.append("## G. lint 伪迹定案")
    lh = report["lint_hypothesis"]
    for label in ("lint_zero", "lint_nonzero"):
        c = lh[label]
        lines.append(f"- {label}: n={c.get('n')} completed缺/0={c.get('completed_missing_or_0_pct')}%"
                     f" 子任务缺/0={c.get('total_subtasks_missing_or_0_pct')}%"
                     f" timed_out={c.get('timed_out_pct')}%")
    lines.append(f"- 判定：{lh['verdict']}")
    lines.append("")
    lines.append("## H. warnings 3+ 回落的中介检验")
    wm = report["warning_mediation"]
    parts_all = "  ".join(f"{k}={v['fail_pct']}%(n{v['n']})" for k, v in wm["all"].items())
    parts_ex = "  ".join(f"{k}={v['fail_pct']}%(n{v['n']})" for k, v in wm["excluding_top_files"].items())
    lines.append(f"- 全量：{parts_all}")
    lines.append(f"- 剔除 top 贡献批次（{list(wm['top_files_w3'])}）：{parts_ex}")
    lines.append(f"- 注：{wm['note']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="S-1 P0：规则输出覆盖 × 结果 离线审计（零外发）")
    default_dir = Path(__file__).resolve().parents[1] / "eval_suite"
    parser.add_argument("--input", nargs="*", default=[str(default_dir)],
                        help="目录或 JSONL 文件（可多个；默认 <repo>/eval_suite）")
    parser.add_argument("--min-n", type=int, default=20, help="批次内一致性纳入的最小文件样本数")
    parser.add_argument("--min-cell-n", type=int, default=10, help="批次内单元的最小样本数")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    records, files = load_records(args.input)
    if not records:
        print("[s1] 未找到带 binary_pass 的 bench 记录", file=sys.stderr)
        return 2
    report = audit(records, files, args.min_n, args.min_cell_n)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
