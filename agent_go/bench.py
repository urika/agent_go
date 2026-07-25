"""模型生产力评估编排器（S8 P0）。

设计原则（解耦 §3）：不 import pipeline/executor 等核心模块，通过 subprocess 调 CLI。
核心与评估的唯一接口是 metering.jsonl + meta.json 的数据契约。

CLI:
  agent_go eval bench --tasks eval_suite/ --candidate-models m1,m2 --repeat 3 --output results.jsonl
  agent_go eval models  # 读 results.jsonl 输出决策矩阵
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # yaml 不是 stdlib，bench 启动时提示安装

from .console import _LazyConsole
from .config import AGENT_GO_DIR
from .eval import _read_jsonl, _read_json
from .pricing import MODEL_PRICES

__all__ = ["cmd_bench", "analyze_model_productivity"]
console = _LazyConsole()

# 默认 agent_go 入口（从仓库根目录或 PYTHONPATH 找到）
_AGENT_GO_ENTRY = Path(__file__).resolve().parent.parent / "agent_go.py"


# ═══════════════════════════════════════════════════════════════
# 编排器
# ═══════════════════════════════════════════════════════════════

def cmd_bench(args=None) -> None:
    """对照运行编排器主函数。"""
    if yaml is None:
        console.print("⚠️  需要 PyYAML 以解析任务文件：pip install pyyaml")
        sys.exit(1)

    tasks_dir = Path(args.tasks if args and hasattr(args, "tasks") else "eval_suite")
    models = [m.strip() for m in (getattr(args, "candidate_models", None) or "").split(",") if m.strip()]
    repeat = int(getattr(args, "repeat", 3) or 3)
    output_path = Path(getattr(args, "output", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")

    if not models:
        console.print("❌ 至少指定一个 --candidate-models（逗号分隔）")
        sys.exit(1)

    task_files = sorted(tasks_dir.glob("tasks/*.yaml"))
    if not task_files:
        console.print(f"❌ 未找到任务文件: {tasks_dir}/tasks/*.yaml")
        sys.exit(1)

    console.print(f"🚀 bench 开始: {len(task_files)} 任务 × {len(models)} 模型 × {repeat} 重复 = {len(task_files)*len(models)*repeat} 次执行")
    console.print(f"   模型: {', '.join(models)}")
    console.print(f"   输出: {output_path}")

    results: list[dict] = []
    total = len(task_files) * len(models) * repeat
    current = 0

    for tf in task_files:
        task = yaml.safe_load(tf.read_text(encoding="utf-8"))
        task_id = task["id"]
        repo = Path(task["repo"])
        if not repo.is_absolute():
            repo = Path.cwd() / repo

        for model in models:
            for r in range(repeat):
                current += 1
                console.print(f"\n[{current}/{total}] {task_id} | {model} | repeat={r+1}")
                result_entry = _run_one_task(task, repo, model, task_id)
                result_entry["model"] = model
                result_entry["repeat"] = r + 1
                results.append(result_entry)

                # 实时追加到输出文件
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(result_entry, ensure_ascii=False) + "\n")

    console.print(f"\n✅ bench 完成: {len(results)} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


def _run_one_task(task: dict, repo: Path, model: str, task_id: str,
                  preserve: bool = False) -> list[dict]:
    """跑一次任务 → 读产物 → 返回每子任务的结构化结果列表。

    preserve=True 时传 --preserve-worktrees 给 agent_go run，保留 worktree 供交叉评判读 diff。
    """
    start = time.time()

    # 1. 写临时 config（注入被测模型到 worker_models.medium）
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        config = {
            "plan_api": {
                "provider": "anthropic", "model": "claude-sonnet-5",
                "base_url": "https://api.anthropic.com/v1/messages", "api_key": "",
                "max_tokens": 4096, "temperature": 0.2,
            },
            "worker_models": {"easy": model, "medium": model, "hard": model},
            "behavior": {"auto_confirm_plan": True, "auto_confirm_subtasks": True},
        }
        json.dump(config, tf)
        tmp_config = tf.name

    # 2. subprocess 调 agent_go run（进程级隔离，不 import 核心）
    agent_go_entry = _AGENT_GO_ENTRY
    # 快照现有任务目录，用于精确匹配 subprocess 创建的任务（避免竞态）
    _before_dirs = set(AGENT_GO_DIR.glob("task-*")) if AGENT_GO_DIR.exists() else set()
    try:
        result = subprocess.run(
            [sys.executable, str(agent_go_entry), "run",
             str(repo), task["task"],
             "--yes", "--headless", "--preserve-worktrees",
             "--config", tmp_config],
            capture_output=True, text=True, timeout=task.get("timeout", 600),
            cwd=str(repo.parent if repo.parent != repo else repo),
        )
        exit_code = result.returncode
        stderr_tail = result.stderr[-500:] if result.stderr else ""
    except subprocess.TimeoutExpired:
        exit_code = -1
        stderr_tail = "bench: subprocess timeout"
    finally:
        Path(tmp_config).unlink(missing_ok=True)

    elapsed = round(time.time() - start, 2)

    # 3. 读产物（数据契约：metering.jsonl + meta.json）
    #    精确匹配 subprocess 创建的任务目录，避免并发竞态
    _after_dirs = set(AGENT_GO_DIR.glob("task-*")) if AGENT_GO_DIR.exists() else set()
    _new_dirs = _after_dirs - _before_dirs
    return _collect_result(task_id, model, elapsed, exit_code, stderr_tail, _new_dirs)


def _collect_result(task_id: str, model: str, elapsed: float,
                    exit_code: int, stderr: str,
                    new_dirs: "Optional[set[Path]]" = None) -> dict:
    """从 agent_go 任务目录读 metering + meta，聚合为一条结果。

    new_dirs: 精确的任务目录集合（通过 subprocess 前后快照差分得到，避免并发竞态）。
    若未提供则回退到按名称排序取最新（兼容旧调用路径）。
    """
    if new_dirs and len(new_dirs) == 1:
        td = new_dirs.pop()
    elif new_dirs and len(new_dirs) > 1:
        # 罕见：一次 subprocess 创建了多个目录（如 resume），取最新的
        td = sorted(new_dirs, reverse=True)[0]
    else:
        # 回退：按名称排序取最新
        task_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        td = task_dirs[0] if task_dirs else None

    metering = _read_jsonl(td / "metering.jsonl") if td else []
    meta = _read_json(td / "meta.json") if td else {}

    # cost 聚合
    total_cost = sum(ev.get("cost_usd", 0) or 0 for ev in metering)
    total_latency = sum(ev.get("latency_ms", 0) or 0 for ev in metering)

    # 子任务结果
    results = meta.get("results", [])
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    retry_total = sum(r.get("retry_count", 0) for r in results)
    all_passed = all(r.get("verify_ok", False) for r in results if r.get("status") == "completed")

    return {
        "task_id": task_id,
        "model": model,
        "task_dir": str(td) if td else "",
        "elapsed_sec": elapsed,
        "subprocess_exit": exit_code,
        "completed": completed,
        "failed": failed,
        "total_subtasks": len(results),
        "pass_rate": round(completed / len(results), 4) if results else 0,
        "all_verify_ok": all_passed,
        "total_retries": retry_total,
        "total_cost_usd": round(total_cost, 6),
        "total_latency_ms": round(total_latency, 2),
        "dollar_per_pass": round(total_cost / completed, 6) if completed else None,
        "stderr_tail": stderr[-200:],
    }


# ═══════════════════════════════════════════════════════════════
# 决策汇总
# ═══════════════════════════════════════════════════════════════

def analyze_model_productivity(results_path: Path) -> dict[str, Any]:
    """读 bench results.jsonl，按模型聚合生产力指标 + 决策建议。

    Returns:
        {"models": {model: {pass_rate, dollar_per_pass, ...}},
         "difficulty_breakdown": ...,
         "recommendations": [...]}
    """
    results = _read_jsonl(results_path)
    if not results:
        return {"error": "无数据"}

    by_model: dict[str, list[dict]] = {}
    by_difficulty: dict[str, list[dict]] = {}  # 注：当前任务 YAML 有 difficulty 字段

    for r in results:
        model = r.get("model", "unknown")
        by_model.setdefault(model, []).append(r)

    models = {}
    for model, items in by_model.items():
        n = len(items)
        completed_total = sum(it["completed"] for it in items)
        subtask_total = sum(it["total_subtasks"] for it in items)
        all_costs = [it["total_cost_usd"] for it in items if it["total_cost_usd"] > 0]
        avg_cost = round(sum(all_costs) / len(all_costs), 6) if all_costs else 0

        pass_rates = [it["pass_rate"] for it in items]
        avg_pass_rate = round(sum(pass_rates) / n, 4) if n else 0

        # 决策规则
        recommendation, roles, reason = _recommend(model, avg_pass_rate, avg_cost, n)

        models[model] = {
            "sample_size": n,
            "avg_pass_rate": avg_pass_rate,
            "avg_cost_usd": avg_cost,
            "dollar_per_pass": round(avg_cost / max(avg_pass_rate, 0.01), 6),
            "completed_subtasks": completed_total,
            "total_subtasks": subtask_total,
            "recommendation": recommendation,
            "recommended_roles": roles,
            "reason": reason,
        }

    return {"models": models, "total_runs": len(results)}


def _recommend(model: str, pass_rate: float, avg_cost: float, n: int) -> tuple[str, list[str], str]:
    """决策规则（PRD §3.7 对齐：60/70/75/80 四档阈值）"""
    if n < 3:
        return ("insufficient_data", [], f"仅 {n} 样本，需 ≥3 才可决策")
    if pass_rate < 0.60:
        return ("discouraged", [], f"通过率 {pass_rate:.0%} <60%，省钱产出垃圾（PRD 反指标）")
    if pass_rate >= 0.80:
        return ("recommended", ["worker_easy", "worker_medium", "worker_hard"],
                f"通过率 {pass_rate:.0%} ≥80%，全角色可用")
    if pass_rate >= 0.75:
        return ("conditional", ["worker_easy", "worker_medium", "worker_hard"],
                f"通过率 {pass_rate:.0%} ≥75%，全角色可用（注意 hard 任务表现）")
    if pass_rate >= 0.70:
        return ("conditional", ["worker_easy", "worker_medium"],
                f"通过率 {pass_rate:.0%} ≥70%，easy/medium 可用")
    return ("conditional", ["worker_easy"],
            f"通过率 {pass_rate:.0%} ≥60%，仅 easy 可用")


def cmd_models(args=None) -> None:
    """打印模型生产力决策矩阵。"""
    results_path = Path(getattr(args, "results", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")
    data = analyze_model_productivity(results_path)

    if "error" in data:
        console.print(f"⚠️  {data['error']} → 先跑 agent_go eval bench")
        return

    console.print(f"\n📊 模型生产力评估（{data['total_runs']} 次执行）")
    console.print("─" * 80)
    console.print(f"{'模型':<25} {'样本':>4} {'通过率':>7} {'$/pass':>10} {'建议':<18} {'原因'}")
    console.print("─" * 80)

    for model, m in data["models"].items():
        icon = {"recommended": "★", "conditional": "⚠", "discouraged": "✗", "insufficient_data": "?"}[m["recommendation"]]
        console.print(f"{icon} {model:<23} {m['sample_size']:>4} {m['avg_pass_rate']:>6.0%} "
                      f"${m['dollar_per_pass']:>9.4f} {m['recommendation']:<18} {m['reason']}")
    console.print("─" * 80)


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════

# _read_jsonl / _read_json 已抽取到 eval.py（共享实现）
