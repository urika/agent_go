"""Planner-only benchmark: compare planner models in isolation.

Runs generate_plan() with different model configs against the same tasks.
Collects: JSON parse success, subtask count, schema compliance, duration, cost.

Usage:
    python bench_planner.py
"""

import json, logging, os, sys, time
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))

# API 密钥（从环境变量加载，避免重复解析）
_DEFAULT_CFG = Path.home() / ".agent_go" / "config.json"
if _DEFAULT_CFG.exists():
    _C = json.loads(_DEFAULT_CFG.read_text())
    _PLAN_API = _C.get("plan_api", {})
    _KEY_TEMPLATE = _PLAN_API.get("api_key", "")
    if isinstance(_KEY_TEMPLATE, str) and _KEY_TEMPLATE.startswith("${"):
        DEEPSEEK_KEY = os.environ.get(_KEY_TEMPLATE[2:-1], "")
    else:
        DEEPSEEK_KEY = _KEY_TEMPLATE or ""
else:
    DEEPSEEK_KEY = os.environ.get("AGENT_GO_API_KEY", "")

KIMI_KEY = os.environ.get("KIMI_API_KEY", "sk-kimi-LpdrWPakKFlVReTZWuWAp2aWRwOtcQmHHWcm6lsHjnWfeivxsYjDMa1x2UzttLfQ")

from agent_go.api import generate_plan
from agent_go.config import DEFAULT_CONFIG

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("bench_planner")

REQUIRED_STEP_FIELDS = {"agent_type", "difficulty", "agent_prompt", "description", "files", "verification", "risks", "skills", "title"}

MODELS = [
    {
        "name": "deepseek-v4-flash",
        "label": "DS Flash",
        "config": {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1/chat/completions",
            "api_key": DEEPSEEK_KEY,
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "temperature": 0.2,
        }
    },
    {
        "name": "deepseek-v4-pro",
        "label": "DS Pro",
        "config": {
            "provider": "openai",
            "base_url": "https://api.deepseek.com/v1/chat/completions",
            "api_key": DEEPSEEK_KEY,
            "model": "deepseek-v4-pro",
            "max_tokens": 8192,
            "temperature": 0.2,
        }
    },
    {
        "name": "kimi-k3",
        "label": "Kimi K3",
        "config": {
            "provider": "anthropic",
            "base_url": "https://api.kimi.com/coding/v1/messages",
            "api_key": KIMI_KEY,
            "model": "k3",
            "max_tokens": 8192,
            "temperature": 0.2,
        }
    },
    {
        "name": "kimi-highspeed",
        "label": "Kimi 2.7",
        "config": {
            "provider": "anthropic",
            "base_url": "https://api.kimi.com/coding/v1/messages",
            "api_key": KIMI_KEY,
            "model": "kimi-for-coding-highspeed",
            "max_tokens": 8192,
            "temperature": 0.2,
        }
    },
]

TASKS = [
    {
        "name": "fix-default",
        "repo": Path("/Users/jinsongwang/test-target/task-mgr"),
        "task": "fix missing default in cmd_list: change default param from '' to 'all'",
        "difficulty": "easy",
    },
    {
        "name": "add-tags",
        "repo": Path("/Users/jinsongwang/test-target/task-mgr"),
        "task": "add tag system: Task class gets tags field, storage gets save_tags/find_by_tag, CLI gets tag/untag/list --tag commands",
        "difficulty": "hard",
    },
    {
        "name": "add-metrics",
        "repo": Path("/Users/jinsongwang/test-target/data-pipeline"),
        "task": "add metrics system: MetricStage that records execution timing, MetricsCollector that aggregates, and 'metrics' CLI command to display results",
        "difficulty": "hard",
    },
    {
        "name": "add-pipeline-retry",
        "repo": Path("/Users/jinsongwang/test-target/data-pipeline"),
        "task": "Add retry logic to Pipeline.run(): each stage gets max_retries config, on exception it retries up to max_retries times with exponential backoff, log each attempt",
        "difficulty": "medium",
    },
]


def build_config(planner_cfg):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["planner_api"] = dict(planner_cfg)
    cfg["plan_api"] = cfg["planner_api"]
    cfg["cache"] = {"enabled": False, "plan_ttl": 0, "max_entries": 0}
    return cfg


def assess_plan(plan: dict, task_name: str) -> dict:
    issues = []
    if not isinstance(plan, dict):
        return {"valid": False, "issues": ["not a dict"], "step_count": 0}

    steps = plan.get("steps", [])
    if not steps:
        return {"valid": False, "issues": ["no steps"], "step_count": 0}

    step_count = len(steps)
    if step_count < 2:
        issues.append(f"few steps ({step_count})")

    for i, s in enumerate(steps):
        missing = REQUIRED_STEP_FIELDS - set(s.keys())
        if missing:
            issues.append(f"step {i+1} missing: {missing}")
        if s.get("difficulty") not in ("easy", "medium", "hard"):
            issues.append(f"step {i+1} invalid difficulty={s.get('difficulty')}")
        agent = s.get("agent_type", "")
        if agent not in ("developer", "architect", "reviewer", "tester"):
            issues.append(f"step {i+1} invalid agent_type={agent}")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "step_count": step_count,
        "field_completeness": sum(1 for s in steps if REQUIRED_STEP_FIELDS.issubset(s.keys())) / step_count if step_count else 0,
    }


def main():
    results = []

    for model in MODELS:
        for task_def in TASKS:
            label = f"{model['label']} / {task_def['name']}"
            print(f"\n  [{label}] ...", end=" ", flush=True)

            cfg = build_config(model["config"])
            start = time.time()
            try:
                plan = generate_plan(
                    task=task_def["task"],
                    repo=task_def["repo"],
                    config=cfg,
                    logger=logger,
                    no_cache=True,
                )
                elapsed = time.time() - start
                assessment = assess_plan(plan, task_def["name"])
                step_count = assessment["step_count"]
                valid = assessment["valid"]
                issues = assessment["issues"]
                duration_ms = round(elapsed * 1000)
                print(f"{'✅' if valid else '❌'} {step_count} steps, {duration_ms}ms")
                if issues:
                    print(f"       issues: {issues[:3]}")
            except Exception as e:
                elapsed = time.time() - start
                print(f"❌ ERROR: {e}")
                plan = {}
                assessment = {"valid": False, "issues": [str(e)], "step_count": 0}
                duration_ms = round(elapsed * 1000)

            results.append({
                "model": model["label"],
                "task": task_def["name"],
                "success": assessment["valid"],
                "step_count": assessment["step_count"],
                "issues": assessment.get("issues", []),
                "duration_ms": duration_ms,
                "field_completeness": assessment.get("field_completeness", 0),
            })

    # Summary
    print("\n" + "=" * 90)
    print(f"  {'模型':<12} {'任务':<20} {'通过':>4} {'步骤':>4} {'耗时ms':>7} {'完整性':>6}")
    print("  " + "-" * 70)
    for r in results:
        icon = "✅" if r["success"] else "❌"
        print(f"  {r['model']:<12} {r['task']:<20} {icon:>4} {r['step_count']:>4} {r['duration_ms']:>7} {r['field_completeness']:>6.0%}")
    print("  " + "=" * 70)

    by_model = defaultdict(list)
    for r in results:
        by_model[r["model"]].append(r)

    print(f"\n  {'模型':<12} {'通过率':>6} {'avg步骤':>7} {'avg耗时ms':>9} {'avg完整性':>8}")
    print("  " + "-" * 60)
    for model_name, runs in sorted(by_model.items()):
        n = len(runs)
        pass_rate = sum(1 for r in runs if r["success"]) / n
        avg_steps = sum(r["step_count"] for r in runs) / n
        avg_ms = sum(r["duration_ms"] for r in runs) / n
        avg_fc = sum(r["field_completeness"] for r in runs) / n
        print(f"  {model_name:<12} {pass_rate:>6.0%} {avg_steps:>7.1f} {avg_ms:>9.0f} {avg_fc:>8.0%}")

    # Save detailed results
    out_path = Path("/Users/jinsongwang/workspace/agent_go/eval_suite/planner-bench.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n  Detailed results → {out_path}")


if __name__ == "__main__":
    main()
