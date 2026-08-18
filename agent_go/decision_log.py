"""Decision Log（M6.2）：统一决策记录，可审计、可复盘。

每次关键决策（模型推荐应用/配置修改/profile 切换/交付决策）追加一条记录到
`~/.agent_go/decision_log.jsonl`。决策辅助（M6）的"可复现、可审计"支撑：
record 记录「为何改/基于何证据/期望影响/谁确认」，actual 在复跑后回填。

设计约束（决策辅助设计文档 §1 三边界）：
  - 建议不自动执行：本模块只记录，执行动作仍由 router recommend --apply / config 编辑触发
  - 证据绑定：evidence_refs 记录决策依据（baseline/任务/失败模式路径）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import AGENT_GO_DIR

DECISION_LOG_FILENAME = "decision_log.jsonl"


@dataclass
class DecisionEvent:
    """一条决策记录。

    change: 决策内容（改了什么，如 "worker_models.hard: sonnet → opus-4-7"）
    goal: 分析目标（--analysis-goal，人预置）
    evidence_refs: 决策依据（baseline/任务/失败模式路径列表）
    expected_impact: 预期影响（量化目标方向）
    confirmer: 确认者（cli / web:admin / token 哈希前缀）
    actual: 复跑后实际结果（后续回填，初始空）
    source: 来源命令（eval insight / router recommend / config put / profile activate / merge）
    """
    change: str = ""
    goal: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    expected_impact: str = ""
    confirmer: str = ""
    actual: str = ""
    source: str = ""
    ts: str = ""
    schema_version: int = 1

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_path() -> Path:
    return AGENT_GO_DIR / DECISION_LOG_FILENAME


def record_decision(
    change: str,
    *,
    goal: str = "",
    evidence_refs: Optional[list[str]] = None,
    expected_impact: str = "",
    confirmer: str = "",
    source: str = "",
) -> DecisionEvent:
    """追加写入一条决策记录。失败不中断主流程。"""
    event = DecisionEvent(
        change=change,
        goal=goal,
        evidence_refs=list(evidence_refs or []),
        expected_impact=expected_impact,
        confirmer=confirmer,
        source=source,
    )
    record = asdict(event)
    record["_event_type"] = "decision"
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return event


def list_decisions(limit: int = 50) -> list[dict[str, Any]]:
    """读取决策记录（倒序，最新在前）。"""
    path = _log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("_event_type") == "decision":
            out.append(rec)
            if len(out) >= limit:
                break
    return out


def decision_count() -> int:
    """决策记录总数。"""
    path = _log_path()
    if not path.exists():
        return 0
    try:
        return sum(1 for _ in path.open(encoding="utf-8"))
    except OSError:
        return 0
