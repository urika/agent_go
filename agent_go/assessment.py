"""假阳性评估数据层 — 数据模型、持久化读写、分析聚合。

设计原则：
  - 纯数据模块：不 import executor/evaluator/bench 等核心模块
  - 零循环依赖：仅引用 pathlib / typing / json 等 stdlib
  - 接口参数化：所有函数显式接收路径参数，不依赖全局常量
  - 数据契约：AssessmentEvent 是所有评估事件的唯一标准格式

用法：
    from .assessment import AssessmentEvent, write, load, compute_false_positive_rate

    event = AssessmentEvent(task_id="t1", subtask_id="s1", ...)
    write(task_dir / "assessment.jsonl", event)

    events = load(task_dir)
    metrics = compute_false_positive_rate(events)
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class AssessmentEvent:
    """一次语义评估事件的标准数据模型。

    字段说明：
      task_id / subtask_id       — 来源标识
      trigger_source             — "auto"（L1 自动触发）| "manual"（配置开启）
      verification               — 原始验证命令
      verification_confidence    — M5 验证置信度等级
      evaluator_strategy         — 使用的评估策略名
      evaluator_provider/model   — 评估用的 LLM
      passed                     — 语义评估是否通过
      confidence                 — LLM 对自己判断的置信度 0.0-1.0
      reason / suggestions       — 详细理由和修复建议
      cost_usd / latency_ms      — 资源消耗
    """
    task_id: str
    subtask_id: str
    trigger_source: str                     # "auto" | "manual" | "config"
    verification: str = ""
    verification_confidence: Literal["deterministic", "heuristic", "manual", "none", "unknown"] = "unknown"
    evaluator_strategy: str = "default"
    evaluator_provider: str = ""
    evaluator_model: str = ""
    passed: bool = True
    confidence: float = 1.0
    reason: str = ""
    suggestions: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    timestamp: str = ""
    schema_version: int = 1

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════

ASSESSMENT_FILENAME = "assessment.jsonl"


def write(path: Path, event: AssessmentEvent) -> None:
    """追加写入一条评估事件到 assessment.jsonl。"""
    record = asdict(event)
    record["_event_type"] = "false_positive_assessment"
    try:
        with open(str(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 写入失败不中断主流程


def load(task_dir: Path) -> list[AssessmentEvent]:
    """读取一个任务目录下的所有评估事件。

    支持两种来源，按优先级：
      1. assessment.jsonl（推荐，结构完整）
      2. metering.jsonl 中的 evaluator 事件（向后兼容旧数据）
    """
    path = task_dir / ASSESSMENT_FILENAME
    if path.exists():
        return _parse_jsonl(path)
    # 回退：从 metering.jsonl 中筛选 evaluator 事件
    fallback = task_dir / "metering.jsonl"
    if fallback.exists():
        return _metering_fallback(fallback)
    return []


def load_all(base_dir: Path) -> list[AssessmentEvent]:
    """扫描所有 task-* 目录，聚合全部评估事件。"""
    all_events: list[AssessmentEvent] = []
    for td in sorted(base_dir.glob("task-*"), reverse=True):
        all_events.extend(load(td))
    return all_events


def _parse_jsonl(path: Path) -> list[AssessmentEvent]:
    """解析 assessment.jsonl。"""
    events: list[AssessmentEvent] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(AssessmentEvent(
                task_id=data.get("task_id", ""),
                subtask_id=data.get("subtask_id", ""),
                trigger_source=data.get("trigger_source", "manual"),
                verification=data.get("verification", ""),
                verification_confidence=data.get("verification_confidence", "unknown"),
                evaluator_strategy=data.get("evaluator_strategy", "default"),
                evaluator_provider=data.get("evaluator_provider", ""),
                evaluator_model=data.get("evaluator_model", ""),
                passed=data.get("passed", True),
                confidence=data.get("confidence", 1.0),
                reason=data.get("reason", ""),
                suggestions=data.get("suggestions", ""),
                cost_usd=data.get("cost_usd", 0.0),
                latency_ms=data.get("latency_ms", 0.0),
                timestamp=data.get("timestamp", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return events


def _metering_fallback(path: Path) -> list[AssessmentEvent]:
    """从 metering.jsonl 回退解析（旧格式兼容）。"""
    events: list[AssessmentEvent] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("role") != "evaluator":
            continue
        result = data.get("result", "success")
        events.append(AssessmentEvent(
            task_id=data.get("task_id", ""),
            subtask_id=data.get("subtask_id", ""),
            trigger_source="manual",
            verification="",
            verification_confidence="unknown",
            evaluator_provider=data.get("actual_provider", ""),
            evaluator_model=data.get("actual_model", ""),
            passed=(result != "quality_fail"),
            confidence=1.0 if result == "success" else 0.0,
            reason=data.get("reason", ""),
            cost_usd=data.get("cost_usd", 0.0),
            latency_ms=data.get("latency_ms", 0.0),
        ))
    return events


# ═══════════════════════════════════════════════════════════════
# 分析聚合
# ═══════════════════════════════════════════════════════════════

def compute_false_positive_rate(events: list[AssessmentEvent]) -> dict[str, Any]:
    """从评估事件列表计算假阳性率指标。

    Returns:
        total_evaluated:      执行评估的子任务数
        passed:               评估通过数
        flagged:              评估未通过数（可疑假阳性）
        false_positive_rate:  flagged / total_evaluated（百分比，无数据时 None）
        avg_confidence:       通过事件的平 均置信度
        auto_trigger_rate:    自动触发的事件占比
    """
    if not events:
        return {
            "total_evaluated": 0,
            "passed": 0,
            "flagged": 0,
            "false_positive_rate": None,
            "avg_confidence": None,
            "auto_trigger_rate": None,
        }

    total = len(events)
    passed = sum(1 for e in events if e.passed)
    flagged = total - passed
    auto_count = sum(1 for e in events if e.trigger_source == "auto")

    passed_events = [e for e in events if e.passed]
    avg_conf = round(sum(e.confidence for e in passed_events) / len(passed_events), 2) if passed_events else None

    return {
        "total_evaluated": total,
        "passed": passed,
        "flagged": flagged,
        "false_positive_rate": round(flagged / total * 100) if total else None,
        "avg_confidence": avg_conf,
        "auto_trigger_rate": round(auto_count / total * 100) if total else None,
    }


def summarize_by_strategy(events: list[AssessmentEvent]) -> dict[str, Any]:
    """按评估策略分组统计（支持策略运营对比）。"""
    groups: dict[str, list[AssessmentEvent]] = {}
    for e in events:
        groups.setdefault(e.evaluator_strategy, []).append(e)
    return {
        name: compute_false_positive_rate(group)
        for name, group in groups.items()
    }
