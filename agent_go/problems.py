"""Problem 实体数据层 — 跨任务失败一等公民（B4 决策）+ 知识生命周期（H3 谦逊层）.

设计依据：
  - B4 决策（business-architecture.md）：聚合优先 + 最小状态机骨架
    三态 opened → analyzed → resolved + 复发重开；failure_pattern 去重。
  - H3 谦逊层（humility-layer-design.md）：半衰期（stale_after_days → dormant 派生状态）
    + 葬礼（resolution_summary，KnowledgeStore 直接输入）。
  - A5 决策：全局 ~/.agent_go/problems.jsonl（跨任务累积）；任务级存原始（deviation.jsonl），
    全局聚合 Problem。

设计原则（与 deviation.py 同风格）：
  - 纯数据模块：仅 stdlib（json/dataclasses/hashlib/pathlib/typing）
  - 接口参数化：所有函数显式接收路径参数
  - 追加写 JSONL：record 是 upsert（重写整文件），失败不抛异常

用法：
    from .problems import record, mark_analyzed, mark_resolved, load, aggregate

    record(PROBLEMS_PATH, failure_pattern="shell_fail", failure_class="verification_failure",
           task_id="task-x", subtask_id="sub-1", evidence="pytest: command not found")
    mark_resolved(PROBLEMS_PATH, "p-shell_fail-abc123", resolved_by="commit-x",
                  resolution_summary="worktree 未继承 venv；TASK.md 注明 source venv 后修复")
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

PROBLEM_STATES = ("opened", "analyzed", "resolved")

DEFAULT_STALE_AFTER_DAYS = 90  # H3 半衰期默认值

PROBLEMS_FILENAME = "problems.jsonl"


def _now_iso() -> str:
    return datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class Problem:
    """跨任务失败的一等公民（B4 + H3）。

    字段分组：
      身份与去重：id / failure_pattern / failure_class
      来源与复发：task_id / subtask_id / first_seen_at / last_seen_at / occurrence_count
      证据：      summary / evidence / root_cause_category
      生命周期：  status（opened|analyzed|resolved）/ root_cause / resolved_by / github_issue
      H3 半衰期：stale_after_days（未复发超期 → dormant 派生状态，不新增状态机节点）
      H3 葬礼：  resolution_summary（resolved 时记录「为何曾重要、如何被修」）
    """

    id: str
    failure_pattern: str
    failure_class: str = ""
    task_id: str = ""
    subtask_id: str = ""
    summary: str = ""
    evidence: str = ""
    root_cause_category: str = "unknown"
    occurrence_count: int = 1
    first_seen_at: str = ""
    last_seen_at: str = ""
    status: str = "opened"
    root_cause: str = ""
    resolved_by: str = ""
    github_issue: str = ""
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    resolution_summary: str = ""
    schema_version: int = 1

    def __post_init__(self):
        if not self.id:
            self.id = make_problem_id(self.failure_pattern)
        if not self.first_seen_at:
            self.first_seen_at = _now_iso()
        if not self.last_seen_at:
            self.last_seen_at = self.first_seen_at

    def is_dormant(self, now: Optional[datetime] = None) -> bool:
        """H3 半衰期：派生状态（不写入 status）。

        规则：opened 且距 last_seen_at 超过 stale_after_days → 视为休眠
        （长期未复发的失败，置信度下降；analyzed/resolved 不参与休眠判定）。
        """
        if self.status != "opened":
            return False
        now = now or datetime.now()
        try:
            last = datetime.fromisoformat(self.last_seen_at)
        except (ValueError, TypeError):
            return False
        return (now - last) > timedelta(days=self.stale_after_days)


def make_problem_id(failure_pattern: str) -> str:
    """由 failure_pattern 派生稳定 id（跨任务唯一，去重键）。"""
    digest = hashlib.sha256((failure_pattern or "").encode("utf-8")).hexdigest()[:12]
    return f"p-{digest}"


# ═══════════════════════════════════════════════════════════════
# 持久化（JSONL 追加/重写，失败不抛异常）
# ═══════════════════════════════════════════════════════════════

def load(problems_path: Path | str) -> list[Problem]:
    """读取全局 problems.jsonl（容错：坏行跳过）。"""
    path = Path(problems_path)
    if not path.exists():
        return []
    problems: list[Problem] = []
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            problems.append(Problem(
                id=data.get("id", ""),
                failure_pattern=data.get("failure_pattern", ""),
                failure_class=data.get("failure_class", ""),
                task_id=data.get("task_id", ""),
                subtask_id=data.get("subtask_id", ""),
                summary=data.get("summary", ""),
                evidence=data.get("evidence", ""),
                root_cause_category=data.get("root_cause_category", "unknown"),
                occurrence_count=int(data.get("occurrence_count", 1)),
                first_seen_at=data.get("first_seen_at", ""),
                last_seen_at=data.get("last_seen_at", ""),
                status=data.get("status", "opened"),
                root_cause=data.get("root_cause", ""),
                resolved_by=data.get("resolved_by", ""),
                github_issue=data.get("github_issue", ""),
                stale_after_days=int(data.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)),
                resolution_summary=data.get("resolution_summary", ""),
            ))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return problems


def _save(problems_path: Path, problems: list[Problem]) -> None:
    """整体重写 problems.jsonl（record 是 upsert，需重写）。失败静默（非关键路径）。"""
    try:
        problems_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(asdict(p), ensure_ascii=False) for p in problems]
        problems_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    except OSError:
        pass


def record(
    problems_path: Path | str,
    *,
    failure_pattern: str,
    failure_class: str = "",
    task_id: str = "",
    subtask_id: str = "",
    summary: str = "",
    evidence: str = "",
    root_cause_category: str = "unknown",
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> Optional[Problem]:
    """记录一次失败（upsert）。

    - 新 failure_pattern → 新建 Problem（opened，occurrence_count=1）
    - 已有 pattern → occurrence_count++、last_seen_at 更新
      - 若 status=resolved → 重开为 opened（复发重开：说明上次修复未生效；
        resolution_summary/resolved_by 保留为历史证据）
    """
    if not failure_pattern:
        return None
    path = Path(problems_path)
    problems = load(path)
    for p in problems:
        if p.failure_pattern == failure_pattern:
            p.occurrence_count += 1
            p.last_seen_at = _now_iso()
            if p.task_id and task_id and p.task_id != task_id:
                p.summary = (p.summary + f"；又见于 {task_id}")[:300] if p.summary else f"又见于 {task_id}"
            if p.status == "resolved":
                p.status = "opened"  # 复发重开（B4 状态机）
            _save(path, problems)
            return p
    # 新 Problem
    prob = Problem(
        id=make_problem_id(failure_pattern),
        failure_pattern=failure_pattern,
        failure_class=failure_class,
        task_id=task_id,
        subtask_id=subtask_id,
        summary=summary[:300],
        evidence=evidence[:500],
        root_cause_category=root_cause_category,
        stale_after_days=stale_after_days,
    )
    problems.append(prob)
    _save(path, problems)
    return prob


def mark_analyzed(
    problems_path: Path | str,
    problem_id: str,
    *,
    root_cause: str,
    root_cause_category: str = "unknown",
) -> Optional[Problem]:
    """opened → analyzed：根因归属（复用 readonly_review 产物，不新增 LLM 调用）。"""
    path = Path(problems_path)
    problems = load(path)
    for p in problems:
        if p.id == problem_id:
            p.status = "analyzed"
            p.root_cause = root_cause[:500]
            p.root_cause_category = root_cause_category
            _save(path, problems)
            return p
    return None


def mark_resolved(
    problems_path: Path | str,
    problem_id: str,
    *,
    resolved_by: str,
    resolution_summary: str,
) -> Optional[Problem]:
    """→ resolved（H3 葬礼：resolution_summary 必填，记录「为何曾重要、如何被修」）。"""
    path = Path(problems_path)
    problems = load(path)
    for p in problems:
        if p.id == problem_id:
            p.status = "resolved"
            p.resolved_by = resolved_by[:200]
            p.resolution_summary = resolution_summary[:500]
            _save(path, problems)
            return p
    return None


# ═══════════════════════════════════════════════════════════════
# 聚合分析（B4 首要消费者：复发率 / top 失败模式 / 趋势）
# ═══════════════════════════════════════════════════════════════

def aggregate(problems: list[Problem], now: Optional[datetime] = None) -> dict[str, Any]:
    """聚合分析：状态分布 / 复发率 / top 模式 / 休眠数。

    Returns:
        {
          "total": int,
          "status_counts": {opened/analyzed/resolved: int},
          "dormant_count": int,          # H3 半衰期派生
          "recurrence_count": int,       # occurrence_count > 1 的数量（复发过）
          "total_occurrences": int,      # 全部 occurrence_count 之和
          "top_patterns": [(pattern, occurrence_count), ...]  # 按发生次数排序 top 5
        }
    """
    now = now or datetime.now()
    status_counts = {s: 0 for s in PROBLEM_STATES}
    dormant = 0
    recurrence = 0
    total_occ = 0
    for p in problems:
        status_counts[p.status] = status_counts.get(p.status, 0) + 1
        if p.is_dormant(now):
            dormant += 1
        if p.occurrence_count > 1:
            recurrence += 1
        total_occ += p.occurrence_count
    top = sorted(
        ((p.failure_pattern, p.occurrence_count) for p in problems),
        key=lambda x: -x[1],
    )[:5]
    return {
        "total": len(problems),
        "status_counts": status_counts,
        "dormant_count": dormant,
        "recurrence_count": recurrence,
        "total_occurrences": total_occ,
        "top_patterns": top,
    }
