"""Versioned Bench record schema and JSONL validator (M0-4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .failure import FAILURE_CLASSES


BENCH_SCHEMA_VERSION = 1
REQUIRED_FIELDS = {
    "task_id", "task_version", "suite", "source_batch", "model",
    "planner_model", "judge_model", "repeat", "difficulty", "failure_class",
    "accepted_delivery", "delivery_branch_created", "pr_created",
    "spec_compliance", "architecture_compliance", "total_cost_usd", "elapsed_sec",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_record(record: Any) -> list[str]:
    """Return validation errors; an empty list means the record is valid."""
    if not isinstance(record, dict):
        return ["record must be an object"]
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - record.keys())
    errors.extend(f"missing required field: {field}" for field in missing)
    if record.get("bench_schema_version") != BENCH_SCHEMA_VERSION:
        errors.append(f"bench_schema_version must be {BENCH_SCHEMA_VERSION}")
    for field in ("task_id", "task_version", "suite", "source_batch", "model", "planner_model", "judge_model"):
        if field in record and not isinstance(record[field], str):
            errors.append(f"{field} must be a string")
    if "repeat" in record and (not isinstance(record["repeat"], int) or isinstance(record["repeat"], bool) or record["repeat"] < 1):
        errors.append("repeat must be a positive integer")
    if "difficulty" in record and record["difficulty"] not in {"easy", "medium", "hard"}:
        errors.append("difficulty must be easy, medium, or hard")
    if record.get("failure_class") is not None and record.get("failure_class") not in FAILURE_CLASSES:
        errors.append("failure_class must be null or a fixed failure class")
    errors.extend(
        f"{field} must be boolean"
        for field in ("accepted_delivery", "delivery_branch_created", "pr_created")
        if field in record and not isinstance(record[field], bool)
    )
    for field in ("spec_compliance", "architecture_compliance"):
        if field in record and record[field] is not None and not isinstance(record[field], bool):
            errors.append(f"{field} must be boolean or null")
    for field in ("total_cost_usd", "elapsed_sec"):
        if field in record and (not _is_number(record[field]) or record[field] < 0):
            errors.append(f"{field} must be a non-negative number")
    return errors


def validate_results_file(path: str | Path) -> list[dict[str, Any]]:
    """Validate every JSONL record and raise ``ValueError`` on any error."""
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_no, "errors": [f"invalid JSON: {exc.msg}" ]})
            continue
        record_errors = validate_record(record)
        if record_errors:
            errors.append({"line": line_no, "errors": record_errors})
        else:
            records.append(record)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return records
