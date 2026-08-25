"""质量/性能/成本/可靠性/UX 评估分析 + eval gate（$/pass 基线 + 回归检测）。

聚合 bench 结果，按 per-role 成本、模型能力、任务难度分析质量，
维护成本基线并执行 gate 检查（绝对阈值 + 回归检测）。
"""
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
    events: list[dict[str, Any]] = []
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

    p1 = 0.0
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
from .pricing import (  # noqa: E402
    MODEL_PRICES,
    PROVIDER_DEFAULT_MODEL,
    LEGACY_PROVIDER_DEFAULT_MODEL,
    PROVIDER_DEFAULT_MODEL_CUTOFF,
)


def _local_tco_usd(model: str) -> float:
    """本地模型 TCO 成本（每次调用估算）。委托 metrics.local_tco_usd 共享实现。"""
    from .metrics import local_tco_usd as _shared_tco
    return _shared_tco(model)


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
    cost_from_local_tco = 0.0
    local_tco_by_model: dict[str, float] = {}
    # 缺 cost_usd 且模型在价目表的事件，按 token 重算（旧日志/fallback 用）
    rebuild_usage: dict[str, dict[str, int]] = {}
    unknown_model_events = 0   # 既无 cost_usd 又不在 MODEL_PRICES 的事件（监控价目表覆盖度）
    fallback_events = 0        # result="fallback" 或 fallback_reason 非空（PRD §line 173 留痕字段）
    policy_violations: dict[str, int] = {}  # 政策违规类型 → 次数
    # C3 诊断维度（R8/R13 字段，缺省即跳过，旧批次兼容）
    route_counts: dict[str, int] = {}           # route_target 分布（cloud/local/local_forced）
    hit_ratio_by_model: dict[str, list[float]] = {}  # R13 hit_ratio 按模型分档
    injection_counts: dict[str, int] = {}       # feedback_injected kind 分布（含 worker_diag 聚合）
    injected_events = 0                         # feedback_injected 非空的调用数

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
               # 本地 TCO（2026-08-12）：本地模型 metering 成本为 0，若配置了
               # local_model_cost[model] 则按每次调用折算，让本地模型纳入 $/pass/gate
               _local_tco = 0.0
               if ev_cost <= 0 and ev.get("is_local"):
                   _local_tco = _local_tco_usd(model)
               if _local_tco > 0:
                   cost_from_local_tco += _local_tco
                   local_tco_by_model[model] = local_tco_by_model.get(model, 0.0) + _local_tco
                   # 计入 by_role 的成本（TCO 口径）
                   by_role[role]["cost_usd"] += _local_tco
               elif ev_cost > 0:
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

               # C3：R8 路由分布 / R13 hit_ratio / 注入标记（字段缺省即跳过）
               _rt = ev.get("route_target", "")
               if _rt:
                   route_counts[_rt] = route_counts.get(_rt, 0) + 1
               _hr = ev.get("hit_ratio")
               if isinstance(_hr, (int, float)) and model:
                   hit_ratio_by_model.setdefault(model, []).append(float(_hr))
               _fb = ev.get("feedback_injected") or []
               if _fb:
                   injected_events += 1
                   for _k in _fb:
                       injection_counts[_k] = injection_counts.get(_k, 0) + 1
               # worker_diag 聚合事件（subtask 级）：并入注入 kind 分布
               if ev.get("role") == "worker_diag":
                   for _k, _n in (ev.get("injection_counts") or {}).items():
                       try:
                           injection_counts[_k] = injection_counts.get(_k, 0) + int(_n)
                       except (TypeError, ValueError):
                           continue

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
    # 本地 TCO 按模型并入 model_costs（不覆盖已有重算值）
    for model, tco in local_tco_by_model.items():
        model_costs[model] = round((model_costs.get(model, 0.0) or 0.0) + tco, 4)
    cost = round(cost_from_metering + cost_from_rebuild + cost_from_local_tco, 4)

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

    # C3 输出组装：route 分布 + cloud 回退告警 + hit_ratio 分档 + 注入分布
    route_total = sum(route_counts.values())
    route_distribution = {
       k: {"count": v, "pct": round(v / route_total * 100, 1)}
       for k, v in sorted(route_counts.items())
    } if route_total else {}
    route_cloud_warning = bool(route_total) and route_counts.get("cloud", 0) / route_total > 0.3
    hit_ratio_stats: dict[str, Any] = {}
    for _m, _vals in sorted(hit_ratio_by_model.items()):
       _s = sorted(_vals)
       _n = len(_s)
       _entry: dict[str, Any] = {
           "n": _n,
           "mean": round(sum(_s) / _n, 4),
           "p50": round(_s[_n // 2] if _n % 2 else (_s[_n // 2 - 1] + _s[_n // 2]) / 2, 4),
           "p90": round(_s[min(_n - 1, int(_n * 0.9))], 4),
       }
       if _n < 3:
           _entry["note"] = "样本<3，不参评"
       hit_ratio_stats[_m] = _entry

    return {
       "total_calls": total_calls, "total_prompt_tokens": total_prompt, "total_completion_tokens": total_completion,
       "estimated_cost_usd": cost,
       # 成本来源透明化：metering（真实）+ rebuild（token 重算补缺）
       "cost_source_breakdown": {"metering": round(cost_from_metering, 6), "rebuilt": cost_from_rebuild,
                             "local_tco": round(cost_from_local_tco, 6)},
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
       # C3 诊断维度（R8/R13 消费；无数据时为空结构，不报错）
       "route_distribution": route_distribution,
       "route_cloud_warning": route_cloud_warning,
       "hit_ratio_by_model": hit_ratio_stats,
       "diagnostics": {
           "feedback_injected_events": injected_events,
           "injection_counts": dict(sorted(injection_counts.items())),
       },
    }


# ═══════════════════════════════════════════════════════════════
# 发布门禁：$/pass rate（北极星指标）
# ═══════════════════════════════════════════════════════════════

def _gate_cost_from_records(records: list[dict[str, Any]]) -> tuple[Optional[float], int, float]:
    """从 bench results records 计算 $/pass 门禁指标（batch 隔离语义）。

    与 metric-freeze 对齐：valid_cost / diagnostic_pass（dollar_per_pass_diagnostic_usd）。
    相比 analyze_cost(AGENT_GO_DIR) 全库扫描，此路径只统计给定 batch 的记录，
    排除 timed_out 等无效任务，避免全库噪声淹没 batch 真实值（ISSUE-37）。

    Returns:
       (actual $/pass, completed_subtasks, estimated_cost_usd)
    """
    from .metrics import compute_frozen_metrics
    fm = compute_frozen_metrics(records or [])
    actual = fm.get("dollar_per_pass_diagnostic_usd")
    completed = fm.get("valid_task_count", 0)
    total_cost = fm.get("valid_cost_usd", 0.0)
    return actual, completed, total_cost


def gate_cost(baseline: float, tasks_dir: Path, records: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """$/pass rate 发布门禁。

    PRD 铁律：发布门禁「$/pass rate 不劣化」。取 $/pass rate（默认 analyze_cost 全库；
    传入 records 时用 batch 数据），与 baseline 比较，返回结构化判定结果，供 cmd_eval 决定退出码。

    失败语义：actual is not None 且 actual > baseline → 不通过（实际成本/通过率劣于基线）。
    无数据语义：actual is None（无完成任务或无 metering）→ 通过，但标注门禁未生效
               （早期仓库/新 fork 无数据时不阻挡 CI）。

    Args:
       baseline: 允许的 $/pass rate 上限（美元/通过子任务）。如 0.05 表示 ≤ $0.05/pass。
       tasks_dir: 任务目录（通常 AGENT_GO_DIR）。
       records: 可选 bench results records。非空时用 batch 数据计算 $/pass（ISSUE-37），
                忽略 tasks_dir 全库扫描。

    Returns:
       dict: {passed: bool, actual: float|None, baseline: float,
              completed_subtasks: int, estimated_cost_usd: float, reason: str}
    """
    if records:
        actual, completed, total_cost = _gate_cost_from_records(records)
    else:
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
                        update: bool = False,
                        records: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """$/pass rate 回归门禁（PRD "不劣化"语义）。

    对比当前 rate 与已存储基线，劣化幅度 > tolerance（默认 10%）→ 不通过。
    无基线时：首次运行自动写入基线并通过（建立基线）。
    actual is None（无数据）：通过，门禁未生效。

    Args:
       tasks_dir: 任务目录（基线文件存于此）。
       tolerance: 允许的劣化比例（0.10 = 10%）。当前 rate ≤ 基线×(1+tolerance) 即通过。
       update: True 时无论结果都更新基线（用于主动刷新基线，如模型升级后重置）。
       records: 可选 bench results records。非空时用 batch 数据计算 $/pass（ISSUE-37）。

    Returns:
       dict: {passed, actual, baseline, tolerance, regression_pct, reason, updated}
    """
    if records:
        actual, completed, total_cost = _gate_cost_from_records(records)
    else:
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
    greywatch = 0
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
           elif r.get("sandbox_type") == "greywatch":
               greywatch += 1
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

    total_sandbox = greywall + greywatch + native + headless
    return {
       "tasks_total": tasks_total, "completed": completed, "failed": failed,
       "success_rate": round(completed / tasks_total * 100) if tasks_total else 0,
       "sandbox": {"greywall": greywall, "greywatch": greywatch, "native": native,
                    "headless": headless,
                    "greywall_pct": round((greywall + greywatch) / total_sandbox * 100) if total_sandbox else 0},
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
    agent_counts: dict[str, int] = {}
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
    elif sub == "validate-schema":
        from .bench_schema import validate_results_file
        try:
            records = validate_results_file(getattr(args, "results", "eval_suite/results.jsonl"))
            console.print(f"schema valid: {len(records)} records")
        except (OSError, ValueError) as exc:
            console.error(f"schema invalid: {exc}")
            raise SystemExit(1)
    elif sub == "metric-freeze":
        from .metric_report import build_metric_freeze_report, write_metric_freeze_report
        try:
            report = build_metric_freeze_report(
                getattr(args, "results", "eval_suite/results.jsonl"),
                source_batch=getattr(args, "source_batch", ""),
                suite=getattr(args, "bench_suite", ""),
                catalog_path=getattr(args, "catalog", "") or None,
                config_path=getattr(args, "config_file", "") or None,
            )
            output = getattr(args, "report_output", "") or "metric-freeze-report.json"
            path = write_metric_freeze_report(report, output)
            console.print(f"Metric Freeze report written: {path}")
        except (OSError, ValueError) as exc:
            console.error(f"Metric Freeze failed: {exc}")
            raise SystemExit(1)
    elif sub == "batch-manifest":
        from .batch_governance import build_batch_manifest, write_batch_manifest
        try:
            manifest = build_batch_manifest(
                getattr(args, "results", "eval_suite/results.jsonl"),
                source_batch=getattr(args, "source_batch", ""),
                suite=getattr(args, "bench_suite", ""),
                catalog_path=getattr(args, "catalog", "") or None,
                config_path=getattr(args, "config_file", "") or None,
                proxy_context_path=getattr(args, "proxy_context", "") or None,
            )
            output = getattr(args, "manifest_output", "") or "manifest.json"
            path = write_batch_manifest(manifest, output)
            console.print(f"Batch manifest written: {path}")
        except (OSError, ValueError) as exc:
            console.error(f"Batch manifest failed: {exc}")
            raise SystemExit(1)
    elif sub == "baseline":
        # S10-P2：对照基线（claude -p 裸跑，不走 harness）
        from .bench import cmd_baseline
        cmd_baseline(args)
    elif sub == "cost-baseline":
        # S10：删失校正成本基线（排除 timed_out 右删失，P90×tolerance）
        from .bench import cmd_cost_baseline
        cmd_cost_baseline(args)
    elif sub == "models":
       # 模型生产力决策矩阵
       from .bench import cmd_models
       cmd_models(args)
    elif sub == "recommend":
       # CR-G5：bench 推荐 → worker_models 自动衔接（dry-run / --apply）
       from .bench import cmd_recommend
       cmd_recommend(args)
    elif sub == "insight":
       # M6.1 决策辅助 MVP：证据物化 + LLM 推理 → 结构化建议
       cmd_insight(args)
    elif sub == "calibrate-difficulty":
       # P2：基于 bench 实测自动校准任务难度标签（dry-run / --apply 写回 YAML）
       from .bench import cmd_calibrate_difficulty
       cmd_calibrate_difficulty(args)
    elif sub == "judge":
       # 交叉评判矩阵（S8 P1，第 2 层语义评估）
       from .cross_judge import cmd_judge
       cmd_judge(args)
    elif sub == "gate":
       # 发布门禁：两种模式互斥
       #   --check-regression：PRD "不劣化"语义，对比历史基线（劣化 > 容差即失败）
       #   --baseline X（默认）：绝对阈值模式（actual > X 即失败，X 缺省 0.05）
       #   --results FILE（可选）：用 bench batch 数据计算 $/pass（ISSUE-37），
       #    而非扫描 AGENT_GO_DIR 全库（全库含历史高成本模型/探索任务，噪声淹没 batch 真实值）。
       gate_records = None
       results_arg = getattr(args, "results", "") if args is not None else ""
       if results_arg and results_arg != "eval_suite/results.jsonl":
            try:
                from .bench_schema import validate_results_file
                gate_records = validate_results_file(results_arg)
                console.print(f"[gate] 使用 batch 数据: {results_arg} ({len(gate_records)} records, source_batch={gate_records[0].get('source_batch', '') if gate_records else ''})")
            except (OSError, ValueError) as exc:
                console.error(f"[gate] 读取 --results 失败，回退 AGENT_GO_DIR 全库: {exc}")
                gate_records = None
       if check_regression or update_baseline:
            result = gate_cost_regression(AGENT_GO_DIR, update=update_baseline, records=gate_records)
            _print_gate_report(result)
       else:
            if baseline is None:
                baseline = 0.05
                console.warning("未指定 --baseline，使用 PRD Q3 默认 0.05（$/pass rate ≤ $0.05）")
                console.print("   提示：用 --check-regression 切换到「不劣化」语义（对比历史基线）")
            result = gate_cost(baseline, AGENT_GO_DIR, records=gate_records)
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
    console.print("  ─────────────────────────────")
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
    console.print("  ─────────────────────────────")
    console.print(f"  评分: {p['score']}/100")
    console.print("─" * 50)


def _print_cost_report(c: dict[str, Any]) -> None:
    console.print("\n💰 成本报告")
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
       console.print("  按角色:")
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
    console.print("\n🔧 可靠性报告")
    console.print("─" * 50)
    console.print(f"  任务完成率:          {r['success_rate']}% ({r['completed']}/{r['tasks_total']})")
    sand = r["sandbox"]
    console.print(f"  Sandbox:             greywall={sand['greywall_pct']}% (watch={sand.get('greywatch', 0)}) native={sand['native']}/{sand['headless']}")
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
    console.print("\n📈 使用习惯报告")
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


# ── M6.1 决策辅助：eval insight（证据物化 + LLM 推理 → 结构化建议）──────────

_INSIGHT_OUTPUT_SCHEMA = """输出要求：只输出合法 JSON 数组（不要 markdown 包裹、不要解释文字），每个元素是一条建议，字段：
{
  "problem": "问题描述（简短）",
  "evidence_refs": ["证据引用"],
  "cause_hypothesis": "根因假设",
  "action": "建议动作（人类可读）",
  "action_type": "可自动应用的动作类型，从以下枚举选一：worker_models（改 worker_models 难度路由）/fallback_chain（改 worker_models_fallback_chain 降级链）/role_model（改 router.roles 角色模型）/cost_budget（改 cost_control.max_budget_usd）/manual（无法自动应用，需人工执行）",
  "action_payload": {"自动应用的参数对象，JSON 格式；manual 类型为 {}}",
  "applies_when": "适用前提（环境/配置条件，如 'goal_policy=force 时'、'仅当 worker 为 flash 时'）。必须基于环境快照声明该建议的生效条件，避免环境变化后建议失效",
  "expected_impact": "预期影响（量化目标方向）",
  "cost_risk": "成本/风险",
  "confidence": 0.0到1.0的数,
  "requires_approval": true
}

【action_type 对应 payload 格式】（apply 时按此执行 config 变更）：
- worker_models: {"easy": "<model>", "medium": "<model>", "hard": "<model>"}
- fallback_chain: {"difficulty": "easy|medium|hard", "chain": ["<model1>", "<model2>"]}
- role_model: {"role": "planner|evaluator|worker|reviewer", "model": "<model>"}
- cost_budget: {"max_budget_usd": <数值>}
- manual: {}

【evidence_refs 只能使用以下证据路径前缀】（禁止使用其他格式）：
- "metrics/<指标名>"：可用指标 pass_rate_diagnostic / accepted_delivery_rate / dollar_per_pass_usd / valid_cost_usd / first_pass_rate / timeout_rate
- "failure_class/<类名>"：失败类别（如 verification_failure/timeout/infrastructure_failure/model_failure）
- "task/<task_id>"：具体任务（如 task/add-tag-system）
- "environment/<配置项>"：plan_model / goal_policy / worker_models / router_enabled
- "batch"：批次整体（直接用字符串 "batch"，无需 task_id）
证据引用必须来自上述证据上下文，不允许凭空编造数据。

【applies_when 必须声明】：每条建议必须基于环境快照（plan_model/goal_policy/worker_models 等）声明其适用前提。若建议依赖特定配置状态（如 goal_policy=force），必须写明；若建议在任何环境都适用，写 "通用"。这是防止环境变化后建议失效的关键约束。"""


def _insight_llm(evidence: dict, goal: str, plan: str, config: dict, logger) -> str:
    """LLM 推理：注入证据上下文 + 目标 + 计划候选，输出 JSON 建议列表。"""
    from .evidence import evidence_to_prompt_context
    from .api import call_api

    ctx = evidence_to_prompt_context(evidence)
    goal_text = goal or "提高任务通过率并控制成本"
    plan_text = plan or "（未指定，从常见策略候选：换模型/调整降级链/难道路由/e2e 判定/验证白名单/环境修复）"
    prompt = (
        f"你是一个软件交付策略分析师。基于以下 bench 批次的证据，针对分析目标给出优化建议。\n\n"
        f"===== 分析目标 =====\n{goal_text}\n\n"
        f"===== 预设计划（行动候选） =====\n{plan_text}\n\n"
        f"===== 证据（真实数据，仅可基于此推理） =====\n{ctx}\n===== 结束 =====\n\n"
        f"{_INSIGHT_OUTPUT_SCHEMA}"
    )
    messages = [
        {"role": "system", "content": "You are a strategy analyst. Output ONLY valid JSON. No markdown, no explanation."},
        {"role": "user", "content": prompt},
    ]
    return call_api(config, messages, logger)


def _parse_insight_suggestions(content: str) -> list[dict]:
    """解析 LLM 返回的 JSON 建议列表（容错：markdown 包裹/前缀文字剥离）。"""
    text = content.strip()
    # 剥离 markdown 代码块
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    # 找第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    # LLM 输出 schema {"suggestions": [...]}：提取内层列表；直接 list 输入也兼容
    if isinstance(data, dict):
        sug = data.get("suggestions")
        if isinstance(sug, list):
            return sug
        return [data] if data else []
    return data if isinstance(data, list) else []


def _validate_suggestion_evidence(suggestion: dict, evidence: dict) -> list[str]:
    """校验 evidence_refs 是否在证据包中存在（防 LLM 凭空编造）。返回缺失引用列表。

    宽松匹配：支持前缀匹配（metrics/x 匹配 metrics 任一 key；task/x 匹配任务 id 包含；
    failure_class/x 匹配失败类名；environment/x 匹配环境 key；batch 直接通过）。
    """
    refs = suggestion.get("evidence_refs") or []
    missing = []
    metrics_keys = set(evidence.get("metrics", {}).keys())
    env_keys = set(evidence.get("environment", {}).keys())
    task_ids = [r.get("task_id", "") for r in evidence.get("per_task", [])]
    fc_names = set(evidence["failure_modes"]["by_failure_class"].keys())
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            continue
        ref_l = ref.lower()
        if ref_l in ("batch", "evidence", "证据"):
            continue
        ok = False
        if "/" in ref:
            top, sub = ref.split("/", 1)
            top_l = top.lower()
            if top_l == "metrics":
                ok = any(sub in k or k in sub for k in metrics_keys) or sub in metrics_keys
            elif top_l in ("failure_class", "failure_modes", "failure"):
                ok = any(sub in f or f in sub for f in fc_names) or sub in fc_names
            elif top_l == "environment":
                ok = any(sub in k or k in sub for k in env_keys) or sub in env_keys
            elif top_l in ("task", "task_id"):
                ok = any(sub in t or t.endswith(sub) for t in task_ids)
            else:
                ok = any(sub in t for t in task_ids)
        else:
            ok = (any(ref in t or t.endswith(ref) for t in task_ids)
                  or ref in fc_names or ref in metrics_keys)
        if not ok:
            missing.append(ref)
    return missing


_APPLIABLE_ACTION_TYPES = ("worker_models", "fallback_chain", "role_model", "cost_budget")


def _apply_insight_action(action_type: str, payload: dict) -> dict:
    """M6.5 确认后应用：把 insight 建议的 action_type+payload 落到当前生效配置文件。

    已知 action_type 自动执行 config 变更；未知/manual 返回 skipped。
    执行时：备份原配置 → 写回 → decision log 记录（含 evidence/impact）。
    返回 {applied: bool, change: str, backup: str, error?: str}。
    """
    import json as _json
    import shutil as _shutil
    import time as _time
    from .profiles import active_config_source
    from .decision_log import record_decision

    if action_type == "manual":
        return {"applied": False, "change": "manual 类型（需人工执行），未自动应用", "backup": ""}
    if action_type not in _APPLIABLE_ACTION_TYPES:
        return {"applied": False, "change": f"未知 action_type: {action_type}（未自动应用）", "backup": ""}
    if not isinstance(payload, dict):
        return {"applied": False, "change": "payload 非 JSON 对象", "backup": ""}

    target = active_config_source()
    try:
        data = _json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (_json.JSONDecodeError, OSError) as e:
        return {"applied": False, "change": f"配置文件读取失败: {e}", "backup": ""}

    # 应用变更
    if action_type == "worker_models":
        data["worker_models"] = payload
    elif action_type == "fallback_chain":
        diff = str(payload.get("difficulty", "hard"))
        chain = payload.get("chain") or []
        data.setdefault("worker_models_fallback_chain", {})[diff] = chain
    elif action_type == "role_model":
        role = str(payload.get("role", ""))
        model = str(payload.get("model", ""))
        if not role or not model:
            return {"applied": False, "change": "role_model 需 role+model", "backup": ""}
        data.setdefault("router", {}).setdefault("roles", {}).setdefault(role, {})["model"] = model
    elif action_type == "cost_budget":
        data.setdefault("cost_control", {})["max_budget_usd"] = payload.get("max_budget_usd")
    change_desc = f"insight apply {action_type}: {_json.dumps(payload, ensure_ascii=False)[:120]}"

    # 备份 + 写回
    ts = _time.strftime("%Y%m%d-%H%M%S")
    backup = target.parent / f"{target.stem}.insight-backup-{ts}.json"
    try:
        _shutil.copy2(target, backup)
        target.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return {"applied": False, "change": f"写回失败: {e}", "backup": ""}

    # decision log
    try:
        record_decision(
            change=change_desc,
            goal="insight 建议应用（M6.5 确认后自动应用）",
            source="insight.apply",
            confirmer="cli",
        )
    except Exception:
        pass
    return {"applied": True, "change": change_desc, "backup": str(backup)}


def cmd_insight(args=None) -> None:
    """eval insight（M6.1）：证据物化 + LLM 推理 → 结构化建议。

    --results <batch>（必选，immutable 批次）；--analysis-goal/--analysis-plan/--output。
    """
    import logging
    logger = logging.getLogger(__name__)
    results_arg = getattr(args, "results", "") or ""
    if not results_arg.strip():
        console.error("insight 需要 --results <batch 路径>（如 eval_suite/baselines/m4-mixB-hard）")
        return
    batch = results_arg.split(",")[0].strip()
    goal = getattr(args, "analysis_goal", "") or ""
    plan = getattr(args, "analysis_plan", "") or ""
    output = getattr(args, "output", "") or ""

    from .evidence import materialize_evidence, EvidenceError
    try:
        evidence = materialize_evidence(batch)
    except EvidenceError as e:
        console.error(f"证据物化失败: {e}")
        return

    console.print(f"📊 证据物化完成: {evidence['source_batch']} ({evidence['record_count']} 条记录, hash={evidence['evidence_hash']})")
    console.print(f"   通过率: {evidence['metrics'].get('pass_rate_diagnostic')} | $/pass: {evidence['metrics'].get('dollar_per_pass_usd')}")
    console.print(f"   失败模式: {list(evidence['failure_modes']['by_failure_class'].keys())}")

    from .config import load_config
    config = load_config()
    console.print("\n🤖 LLM 分析推理中...")
    try:
        content = _insight_llm(evidence, goal, plan, config, logger)
    except Exception as e:
        console.error(f"LLM 推理失败: {e}")
        return

    suggestions = _parse_insight_suggestions(content)
    if not suggestions:
        console.error("LLM 返回无法解析为建议 JSON。原始响应预览：")
        console.print(content[:300])
        return

    # 证据引用后校验
    for s in suggestions:
        missing = _validate_suggestion_evidence(s, evidence)
        s["_evidence_missing"] = missing

    valid = [s for s in suggestions if not s["_evidence_missing"]]
    dropped = len(suggestions) - len(valid)
    if dropped:
        console.warning(f"⚠️ {dropped} 条建议因 evidence_refs 无效被丢弃（防凭空编造）")

    # M6.5：--apply <index> 应用第 index 条建议（确认后自动应用）
    apply_idx = getattr(args, "apply_index", None)
    if apply_idx is not None:
        try:
            idx = int(apply_idx)
        except (TypeError, ValueError):
            console.error(f"--apply 需为建议序号（1-{len(valid)}）: {apply_idx}")
            return
        if not (1 <= idx <= len(valid)):
            console.error(f"--apply 序号超出范围: {idx}（共 {len(valid)} 条有效建议）")
            return
        sug = valid[idx - 1]
        action_type = str(sug.get("action_type", "manual"))
        payload = sug.get("action_payload") or {}
        console.print(f"\n🔧 应用建议 {idx}: {sug.get('problem', '?')}")
        console.print(f"   action_type={action_type} | payload={json.dumps(payload, ensure_ascii=False)[:100]}")
        result = _apply_insight_action(action_type, payload)
        if result["applied"]:
            console.success(f"✅ 已应用: {result['change']}")
            console.print(f"   备份: {result['backup']}")
            console.print("   建议复跑验证: agent_go eval bench --tasks ... 后对比通过率")
        else:
            console.warning(f"⚠️ 未自动应用: {result['change']}")
        return

    # 输出
    report_lines = [
        f"# 决策辅助洞察（{evidence['source_batch']}）",
        "",
        f"- 分析目标: {goal or '（未指定）'}",
        f"- 通过率: {evidence['metrics'].get('pass_rate_diagnostic')} | $/pass: {evidence['metrics'].get('dollar_per_pass_usd')}",
        f"- 建议数: {len(valid)} 条（{dropped} 条被证据校验丢弃）",
        "",
    ]
    for i, s in enumerate(valid, 1):
        report_lines += [
            f"## 建议 {i}: {s.get('problem', '?')}",
            f"- 根因假设: {s.get('cause_hypothesis', '?')}",
            f"- 证据: {', '.join(s.get('evidence_refs', []))}",
            f"- 建议动作: {s.get('action', '?')}",
            f"- 适用前提: {s.get('applies_when', '通用')}",
            f"- 预期影响: {s.get('expected_impact', '?')}",
            f"- 成本/风险: {s.get('cost_risk', '?')}",
            f"- 置信度: {s.get('confidence', '?')} | 需人工确认: {s.get('requires_approval', True)}",
            "",
        ]
    report = "\n".join(report_lines)

    if output == "-" or not output:
        console.print("\n" + report)
    else:
        out_path = Path(output)
        out_path.write_text(report, encoding="utf-8")
        console.print(f"\n✅ 洞察报告已写入: {out_path}")
    # M6.3：无论 --output 指定与否，报告同时归档到 ~/.agent_go/insights/（web 可消费）
    try:
        from .config import AGENT_GO_DIR
        ins_dir = AGENT_GO_DIR / "insights"
        ins_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        batch_name = evidence["source_batch"].replace("/", "_")
        (ins_dir / f"{batch_name}-{ts}.md").write_text(report, encoding="utf-8")
    except OSError:
        pass
    # 结构化 JSON 输出（stdout 可管道消费）
    console.print("\n" + json.dumps(valid, ensure_ascii=False, indent=2))
