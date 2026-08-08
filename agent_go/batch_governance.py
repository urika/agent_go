"""Result batch governance and immutable baseline manifests (M0-10)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .bench_schema import BENCH_SCHEMA_VERSION, validate_results_file
from .metric_report import build_metric_freeze_report, write_metric_freeze_report


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _single_batch(records: list[dict[str, Any]]) -> tuple[str, str]:
    batches = {record.get("source_batch", "") for record in records}
    suites = {record.get("suite", "canonical") for record in records}
    if len(batches) != 1 or not next(iter(batches), ""):
        raise ValueError("results must contain one non-empty source_batch")
    if len(suites) != 1:
        raise ValueError("results must contain one suite")
    return next(iter(batches)), next(iter(suites))


def build_batch_manifest(
    results_path: str | Path,
    *,
    source_batch: str = "",
    suite: str = "",
    catalog_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a manifest after validating schema and batch isolation."""
    path = Path(results_path)
    records = validate_results_file(path)
    actual_batch, actual_suite = _single_batch(records)
    if source_batch and source_batch != actual_batch:
        raise ValueError("source_batch argument does not match result records")
    if suite and suite != actual_suite:
        raise ValueError("suite argument does not match result records")
    report = build_metric_freeze_report(
        path, source_batch=actual_batch, suite=actual_suite,
        catalog_path=catalog_path, config_path=config_path,
    )
    return {
        "manifest_version": 1,
        "immutable": True,
        "source_batch": actual_batch,
        "suite": actual_suite,
        "bench_schema_version": BENCH_SCHEMA_VERSION,
        "results_sha256": _sha256(path),
        "record_count": len(records),
        "task_ids": sorted({record["task_id"] for record in records}),
        "models": sorted({record["model"] for record in records}),
        "repeat_values": sorted({int(record["repeat"]) for record in records}),
        "task_catalog_hash": report["task_catalog_hash"],
        "config_hash": report["config_hash"],
        "metric_freeze_version": report["metric_freeze_version"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def write_batch_manifest(manifest: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def archive_baseline(
    results_path: str | Path,
    output_root: str | Path,
    *,
    source_batch: str = "",
    suite: str = "",
    catalog_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    """Copy a validated immutable batch into baselines/<source_batch>.

    The source file is never removed or modified.
    """
    source = Path(results_path)
    manifest = build_batch_manifest(
        source, source_batch=source_batch, suite=suite,
        catalog_path=catalog_path, config_path=config_path,
    )
    target = Path(output_root) / "baselines" / manifest["source_batch"]
    target.mkdir(parents=True, exist_ok=True)
    copied = target / "results.jsonl"
    shutil.copy2(source, copied)
    manifest["results_sha256"] = _sha256(copied)
    write_batch_manifest(manifest, target / "manifest.json")
    report = build_metric_freeze_report(
        copied, source_batch=manifest["source_batch"], suite=manifest["suite"],
        catalog_path=catalog_path, config_path=config_path,
    )
    write_metric_freeze_report(report, target / "summary.json")
    return target


def validate_mergeable_batches(paths: list[str | Path], *, source_batch: str = "") -> list[dict[str, Any]]:
    """Reject direct merges across schema versions or source batches."""
    errors: list[dict[str, Any]] = []
    schema_versions: set[int] = set()
    batches: set[str] = set()
    for path in paths:
        try:
            records = validate_results_file(path)
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        schema_versions.update(record["bench_schema_version"] for record in records)
        batches.update(record["source_batch"] for record in records)
    if len(schema_versions) > 1:
        errors.append({"error": "cannot merge different bench_schema_version values"})
    if len(batches) > 1 or (source_batch and batches != {source_batch}):
        errors.append({"error": "cannot merge different source_batch values"})
    return errors
