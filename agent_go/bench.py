"""模型生产力评估编排器（S8 P0）。

设计原则（解耦 §3）：不 import pipeline/executor 等核心模块，通过 subprocess 调 CLI。
核心与评估的唯一接口是 metering.jsonl + meta.json 的数据契约。

CLI:
  agent_go eval bench --tasks eval_suite/ --candidate-models m1,m2 --repeat 3 --output results.jsonl
  agent_go eval models  # 读 results.jsonl 输出决策矩阵
"""

import argparse
import json
import re
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
from .config import AGENT_GO_DIR, CONFIG_PATH
from .eval import _read_jsonl, _read_json
from .assessment import load_all as load_all_assessments, compute_false_positive_rate
from .pricing import MODEL_PRICES

__all__ = ["cmd_bench", "analyze_model_productivity"]
console = _LazyConsole()


def _run_with_grace(proc: subprocess.Popen, hard_timeout: int, grace_sec: int = 60):
    """P1 Layer 1：cooperative timeout —— 不直接 SIGKILL，先 SIGTERM 给 agent_go 写 meta 的机会。

    Returns: (stdout, stderr) bytes

    流程：
    1. 监控子进程，最多 hard_timeout 秒
    2. 剩余 grace_sec 秒时，发 SIGTERM（让 pipeline.py handler 触发 save meta.json）
    3. 再等 grace_sec 秒
    4. 仍未退出 → SIGKILL（实在不行）
    """
    import time as _time
    import threading as _threading
    deadline = _time.time() + hard_timeout
    poll_interval = min(5, hard_timeout // 20)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    reader_threads: list[_threading.Thread] = []

    if proc.stdout:
        def _read_out():
            for line in iter(proc.stdout.readline, ""):
                stdout_lines.append(line)
        t = _threading.Thread(target=_read_out, daemon=True)
        t.start()
        reader_threads.append(t)
    if proc.stderr:
        def _read_err():
            for line in iter(proc.stderr.readline, ""):
                stderr_lines.append(line)
        t = _threading.Thread(target=_read_err, daemon=True)
        t.start()
        reader_threads.append(t)

    while True:
        remaining = deadline - _time.time()
        if proc.poll() is not None:
            break
        if remaining <= 0:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            break
        if remaining <= grace_sec and not getattr(proc, "_terminated", False):
            try:
                proc.terminate()
                proc._terminated = True
            except ProcessLookupError:
                pass
        _time.sleep(min(poll_interval, remaining))
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait()
    for t in reader_threads:
        t.join(timeout=5)
    return type("R", (), {"stdout": "".join(stdout_lines), "stderr": "".join(stderr_lines)})()

# 默认 agent_go 入口：用脚本绝对路径（不依赖 pip 安装或 PYTHONPATH，
# 避免子进程 cwd 在 fixture repo 内时找不到 agent_go 包）
_AGENT_GO_ENTRY = ["-m", "agent_go"]


# ═══════════════════════════════════════════════════════════════
# 编排器
# ═══════════════════════════════════════════════════════════════

def cmd_bench(args=None) -> None:
    """对照运行编排器主函数。"""
    if yaml is None:
        console.warning("需要 PyYAML 以解析任务文件：pip install pyyaml")
        sys.exit(1)

    # 关键修复：用 bench.py 所在目录（agent_go workspace）作为基准解析相对路径
    # 这样不依赖 caller cwd（修复 macOS xcrun shim + cwd 漂移导致的 "can't open file" 错误）
    _workspace = Path(__file__).resolve().parent.parent

    tasks_arg = args.tasks if args and hasattr(args, "tasks") else "eval_suite"
    tasks_dir = Path(tasks_arg)
    if not tasks_dir.is_absolute():
        tasks_dir = _workspace / tasks_dir

    output_arg = getattr(args, "output", None) or "eval_suite/results.jsonl"
    output_path = Path(output_arg)
    if not output_path.is_absolute():
        output_path = _workspace / output_path

    models = [m.strip() for m in (getattr(args, "candidate_models", None) or "").split(",") if m.strip()]
    repeat = int(getattr(args, "repeat", 3) or 3)

    if not models:
        console.error("至少指定一个 --candidate-models（逗号分隔）")
        sys.exit(1)

    task_files = sorted(tasks_dir.glob("tasks/*.yaml"))
    if not task_files:
        console.error(f"未找到任务文件: {tasks_dir}/tasks/*.yaml")
        sys.exit(1)

    console.print(f"🚀 bench 开始: {len(task_files)} 任务 × {len(models)} 模型 × {repeat} 重复 = {len(task_files)*len(models)*repeat} 次执行")
    console.print(f"   模型: {', '.join(models)}")
    console.print(f"   输出: {output_path}")

    results: list[dict] = []
    total = len(task_files) * len(models) * repeat
    current = 0
    no_skills = bool(getattr(args, "no_skills", False))

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
                result_entry = _run_one_task(task, repo, model, task_id, no_skills=no_skills)
                result_entry["model"] = model
                result_entry["repeat"] = r + 1
                results.append(result_entry)

                # 实时追加到输出文件
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(result_entry, ensure_ascii=False) + "\n")

    console.print(f"\n✅ bench 完成: {len(results)} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


def _run_one_task(task: dict, repo: Path, model: str, task_id: str,
                  preserve: bool = False, no_skills: bool = False) -> list[dict]:
    """跑一次任务 → 读产物 → 返回每子任务的结构化结果列表。

    preserve=True 时传 --preserve-worktrees 给 agent_go run，保留 worktree 供交叉评判读 diff。
    """
    start = time.time()

    # 1. 写临时 config：继承用户的 plan_api，只覆盖 worker_models 为被测模型
    # 关键修复（ISSUE bench 空 api_key）：之前硬编码 anthropic + api_key="" 导致
    # plan 生成回落到本地模型（offline）→ 全部 [no_changes] / pass_rate=0%。
    # 现在从用户 ~/.agent_go/config.json 读取 plan_api，保留其 provider/base_url/api_key/model。
    user_config: dict = {}
    if CONFIG_PATH.exists():
        try:
            user_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            user_config = {}

    plan_api = dict(user_config.get("plan_api", {}))  # 深拷贝，保留用户的 key
    if not plan_api or not plan_api.get("api_key"):
        # 退化：用户 config 没设 plan_api 时用 deepseek 默认（不写死 anthropic）
        plan_api = {
            "provider": "deepseek",
            "base_url": "http://127.0.0.1:4000",
            "api_key": "${DEEPSEEK_API_KEY}",
            "model": "sonnet[1m]",
            "max_tokens": 4096,
            "temperature": 0.2,
        }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        config = {
            "plan_api": plan_api,
            "worker_models": {"easy": model, "medium": model, "hard": model},
            "behavior": {"auto_confirm_plan": True, "auto_confirm_subtasks": True},
            "evaluator": {"enabled": True},
        }
        # 继承用户的 skills / agent_loop 配置（skill 自动发现等），否则 bench 默认关闭
        for _k in ("skills", "agent_loop", "verification"):
            if user_config.get(_k):
                config[_k] = dict(user_config[_k])
        # --no-skills 时强制关闭 skill 自动发现（用于 skill on/off 对比）
        if no_skills:
            config.setdefault("skills", {})["auto_discover"] = False
            config.setdefault("skills", {})["max_auto_skills"] = 0
        json.dump(config, tf)
        tmp_config = tf.name

    # 2. subprocess 调 agent_go run（进程级隔离，不 import 核心）
    # 关键修复：用绝对路径调 agent_go.py，cwd 设为 agent_go 工作目录（不是 fixture 父目录）
    # 这样 agent_go 子进程能找到 agent_go 包（fixture 父目录下没有 agent_go 包）。
    agent_go_cmd = [sys.executable] + _AGENT_GO_ENTRY
    # 工作目录用 agent_go.py 的父目录（workspace），不是 fixture 父目录
    workspace_dir = Path(__file__).resolve().parent.parent
    # 快照现有任务目录，用于精确匹配 subprocess 创建的任务（避免竞态）
    _before_dirs = set(AGENT_GO_DIR.glob("task-*")) if AGENT_GO_DIR.exists() else set()
    try:
        # P1 Layer 1：cooperative timeout —— 用 Popen + 监控代替硬 timeout
        # 剩余 grace_sec 秒时发 SIGTERM，让 agent_go 完成当前步骤 + save meta
        # grace_sec 后才 SIGKILL（实在不行才硬杀）
        hard_timeout = task.get("timeout", 1800)
        grace_sec = 60
        proc = subprocess.Popen(
            agent_go_cmd + ["run",
             str(repo), task["task"],
             "--yes", "--headless", "--preserve-worktrees",
             "--config", tmp_config],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workspace_dir),
        )
        result = _run_with_grace(proc, hard_timeout=hard_timeout, grace_sec=grace_sec)
        result = subprocess.CompletedProcess(
            args=proc.args, returncode=proc.returncode,
            stdout=result.stdout, stderr=result.stderr,
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
    #    精确匹配 subprocess 创建的任务目录：优先从子进程输出解析 task ID，
    #    避免并发竞态（目录差分在并发任务下会匹配错目录）。
    _resolved_td = None
    if result.stdout or result.stderr:
        _combined = (result.stdout or "") + "\n" + (result.stderr or "")
        _m = re.search(r"agent_go\.(task-\d{8}-\d{6}-\d{3}-[0-9a-f]{4})", _combined)
        if _m:
            _candidate = AGENT_GO_DIR / _m.group(1)
            if _candidate.exists():
                _resolved_td = _candidate
    _new_dirs = set()
    if _resolved_td is None:
        _after_dirs = set(AGENT_GO_DIR.glob("task-*")) if AGENT_GO_DIR.exists() else set()
        _new_dirs = _after_dirs - _before_dirs
    return _collect_result(task_id, model, elapsed, exit_code, stderr_tail, _new_dirs, exact_td=_resolved_td)


def _collect_result(task_id: str, model: str, elapsed: float,
                    exit_code: int, stderr: str,
                    new_dirs: "Optional[set[Path]]" = None,
                    exact_td: "Optional[Path]" = None) -> dict:
    """从 agent_go 任务目录读 metering + meta，聚合为一条结果。

    exact_td: 精确任务目录（从子进程输出解析，优先）。
    new_dirs: 目录差分结果（回退方案，仅当 exact_td 为 None 时使用）。
    若两者都不可用则回退到按名称排序取最新（兼容旧调用路径）。
    """
    td = exact_td
    if td is None:
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

        efficiency_score = _model_efficiency_score(avg_pass_rate, avg_cost)
        cost_per_pass = _model_cost_per_pass(avg_cost, avg_pass_rate)
        models[model] = {
            "sample_size": n,
            "avg_pass_rate": avg_pass_rate,
            "avg_cost_usd": avg_cost,
            "dollar_per_pass": cost_per_pass,
            "efficiency_score": efficiency_score,
            "completed_subtasks": completed_total,
            "total_subtasks": subtask_total,
            "recommendation": recommendation,
            "recommended_roles": roles,
            "reason": reason,
        }

        # 从 agent_go 任务目录读取评估事件计算假阳性率
        fp_data = _compute_fp_for_model(model, AGENT_GO_DIR)
        if fp_data:
            models[model]["false_positive_rate"] = fp_data["fp_rate"]
            models[model]["avg_confidence"] = fp_data["avg_confidence"]

    return {"models": models, "total_runs": len(results)}


def _compute_fp_for_model(model: str, base_dir: Path) -> Optional[dict]:
    """扫描 AGENT_GO_DIR 下该模型相关的评估事件，计算假阳性率。

    注意：bench 当前设计下评估数据在 run 级别 task_dir 中，模型信息
    可从 metering.jsonl 的 evaluator 事件中匹配 actual_model。
    若数据不足则返回 None。
    """
    try:
        events = load_all_assessments(base_dir)
    except Exception:
        return None
    if not events:
        return None
    matched = [e for e in events if model in e.evaluator_model]
    if not matched:
        return None
    fp = compute_false_positive_rate(matched)
    if fp["total_evaluated"] == 0:
        return None
    return {"fp_rate": fp["false_positive_rate"], "avg_confidence": fp["avg_confidence"]}


def _model_efficiency_score(pass_rate: float, avg_cost: float) -> float:
    """量化模型效率：每美元获得的通过率（越高越经济）。

    公式: efficiency = pass_rate / avg_cost
    含义: 每花 1 美元能获得多少通过率（0-100% 归一化）
    示例: pass_rate=80%, cost=$0.50 → efficiency=1.6 passes/dollar
           pass_rate=50%, cost=$0.20 → efficiency=2.5 passes/dollar (更经济)
    """
    if avg_cost <= 0 or pass_rate <= 0:
        return 0.0
    return round(pass_rate / avg_cost, 4)


def _model_cost_per_pass(avg_cost: float, avg_pass_rate: float) -> Optional[float]:
    """每个通过子任务的平均成本（越低越经济）。"""
    if avg_pass_rate <= 0 or avg_cost <= 0:
        return None
    return round(avg_cost / avg_pass_rate, 6)

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
        console.warning(f"{data['error']} → 先跑 agent_go eval bench")
        return

    console.print(f"\n📊 模型生产力评估（{data['total_runs']} 次执行）")
    console.print("─" * 100)
    console.print(f"{'模型':<22} {'样本':>4} {'通过率':>7} {'$/pass':>9} {'效率':>8} {'假阳性':>7} {'建议':<12} {'原因'}")
    console.print("─" * 100)

    for model, m in data["models"].items():
        icon = {"recommended": "★", "conditional": "⚠", "discouraged": "✗", "insufficient_data": "?"}[m["recommendation"]]
        fp_str = f"{m.get('false_positive_rate', '?'):>3}%" if isinstance(m.get('false_positive_rate'), (int, float)) else "   ?"
        eff = m.get("efficiency_score", 0)
        eff_str = f"{eff:>7.1f}" if eff > 0 else "     -"
        console.print(f"{icon} {model:<20} {m['sample_size']:>4} {m['avg_pass_rate']:>6.0%} "
                      f"${(m['dollar_per_pass'] or 0):>8.4f} {eff_str} {fp_str:>7} "
                      f"{m['recommendation']:<12} {m['reason']}")
    console.print("─" * 100)
    console.print("效率 = passes/dollar (每美元获得的通过率，越高越经济)")


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════

# _read_jsonl / _read_json 已抽取到 eval.py（共享实现）
