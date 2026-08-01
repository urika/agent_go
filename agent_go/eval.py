import json
import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from .console import _LazyConsole
from .assessment import load as load_assessments, compute_false_positive_rate

logger = logging.getLogger(__name__)

console = _LazyConsole()

__all__ = [
    "analyze_quality", "analyze_performance",
    "aggregate_quality", "aggregate_performance", "cmd_eval",
    "gate_cost", "gate_cost_regression",
    "load_cost_baseline", "save_cost_baseline",
]

def _read_meta(task_dir: Path) -> Optional[dict[str, Any]]:
    path = Path(task_dir) / "meta.json"
    if not path.exists():
       return None
    try:
       return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
       logger.debug("Failed to read meta.json from %s: %s", path, e)
       return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL file, return list of dicts (shared by bench / cross_judge)."""
    if not path or not path.exists():
       return []
    items = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
       if line:
           try:
               items.append(json.loads(line))
           except json.JSONDecodeError:
               pass
    return items


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON file, return dict (shared by bench / cross_judge)."""
    if not path or not path.exists():
       return {}
    try:
       return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
       return {}


def _read_log_events(log_path: Path, event_name: str) -> list[dict[str, Any]]:
    events = []
    if not log_path.exists():
       return events
    # 匹配 JSON 中 "event":"xxx" 或 "event": "xxx"（兼容有无空格）
    search = re.compile(rf'"event"\s*:\s*"{re.escape(event_name)}"')
    for line in log_path.read_text(encoding="utf-8").strip().split("\n"):
       if search.search(line):
           try:
               json_part = line.split(" | ")[-1]
               events.append(json.loads(json_part))
           except (json.JSONDecodeError, IndexError) as e:
               logger.debug("Failed to parse log event line: %s", e)
    return events


# ═══════════════════════════════════════════════════════════════
# Quality
# ═══════════════════════════════════════════════════════════════

def analyze_quality(meta: Optional[dict[str, Any]], task_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    if meta is None:
       return None
    results = meta.get("results", [])
    subtasks = meta.get("subtasks", [])
    if not results:
       return None

    total = len(results)
    completed = sum(1 for r in results if r.get("status") == "completed")
    no_changes = sum(1 for r in results if r.get("status") == "no_changes")
    failed = sum(1 for r in results if r.get("status") == "failed")

    q1 = round(completed / total * 100) if total else 0
    q2 = round((completed + no_changes) / total * 100) if total else 0

    # Q3 首次验证通过率：retry_count==0 且 verify_ok（此前只看 retry_count，
    # 0 次重试但验证失败的也被误算进「首次通过」）
    first_pass = sum(1 for r in results if r.get("retry_count", 0) == 0 and r.get("verify_ok"))
    q3 = round(first_pass / total * 100) if total else 0

    with_changes = [r for r in results if r.get("status") != "no_changes"]
    q4 = round(sum(1 for r in with_changes if r.get("verify_ok")) / len(with_changes) * 100) if with_changes else 100

    q5_no_changes_with_new = sum(
       1 for r in results
       if r.get("status") == "no_changes" and r.get("change_stats", {}).get("new_files", 0) > 0
    )
    q5 = round(q5_no_changes_with_new / total * 100) if total else 0

    merge_success = 0
    merge_total = 0
    for r in results:
       for m in r.get("merge_results", []):
           merge_total += 1
           if m.get("status") == "success":
               merge_success += 1
    q6 = round(merge_success / merge_total * 100) if merge_total else 100

    avg_files = avg_insertions = avg_deletions = 0
    with_stats = [r.get("change_stats", {}) for r in results if r.get("change_stats")]
    if with_stats:
       avg_files = round(sum(c.get("files_changed", 0) for c in with_stats) / len(with_stats), 1)
       avg_insertions = round(sum(c.get("insertions", 0) for c in with_stats) / len(with_stats), 1)
       avg_deletions = round(sum(c.get("deletions", 0) for c in with_stats) / len(with_stats), 1)

    q7_precision = q7_recall = 100
    if subtasks and with_stats:
       planned = set()
       for st in subtasks:
           fh = st.get("files_hint", "")
           if fh and fh != "*":
               for f in fh.split(","):
                   planned.add(f.strip())
       actual = set()
       for r in results:
           for f in r.get("change_stats", {}).get("actual_files", []):
               actual.add(f)
       if planned and actual:
           inter = planned & actual
           q7_precision = round(len(inter) / len(planned) * 100)
           q7_recall = round(len(inter) / len(actual) * 100)

    # Phase 4: 新增验证循环指标
    retried = [r for r in results if r.get("retry_count", 0) > 0]
    retry_success_rate = round(
       sum(1 for r in retried if r.get("status") == "completed") / len(retried) * 100
    ) if retried else 100

    blocked = sum(1 for r in results if r.get("status") == "blocked")
    blocked_rate = round(blocked / total * 100) if total else 0

    # 平均重试次数（设计稿 P8 指标，归入质量报告随 Q8/Q9 一起展示）
    avg_retries = round(sum(r.get("retry_count", 0) for r in results) / total, 2) if total else 0

    return {
       "task_id": meta.get("task_id", ""),
       "status": meta.get("status", ""),
       "subtasks": {"total": total, "completed": completed, "no_changes": no_changes, "failed": failed, "blocked": blocked},
       "Q1_task_success_rate": q1,
       "Q2_subtask_success_rate": q2,
       "Q3_first_pass_rate": q3,
       "Q4_verify_pass_rate": q4,
       "Q5_new_file_miss_rate": q5,
       "Q6_merge_success_rate": q6,
       "Q7_plan_accuracy_precision": q7_precision,
       "Q7_plan_accuracy_recall": q7_recall,
       "Q8_retry_success_rate": retry_success_rate,
            "Q9_blocked_rate": blocked_rate,
            "Q10_avg_retries": avg_retries,
            # L3 假阳性率指标（从 assessment.jsonl 读取）
            **_compute_q11(task_dir),
            "change_scale": {"avg_files": avg_files, "avg_insertions": avg_insertions, "avg_deletions": avg_deletions},
       "score": round(q1 * 0.4 + q3 * 0.3 + q4 * 0.3),
    }


# ═══════════════════════════════════════════════════════════════
# Performance
# ═══════════════════════════════════════════════════════════════

def analyze_performance(meta: Optional[dict[str, Any]], log_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    if meta is None:
       return None
    results = meta.get("results", [])
    if not results:
       return None

    durations = [r.get("duration_sec", 0) for r in results if r.get("duration_sec")]
    p3 = round(sum(durations) / len(durations), 1) if durations else 0
    p4 = _percentiles(durations, [50, 95, 99])

    timing_totals = {"worktree_create_ms": 0, "merge_upstream_ms": 0, "claude_execute_ms": 0,
                    "verification_ms": 0, "git_commit_ms": 0}
    timing_count = 0
    for r in results:
       t = r.get("timing")
       if t:
           timing_count += 1
           for k in timing_totals:
               timing_totals[k] += t.get(k, 0)

    p5 = {}
    if timing_count:
       total_ms = sum(timing_totals.values()) or 1
       for k, v in timing_totals.items():
           p5[k] = round(v / total_ms * 100, 1)

    p1 = 0
    p6 = 100
    sum_duration = sum(durations)
    p2 = 0

    if log_path:
       plan_events = _read_log_events(log_path, "plan_complete")
       for ev in plan_events:
           p2 = ev.get("plan_duration_ms", p2)

       lines = log_path.read_text(encoding="utf-8").strip().split("\n")
       try:
           first_ts = datetime.strptime(lines[0].split(" | ")[0], "%Y-%m-%d %H:%M:%S")
           last_ts = datetime.strptime(lines[-1].split(" | ")[0], "%Y-%m-%d %H:%M:%S")
           p1 = round((last_ts - first_ts).total_seconds(), 1)
       except (ValueError, IndexError) as e:
           logger.debug("Failed to parse log timestamps: %s", e)
    if p1 > 0:
       p6 = round(sum_duration / p1 * 100)

    return {
       "task_id": meta.get("task_id", ""),
       "P1_total_duration_sec": p1,
       "P2_plan_duration_ms": p2,
       "P3_avg_subtask_sec": p3,
       "P4_duration_percentiles": p4,
       "P5_phase_breakdown_pct": p5,
       "P6_concurrency_efficiency_pct": p6,
       "score": _perf_score(p1, p4.get(95, p3), p6),
    }


def _perf_score(p1: float, p95: float, p6: float) -> float:
    if p1 <= 0:
       return 50
    p1_score = max(0, min(100, 100 - p1 / 3))
    p95_score = max(0, min(100, 100 - p95 / 6))
    p6_score = max(0, min(100, p6))
    return round(p1_score * 0.3 + p95_score * 0.3 + p6_score * 0.4)


def _compute_q11(task_dir: Optional[Path]) -> dict:
    """从 assessment.jsonl 读取假阳性率指标。

    Returns: dict with Q11_* fields, or empty dict if no assessment data.
    """
    if not task_dir:
       return {}
    events = load_assessments(task_dir)
    if not events:
       return {}
    fp = compute_false_positive_rate(events)
    if fp["total_evaluated"] == 0:
       return {}
    return {
       "Q11_false_positive_rate": fp["false_positive_rate"],
       "Q11_avg_confidence": fp.get("avg_confidence"),
       "Q11_evaluated_count": fp["total_evaluated"],
       "Q11_flagged_count": fp["flagged"],
       "Q11_auto_trigger_rate": fp.get("auto_trigger_rate"),
    }


def _percentiles(data: list[float], percents: list[int]) -> dict[int, float]:
    if not data:
       return {p: 0 for p in percents}
    s = sorted(data)
    result = {}
    for p in percents:
       k = (p / 100) * (len(s) - 1)
       f = int(k)
       c = f + 1 if f + 1 < len(s) else f
       result[p] = round(s[f] + (s[c] - s[f]) * (k - f), 1) if c != f else round(s[f], 1)
    return result


# ═══════════════════════════════════════════════════════════════
# Aggregation
# ═══════════════════════════════════════════════════════════════

def _scan_task_dirs(base_dir: Path) -> list[Path]:
    return sorted(Path(base_dir).glob("task-*"), reverse=True)


def aggregate_quality(tasks_dir: Path) -> Optional[dict[str, Any]]:
    items = []
    for td in _scan_task_dirs(tasks_dir):
       meta = _read_meta(td)
       q = analyze_quality(meta, task_dir=td)
       if q:
           items.append(q)
    if not items:
       return None
    return {
       "tasks_analyzed": len(items),
       "avg_success_rate": round(sum(r["Q1_task_success_rate"] for r in items) / len(items)),
       "avg_first_pass": round(sum(r["Q3_first_pass_rate"] for r in items) / len(items)),
       "avg_verify_pass": round(sum(r["Q4_verify_pass_rate"] for r in items) / len(items)),
       "avg_new_file_miss": round(sum(r["Q5_new_file_miss_rate"] for r in items) / len(items)),
       "avg_merge_success": round(sum(r["Q6_merge_success_rate"] for r in items) / len(items)),
       "avg_retry_success": round(sum(r["Q8_retry_success_rate"] for r in items) / len(items)),
       "avg_blocked_rate": round(sum(r["Q9_blocked_rate"] for r in items) / len(items)),
       "avg_retries": round(sum(r.get("Q10_avg_retries", 0) for r in items) / len(items), 2),
       "avg_score": round(sum(r["score"] for r in items) / len(items)),
       "avg_false_positive_rate": round(
           sum(r.get("Q11_false_positive_rate", 0) or 0 for r in items) /
           max(sum(1 for r in items if r.get("Q11_false_positive_rate") is not None), 1)
       ),
       "avg_semantic_confidence": round(
           sum(r.get("Q11_avg_confidence", 0) or 0 for r in items) /
           max(sum(1 for r in items if r.get("Q11_avg_confidence") is not None), 1), 2
       ) if any(r.get("Q11_avg_confidence") is not None for r in items) else None,
    }


def _resolve_provider_default(provider: str, timestamp: Optional[str] = None) -> str:
    """根据 provider 和日志时间戳选择正确的默认模型。

    用法：旧日志（早于 PROVIDER_DEFAULT_MODEL_CUTOFF）使用 LEGACY_PROVIDER_DEFAULT_MODEL，
    新日志使用 PROVIDER_DEFAULT_MODEL。这样历史 $/pass rate 不会被新默认价（往往更便宜）拉低。
    """
    is_legacy = True
    if timestamp:
       try:
           ts_date = timestamp[:10]
           is_legacy = ts_date < PROVIDER_DEFAULT_MODEL_CUTOFF
       except (TypeError, ValueError):
           is_legacy = True
    if is_legacy:
       return LEGACY_PROVIDER_DEFAULT_MODEL.get(provider, PROVIDER_DEFAULT_MODEL.get(provider, ""))
    return PROVIDER_DEFAULT_MODEL.get(provider, "")


def aggregate_performance(tasks_dir: Path) -> Optional[dict[str, Any]]:
    all_durations = []
    p1_values = []
    for td in _scan_task_dirs(tasks_dir):
       meta = _read_meta(td)
       if meta:
           for r in meta.get("results", []):
               d = r.get("duration_sec")
               if d:
                   all_durations.append(d)
           log_path = td / "execution.log"
           p = analyze_performance(meta, log_path)
           if p and p.get("P1_total_duration_sec"):
               p1_values.append(p["P1_total_duration_sec"])

    p4 = _percentiles(all_durations, [50, 95, 99]) if all_durations else {}
    return {
       "tasks_analyzed": len(p1_values),
       "subtasks_total": len(all_durations),
       "avg_duration_sec": round(sum(all_durations) / len(all_durations), 1) if all_durations else 0,
       "P50_sec": p4.get(50, 0),
       "P95_sec": p4.get(95, 0),
       "P99_sec": p4.get(99, 0),
       "avg_task_duration_sec": round(sum(p1_values) / len(p1_values), 1) if p1_values else 0,
    }


# 定价表已迁至 pricing.py（纯配置数据，避免 eval 成为配置的事实标准源）。
# eval.py 通过模块级 import 引用，analyze_cost 内直接使用 MODEL_PRICES / PROVIDER_DEFAULT_MODEL。
from .pricing import (
    MODEL_PRICES,
    PROVIDER_DEFAULT_MODEL,
    LEGACY_PROVIDER_DEFAULT_MODEL,
    PROVIDER_DEFAULT_MODEL_CUTOFF,
)


def analyze_cost(tasks_dir: Path) -> dict[str, Any]:
    total_calls = 0
    total_prompt = 0
    total_completion = 0
    by_model = {}
    by_role = {}
    errors = 0
    cache_hits = 0
    cache_checks = 0
    # D1/D2 修复：成本双轨——真实 metering cost_usd（主）+ token 重算（仅补缺 cost_usd 的事件）
    cost_from_metering = 0.0
    # 缺 cost_usd 且模型在价目表的事件，按 token 重算（旧日志/fallback 用）
    rebuild_usage: dict[str, dict[str, int]] = {}
    unknown_model_events = 0   # 既无 cost_usd 又不在 MODEL_PRICES 的事件（监控价目表覆盖度）
    fallback_events = 0        # result="fallback" 或 fallback_reason 非空（PRD §line 173 留痕字段）
    policy_violations: dict[str, int] = {}  # 政策违规类型 → 次数

    for td in _scan_task_dirs(tasks_dir):
       # Phase 1 配套：优先读取结构化的 metering.jsonl
       metering_path = td / "metering.jsonl"
       if metering_path.exists():
           for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
               if not line:
                   continue
               try:
                   ev = json.loads(line)
               except json.JSONDecodeError:
                   continue
               total_calls += 1
               p = ev.get("prompt_tokens", 0) or 0
               c = ev.get("completion_tokens", 0) or 0
               total_prompt += p
               total_completion += c
               provider = ev.get("actual_provider", "?")
               model = ev.get("actual_model") or _resolve_provider_default(provider, ev.get("timestamp"))
               if model not in by_model:
                   by_model[model] = {"calls": 0, "prompt": 0, "completion": 0}
               by_model[model]["calls"] += 1
               by_model[model]["prompt"] += p
               by_model[model]["completion"] += c
               role = ev.get("role", "unknown")
               if role not in by_role:
                   by_role[role] = {"calls": 0, "cost_usd": 0.0}
               by_role[role]["calls"] += 1
               by_role[role]["cost_usd"] += ev.get("cost_usd", 0.0) or 0.0
               # D1/D2：优先用真实 cost_usd；缺则留待 token 重算
               ev_cost = ev.get("cost_usd", 0.0) or 0.0
               if ev_cost > 0:
                   cost_from_metering += ev_cost
               elif model and model in MODEL_PRICES:
                   # 缺 cost_usd 但模型已知 → 按 token 重算补
                   rebuild_usage.setdefault(model, {"prompt": 0, "completion": 0})
                   rebuild_usage[model]["prompt"] += p
                   rebuild_usage[model]["completion"] += c
               else:
                   # 既无 cost_usd 又不在价目表（如 claude-code-executor 缺 cost_usd）→ 无法计价，计为未知
                   unknown_model_events += 1
               # D5 可观测：降级事件（PRD §line 173 留痕字段终于被读）
               if ev.get("result") == "fallback" or (ev.get("fallback_reason") or ""):
                   fallback_events += 1
               # 政策违规事件（如 Planner 配置 fallback 降级）
               pv = ev.get("policy_violation", "")
               if pv:
                   policy_violations[pv] = policy_violations.get(pv, 0) + 1
               if ev.get("result") in ("failed", "quality_fail"):
                   errors += 1

       # 回退：从 execution.log 读取旧的 api_call 事件
       log_path = td / "execution.log"
       for ev in _read_log_events(log_path, "api_call"):
           total_calls += 1
           p = ev.get("prompt_tokens", 0)
           c = ev.get("completion_tokens", 0)
           total_prompt += p
           total_completion += c
           provider = ev.get("provider", "?")
           model = ev.get("model") or _resolve_provider_default(provider, ev.get("timestamp"))
           if model not in by_model:
               by_model[model] = {"calls": 0, "prompt": 0, "completion": 0}
           by_model[model]["calls"] += 1
           by_model[model]["prompt"] += p
           by_model[model]["completion"] += c
           # 旧日志无 cost_usd，按 token 重算（模型未知则计 unknown）
           if model and model in MODEL_PRICES:
               rebuild_usage.setdefault(model, {"prompt": 0, "completion": 0})
               rebuild_usage[model]["prompt"] += p
               rebuild_usage[model]["completion"] += c
           else:
               unknown_model_events += 1
       for ev in _read_log_events(log_path, "api_error"):
           errors += 1
       for ev in _read_log_events(log_path, "plan_complete"):
           cache_checks += 1
           if ev.get("cache_hit"):
               cache_hits += 1

    # token 重算（仅对缺 cost_usd 且模型在价目表的事件）
    cost_from_rebuild = 0.0
    model_costs = {}
    # 先把 metering 真实成本按模型分摊到 model_costs（从 by_role 无法反推模型，故 model_costs 用重算值代表可重算部分）
    for model, usage in rebuild_usage.items():
       price = MODEL_PRICES[model]
       pc = usage["prompt"] / 1_000_000 * price.get("prompt", 1)
       cc = usage["completion"] / 1_000_000 * price.get("completion", 5)
       model_costs[model] = round(pc + cc, 4)
       cost_from_rebuild += pc + cc
    cost_from_rebuild = round(cost_from_rebuild, 4)
    cost = round(cost_from_metering + cost_from_rebuild, 4)

    tasks = list(_scan_task_dirs(tasks_dir))
    subtask_total = 0
    completed_total = 0
    for td in tasks:
       meta = _read_meta(td)
       if meta:
           results = meta.get("results", [])
           subtask_total += len(results)
           completed_total += sum(1 for r in results if r.get("status") == "completed")

    # 北极星指标：$/pass rate
    dollar_per_pass = None
    if completed_total > 0:
       dollar_per_pass = round(cost / completed_total, 4)

    return {
       "total_calls": total_calls, "total_prompt_tokens": total_prompt, "total_completion_tokens": total_completion,
       "estimated_cost_usd": cost,
       # 成本来源透明化：metering（真实）+ rebuild（token 重算补缺）
       "cost_source_breakdown": {"metering": round(cost_from_metering, 6), "rebuilt": cost_from_rebuild},
       "unknown_model_events": unknown_model_events,
       "fallback_events": fallback_events,
       "policy_violations": policy_violations,
       "by_model": model_costs,
       "by_role": {r: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)} for r, v in sorted(by_role.items())},
       "errors": errors, "cache_hits": cache_hits, "cache_checks": cache_checks,
       "cache_hit_rate": round(cache_hits / cache_checks * 100) if cache_checks else 0,
       "avg_cost_per_task": round(cost / len(tasks), 4) if tasks else 0,
       "avg_cost_per_subtask": round(cost / subtask_total, 4) if subtask_total else 0,
       "completed_subtasks": completed_total,
       "dollar_per_pass_rate": dollar_per_pass,
    }


# ═══════════════════════════════════════════════════════════════
# 发布门禁：$/pass rate（北极星指标）
# ═══════════════════════════════════════════════════════════════

def gate_cost(baseline: float, tasks_dir: Path) -> dict[str, Any]:
    """$/pass rate 发布门禁。

    PRD 铁律：发布门禁「$/pass rate 不劣化」。本函数取 analyze_cost 的 dollar_per_pass_rate，
    与 baseline 比较，返回结构化判定结果，供 cmd_eval 决定退出码。

    失败语义：actual is not None 且 actual > baseline → 不通过（实际成本/通过率劣于基线）。
    无数据语义：actual is None（无完成任务或无 metering）→ 通过，但标注门禁未生效
               （早期仓库/新 fork 无数据时不阻挡 CI）。

    Args:
       baseline: 允许的 $/pass rate 上限（美元/通过子任务）。如 0.05 表示 ≤ $0.05/pass。
       tasks_dir: 任务目录（通常 AGENT_GO_DIR）。

    Returns:
       dict: {passed: bool, actual: float|None, baseline: float,
              completed_subtasks: int, estimated_cost_usd: float, reason: str}
    """
    cost_report = analyze_cost(tasks_dir)
    actual = cost_report.get("dollar_per_pass_rate")
    completed = cost_report.get("completed_subtasks", 0)
    total_cost = cost_report.get("estimated_cost_usd", 0.0)

    if actual is None:
       return {
           "passed": True,
           "actual": None,
           "baseline": baseline,
           "completed_subtasks": completed,
           "estimated_cost_usd": total_cost,
           "reason": "无完成任务或无 metering 数据，门禁未生效",
       }
    if actual > baseline:
       return {
           "passed": False,
           "actual": actual,
           "baseline": baseline,
           "completed_subtasks": completed,
           "estimated_cost_usd": total_cost,
           "reason": f"$/pass rate {actual} 超过基线 {baseline}（劣化 {actual - baseline:.4f}）",
       }
    return {
       "passed": True,
       "actual": actual,
       "baseline": baseline,
       "completed_subtasks": completed,
       "estimated_cost_usd": total_cost,
       "reason": f"$/pass rate {actual} 在基线 {baseline} 以内",
    }


# PRD "不劣化"语义：相对基线对比（vs 绝对阈值）。
# 基线文件 .agent_go/cost_baseline.json 存储上次记录的 $/pass rate，
# `eval gate --check-regression` 对比当前 rate 与基线，劣化 > 阈值即失败。
_BASELINE_FILENAME = "cost_baseline.json"
_REGRESSION_TOLERANCE = 0.10  # 允许 10% 波动（噪声容差）


def _baseline_path(tasks_dir: Path) -> Path:
    return Path(tasks_dir) / _BASELINE_FILENAME


def load_cost_baseline(tasks_dir: Path) -> Optional[float]:
    """读取已存储的 $/pass rate 基线。无基线返回 None。

    关键修复（ISSUE #9）：区分「文件不存在（合法首次）」与「文件存在但读取失败（不应静默重置）」。
    读失败时返回 None 但通过 _baseline_read_error 异常路径让调用方感知；
    此函数保持简单返回 None，由 gate_cost_regression 检查文件存在性后再决定。
    """
    p = _baseline_path(tasks_dir)
    if not p.exists():
       return None
    try:
       data = json.loads(p.read_text(encoding="utf-8"))
       return data.get("dollar_per_pass_rate")
    except (json.JSONDecodeError, OSError):
       return None


def save_cost_baseline(tasks_dir: Path, rate: float) -> None:
    """持久化当前 $/pass rate 作为下次对比的基线。"""
    p = _baseline_path(tasks_dir)
    try:
       p.write_text(json.dumps({
           "dollar_per_pass_rate": rate,
           "updated_at": datetime.now().isoformat(),
       }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
       pass


def gate_cost_regression(tasks_dir: Path, tolerance: float = _REGRESSION_TOLERANCE,
                        update: bool = False) -> dict[str, Any]:
    """$/pass rate 回归门禁（PRD "不劣化"语义）。

    对比当前 rate 与已存储基线，劣化幅度 > tolerance（默认 10%）→ 不通过。
    无基线时：首次运行自动写入基线并通过（建立基线）。
    actual is None（无数据）：通过，门禁未生效。

    Args:
       tasks_dir: 任务目录（基线文件存于此）。
       tolerance: 允许的劣化比例（0.10 = 10%）。当前 rate ≤ 基线×(1+tolerance) 即通过。
       update: True 时无论结果都更新基线（用于主动刷新基线，如模型升级后重置）。

    Returns:
       dict: {passed, actual, baseline, tolerance, regression_pct, reason, updated}
    """
    cost_report = analyze_cost(tasks_dir)
    actual = cost_report.get("dollar_per_pass_rate")
    completed = cost_report.get("completed_subtasks", 0)
    total_cost = cost_report.get("estimated_cost_usd", 0.0)
    stored_baseline = load_cost_baseline(tasks_dir)

    def _result(passed, reason, baseline_val, updated):
       return {
           "passed": passed, "actual": actual, "baseline": baseline_val,
           "tolerance": tolerance,
           "regression_pct": (
               round((actual - baseline_val) / baseline_val * 100, 2)
               if (actual is not None and baseline_val and baseline_val > 0) else None
           ),
           "completed_subtasks": completed, "estimated_cost_usd": total_cost,
           "reason": reason, "updated": updated,
       }

    if actual is None:
       return _result(True, "无完成任务或无 metering 数据，门禁未生效", stored_baseline, False)

    if stored_baseline is None:
       # 关键修复（ISSUE #9）：区分「文件不存在（首次）」与「文件存在但读取失败（permission/disk 错误）」
       # 文件不存在是合法首次 → 建立基线；读取失败则 FAIL 让运维介入，不静默重置
       baseline_file = _baseline_path(tasks_dir)
       if baseline_file.exists():
           # 文件存在但 load_cost_baseline 返回 None → 读失败
           return _result(
               False,
               f"基线文件 {baseline_file} 存在但无法读取（permission/disk/JSON 错误）；"
               f"拒绝静默重置，请运维介入修复文件后重试。当前 rate {actual} 未与基线对比",
               None, False,
           )
       # 真正首次：建立基线
       save_cost_baseline(tasks_dir, actual)
       return _result(True, f"无历史基线，已建立基线 {actual}", actual, True)

    if update:
       save_cost_baseline(tasks_dir, actual)
       return _result(True, f"基线已更新为 {actual}（--update 强制）", actual, True)

    # 回归判定：劣化比例 = (actual - baseline) / baseline
    if stored_baseline > 0:
       regression_pct = (actual - stored_baseline) / stored_baseline
    else:
       regression_pct = 0
    if regression_pct > tolerance:
       return _result(
           False,
           f"$/pass rate {actual} 较基线 {stored_baseline} 劣化 {regression_pct*100:.1f}%"
           f"（超过容差 {tolerance*100:.0f}%）",
           stored_baseline, False,
       )
    return _result(
       True,
       f"$/pass rate {actual} 较基线 {stored_baseline} 劣化 {regression_pct*100:.1f}%"
       f"（容差 {tolerance*100:.0f}% 以内）",
       stored_baseline, False,
    )


# ═══════════════════════════════════════════════════════════════
# Reliability
# ═══════════════════════════════════════════════════════════════

def analyze_reliability(tasks_dir: Path) -> dict[str, Any]:
    tasks_total = 0
    completed = 0
    failed = 0
    interrupted = 0
    resumed = 0
    greywall = 0
    native = 0
    headless = 0
    total_retries = 0
    subtask_total = 0
    blocked = 0
    retried = 0
    retried_success = 0
    # K5：中断恢复成功率。被中断过的任务（task_paused 事件），恢复后最终 status=="completed" 的比例。
    interrupted_tasks = 0        # 至少被中断过一次的任务数
    interrupted_then_completed = 0  # 中断过且最终 completed 的任务数

    for td in _scan_task_dirs(tasks_dir):
       meta = _read_meta(td)
       if not meta:
           continue
       tasks_total += 1
       status = meta.get("status", "")
       if status == "completed":
           completed += 1
       elif status == "failed":
           failed += 1
       results = meta.get("results", [])
       subtask_total += len(results)
       for r in results:
           r_status = r.get("status", "")
           if r_status == "blocked":
               blocked += 1
           if r.get("sandbox_type") == "greywall":
               greywall += 1
           elif r.get("sandbox_type") == "native":
               native += 1
           else:
               headless += 1
           rc = r.get("retry_count", 0)
           total_retries += rc
           if rc > 0:
               retried += 1
               if r_status == "completed":
                   retried_success += 1
       # 从日志统计中断/恢复 + K5 派生
       log_path = td / "execution.log"
       if log_path.exists():
           was_interrupted = False
           for ev in _read_log_events(log_path, "task_paused"):
               interrupted += 1
               was_interrupted = True
           for ev in _read_log_events(log_path, "subtask_resume"):
               resumed += 1
           # K5：该任务被中断过 → 计入分母；若最终 completed → 计入分子
           if was_interrupted:
               interrupted_tasks += 1
               if status == "completed":
                   interrupted_then_completed += 1

    total_sandbox = greywall + native + headless
    return {
       "tasks_total": tasks_total, "completed": completed, "failed": failed,
       "success_rate": round(completed / tasks_total * 100) if tasks_total else 0,
       "sandbox": {"greywall": greywall, "native": native, "headless": headless,
                    "greywall_pct": round(greywall / total_sandbox * 100) if total_sandbox else 0},
       "retries_total": total_retries,
       "retry_rate": round(total_retries / subtask_total * 100) if subtask_total else 0,
       "retry_success_rate": round(retried_success / retried * 100) if retried else 100,
       "blocked": blocked,
       "blocked_rate": round(blocked / subtask_total * 100) if subtask_total else 0,
       "interrupted": interrupted,
       "resumed": resumed,
       # K5 中断恢复成功率：被中断过的任务中最终 completed 的比例（PRD K5 年度目标 ≥99.9%）
       "interrupted_tasks": interrupted_tasks,
       "resume_success_rate": (
           round(interrupted_then_completed / interrupted_tasks * 100, 1)
           if interrupted_tasks else None
       ),
    }


# ═══════════════════════════════════════════════════════════════
# UX
# ═══════════════════════════════════════════════════════════════

def analyze_ux(tasks_dir: Path) -> dict[str, Any]:
    total = 0
    with_docs = 0
    plan_iterations = []
    agent_counts = {}
    skill_subtasks = 0
    subtask_total = 0

    for td in _scan_task_dirs(tasks_dir):
       meta = _read_meta(td)
       if not meta:
           continue
       total += 1
       if meta.get("reference_docs"):
           with_docs += 1
       for ev in _read_log_events(td / "execution.log", "plan_generate"):
           plan_iterations.append(ev.get("iteration", 1))
       for r in meta.get("results", []):
           subtask_total += 1
           at = r.get("agent_type_source", "default")
           agent_counts[at] = agent_counts.get(at, 0) + 1
       for st in meta.get("subtasks", []):
           if st.get("skills"):
               skill_subtasks += 1

    non_dev = sum(c for k, c in agent_counts.items() if k != "default")
    return {
       "tasks_total": total,
       "docs_usage_pct": round(with_docs / total * 100) if total else 0,
       "avg_plan_iterations": round(sum(plan_iterations) / len(plan_iterations), 1) if plan_iterations else 0,
       "agent_diversity_pct": round(non_dev / subtask_total * 100) if subtask_total else 0,
       "agent_distribution": agent_counts,
       "skill_usage_pct": round(skill_subtasks / subtask_total * 100) if subtask_total else 0,
    }


# 注：estimate_task_duration（M4 时间预估）已迁移到 planning.py。
# 它逻辑上是"预执行估算"（在线、嵌入 cmd_run），不是"离线评估"，放在 eval.py 会让
# 核心流程反向依赖评估模块，违背解耦原则。详见 docs/architecture.md「模块依赖与解耦原则」。


# ═══════════════════════════════════════════════════════════════
# CLI output
# ═══════════════════════════════════════════════════════════════

def cmd_eval(args=None) -> None:
    import sys
    from .config import AGENT_GO_DIR

    if args is not None and hasattr(args, "subcommand"):
       sub = args.subcommand
       task_id = getattr(args, "task_id", None) or ""
       all_mode = bool(getattr(args, "eval_all", False)) or task_id == "--all"
       baseline = getattr(args, "baseline", None)
       check_regression = bool(getattr(args, "check_regression", False))
       update_baseline = bool(getattr(args, "update_baseline", False))
    else:
       if len(sys.argv) < 3:
           console.print("Usage: agent_go eval <quality|perf|cost|reliability|ux|gate|bench|models|all> [task-id|--all]")
           return
       sub = sys.argv[2]
       task_id = sys.argv[3] if len(sys.argv) > 3 else ""
       all_mode = task_id == "--all"
       # 解析 --baseline X（用于 gate 子命令绝对阈值模式）
       baseline = None
       if "--baseline" in sys.argv:
           i = sys.argv.index("--baseline")
           if i + 1 < len(sys.argv):
               try:
                   baseline = float(sys.argv[i + 1])
               except ValueError:
                   pass
       check_regression = "--check-regression" in sys.argv
       update_baseline = "--update-baseline" in sys.argv

    if sub == "quality":
       if all_mode:
           _print_aggregate_quality(aggregate_quality(AGENT_GO_DIR))
       else:
           td = _resolve_task_dir(AGENT_GO_DIR, task_id)
           if td:
               _print_quality_report(analyze_quality(_read_meta(td), task_dir=td))
           else:
               console.print("暂无任务")
    elif sub == "perf":
       if all_mode:
           _print_aggregate_perf(aggregate_performance(AGENT_GO_DIR))
       else:
           td = _resolve_task_dir(AGENT_GO_DIR, task_id)
           if td:
               _print_perf_report(analyze_performance(_read_meta(td), td / "execution.log"))
           else:
               console.print("暂无任务")
    elif sub == "cost":
       _print_cost_report(analyze_cost(AGENT_GO_DIR))
    elif sub == "bench":
        # 模型对照评估编排器（S8，subprocess 隔离核心）
        from .bench import cmd_bench
        cmd_bench(args)
    elif sub == "baseline":
        # S10-P2：对照基线（claude -p 裸跑，不走 harness）
        from .bench import cmd_baseline
        cmd_baseline(args)
    elif sub == "models":
       # 模型生产力决策矩阵
       from .bench import cmd_models
       cmd_models(args)
    elif sub == "judge":
       # 交叉评判矩阵（S8 P1，第 2 层语义评估）
       from .cross_judge import cmd_judge
       cmd_judge(args)
    elif sub == "gate":
       # 发布门禁：两种模式互斥
       #   --check-regression：PRD "不劣化"语义，对比历史基线（劣化 > 容差即失败）
       #   --baseline X（默认）：绝对阈值模式（actual > X 即失败，X 缺省 0.05）
       if check_regression or update_baseline:
           result = gate_cost_regression(AGENT_GO_DIR, update=update_baseline)
           _print_gate_report(result)
       else:
           if baseline is None:
               baseline = 0.05
               console.warning("未指定 --baseline，使用 PRD Q3 默认 0.05（$/pass rate ≤ $0.05）")
               console.print("   提示：用 --check-regression 切换到「不劣化」语义（对比历史基线）")
           result = gate_cost(baseline, AGENT_GO_DIR)
           _print_gate_report(result)
       # 门禁失败 → 非零退出，CI 红灯
       if not result["passed"]:
           sys.exit(1)
    elif sub == "reliability":
       _print_reliability_report(analyze_reliability(AGENT_GO_DIR))
    elif sub == "ux":
       _print_ux_report(analyze_ux(AGENT_GO_DIR))
    elif sub == "all":
       console.print("═" * 60)
       agg_q = aggregate_quality(AGENT_GO_DIR)
       if agg_q:
           _print_aggregate_quality(agg_q)
       agg_p = aggregate_performance(AGENT_GO_DIR)
       if agg_p:
           _print_aggregate_perf(agg_p)
       _print_cost_report(analyze_cost(AGENT_GO_DIR))
       _print_reliability_report(analyze_reliability(AGENT_GO_DIR))
       _print_ux_report(analyze_ux(AGENT_GO_DIR))
       console.print("═" * 60)
    else:
       console.print(f"未知子命令: {sub}。可用: quality, perf, cost, reliability, ux, gate, bench, models, all")


def _resolve_task_dir(base_dir: Path, task_id: str) -> Optional[Path]:
    if task_id:
       td = Path(base_dir) / task_id
       return td if td.exists() else None
    tasks = _scan_task_dirs(base_dir)
    return tasks[0] if tasks else None


def _print_quality_report(q: Optional[dict[str, Any]]) -> None:
    if q is None:
       console.print("无数据")
       return
    console.print(f"\n质量报告 — {q['task_id']}")
    console.print("─" * 50)
    s = q["subtasks"]
    blocked = s.get("blocked", 0)
    console.print(f"  Subtask: {s['total']} total | {s['completed']} ok | {s['no_changes']} no-op | {s['failed']} fail | {blocked} blocked")
    console.print(f"  Q1 任务成功率:       {q['Q1_task_success_rate']}%")
    console.print(f"  Q2 Subtask成功率:    {q['Q2_subtask_success_rate']}%")
    console.print(f"  Q3 首次通过率:       {q['Q3_first_pass_rate']}%")
    console.print(f"  Q4 验证通过率:       {q['Q4_verify_pass_rate']}%")
    console.print(f"  Q5 新文件遗漏率:     {q['Q5_new_file_miss_rate']}%")
    console.print(f"  Q6 产物传递成功率:   {q['Q6_merge_success_rate']}%")
    console.print(f"  Q7 计划准确性:       P={q['Q7_plan_accuracy_precision']}% R={q['Q7_plan_accuracy_recall']}%")
    console.print(f"  Q8 重试修复成功率:   {q['Q8_retry_success_rate']}%")
    console.print(f"  Q9 级联阻断率:       {q['Q9_blocked_rate']}%")
    console.print(f"  Q10 平均重试次数:    {q.get('Q10_avg_retries', 0)}")
    q11 = q.get("Q11_false_positive_rate")
    if q11 is not None:
        console.print(f"  ⚠ Q11 假阳性率:       {q11}% ({q.get('Q11_flagged_count', 0)} flagged / {q.get('Q11_evaluated_count', 0)} evaluated)")
        conf = q.get("Q11_avg_confidence")
        if conf is not None:
            console.print(f"  ★ 平均语义置信度:     {conf}")
        auto = q.get("Q11_auto_trigger_rate")
        if auto is not None:
            console.print(f"  ★ 自动触发率:         {auto}%")
    cs = q["change_scale"]
    console.print(f"  变更规模:            avg {cs['avg_files']} files, +{cs['avg_insertions']}/-{cs['avg_deletions']}")
    console.print(f"  ─────────────────────────────")
    console.print(f"  评分: {q['score']}/100")
    console.print("─" * 50)


def _print_perf_report(p: Optional[dict[str, Any]]) -> None:
    if p is None:
       console.print("无数据")
       return
    console.print(f"\n性能报告 — {p['task_id']}")
    console.print("─" * 50)
    console.print(f"  P1 端到端耗时:       {p['P1_total_duration_sec']}s")
    console.print(f"  P2 Plan耗时:         {p['P2_plan_duration_ms']}ms")
    console.print(f"  P3 平均Subtask耗时:  {p['P3_avg_subtask_sec']}s")
    p4 = p["P4_duration_percentiles"]
    console.print(f"  P4 耗时分布:         P50={p4.get(50,0)}s P95={p4.get(95,0)}s P99={p4.get(99,0)}s")
    p5 = p.get("P5_phase_breakdown_pct", {})
    if p5:
       claude = p5.get("claude_execute_ms", 0)
       verify = p5.get("verification_ms", 0)
       console.print(f"  P5 阶段占比:         claude={claude}% verify={verify}% other={100-claude-verify}%")
    console.print(f"  P6 并发效率:         {p['P6_concurrency_efficiency_pct']}%")
    console.print(f"  ─────────────────────────────")
    console.print(f"  评分: {p['score']}/100")
    console.print("─" * 50)


def _print_cost_report(c: dict[str, Any]) -> None:
    console.print(f"\n💰 成本报告")
    console.print("─" * 50)
    console.print(f"  API 调用:            {c['total_calls']} 次")
    console.print(f"  Token:               {c['total_prompt_tokens']:,} in + {c['total_completion_tokens']:,} out")
    console.print(f"  预估费用:            ${c['estimated_cost_usd']}")
    # 成本来源透明化（D1/D2 修复）：真实 metering vs token 重算
    src = c.get("cost_source_breakdown") or {}
    if src:
       console.print(f"    来源 metering:     ${src.get('metering', 0)} (真实计费)")
       console.print(f"    来源 token 重算:   ${src.get('rebuilt', 0)} (补缺 cost_usd)")
    if c["by_model"]:
       for model, cost in c["by_model"].items():
           console.print(f"    {model}:  ${cost}")
    if c.get("by_role"):
       console.print(f"  按角色:")
       for role, v in c["by_role"].items():
           console.print(f"    {role}:  {v['calls']} 次, ${v['cost_usd']}")
    console.print(f"  API 错误:            {c['errors']} 次")
    # D5 可观测：降级事件 + 未知模型（价目表覆盖度监控）
    if c.get("fallback_events"):
       console.warning(f"降级事件:         {c['fallback_events']} 次 (result=fallback 或 fallback_reason 非空)")
    if c.get("unknown_model_events"):
       console.warning(f"未知模型事件:     {c['unknown_model_events']} 次 (无法计价，cost_usd 缺失且模型不在价目表)")
    if c.get("policy_violations"):
       for pv_type, count in c["policy_violations"].items():
           console.print(f"  🚩  政策违规:         {pv_type} ×{count}")
    console.print(f"  缓存命中:            {c['cache_hits']}/{c['cache_checks']} ({c['cache_hit_rate']}%)")
    console.print(f"  每任务成本:          ${c['avg_cost_per_task']}")
    console.print(f"  每子任务成本:        ${c.get('avg_cost_per_subtask', 0)}")
    console.print(f"  完成子任务:          {c.get('completed_subtasks', 0)} 个")
    dpp = c.get('dollar_per_pass_rate')
    dpp_str = f"${dpp}" if dpp is not None else "N/A"
    console.print(f"  ★ $/pass rate:       {dpp_str}  (北极星)")
    console.print("─" * 50)


def _print_gate_report(g: dict[str, Any]) -> None:
    """打印 $/pass rate 门禁判定结果。"""
    verdict = "✅ 通过" if g["passed"] else "❌ 不通过"
    actual_str = f"${g['actual']}" if g["actual"] is not None else "N/A"
    console.print(f"\n🚦 发布门禁 ($/pass rate): {verdict}")
    console.print("─" * 50)
    console.print(f"  实际 $/pass rate:    {actual_str}")
    console.print(f"  基线阈值:            ${g['baseline']}")
    console.print(f"  完成子任务:          {g['completed_subtasks']} 个")
    console.print(f"  累计成本:            ${g['estimated_cost_usd']}")
    console.print(f"  判定原因:            {g['reason']}")
    if not g["passed"]:
       console.print("  → 门禁失败，CI 应中断发布")
    console.print("─" * 50)


def _print_reliability_report(r: dict[str, Any]) -> None:
    console.print(f"\n🔧 可靠性报告")
    console.print("─" * 50)
    console.print(f"  任务完成率:          {r['success_rate']}% ({r['completed']}/{r['tasks_total']})")
    sand = r["sandbox"]
    console.print(f"  Sandbox:             greywall={sand['greywall_pct']}% native={sand['native']}/{sand['headless']}")
    console.print(f"  重试次数:            {r['retries_total']}")
    console.print(f"  重试率:              {r['retry_rate']}%")
    console.print(f"  重试修复成功率:      {r['retry_success_rate']}%")
    console.print(f"  阻断子任务:          {r['blocked']} 个 ({r['blocked_rate']}%)")
    console.print(f"  中断/恢复:           {r['interrupted']}/{r['resumed']}")
    # K5 中断恢复成功率（PRD K5 年度目标 ≥99.9%）
    rsr = r.get("resume_success_rate")
    rsr_str = f"{rsr}%" if rsr is not None else "N/A（无中断任务）"
    console.print(f"  ★ K5 中断恢复成功率: {rsr_str} ({r.get('interrupted_tasks', 0)} 个中断任务)")
    console.print("─" * 50)


def _print_ux_report(u: dict[str, Any]) -> None:
    console.print(f"\n📈 使用习惯报告")
    console.print("─" * 50)
    console.print(f"  分析任务数:          {u['tasks_total']}")
    console.print(f"  文档挂载率:          {u['docs_usage_pct']}%")
    console.print(f"  平均 Plan 迭代:      {u['avg_plan_iterations']}")
    console.print(f"  Agent 多样性:        {u['agent_diversity_pct']}%")
    if u["agent_distribution"]:
       console.print(f"  Agent 分布:          {u['agent_distribution']}")
    console.print(f"  Skill 使用率:        {u['skill_usage_pct']}%")
    console.print("─" * 50)


def _print_aggregate_quality(agg: Optional[dict[str, Any]]) -> None:
    if agg is None:
       console.print("无历史数据")
       return
    console.print(f"\n质量聚合 — {agg['tasks_analyzed']} 个任务")
    console.print("─" * 50)
    console.print(f"  平均成功率:          {agg['avg_success_rate']}%")
    console.print(f"  平均首次通过率:      {agg['avg_first_pass']}%")
    console.print(f"  平均验证通过率:      {agg['avg_verify_pass']}%")
    console.print(f"  平均新文件遗漏率:    {agg['avg_new_file_miss']}%")
    console.print(f"  平均产物传递成功率:  {agg['avg_merge_success']}%")
    console.print(f"  平均重试修复成功率:  {agg['avg_retry_success']}%")
    console.print(f"  平均级联阻断率:      {agg['avg_blocked_rate']}%")
    console.print(f"  平均评分:            {agg['avg_score']}/100")
    fp = agg.get("avg_false_positive_rate")
    if fp is not None:
        console.print(f"  ★ 平均假阳性率:       {fp}%")
    conf = agg.get("avg_semantic_confidence")
    if conf is not None:
        console.print(f"  ★ 平均语义置信度:     {conf}")
    console.print("─" * 50)


def _print_aggregate_perf(agg: Optional[dict[str, Any]]) -> None:
    if agg is None or agg["tasks_analyzed"] == 0:
       console.print("无历史数据")
       return
    console.print(f"\n性能聚合 — {agg['tasks_analyzed']} 任务, {agg['subtasks_total']} subtasks")
    console.print("─" * 50)
    console.print(f"  平均耗时:            {agg['avg_duration_sec']}s")
    console.print(f"  耗时分布:            P50={agg['P50_sec']}s P95={agg['P95_sec']}s P99={agg['P99_sec']}s")
    console.print(f"  平均任务耗时:        {agg['avg_task_duration_sec']}s")
    console.print("─" * 50)
