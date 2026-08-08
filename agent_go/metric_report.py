"""Reproducible Metric Freeze report generation (M0-9)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .bench_schema import validate_results_file, BENCH_SCHEMA_VERSION
from .config import CONFIG_PATH, DEFAULT_CONFIG
from .metrics import compute_frozen_metrics


METRIC_FREEZE_VERSION = 1


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordinary = [
        record for record in records
        if record.get("suite", "canonical") != "stress" and not record.get("high_variance", False)
    ]
    stress = [
        record for record in records
        if record.get("suite") == "stress" or record.get("high_variance", False)
    ]
    return ordinary, stress


def build_metric_freeze_report(
    results_path: str | Path,
    *,
    source_batch: str = "",
    suite: str = "",
    catalog_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a versioned report and reject mixed or anonymous batches."""
    records = validate_results_file(results_path)
    if not records:
        raise ValueError("cannot freeze an empty results file")

    batches = {record.get("source_batch", "") for record in records}
    requested_batch = source_batch or (next(iter(batches)) if len(batches) == 1 else "")
    if not requested_batch or batches != {requested_batch}:
        raise ValueError("Metric Freeze requires one non-empty immutable source_batch")

    suites = {record.get("suite", "canonical") for record in records}
    requested_suite = suite or (next(iter(suites)) if len(suites) == 1 else "")
    if not requested_suite or suites != {requested_suite}:
        raise ValueError("Metric Freeze requires one suite; split mixed-suite results first")

    catalog = Path(catalog_path) if catalog_path else Path(__file__).resolve().parent.parent / "eval_suite" / "task_catalog.json"
    catalog_hash = _sha256_file(catalog)
    if not catalog_hash:
        raise ValueError(f"task catalog not found: {catalog}")

    if config_path:
        config_file = Path(config_path)
        config_hash = _sha256_file(config_file)
        if not config_hash:
            raise ValueError(f"config file not found: {config_file}")
    else:
        config_hash = _sha256_file(CONFIG_PATH)
        if not config_hash:
            default_config = json.dumps(
                DEFAULT_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            config_hash = hashlib.sha256(default_config).hexdigest()
    if config_hash is None:
        embedded_hashes = {record.get("config_hash") for record in records if record.get("config_hash")}
        if len(embedded_hashes) == 1:
            config_hash = next(iter(embedded_hashes))

    ordinary, stress = _metric_records(records)
    elapsed = [float(record.get("elapsed_sec") or 0.0) for record in records]
    timestamps = [
        value for record in records
        for value in (record.get("started_at"), record.get("finished_at"), record.get("timestamp"))
        if value
    ]
    repeats = sorted({int(record["repeat"]) for record in records})
    models = sorted({record["model"] for record in records})
    return {
        "metric_freeze_version": METRIC_FREEZE_VERSION,
        "bench_schema_version": BENCH_SCHEMA_VERSION,
        "source_batch": requested_batch,
        "source_batch_immutable": True,
        "suite": requested_suite,
        "models": models,
        "repeat": repeats[0] if len(repeats) == 1 else repeats,
        "repeat_values": repeats,
        "task_count": len({record["task_id"] for record in records}),
        "record_count": len(records),
        "task_catalog_hash": catalog_hash,
        "config_hash": config_hash,
        "run_time_range": {
            "first_timestamp": min(timestamps) if timestamps else None,
            "last_timestamp": max(timestamps) if timestamps else None,
            "elapsed_sec_min": min(elapsed) if elapsed else None,
            "elapsed_sec_max": max(elapsed) if elapsed else None,
        },
        "metrics": compute_frozen_metrics(ordinary),
        "stress_metrics": compute_frozen_metrics(stress),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_metric_freeze_report(report: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
