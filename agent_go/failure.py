"""Stable failure classes and policy for M0-3."""

from __future__ import annotations

from typing import Any


FAILURE_CLASSES = frozenset({
    "model_failure",
    "verification_failure",
    "timeout",
    "budget_abort",
    "infrastructure_failure",
    "delivery_failure",
    "user_cancelled",
    "system_error",
})

# These are intentionally not renamed in persisted data.  They remain useful
# low-level evidence while ``failure_class`` is the stable aggregation key.
KILL_REASON_CLASS = {
    "over_budget_l1": "budget_abort",
    "over_budget_l2": "budget_abort",
    "over_budget_l3": "budget_abort",
    "stuck": "timeout",
    "hard_timeout": "timeout",
    "goal_timeout": "timeout",
    "goal_turns_exceeded": "timeout",
    "stuck_or_hardtimeout": "timeout",
    "infra": "infrastructure_failure",
    "metering_unavailable": "infrastructure_failure",
    "cleanup_failure": "infrastructure_failure",
    "system_error": "system_error",
    "user_cancelled": "user_cancelled",
    "cancelled": "user_cancelled",
    "delivery_failed": "delivery_failure",
    "interrupted_or_unknown": "system_error",
    # plan 质量门拦截（planner 计划未过确定性预检，未进入执行）：harness/planner
    # 侧事件，能力观测未发生 → system_error（capability_failure=False）。
    "plan_gate_blocked": "system_error",
}

# Capability denominator excludes failures where the harness or user, rather
# than the model, prevented a valid capability observation.
FAILURE_POLICY = {
    "model_failure": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "verification_failure": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "timeout": {"capability_failure": True, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "budget_abort": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "infrastructure_failure": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "delivery_failure": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
    "user_cancelled": {"capability_failure": False, "cost_included": True, "resume_allowed": False, "preserve_worktree": True},
    "system_error": {"capability_failure": False, "cost_included": True, "resume_allowed": True, "preserve_worktree": True},
}

_CLASS_PRIORITY = (
    "user_cancelled", "budget_abort", "timeout", "infrastructure_failure",
    "system_error", "delivery_failure", "verification_failure", "model_failure",
)


def failure_policy(failure_class: str | None) -> dict[str, Any]:
    """Return policy flags for a class, or safe defaults for no failure."""
    if failure_class in FAILURE_POLICY:
        return dict(FAILURE_POLICY[failure_class])
    return {"capability_failure": False, "cost_included": True, "resume_allowed": False, "preserve_worktree": False}


def classify_failure(
    result: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    *,
    timed_out: bool = False,
) -> str | None:
    """Map runtime evidence to one stable failure class."""
    result = result or {}
    meta = meta or {}
    explicit = result.get("failure_class") or meta.get("failure_class")
    if explicit in FAILURE_CLASSES:
        return explicit
    if meta.get("delivery_failed"):
        return "delivery_failure"
    if result.get("crash_but_verified"):
        return "infrastructure_failure"
    # 验证命令被安全门禁拒绝 = 生成/验证质量问题（LLM 生成的命令不合规），
    # 不是外部基础设施故障。归 verification_failure（与 status=failed 且
    # verify_ok=False 的路径一致），避免污染 infrastructure_failure 统计。
    # 2026-08-12 修复：decision-20260812 基线 6 个任务因此误标 infra。
    if any(v.get("rejected") for v in result.get("verification_results", []) if isinstance(v, dict)):
        return "verification_failure"
    kill_reason = result.get("kill_reason") or meta.get("kill_reason")
    if kill_reason in KILL_REASON_CLASS:
        return KILL_REASON_CLASS[kill_reason]
    if timed_out:
        return "timeout"
    if result.get("status") in {"completed", "no_changes"}:
        # 语义评估失败（verification_results 中 type=="semantic" 且 passed=False）
        # 是真实验证失败，不能因 status=completed 而当作无失败（阶段C review P0）。
        # LLM 语义评估未通过 → 产物未达到验证标准，归类 verification_failure。
        _semantic_fails = [
            v for v in result.get("verification_results", [])
            if isinstance(v, dict) and v.get("type") == "semantic"
            and v.get("passed") is False
        ]
        if _semantic_fails:
            return "verification_failure"
        return None
    if result.get("status") == "blocked":
        # A blocked task is an orchestration failure unless a more specific
        # budget/infrastructure reason was already supplied above.
        return "system_error"
    if result.get("status") == "failed":
        if result.get("verify_ok") is False:
            return "verification_failure"
        if result.get("exit_code") not in (None, 0):
            return "model_failure"
        return "system_error"
    return None


def aggregate_failure_class(classes: list[str | None], meta: dict[str, Any] | None = None) -> str | None:
    """Choose a deterministic task-level class from subtask evidence."""
    meta = meta or {}
    explicit = meta.get("failure_class")
    if explicit in FAILURE_CLASSES:
        return explicit
    if meta.get("delivery_failed"):
        return "delivery_failure"
    present = set(c for c in classes if c in FAILURE_CLASSES)
    for failure_class in _CLASS_PRIORITY:
        if failure_class in present:
            return failure_class
    return None


# ═══════════════════════════════════════════════════════════
# H2 谦逊层：层间归因（layer attribution）
# ═══════════════════════════════════════════════════════════

# 优先级 = 复盘动作顺序（越靠前越「可修」）：
#   planner_out_of_scope（修 plan）> contract_broken（修依赖）
#   > constraint_blocked（调预算）> spec_too_broad（修 spec）> worker_capability（换模型）
LAYER_PRIORITY: tuple[str, ...] = (
    "planner_out_of_scope",
    "contract_broken",
    "constraint_blocked",
    "spec_too_broad",
    "worker_capability",
)

# 规则层：planner 违反 do-not-touch / 文件所有权（plan 预检 blocking issue）
_PLANNER_BLOCKING_TYPES = frozenset({
    "spec_do_not_touch_violation",
    "scope_conflict",
    "file_overlap_without_dependency",
})

# 功能层：模型/验证能力不足的 failure_class
_WORKER_CAPABILITY_CLASSES = frozenset({"model_failure", "verification_failure", "timeout"})


def attribute_layer(
    failure_class: str | None = None,
    meta: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> str | None:
    """H2 谦逊层：把失败归因到「层」（纯函数，确定性，零 LLM）。

    六层映射（humility-layer-design.md §H2）——agent_go 可确定性判定的 5 个归因：
      规范层 spec_too_broad       = 验收无法锚定（A2 弱锚定）或追踪不完整（有未覆盖 AC）
      规则层 planner_out_of_scope = planner 违反 do-not-touch/文件所有权（plan 预检阻断）
      规则层 constraint_blocked   = 预算/成本阻断（budget_abort / blocked）
      协议层 contract_broken      = 上游合并冲突 / artifact 传递断裂
      功能层 worker_capability    = 模型/验证能力不足（model/verification/timeout）
    （目标层/原则层 = 人工决策域，不做确定性归因）

    价值：复盘时回答「该修 spec、修 planner、调预算还是换模型」。
    """
    meta = meta or {}
    result = result or {}

    # 规则层：planner 越界（plan 预检 blocking）
    plan_quality = meta.get("plan_quality") or {}
    blocking_types = {
        str(i.get("type")) for i in (plan_quality.get("blocking_issues") or [])
        if isinstance(i, dict)
    }
    if blocking_types & _PLANNER_BLOCKING_TYPES:
        return "planner_out_of_scope"

    # 协议层：上游合并冲突 / artifact 断裂
    if result.get("merge_conflicts"):
        return "contract_broken"
    if "上游合并冲突" in str(result.get("failure_reason") or ""):
        return "contract_broken"

    # 规则层：预算/成本阻断
    if failure_class == "budget_abort" or result.get("status") == "blocked":
        return "constraint_blocked"

    # 规范层 vs 功能层：同为能力失败时，若验收「无法锚定/覆盖不全」优先归因到 spec
    if failure_class in _WORKER_CAPABILITY_CLASSES:
        warnings = plan_quality.get("warnings") or []
        has_unanchored = any(
            isinstance(w, dict) and w.get("type") == "verification_not_anchored"
            for w in warnings
        )
        trace = meta.get("traceability") or {}
        has_missing = bool(trace.get("missing_requirement_ids"))
        if has_unanchored or has_missing:
            return "spec_too_broad"
        return "worker_capability"

    return None
