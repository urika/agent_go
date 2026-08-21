import subprocess
import json
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "collect_timing", "collect_change_stats",
    "collect_merge_result", "extract_usage",
    "estimate_cost", "DEFAULT_PRICING", "aggregate_metering",
    "compute_frozen_metrics", "is_valid_metric_task",
    "aggregate_failure_classes", "local_tco_usd",
    "compute_trust_metrics", "compute_blind_spot_hit_rate",
]

# local_model_cost 配置缓存（local_tco_usd 惰性加载一次）
_local_tco_loaded: bool = False
_local_tco_cost: dict = {}


def local_tco_usd(model: str) -> float:
    """本地模型 TCO 成本（每次调用估算）。

    读取优先级（P2.2 local_model_cost 迁入 ① registry）：
    1. ① registry：按 backend_model（真实后端模型名，metering actual_model）或
       id 匹配，返回该实体的 cost.tco_per_call
    2. fallback config.local_model_cost[model]（兼容现有，registry 未注册时）

    本地模型 metering cost_usd=0，直接进 $/pass 会让 gate 视为"免费"失真。
    配置后返回该模型每次调用的 TCO 估算成本（电费 + 硬件折旧）。未配置返回 0。

    共享函数：bench（_collect_result 聚合 metering）与 eval（analyze_cost）
    均调用，保证 metric-freeze/gate 的本地基线 $/pass 含 TCO。
    """
    # ① registry 优先（按真实后端模型名/id 匹配）
    try:
        from .models_registry import load_registry
        for entity in load_registry().values():
            if entity.cost.tco_per_call and (
                entity.backend_model == model or entity.id == model
            ):
                return float(entity.cost.tco_per_call)
    except Exception:
        pass

    # fallback：config.local_model_cost（兼容现有）
    global _local_tco_loaded, _local_tco_cost
    if not _local_tco_loaded:
        _local_tco_loaded = True
        try:
            from .config import load_config
            _local_tco_cost = load_config().get("local_model_cost", {}) or {}
        except Exception:
            _local_tco_cost = {}
    if not _local_tco_cost:
        return 0.0
    return float(_local_tco_cost.get(model, 0.0) or 0.0)


def is_valid_metric_task(record: dict[str, Any]) -> bool:
    """Return whether a record belongs in product KPI denominators.

    Explicitly invalid records and harness/user/system failures are excluded;
    model, verification, timeout, and delivery outcomes remain observable.
    """
    if record.get("valid_task") is False or record.get("excluded") is True:
        return False
    return record.get("failure_class") not in {
        "budget_abort", "infrastructure_failure", "user_cancelled", "system_error",
    }


def compute_frozen_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the M0-6 product and diagnostic metrics deterministically.

    Rates are decimals in [0, 1].  Empty denominators return ``None`` rather
    than zero so "no observations" cannot be confused with failure.
    """
    valid = [r for r in records if is_valid_metric_task(r)]
    excluded = [r for r in records if not is_valid_metric_task(r)]
    valid_count = len(valid)
    accepted_count = sum(1 for r in valid if r.get("accepted_delivery") is True)
    valid_cost = sum(float(r.get("total_cost_usd") or 0.0) for r in valid)

    def rate(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 6) if denominator else None

    failure_summary = aggregate_failure_classes(records)
    failure_counts = failure_summary["failure_class_counts"]
    exclusion_reasons = failure_summary["excluded_reasons"]

    retries = sum(1 for r in valid if (r.get("total_retries") or 0) > 0)
    first_pass = sum(
        1 for r in valid
        if r.get("binary_pass") is True and (r.get("total_retries") or 0) == 0
    )
    timeout_count = sum(1 for r in valid if r.get("failure_class") == "timeout")
    delivery_failure_count = sum(1 for r in valid if r.get("failure_class") == "delivery_failure")
    pr_created_count = sum(1 for r in valid if r.get("pr_created") is True)
    accepted_elapsed = [float(r.get("elapsed_sec") or 0.0) for r in valid if r.get("accepted_delivery") is True]
    human_minutes = sum(float(r.get("human_intervention_minutes") or 0.0) for r in valid)
    diagnostic_pass = sum(float(r.get("pass_rate") or 0.0) for r in valid)

    return {
        "valid_task_count": valid_count,
        "excluded_task_count": len(excluded),
        "excluded_reasons": exclusion_reasons,
        "failure_class_counts": failure_counts,
        "failure_class_summary": failure_summary,
        "accepted_delivery_count": accepted_count,
        "accepted_delivery_rate": rate(accepted_count, valid_count),
        "valid_cost_usd": round(valid_cost, 6),
        "cost_per_accepted_delivery_usd": round(valid_cost / accepted_count, 6) if accepted_count else None,
        "first_pass_rate": rate(first_pass, valid_count),
        "time_to_accepted_delivery_sec": (
            round(sum(accepted_elapsed) / len(accepted_elapsed), 6) if accepted_elapsed else None
        ),
        "human_intervention_minutes": round(human_minutes, 6),
        "timeout_rate": rate(timeout_count, valid_count),
        "retry_rate": rate(retries, valid_count),
        "delivery_failure_rate": rate(delivery_failure_count, valid_count),
        "pr_creation_rate": rate(pr_created_count, valid_count),
        # Diagnostic only: compare within identical suite + source_batch.
        "pass_rate_diagnostic": rate(1, valid_count) if valid_count == 0 else round(diagnostic_pass / valid_count, 6),
        "dollar_per_pass_diagnostic_usd": round(valid_cost / diagnostic_pass, 6) if diagnostic_pass else None,
    }


def aggregate_failure_classes(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate failure classes without collapsing operational failures.

    Product metrics count timeout as a failure. Timed-out records are also
    marked right-censored for cost-baseline analysis; cleanup races are kept
    separate and never counted as timeout failures.
    """
    from .failure import FAILURE_CLASSES, failure_policy

    counts = {failure_class: 0 for failure_class in sorted(FAILURE_CLASSES)}
    costs = {failure_class: 0.0 for failure_class in sorted(FAILURE_CLASSES)}
    excluded_reasons: dict[str, int] = {}
    unclassified = 0
    valid_count = 0
    excluded_count = 0
    timeout_failure_count = 0
    timeout_right_censored_count = 0
    timeout_cleanup_race_count = 0

    for record in records:
        failure_class = record.get("failure_class")
        if failure_class in counts:
            counts[failure_class] += 1
            costs[failure_class] += float(record.get("total_cost_usd") or 0.0)
        else:
            unclassified += 1
        if is_valid_metric_task(record):
            valid_count += 1
        else:
            excluded_count += 1
            reason = failure_class or "invalid_task"
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1

        timed_out = bool(record.get("timed_out"))
        cleanup_race = record.get("kill_reason") == "cleanup_race"
        if cleanup_race:
            timeout_cleanup_race_count += 1
        if failure_class == "timeout" and not cleanup_race:
            timeout_failure_count += 1
        if timed_out and not cleanup_race:
            timeout_right_censored_count += 1

    capability_failure_count = sum(
        counts[failure_class]
        for failure_class in counts
        if failure_policy(failure_class)["capability_failure"]
    )
    return {
        "failure_class_counts": counts,
        "failure_class_cost_usd": {key: round(value, 6) for key, value in costs.items()},
        "valid_task_count": valid_count,
        "excluded_task_count": excluded_count,
        "excluded_reasons": excluded_reasons,
        "unclassified_count": unclassified,
        "capability_failure_count": capability_failure_count,
        "timeout_disposition": {
            "product_semantics": "failure",
            "timeout_failure_count": timeout_failure_count,
            "right_censored_for_cost_baseline_count": timeout_right_censored_count,
            "cleanup_race_count": timeout_cleanup_race_count,
        },
    }

def collect_timing(worktree_create_ms: float, merge_upstream_ms: float, claude_execute_ms: float,
                   verification_ms: float, git_commit_ms: float) -> dict[str, float]:
    return {
        "worktree_create_ms": round(worktree_create_ms),
        "merge_upstream_ms": round(merge_upstream_ms),
        "claude_execute_ms": round(claude_execute_ms),
        "verification_ms": round(verification_ms),
        "git_commit_ms": round(git_commit_ms),
    }


def collect_change_stats(worktree_path: Path) -> dict[str, Any]:
    insertions = 0
    deletions = 0
    actual_files = []

    numstat = subprocess.run(
        ["git", "diff", "--numstat", "HEAD"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    for line in numstat.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            insertions += int(parts[0]) if parts[0] != "-" else 0
            deletions += int(parts[1]) if parts[1] != "-" else 0
            actual_files.append(parts[2])

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    new_files = 0
    for line in status_result.stdout.strip().split("\n"):
        if line.startswith("??"):
            new_files += 1
            filename = line[3:]
            if filename not in actual_files:
                actual_files.append(filename)

    return {
        "files_changed": len(actual_files),
        "insertions": insertions,
        "deletions": deletions,
        "new_files": new_files,
        "modified_files": len(actual_files) - new_files,
        "actual_files": actual_files,
    }


def collect_merge_result(upstream_id: str, success: bool, conflict_files: Optional[list[str]] = None) -> dict[str, Any]:
    result: dict[str, Any] = {"upstream": upstream_id, "status": "success" if success else "conflict"}
    if conflict_files:
        result["conflict_files"] = conflict_files
    return result


def extract_usage(api_response: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    usage = api_response.get("usage", {})
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "model": model,
        "provider": provider,
    }


# ═══════════════════════════════════════════════════════════════
# Cost Estimation
# ═══════════════════════════════════════════════════════════════

# 定价表：每百万 token 的价格（input, output），单位美元
# 来源：各 provider 官方定价页，2026-07 采集
DEFAULT_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    ("anthropic", "claude-sonnet-4-20250514"):     (3.0, 15.0),
    ("anthropic", "claude-haiku-4-5-20251001"):    (0.80, 4.0),
    ("anthropic", "claude-opus-4-20250514"):        (15.0, 75.0),
    # OpenAI
    ("openai", "gpt-4o"):                           (2.50, 10.0),
    ("openai", "gpt-4o-mini"):                      (0.15, 0.60),
    # DeepSeek（deepseek-chat/reasoner 已于 2026-07-24 弃用，由 v4-flash 替代）
    ("deepseek", "deepseek-v4-flash"):              (0.14, 0.28),
    ("deepseek", "deepseek-v4-pro"):                (0.435, 0.87),
    ("deepseek", "deepseek-chat"):                  (0.27, 1.10),   # 已弃用，保留向后兼容
    ("deepseek", "deepseek-reasoner"):              (0.55, 2.19),   # 已弃用，保留向后兼容
    # DeepSeek 经代理 OpenAI 端点（provider=openai 时 provider 对齐，避免查不到 → 0）
    ("openai", "deepseek-v4-flash"):                (0.14, 0.28),
    ("openai", "deepseek-v4-pro"):                  (0.435, 0.87),
    # 智谱 GLM-5.3（anthropic 兼容直连，2026-08 官方 ¥4.2/¥16.8 ≈ $0.6/$2.4）
    ("anthropic", "glm-5.3"):                       (0.60, 2.40),
    ("zhipu", "glm-5.3"):                           (0.60, 2.40),
    # 月之暗面 Kimi K3 / kimi-for-coding（anthropic 兼容直连，$3/$15）
    ("anthropic", "kimi-for-coding"):               (3.0, 15.0),
    ("anthropic", "k3"):                            (3.0, 15.0),
    ("moonshot", "kimi-for-coding"):                (3.0, 15.0),
    ("moonshot", "k3"):                             (3.0, 15.0),
    # 本地模型 — 成本为 0
    ("custom", "*"):                                 (0.0, 0.0),
}


def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 API 调用成本（美元）。

    按 provider + model 查找定价表，计算 input + output token 的总成本。
    未匹配的 provider/model 组合返回 0（不阻塞流程）。

    Args:
        provider: "anthropic" | "openai" | "deepseek" | "custom"
        model: 具体模型名
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数

    Returns:
        估算成本（美元）
    """
    key = (provider, model)
    wildcard = (provider, "*")
    prices = DEFAULT_PRICING.get(key) or DEFAULT_PRICING.get(wildcard, (0, 0))
    input_cost = (prompt_tokens / 1_000_000) * prices[0]
    output_cost = (completion_tokens / 1_000_000) * prices[1]
    return input_cost + output_cost


def aggregate_metering(metering_path: Path) -> dict[str, Any]:
    """汇总 metering.jsonl，返回总 token / 总成本 / 总延迟。

    返回:
        {
            "total_calls": int,
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_tokens": int,
            "cost_usd": float,
            "latency_ms": float,
            "by_role": {role: {"calls": int, "cost_usd": float}},
        }
    """
    totals: dict[str, Any] = {
        "total_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0.0,
        "by_role": {},
    }
    if not metering_path.exists():
        return totals

    try:
        for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["total_calls"] += 1
            pt = int(event.get("prompt_tokens", 0) or 0)
            ct = int(event.get("completion_tokens", 0) or 0)
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["total_tokens"] += pt + ct
            totals["cost_usd"] += float(event.get("cost_usd", 0.0) or 0.0)
            totals["latency_ms"] += float(event.get("latency_ms", 0.0) or 0.0)

            role = str(event.get("role", "unknown"))
            if role not in totals["by_role"]:
                totals["by_role"][role] = {"calls": 0, "cost_usd": 0.0}
            totals["by_role"][role]["calls"] += 1
            totals["by_role"][role]["cost_usd"] += float(event.get("cost_usd", 0.0) or 0.0)
    except OSError:
        pass

    totals["cost_usd"] = round(totals["cost_usd"], 6)
    totals["latency_ms"] = round(totals["latency_ms"], 2)
    for role in totals["by_role"]:
        totals["by_role"][role]["cost_usd"] = round(totals["by_role"][role]["cost_usd"], 6)
    return totals


# K12 阈值（PRD §办公能力扩展）：MCP 工具调用成功率 ≥95%
MCP_TOOL_SUCCESS_THRESHOLD = 0.95


def compute_mcp_tool_success_rate(events: list[dict], exclude_user_config_errors: bool = True) -> dict[str, Any]:
    """K12：MCP 工具调用成功率（PRD ≥95%，排除用户配置错误）。

    Args:
        events: 工具派发结果事件列表，每项形如
            {"tool": "mcp__server__tool", "success": bool,
             "error_type": "server_error" | "user_config_error" | "timeout" | "other" | None}
            - user_config_error：工具未注册 / MCP 服务器未配置（用户环境问题，不计模型/agent 成败）
        exclude_user_config_errors: True（默认）时把 user_config_error 从分子分母都排除

    Returns:
        {"total": len(events), "denominator": n(计入分母数), "successes": k,
         "success_rate": 0-1 或 None(无计入事件), "excluded_user_config": int,
         "passes_threshold": bool(rate ≥ 0.95)}
    """
    if not events:
        return {"total": 0, "denominator": 0, "successes": 0,
                "success_rate": None, "excluded_user_config": 0,
                "passes_threshold": False}
    included = [e for e in events
                if not (exclude_user_config_errors and e.get("error_type") == "user_config_error")]
    n = len(included)
    k = sum(1 for e in included if e.get("success"))
    # 排除后无计入事件 → rate None（无数据可判，不过阈值）
    rate = round(k / n, 4) if n else None
    return {
        "total": len(events),
        "denominator": n,
        "successes": k,
        "success_rate": rate,
        "excluded_user_config": len(events) - n,
        "passes_threshold": bool(rate is not None and rate >= MCP_TOOL_SUCCESS_THRESHOLD),
    }


def compute_blind_spot_hit_rate(task_dirs: list[Path]) -> dict[str, Any]:
    """盲区命中率：盲区标注项最终真出问题的比例（#49 放行门第三指标）。

    逐信号命中规则（同任务终局判定——标注产生在 pipeline 收尾，「出问题」
    取该任务的终局证据：子任务 failed / review 被拒 / 任务未完成 / goal 低合规）：

    - weakly_anchored_subtasks 项：该子任务最终 failed，或任务 review 被
      rejected / changes_requested。
    - inconclusive_evaluations 项：同上（评估不确定 → 后来真失败/被拒）。
    - uncovered_acceptance_ids 项：任务未 completed，或 goal_adherence.level == "low"
      （M4 回溯判定「执行全过但漏验收」），或 review 被拒。

    分母排除两类：unattributed_failures（本身已是失败，是「问题」而非「预测」）、
    baseline_dirty（环境标志位，非可命中的标注项）。

    Returns:
        {"blind_spot_hit_rate": float|None, "blind_spot_items": int,
         "blind_spot_hits": int, "by_signal": {signal: {"items": n, "hits": m}}}
    """
    by_signal: dict[str, dict[str, int]] = {
        "weakly_anchored_subtasks": {"items": 0, "hits": 0},
        "inconclusive_evaluations": {"items": 0, "hits": 0},
        "uncovered_acceptance_ids": {"items": 0, "hits": 0},
    }
    for td in task_dirs:
        meta_path = td / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        blind = meta.get("blind_spots") or {}
        if not isinstance(blind, dict) or not any(
                blind.get(k) for k in by_signal):
            continue
        failed_ids = {
            str(r.get("subtask_id")) for r in (meta.get("results") or [])
            if isinstance(r, dict) and r.get("status") == "failed" and r.get("subtask_id")
        }
        review_bad = False
        review_path = td / "review.json"
        if review_path.exists():
            try:
                decision = (json.loads(review_path.read_text(encoding="utf-8")) or {}).get("decision", "")
                review_bad = decision in ("rejected", "changes_requested")
            except (OSError, json.JSONDecodeError):
                pass
        # 任务级「出问题」：终局状态非交付/完成类（缺失状态保守不计）
        _ok_states = {"completed", "ACCEPTED_DELIVERY", "DELIVERY_READY"}
        task_not_completed = bool(meta.get("status")) and meta.get("status") not in _ok_states
        goal_low = (meta.get("goal_adherence") or {}).get("level") == "low"

        for sid in (blind.get("weakly_anchored_subtasks") or []):
            by_signal["weakly_anchored_subtasks"]["items"] += 1
            if str(sid) in failed_ids or review_bad:
                by_signal["weakly_anchored_subtasks"]["hits"] += 1
        for sid in (blind.get("inconclusive_evaluations") or []):
            by_signal["inconclusive_evaluations"]["items"] += 1
            if str(sid) in failed_ids or review_bad:
                by_signal["inconclusive_evaluations"]["hits"] += 1
        for ac_id in (blind.get("uncovered_acceptance_ids") or []):
            by_signal["uncovered_acceptance_ids"]["items"] += 1
            if task_not_completed or goal_low or review_bad:
                by_signal["uncovered_acceptance_ids"]["hits"] += 1

    items = sum(v["items"] for v in by_signal.values())
    hits = sum(v["hits"] for v in by_signal.values())
    return {
        "blind_spot_hit_rate": round(hits / items, 4) if items else None,
        "blind_spot_items": items,
        "blind_spot_hits": hits,
        "by_signal": by_signal,
    }


def compute_trust_metrics(task_dirs: list[Path]) -> dict[str, Any]:
    """#49 信任指标（渐进自治放行门）：审查后修改率 / 复发可见率 / 盲区命中率。

    数据来源：各 task_dir 的 meta.json（results[].problem_id）+ review.json（decision）。

    审查后修改率 = (rejected + changes_requested) / 有 review 决策的任务数
      —— 交付的「初始可信度」：用户审查后动手改的比例越低越可信。
    复发可见率   = 失败子任务中带 problem_id 的比例
      —— 学习闭环覆盖率：失败能否关联到历史 Problem（#50 接线后才有数据）。
    盲区命中率   = 盲区标注项最终真出问题的比例
      —— 需跨任务历史关联（task X 的盲区 → task Y 的失败），暂返回 None 待数据积累。

    Returns:
        {"review_modification_rate": float|None, "reviewed_tasks": int,
         "recurrence_visibility_rate": float|None, "failed_subtasks": int,
         "blind_spot_hit_rate": float|None, "blind_spot_items": int,
         "blind_spot_hits": int, "blind_spot_by_signal": dict}
    """
    reviewed = 0
    modified = 0
    failed_total = 0
    failed_with_problem = 0
    for td in task_dirs:
        meta_path = td / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in meta.get("results") or []:
            if isinstance(r, dict) and r.get("status") == "failed":
                failed_total += 1
                if r.get("problem_id"):
                    failed_with_problem += 1
        review_path = td / "review.json"
        if review_path.exists():
            try:
                decision = (json.loads(review_path.read_text(encoding="utf-8")) or {}).get("decision", "")
            except (OSError, json.JSONDecodeError):
                decision = ""
            if decision:
                reviewed += 1
                if decision in ("rejected", "changes_requested"):
                    modified += 1
    blind = compute_blind_spot_hit_rate(task_dirs)
    return {
        "review_modification_rate": round(modified / reviewed, 4) if reviewed else None,
        "reviewed_tasks": reviewed,
        "recurrence_visibility_rate": round(failed_with_problem / failed_total, 4) if failed_total else None,
        "failed_subtasks": failed_total,
        "blind_spot_hit_rate": blind["blind_spot_hit_rate"],
        "blind_spot_items": blind["blind_spot_items"],
        "blind_spot_hits": blind["blind_spot_hits"],
        "blind_spot_by_signal": blind["by_signal"],
    }
