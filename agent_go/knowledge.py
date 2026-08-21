"""C4 KnowledgeStore：跨任务历史经验提取，注入修复 prompt（A/B 实验臂）。

数据源（任务清单 N3-2 指定的三类）：
1. 全局 Problem 记忆（~/.agent_go/problems.jsonl）：跨任务失败模式 +
   复发计数 + resolution_summary（H3 葬礼，「如何被修」的一手经验）。
2. 当前任务 deviation.jsonl：同任务前序子任务的偏差记录（根因类别/摘要）。
3. 当前任务 verify_state.json：阈值化 Reflexion 产出的 failure_analysis /
   effective_strategy（B5=b 契约字段，reflexion_triggered 标记来源可信度）。

设计约束：
- 可开关：knowledge.enabled 默认 False（A/B 对照臂 = 不注入）。
- 可淘汰：knowledge.suppressed_ids 按 Problem id 屏蔽错误知识；dormant
  Problem（半衰期过）自动排除；每次注入落 log_event("knowledge_injected")
  记录来源 id，供 A/B 效果归因与错误知识淘汰。
- fail-open：任何读取/解析异常返回空，绝不阻断验证循环。
- 有界：注入条目 ≤ knowledge.max_items（默认 3），每条截断，防 prompt 膨胀。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# 匹配阈值：失败模式 token Jaccard ≥ 此值视为同一模式
_MATCH_THRESHOLD = 0.5


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9_一-鿿]+", _normalize(text)))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _pattern_similar(pa: str, pb: str) -> bool:
    """失败模式相似判定：规范化后相等 / 互含 / token Jaccard ≥ 阈值。"""
    na, nb = _normalize(pa), _normalize(pb)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return _jaccard(_tokens(pa), _tokens(pb)) >= _MATCH_THRESHOLD


def _match_problems(problems_path: Path, pattern: str,
                    suppressed_ids: list, max_items: int) -> list[dict]:
    """从全局 problems.jsonl 匹配历史经验（resolved > analyzed > opened）。"""
    try:
        from .problems import load as load_problems
        problems = load_problems(problems_path)
    except Exception:
        return []
    suppressed = set(suppressed_ids or [])
    matched = []
    for p in problems:
        if p.id in suppressed or p.is_dormant():
            continue
        if not _pattern_similar(p.failure_pattern, pattern):
            continue
        matched.append(p)
    # resolved（有解法）优先，其次 analyzed（有根因），复发多的优先
    _rank = {"resolved": 0, "analyzed": 1, "opened": 2}
    matched.sort(key=lambda p: (_rank.get(p.status, 3), -p.occurrence_count))
    items = []
    for p in matched[:max_items]:
        line = f"[Problem {p.id} · 复发 {p.occurrence_count} 次 · {p.status}] "
        if p.resolution_summary:
            line += f"模式: {p.failure_pattern[:80]} → 解法: {p.resolution_summary[:150]}"
        elif p.root_cause:
            line += f"模式: {p.failure_pattern[:80]} → 根因: {p.root_cause[:150]}"
        else:
            line += f"模式: {p.failure_pattern[:80]} → {p.summary[:150]}"
        items.append({"source_id": p.id, "kind": "problem", "line": line})
    return items


def _match_deviations(task_dir: Optional[Path], pattern: str,
                      max_items: int) -> list[dict]:
    """当前任务 deviation.jsonl：同任务前序子任务的失败模式（防重蹈覆辙）。"""
    if not task_dir:
        return []
    try:
        from .deviation import load as load_deviations
        events = load_deviations(Path(task_dir))
    except Exception:
        return []
    items = []
    for e in events:
        pat = getattr(e, "failure_pattern", "") or ""
        if not pat or not _pattern_similar(pat, pattern):
            continue
        sub = getattr(e, "subtask_id", "") or "?"
        cat = getattr(e, "root_cause_category", "") or "unknown"
        summary = (getattr(e, "summary", "") or "")[:150]
        items.append({
            "source_id": f"deviation:{sub}",
            "kind": "deviation",
            "line": f"[同任务 {sub} · 根因类别 {cat}] {summary}",
        })
        if len(items) >= max_items:
            break
    return items


def _match_verify_states(task_dir: Optional[Path], sub_id: str,
                         max_items: int) -> list[dict]:
    """当前任务 verify_state.json：Reflexion 产出的根因分析与生效策略。

    只取 reflexion_triggered=True 的记录（来源可信，B5=b 契约），
    跳过当前子任务自身（其内容已通过 readonly_review 注入 repair prompt）。
    """
    if not task_dir:
        return []
    import json
    items = []
    try:
        candidates = sorted(Path(task_dir).glob("*/verify_state.json"))
    except OSError:
        return []
    for path in candidates:
        if path.parent.name == sub_id:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not state.get("reflexion_triggered"):
            continue
        strategy = (state.get("effective_strategy") or "").strip()
        analysis = (state.get("failure_analysis") or "").strip()
        if not strategy and not analysis:
            continue
        line = f"[同任务 {path.parent.name} · Reflexion 记录] "
        if analysis:
            line += f"根因: {analysis[:120]}"
        if strategy:
            line += f"{'；' if analysis else ''}生效策略: {strategy[:120]}"
        items.append({
            "source_id": f"verify_state:{path.parent.name}",
            "kind": "verify_state",
            "line": line,
        })
        if len(items) >= max_items:
            break
    return items


def build_repair_knowledge(subtask: dict, pattern_hint: str,
                           task_dir: Optional[Path], config: dict,
                           logger=None, max_items: int = 3) -> dict:
    """构建注入 repair prompt 的历史经验。返回 {"text": str, "sources": [id...]}。

    enabled=False / 无命中 / 任何异常 → {"text": "", "sources": []}（fail-open）。
    """
    cfg = (config or {}).get("knowledge", {}) or {}
    if not cfg.get("enabled", False):
        return {"text": "", "sources": []}
    pattern = (pattern_hint or "")[:200]
    if not pattern.strip():
        return {"text": "", "sources": []}
    try:
        from .config import AGENT_GO_DIR
        max_items = int(cfg.get("max_items", max_items) or max_items)
        suppressed = cfg.get("suppressed_ids", []) or []
        items = _match_problems(AGENT_GO_DIR / "problems.jsonl", pattern,
                                suppressed, max_items)
        remaining = max_items - len(items)
        if remaining > 0:
            items += _match_deviations(Path(task_dir) if task_dir else None,
                                       pattern, remaining)
        remaining = max_items - len(items)
        if remaining > 0:
            items += _match_verify_states(Path(task_dir) if task_dir else None,
                                          subtask.get("id", ""), remaining)
        if not items:
            return {"text": "", "sources": []}
        lines = ["### 历史经验（跨任务失败记忆，仅供参考）", ""]
        lines += [f"- {it['line']}" for it in items]
        lines += ["",
                  "注意：历史经验只用于定位方向，最终仍以当前验证命令的实际结果为准；"
                  "若历史经验与当前现象矛盾，忽略它。"]
        if logger:
            logger.info(f"[knowledge] 注入 {len(items)} 条历史经验: "
                        f"{[it['source_id'] for it in items]}")
        return {"text": "\n".join(lines),
                "sources": [it["source_id"] for it in items]}
    except Exception as e:
        if logger:
            logger.debug(f"[knowledge] 经验提取失败（忽略）: {e}")
        return {"text": "", "sources": []}
