"""Auditable failure metadata migration for historical task directories."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Any

from .config import AGENT_GO_DIR
from .failure import aggregate_failure_class, classify_failure
from .delivery import evaluate_accepted_delivery
from .status import set_task_status


def repair_task_metadata(
    task_dir: str | Path,
    *,
    apply: bool = False,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Infer missing failure fields; preserve original evidence and be reversible."""
    task_path = Path(task_dir)
    meta_path = task_path / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    repaired = json.loads(json.dumps(meta))
    changes: list[str] = []
    results = repaired.get("results") or []
    by_id = {r.get("subtask_id"): r for r in results}

    for result in results:
        if result.get("status") in ("completed", "no_changes") and not result.get("crash_but_verified"):
            if result.pop("failure_class", None) is not None:
                changes.append(f"{result.get('subtask_id')}: cleared_stale_failure_class")
            result.pop("root_failure_class", None)
            result.pop("root_failure_subtask", None)
            # 成功子任务无失败类：清除后不再重新推断，保证幂等收敛
            continue
        if not result.get("failure_class"):
            historical_crash = (
                result.get("status") == "failed"
                and result.get("verify_ok") is True
                and result.get("commit_hash")
                and (result.get("_crash_but_verified_fixed") or result.get("kill_reason") == "none")
            )
            inferred = "infrastructure_failure" if historical_crash else classify_failure(result, repaired)
            if result.get("status") == "blocked":
                root_id = (result.get("blocked_by") or [None])[0]
                root = by_id.get(root_id, {})
                inferred = root.get("failure_class") or classify_failure(root) or "system_error"
                result["root_failure_subtask"] = root_id
                result["root_failure_class"] = inferred
            if inferred:
                result["failure_class"] = inferred
                changes.append(f"{result.get('subtask_id')}: failure_class={inferred}")
        if (
            result.get("status") == "failed"
            and result.get("verify_ok") is True
            and result.get("commit_hash")
            and (result.get("_crash_but_verified_fixed") or result.get("kill_reason") == "none")
            and not result.get("historical_false_failure")
        ):
            result["historical_false_failure"] = True
            result["derived_status"] = "completed"
            changes.append(f"{result.get('subtask_id')}: derived_status=completed")

    if not repaired.get("failure_class"):
        task_class = aggregate_failure_class([r.get("failure_class") for r in results], repaired)
        if task_class:
            repaired["failure_class"] = task_class
            changes.append(f"task: failure_class={task_class}")

    # 无子任务结果但处于终态失败/阻断：提前终止路径（如 plan_quality 阻断）
    # 未产生任何 results，无法推断模型/验证失败，保守归类为 system_error。
    # 幂等：已标记 blocked_without_result 或已有 failure_class 则跳过。
    if (
        not results
        and repaired.get("status") in {"BLOCKED", "VERIFICATION_FAILED", "DELIVERY_FAILED"}
        and not repaired.get("blocked_without_result")
        and not repaired.get("failure_class")
    ):
        repaired["failure_class"] = "system_error"
        repaired["failure_reason"] = repaired.get("failure_reason") or "blocked_without_result"
        repaired["blocked_without_result"] = True
        repaired["root_failure_class"] = "system_error"
        changes.append("task: failure_class=system_error (blocked_without_result)")

    if results and any(r.get("status") == "blocked" for r in results) and not any(
        r.get("status") == "failed" for r in results
    ):
        if repaired.get("status") == "VERIFICATION_FAILED":
            if repaired.get("status_schema_version"):
                set_task_status(repaired, "BLOCKED")
            else:
                repaired["derived_status"] = "BLOCKED"
            if repaired.get("status") != "BLOCKED":
                changes.append("task: derived_status=BLOCKED")

    delivery = evaluate_accepted_delivery(repaired, repaired.get("repo") or None)
    for key, value in delivery.items():
        if repaired.get(key) != value:
            repaired[key] = value
            changes.append(f"task: {key} repaired")
    if repaired.get("status") == "ACCEPTED_DELIVERY" and not delivery["accepted_delivery"]:
        if repaired.get("status_schema_version"):
            set_task_status(repaired, "DELIVERY_READY")
        else:
            repaired["derived_status"] = "DELIVERY_READY"
        changes.append("task: accepted status downgraded to DELIVERY_READY")

    result = {
        "task_id": repaired.get("task_id", task_path.name),
        "task_dir": str(task_path),
        "changes": changes,
        "changed": bool(changes),
        "applied": False,
    }
    if apply and changes:
        if backup_dir:
            backup_path = Path(backup_dir) / task_path.name / "meta.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(meta_path, backup_path)
            result["backup"] = str(backup_path)
        repaired["metadata_migration_version"] = 1
        repaired["metadata_migrated_at"] = datetime.now().isoformat()
        meta_path.write_text(json.dumps(repaired, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["applied"] = True
    return result


def repair_all_tasks(
    base_dir: str | Path = AGENT_GO_DIR,
    *,
    apply: bool = False,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    reports = []
    for task_dir in sorted(Path(base_dir).glob("task-*")):
        if (task_dir / "meta.json").exists():
            try:
                reports.append(repair_task_metadata(task_dir, apply=apply, backup_dir=backup_dir))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                reports.append({"task_id": task_dir.name, "error": str(exc), "changed": False})
    return {
        "base_dir": str(base_dir),
        "apply": apply,
        "task_count": len(reports),
        "changed_task_count": sum(bool(r.get("changed")) for r in reports),
        "error_count": sum("error" in r for r in reports),
        "tasks": reports,
    }
