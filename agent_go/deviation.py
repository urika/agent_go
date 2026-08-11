"""偏差反馈数据层 — Spec/Architecture/验收偏差的模型、持久化、分类与聚合 (M2.5).

设计原则：
  - 纯数据模块：不 import executor/evaluator/bench 等核心模块
  - 零循环依赖：仅引用 pathlib / typing / json 等 stdlib
  - 接口参数化：所有函数显式接收路径参数，不依赖全局常量
  - 数据契约：DeviationEvent 是所有偏差事件的唯一标准格式

用法：
    from .deviation import DeviationEvent, write, load, classify_deviation

    event = classify_deviation(task_id="t1", subtask_id="s1", result={...})
    write(task_dir / "deviation.jsonl", event)

    events = load(task_dir)
    metrics = aggregate_deviations(events)
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional


# ═══════════════════════════════════════════════════════════════
# 常量：偏差类型与根因分类（M2.5 roadmap 定义）
# ═══════════════════════════════════════════════════════════════

DEVIATION_TYPES = ("spec_deviation", "architecture_deviation", "acceptance_gap")

# roadmap M2.5: 偏差根因分类
ROOT_CAUSE_CATEGORIES = (
    "spec_incomplete",       # Spec 不完整
    "plan_misunderstanding", # Plan 误解
    "decomposition_error",   # 拆解错误
    "implementation_error",  # 实现错误
    "verification_insufficient",  # 验证不足
    "delivery_aggregation_error",  # 交付汇总错误
    "unknown",               # 无法归类
)

DEVIATION_FILENAME = "deviation.jsonl"


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class DeviationEvent:
    """一次偏差事件的标准数据模型。

    字段说明：
      task_id / subtask_id        — 来源标识
      deviation_type              — spec_deviation | architecture_deviation | acceptance_gap
      root_cause_category         — 根因分类（ROOT_CAUSE_CATEGORIES 之一）
      summary                     — 偏差摘要（人可读）
      evidence                    — 佐证（失败命令/语义评估原因/范围偏差等）
      failure_class               — 关联 failure.py 稳定类（verification_failure 等）
      failure_pattern             — 失败模式指纹（如 verify_revert / diverge / shell_fail）
      effective_strategy          — 生效策略（readonly_review / semantic_feedback / manual）
      injected_into_repair        — 偏差是否注入下一次 repair prompt
      requires_approval           — 是否需要人工决策（未经批准不改全局 Plan）
      human_decision              — 人工决策结果（"" | approve | modify_spec | reject | rework）
      spec_rewrite_required       — 是否需要回写 Spec
      timestamp / schema_version  — 元数据
    """
    task_id: str
    subtask_id: str
    deviation_type: Literal["spec_deviation", "architecture_deviation", "acceptance_gap"]
    root_cause_category: str = "unknown"
    summary: str = ""
    evidence: str = ""
    failure_class: str = ""
    failure_pattern: str = ""
    effective_strategy: str = ""
    injected_into_repair: bool = False
    requires_approval: bool = True
    human_decision: str = ""
    spec_rewrite_required: bool = False
    timestamp: str = ""
    schema_version: int = 1

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════

def write(path: Path, event: DeviationEvent) -> None:
    """追加写入一条偏差事件到 deviation.jsonl。失败不中断主流程。"""
    record = asdict(event)
    record["_event_type"] = "deviation"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(task_dir: Path) -> list[DeviationEvent]:
    """读取一个任务目录下的全部偏差事件。"""
    path = task_dir / DEVIATION_FILENAME
    if not path.exists():
        return []
    events: list[DeviationEvent] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(DeviationEvent(
                task_id=data.get("task_id", ""),
                subtask_id=data.get("subtask_id", ""),
                deviation_type=data.get("deviation_type", "acceptance_gap"),
                root_cause_category=data.get("root_cause_category", "unknown"),
                summary=data.get("summary", ""),
                evidence=data.get("evidence", ""),
                failure_class=data.get("failure_class", ""),
                failure_pattern=data.get("failure_pattern", ""),
                effective_strategy=data.get("effective_strategy", ""),
                injected_into_repair=data.get("injected_into_repair", False),
                requires_approval=data.get("requires_approval", True),
                human_decision=data.get("human_decision", ""),
                spec_rewrite_required=data.get("spec_rewrite_required", False),
                timestamp=data.get("timestamp", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return events


def load_all(base_dir: Path) -> list[DeviationEvent]:
    """扫描所有 task-* 目录，聚合全部偏差事件。"""
    all_events: list[DeviationEvent] = []
    for td in sorted(base_dir.glob("task-*"), reverse=True):
        all_events.extend(load(td))
    return all_events


# ═══════════════════════════════════════════════════════════════
# 根因分类
# ═══════════════════════════════════════════════════════════════

def classify_deviation(
    *,
    task_id: str,
    subtask_id: str,
    result: Optional[dict[str, Any]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> DeviationEvent:
    """从失败结果判定偏差类型与根因分类。

    分类规则（启发式，确定性可测）：
      - 验证命令被安全门禁拒绝 / metering 不可用 / 进程崩溃但已验证 → 基础设施问题，
        不构成能力偏差，标记 requires_approval=False。
      - 有范围偏差（files_hint 越界/遗漏）→ architecture_deviation（decomposition_error 或
        plan_misunderstanding）。
      - 有语义评估失败原因 → acceptance_gap（verification_insufficient 或 implementation_error）。
      - 其余未通过任务 → acceptance_gap（implementation_error）。
    """
    result = result or {}
    meta = meta or {}
    failure_class = result.get("failure_class") or meta.get("failure_class") or ""
    failure_pattern = result.get("failure_pattern") or ""

    # kill_reason → failure_pattern（no_progress 语义别名）：
    # verify_revert = 回退/振荡（无实质进展）；divergence = 打地鼠（不同缺陷漂移）。
    kill_reason = result.get("kill_reason") or meta.get("kill_reason") or ""
    if not failure_pattern:
        if kill_reason == "verify_revert":
            failure_pattern = "no_progress"
        elif kill_reason == "diverge" or (kill_reason or "").startswith("diverg"):
            failure_pattern = "no_progress_diverge"

    # 基础设施类失败不构成能力偏差
    infra_patterns = (
        "rejected", "metering_unavailable", "crash_but_verified",
        "infrastructure_failure", "system_error", "budget_abort",
        "timeout", "user_cancelled", "delivery_failure",
    )
    _vr_rejected = any(
        isinstance(r, dict) and r.get("rejected") for r in result.get("verification_results", [])
    )
    if failure_class in infra_patterns or _vr_rejected:
        return DeviationEvent(
            task_id=task_id, subtask_id=subtask_id,
            deviation_type="acceptance_gap",
            root_cause_category="unknown",
            summary="基础设施/系统失败，不构成 Spec/架构/验收能力偏差",
            evidence="",
            failure_class=failure_class or "infrastructure_failure",
            failure_pattern=failure_pattern,
            requires_approval=False,
        )

    # 范围偏差 → 架构偏差
    scope = result.get("scope_violation") or meta.get("scope_violation")
    if isinstance(scope, dict) and scope.get("compliant") is False:
        oos = scope.get("out_of_scope", []) or []
        missing = scope.get("missing", []) or []
        evidence = f"越界文件: {oos}; 遗漏文件: {missing}"
        return DeviationEvent(
            task_id=task_id, subtask_id=subtask_id,
            deviation_type="architecture_deviation",
            root_cause_category="decomposition_error" if oos else "plan_misunderstanding",
            summary="改动范围超出子任务边界（架构偏差）",
            evidence=evidence,
            failure_class=failure_class,
            failure_pattern=failure_pattern or "scope_violation",
            requires_approval=True,
        )

    # 语义评估失败 → 验收差距
    semantic_fails = [
        v for v in result.get("verification_results", [])
        if isinstance(v, dict) and v.get("type") == "semantic" and v.get("passed") is False
    ]
    if semantic_fails:
        reason = semantic_fails[-1].get("reason", "") or ""
        evidence = f"语义评估未通过: {reason}"
        cat = "verification_insufficient"
        summary = "实现未达到验收标准（验收差距）"
        return DeviationEvent(
            task_id=task_id, subtask_id=subtask_id,
            deviation_type="acceptance_gap",
            root_cause_category=cat,
            summary=summary,
            evidence=evidence,
            failure_class=failure_class or "verification_failure",
            failure_pattern=failure_pattern,
            requires_approval=True,
        )

    # 一般验证失败 → 实现错误（验收差距）
    failed_cmds = result.get("failed_cmds") or []
    if failed_cmds:
        evidence = f"验证命令失败: {failed_cmds[0][:200]}"
        return DeviationEvent(
            task_id=task_id, subtask_id=subtask_id,
            deviation_type="acceptance_gap",
            root_cause_category="implementation_error",
            summary="实现存在缺陷，验证未通过",
            evidence=evidence,
            failure_class=failure_class or "verification_failure",
            failure_pattern=failure_pattern,
            requires_approval=True,
        )

    return DeviationEvent(
        task_id=task_id, subtask_id=subtask_id,
        deviation_type="acceptance_gap",
        root_cause_category="unknown",
        summary="未通过但缺乏分类证据",
        evidence="",
        failure_class=failure_class,
        failure_pattern=failure_pattern,
        requires_approval=True,
    )


# ═══════════════════════════════════════════════════════════════
# 分析聚合
# ═══════════════════════════════════════════════════════════════

def aggregate_deviations(events: list[DeviationEvent]) -> dict[str, Any]:
    """聚合偏差事件，输出类型/根因分布与人工处理统计。"""
    if not events:
        return {
            "total": 0,
            "by_type": {},
            "by_root_cause": {},
            "by_failure_class": {},
            "require_approval": 0,
            "resolved": 0,
            "spec_rewrite_pending": 0,
        }

    def _count(attr: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in events:
            val = getattr(e, attr) or "unknown"
            out[val] = out.get(val, 0) + 1
        return out

    return {
        "total": len(events),
        "by_type": _count("deviation_type"),
        "by_root_cause": _count("root_cause_category"),
        "by_failure_class": _count("failure_class"),
        "require_approval": sum(1 for e in events if e.requires_approval),
        "resolved": sum(1 for e in events if e.human_decision),
        "spec_rewrite_pending": sum(1 for e in events if e.spec_rewrite_required and not e.human_decision),
    }
