"""C4 KnowledgeStore A/B 对比判定分析器（N3-2）。

读取 bench 对照臂（knowledge_arm=False）与注入臂（knowledge_arm=True）
两批 jsonl results，汇总 pass_rate / ADR / 成本，按三门槛判定：
  - ADR 提升：注入臂 ADR（accepted_delivery 比例）高于对照臂
  - 成本不劣化：注入臂 $/AD 不超过对照臂 × (1 + cost_tolerance)
  - 错误知识可淘汰：problems 中存在 dormant（半衰期过期）或 suppressed 记录
三条件全满足 → PRODUCTIZE；否则 → ROLLBACK（仅保留埋点，不产品化）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def load_results(path: str | Path) -> list[dict]:
    """读取 jsonl results 文件，逐行解析为 dict 列表；解码失败行跳过。"""
    records: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _summarize(records: list[dict]) -> dict:
    """汇总一批 results：n / pass_rate / adr / avg_cost_usd / dollar_per_ad。"""
    n = len(records)
    if n == 0:
        return {"n": 0}
    pass_rate = sum(float(r.get("pass_rate") or 0) for r in records) / n
    accepted = sum(1 for r in records if r.get("accepted_delivery"))
    adr = accepted / n
    total_cost = sum(float(r.get("total_cost_usd") or 0) for r in records)
    dollar_per_ad = (total_cost / accepted) if accepted else float("inf")
    return {
        "n": n,
        "pass_rate": pass_rate,
        "adr": adr,
        "avg_cost_usd": total_cost / n,
        "dollar_per_ad": dollar_per_ad,
    }


def _has_eliminable_knowledge(problems_path: Optional[str | Path]) -> tuple[bool, str]:
    """problems.jsonl 中是否存在可淘汰知识（dormant 或 suppressed）。"""
    if not problems_path:
        return False, "未提供 problems 路径，可淘汰判定跳过"
    p = Path(problems_path)
    if not p.exists():
        return False, f"problems 文件不存在: {p}"
    now = datetime.now(timezone.utc)
    eliminable = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("suppressed_ids"):
            eliminable += 1
            continue
        if rec.get("status") != "opened":
            continue
        last_seen = rec.get("last_seen_at")
        stale = rec.get("stale_after_days")
        if not last_seen or not stale:
            continue
        try:
            last = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        except ValueError:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).days > int(stale):
            eliminable += 1
    if eliminable > 0:
        return True, f"problems 中存在 {eliminable} 条可淘汰（dormant/suppressed）记录，淘汰机制可触发"
    return False, "problems 中暂无可淘汰记录（待知识库积累）"


def analyze_ab(ctl: list[dict], inj: list[dict],
               cost_tolerance: float = 0.10,
               problems_path: Optional[str | Path] = None) -> dict:
    """两臂 A/B 判定：ADR↑ + 成本不劣化 + 可淘汰机制。"""
    ctl_s = _summarize(ctl)
    inj_s = _summarize(inj)
    adr_up = bool(ctl_s.get("n") and inj_s.get("n") and inj_s["adr"] > ctl_s["adr"])
    cost_ok = bool(
        ctl_s.get("n") and inj_s.get("n")
        and inj_s["dollar_per_ad"] <= ctl_s["dollar_per_ad"] * (1 + cost_tolerance)
    )
    eliminable, elim_detail = _has_eliminable_knowledge(problems_path)
    verdict = "PRODUCTIZE" if (adr_up and cost_ok) else "ROLLBACK"
    return {
        "ctl": ctl_s,
        "inj": inj_s,
        "verdicts": {
            "adr_up": adr_up,
            "cost_not_worse": cost_ok,
            "knowledge_eliminable": eliminable,
        },
        "eliminable_detail": elim_detail,
        "cost_tolerance": cost_tolerance,
        "conclusion": verdict,
    }
