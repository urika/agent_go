import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "eval_suite" / "tasks"
CATALOG = ROOT / "eval_suite" / "task_catalog.json"


def test_task_catalog_covers_all_canonical_tasks():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    task_ids = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["id"]
        for path in TASKS.glob("*.yaml")
    }
    assert task_ids == set(catalog)
    assert all(entry.get("suites") for entry in catalog.values())
    required = {"task_version", "business_relevance", "runtime_cost", "high_variance", "semantic_probe", "delivery_probe"}
    assert all(required <= set(entry) for entry in catalog.values())
    assert all(len(entry["task_version"]) == 16 for entry in catalog.values())


def test_suite_selection_is_small_for_smoke_and_stress():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    smoke = [tid for tid, item in catalog.items() if "smoke" in item["suites"]]
    stress = [tid for tid, item in catalog.items() if "stress" in item["suites"]]
    decision = [tid for tid, item in catalog.items() if "decision" in item["suites"]]
    # P2 任务集扩充（2026-08-11）：35 个 canonical 任务。
    # smoke/stress 保持精简（快速冒烟 + 高方差子集），decision 覆盖主力评测任务。
    assert 6 <= len(smoke) <= 10
    assert 4 <= len(stress) <= 8
    assert 20 <= len(decision) <= 35


def test_m0_baseline_freezes_all_tasks_and_stress_exclusion():
    baseline = json.loads((ROOT / "eval_suite" / "m0_baseline.json").read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert baseline["baseline_id"] == "m0-canonical-v1"
    assert set(baseline["tasks"]) == set(catalog)
    assert set(baseline["stress_excluded_from_ordinary_average"]) == {
        tid for tid, item in catalog.items() if item["high_variance"]
    }
