"""证据物化层（M6.1）：把 immutable bench 批次聚合为 LLM 可推理的结构化证据包。

设计原则（decision-assistant-design.md §1 三边界）：
  - 证据强制绑定：materialize_evidence 校验 manifest（immutable），输出含 evidence_hash
  - 只读聚合：不修改任何批次数据
  - 复用现有基建：eval.py 的 _read_jsonl/_read_json、batch manifest/schema

输出证据包（materialize_evidence 返回）供 eval insight 的 LLM 推理注入，
保证 LLM 只能基于真实数据推理（不凭空编造）。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .eval import _read_jsonl, _read_json


class EvidenceError(Exception):
    """证据物化失败（批次不存在/校验失败/数据损坏）。"""


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _aggregate_failure_modes(records: list[dict[str, Any]]) -> dict[str, Any]:
    """失败模式聚合：failure_class × 模型 × 任务。"""
    by_class: Counter = Counter()
    by_model: dict[str, Counter] = {}
    by_task: dict[str, Counter] = {}
    failed_records = []
    for r in records:
        ok = r.get("accepted_delivery") or r.get("binary_pass")
        if ok:
            continue
        fc = r.get("failure_class") or "unknown"
        model = r.get("model") or "unknown"
        task = r.get("task_id") or "unknown"
        by_class[fc] += 1
        by_model.setdefault(model, Counter())[fc] += 1
        by_task.setdefault(task, Counter())[fc] += 1
        failed_records.append({
            "task_id": task,
            "model": model,
            "failure_class": fc,
            "failure_reason": (r.get("failure_reason") or "")[:200],
            "kill_reason": r.get("kill_reason", ""),
            "timed_out": bool(r.get("timed_out")),
        })
    return {
        "by_failure_class": dict(by_class),
        "by_model": {m: dict(c) for m, c in by_model.items()},
        "by_task": {t: dict(c) for t, c in by_task.items()},
        "failed_records": failed_records,
    }


def materialize_evidence(batch_path: str | Path,
                         config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """把 immutable bench 批次聚合为结构化证据包。

    Args:
        batch_path: baselines/<batch>/ 目录或 results.jsonl 路径
        config: 运行时配置（用于环境快照；None 时从 load_config 读）

    Returns:
        结构化证据包 dict，含 evidence_hash（供 LLM 推理注入 + 审计校验）。

    Raises:
        EvidenceError: 批次不存在/校验失败/数据损坏。
    """
    root = Path(batch_path).expanduser()
    if root.is_file():
        root = root.parent
    results_path = root / "results.jsonl"
    if not results_path.exists():
        raise EvidenceError(f"results.jsonl 不存在: {results_path}")

    # 1. manifest 校验（immutable 证据基础）
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    manifest_summary = {
        "source_batch": manifest.get("source_batch", root.name),
        "suite": manifest.get("suite", ""),
        "record_count": manifest.get("record_count", 0),
        "models": manifest.get("models", []),
        "task_ids": manifest.get("task_ids", []),
        "immutable": manifest.get("immutable", False),
        "results_sha256_match": False,
    }
    if manifest.get("results_sha256"):
        actual = _sha256_path(results_path)
        manifest_summary["results_sha256_match"] = actual == manifest["results_sha256"]

    # 2. results.jsonl → 记录 + 失败模式聚合
    records = _read_jsonl(results_path)
    if not records:
        raise EvidenceError(f"results.jsonl 为空或解析失败: {results_path}")
    failure_modes = _aggregate_failure_modes(records)

    # 3. summary.json → 指标
    summary_path = root / "summary.json"
    summary = _read_json(summary_path) if summary_path.exists() else {}
    metrics = summary.get("metrics", {}) or {}
    metric_summary = {
        "task_count": summary.get("task_count", len(records)),
        "pass_rate_diagnostic": metrics.get("pass_rate_diagnostic"),
        "first_pass_rate": metrics.get("first_pass_rate"),
        "accepted_delivery_rate": metrics.get("accepted_delivery_rate"),
        "dollar_per_pass_usd": metrics.get("dollar_per_pass_diagnostic_usd"),
        "valid_cost_usd": metrics.get("valid_cost_usd"),
        "failure_class_counts": metrics.get("failure_class_counts", {}),
        "timeout_rate": metrics.get("timeout_rate"),
    }

    # 4. 逐任务记录摘要（通过率/成本/延迟/失败类）
    per_task = []
    for r in records:
        per_task.append({
            "task_id": r.get("task_id"),
            "model": r.get("model"),
            "difficulty": r.get("difficulty"),
            "passed": bool(r.get("accepted_delivery") or r.get("binary_pass")),
            "pass_rate": r.get("pass_rate"),
            "failure_class": r.get("failure_class"),
            "elapsed_sec": r.get("elapsed_sec"),
            "cost_usd": r.get("dollar_per_pass") or r.get("total_cost_usd"),
            "timed_out": bool(r.get("timed_out")),
        })

    # 5. 环境快照（配置摘要——用于环境漂移检测）
    env_snapshot: dict[str, Any] = {}
    try:
        if config is None:
            from .config import load_config
            config = load_config()
        plan_api = config.get("plan_api", {}) or {}
        env_snapshot = {
            "plan_model": plan_api.get("model", ""),
            "plan_base_url": plan_api.get("base_url", "")[:60],
            "worker_models": config.get("worker_models", {}),
            "goal_policy": (config.get("goal") or {}).get("policy", ""),
            "router_enabled": (config.get("router") or {}).get("enabled", False),
        }
    except Exception:
        env_snapshot = {"note": "环境快照获取失败"}

    # 6. problems.py 历史失败模式（跨任务失败记忆）
    problems_summary: list[dict[str, Any]] = []
    try:
        from .problems import load as load_problems
        from .config import AGENT_GO_DIR
        problems_path = AGENT_GO_DIR / "problems.jsonl"
        if problems_path.exists():
            problems = load_problems(problems_path)
            for p in list(problems)[-10:]:
                problems_summary.append({
                    "id": p.id,
                    "status": p.status,
                    "category": getattr(p, "category", ""),
                    "summary": (getattr(p, "summary", "") or "")[:150],
                })
    except Exception:
        problems_summary = []

    evidence = {
        "schema": "insight_evidence/v1",
        "source_batch": manifest_summary["source_batch"],
        "suite": manifest_summary["suite"],
        "manifest": manifest_summary,
        "metrics": metric_summary,
        "failure_modes": failure_modes,
        "per_task": per_task,
        "environment": env_snapshot,
        "problems_history": problems_summary,
        "record_count": len(records),
    }
    # 证据包 hash（审计/防篡改校验）
    canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode("utf-8")
    evidence["evidence_hash"] = hashlib.sha256(canonical).hexdigest()[:16]
    return evidence


def evidence_to_prompt_context(evidence: dict[str, Any], max_chars: int = 12000) -> str:
    """证据包 → LLM 推理注入的紧凑文本上下文（截断保护）。"""
    parts = [
        f"批次: {evidence['source_batch']} (suite={evidence['suite']}, {evidence['record_count']} 条记录)",
        f"通过率: {evidence['metrics'].get('pass_rate_diagnostic')} (accepted={evidence['metrics'].get('accepted_delivery_rate')})",
        f"成本: $/pass={evidence['metrics'].get('dollar_per_pass_usd')}",
        "",
        "失败模式聚合（failure_class × 模型）:",
    ]
    for model, classes in evidence["failure_modes"]["by_model"].items():
        parts.append(f"  {model}: {classes}")
    parts.append("")
    parts.append("失败任务明细（前 12）:")
    for fr in evidence["failure_modes"]["failed_records"][:12]:
        parts.append(f"  [{fr['failure_class']}] {fr['task_id']} ({fr['model']}): {fr['failure_reason'][:80]}")
    parts.append("")
    parts.append(f"环境快照: plan={evidence['environment'].get('plan_model')} goal={evidence['environment'].get('goal_policy')}")
    text = "\n".join(parts)
    if len(text) > max_chars:
        keep = max(0, max_chars - 30)
        text = text[:keep] + "\n... [证据上下文已截断]"
    return text
