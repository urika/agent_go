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
import shlex
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

__all__ = ["cmd_bench", "cmd_baseline", "analyze_model_productivity"]
console = _LazyConsole()


def _run_with_grace(proc: subprocess.Popen, hard_timeout: int, grace_sec: int = 60):
    """P1 Layer 1：cooperative timeout —— 不直接 SIGKILL，先 SIGTERM 给 agent_go 写 meta 的机会。

    Returns: (stdout, stderr) bytes 与是否因超时被终止的标志

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
    _timed_out = False

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
            _timed_out = True
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            break
        if remaining <= grace_sec and not getattr(proc, "_terminated", False):
            _timed_out = True
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
    # 等待 reader 线程读完管道剩余数据（SIGKILL 时管道 EOF，线程会自行结束）
    for t in reader_threads:
        t.join(timeout=grace_sec)
    # 兜底：若 reader 线程仍残留（异常场景），耗尽管道剩余数据
    for t in reader_threads:
        if t.is_alive() and proc.stderr and not proc.stderr.closed:
            try:
                proc.stderr.read()
            except OSError:
                pass
        if t.is_alive() and proc.stdout and not proc.stdout.closed:
            try:
                proc.stdout.read()
            except OSError:
                pass
        t.join(timeout=2)
    _r = type("R", (), {"stdout": "".join(stdout_lines), "stderr": "".join(stderr_lines)})
    _r.timed_out = _timed_out
    return _r

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
    source_batch = getattr(args, "source_batch", None) or ""

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
                result_entry = _run_one_task(task, repo, model, task_id, no_skills=no_skills, source_batch=source_batch, results_path=output_path)
                result_entry["model"] = model
                result_entry["repeat"] = r + 1
                results.append(result_entry)

                # 实时追加到输出文件
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(result_entry, ensure_ascii=False) + "\n")

    console.print(f"\n✅ bench 完成: {len(results)} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


# ═══════════════════════════════════════════════════════════════
# S10-P2：对照基线（bench-v2-data-requirements.md §2.3）
# ═══════════════════════════════════════════════════════════════

def _run_baseline_one(task: dict, repo: Path, model: str, task_id: str,
                      source_batch: str = "baseline") -> dict:
    """claude -p 裸跑一个任务（不走 agent_go harness），作为对照基线。

    流程：
      1. 在临时副本中运行 `claude -p <task>`（stream-json 模式，提取 cost + elapsed）
      2. 对该临时目录运行任务 YAML 的 verification 命令 → pass 判定
      3. 对临时目录运行 ruff/mypy/pytest → 代码质量指标
    返回与 _collect_result 同构的 record（task_dir=""，source_batch=baseline）。
    """
    import shutil
    import tempfile as _tempfile

    start = time.time()
    tmp_dir = Path(_tempfile.mkdtemp(prefix="bench_baseline_"))
    work = tmp_dir / "repo"
    try:
        # 复制 fixture 到临时目录（不复制 .git，避免污染原 repo / 依赖 git 状态）
        shutil.copytree(repo, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

        # 1. claude -p 裸跑（stream-json 提取 total_cost_usd / elapsed）
        claude_cmd = [
            "claude", "-p", task["task"],
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        _routed_model = model
        if _routed_model:
            claude_cmd.extend(["--model", _routed_model])
        timeout = int(task.get("timeout", 1800)) + 60
        cost_usd = 0.0
        exit_code = -1
        try:
            cp = subprocess.run(
                claude_cmd, cwd=str(work), capture_output=True, text=True, timeout=timeout,
            )
            exit_code = cp.returncode
            for line in (cp.stdout or "").splitlines():
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(ev, dict) and ev.get("type") == "result":
                    cost_usd = ev.get("total_cost_usd") or cost_usd
        except subprocess.TimeoutExpired:
            exit_code = -1

        # 2. verification 命令 → pass 判定（全部退出码 0 才算通过）
        verification_ok = True
        for vcmd in (task.get("verification") or []):
            try:
                vr = subprocess.run(
                    shlex.split(vcmd), cwd=str(work), capture_output=True, text=True, timeout=120,
                )
            except (subprocess.SubprocessError, OSError):
                verification_ok = False
                break
            if vr.returncode != 0:
                verification_ok = False
                break

        # 3. 代码质量（对完整临时副本运行，等价于裸跑产出）
        quality = {
            "lint_errors": _lint_errors_for_worktree(work),
            "tests_broken": _tests_broken_for_worktree(work),
        }
        elapsed = round(time.time() - start, 2)

        return {
            "task_id": task_id,
            "model": model,
            "task_dir": "",
            "elapsed_sec": elapsed,
            "subprocess_exit": exit_code,
            "completed": 1 if verification_ok else 0,
            "failed": 0 if verification_ok else 1,
            "total_subtasks": 1,
            "pass_rate": 1.0 if verification_ok else 0.0,
            "all_verify_ok": verification_ok,
            "total_retries": 0,
            "total_cost_usd": round(cost_usd or 0.0, 6),
            "total_latency_ms": 0,
            "dollar_per_pass": round(cost_usd or 0.0, 6) if verification_ok and cost_usd else None,
            "stderr_tail": "",
            "timed_out": exit_code == -1,
            "judge_model": "",
            "planner_model": "",
            "source_batch": source_batch,
            "semantic_pass": None,
            "binary_pass": verification_ok,
            "per_subtask": [],
            "plan_step_count": 0,
            "lint_errors": quality["lint_errors"],
            "tests_broken": quality["tests_broken"],
            "baseline": True,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def cmd_baseline(args=None) -> None:
    """对照基线编排器：claude -p 裸跑代表性任务（不走 agent_go harness）。

    用于量化 harness 相对裸跑的 pass_rate / 耗时 / 成本 / 代码质量 ROI。
    复用 bench 的任务集/模型/重复参数，结果写入单独文件（默认 baseline.jsonl）。
    """
    if yaml is None:
        console.warning("需要 PyYAML 以解析任务文件：pip install pyyaml")
        sys.exit(1)

    _workspace = Path(__file__).resolve().parent.parent

    tasks_arg = args.tasks if args and hasattr(args, "tasks") else "eval_suite"
    tasks_dir = Path(tasks_arg)
    if not tasks_dir.is_absolute():
        tasks_dir = _workspace / tasks_dir

    output_arg = getattr(args, "output", None) or "eval_suite/baseline.jsonl"
    output_path = Path(output_arg)
    if not output_path.is_absolute():
        output_path = _workspace / output_path

    models = [m.strip() for m in (getattr(args, "candidate_models", None) or "").split(",") if m.strip()]
    repeat = int(getattr(args, "repeat", 3) or 3)
    source_batch = getattr(args, "source_batch", None) or "baseline"

    if not models:
        console.error("至少指定一个 --candidate-models（逗号分隔）")
        sys.exit(1)

    task_files = sorted(tasks_dir.glob("tasks/*.yaml"))
    if not task_files:
        console.error(f"未找到任务文件: {tasks_dir}/tasks/*.yaml")
        sys.exit(1)

    console.print(f"🧪 baseline 开始（claude -p 裸跑）: {len(task_files)} 任务 × {len(models)} 模型 × {repeat} 重复 = {len(task_files)*len(models)*repeat} 次执行")
    console.print(f"   模型: {', '.join(models)}")
    console.print(f"   输出: {output_path}")

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
                entry = _run_baseline_one(task, repo, model, task_id, source_batch=source_batch)
                entry["model"] = model
                entry["repeat"] = r + 1
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    console.print(f"\n✅ baseline 完成: {len(task_files) * len(models) * repeat} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


def _run_one_task(task: dict, repo: Path, model: str, task_id: str,
                  preserve: bool = False, no_skills: bool = False,
                  source_batch: str = "", results_path: Optional[Path] = None) -> list[dict]:
    """跑一次任务 → 读产物 → 返回每子任务的结构化结果列表。

    preserve=True 时传 --preserve-worktrees 给 agent_go run，保留 worktree 供交叉评判读 diff。
    source_batch: 批次标识（如 baseline / results_v2 / smoke-*），写入每条 record。
    results_path: 结果文件路径，用于从历史记录推断子任务数做动态 timeout。
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
        # 继承用户的 evaluator 配置（provider/base_url/model/api_key），
        # 否则 evaluator 回落到 DEFAULT_CONFIG 的 anthropic 默认值 → 403
        # （DEFAULT_CONFIG.evaluator.provider=anthropic + base_url=api.anthropic.com，
        #  用户环境没有可用 Anthropic key 时语义评估必失败）
        if user_config.get("evaluator"):
            evaluator_cfg = dict(user_config["evaluator"])
            evaluator_cfg["enabled"] = True  # bench 强制启用语义评估
            config["evaluator"] = evaluator_cfg
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
        # grace_sec 后才 SIGKILL（实在不行才硬杀）。
        # 动态 timeout：多子任务任务按「子任务数 × 基准耗时」自动扩展，
        # 避免任务被 timeout 截断（K1 提升）。不低于任务 YAML 配置值。
        hard_timeout = _dynamic_timeout(task, task_id, results_path)
        grace_sec = 60
        console.debug(f"[timeout] {task_id} → {hard_timeout}s ({_estimate_subtasks_from_history(task_id, results_path)} subtasks)")
        proc = subprocess.Popen(
            agent_go_cmd + ["run",
             str(repo), task["task"],
             "--yes", "--headless", "--preserve-worktrees",
             "--parallel", "1",   # S10-P2：顺序执行，消除并发对 elapsed/cost 的干扰
             "--config", tmp_config],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(workspace_dir),
        )
        result = _run_with_grace(proc, hard_timeout=hard_timeout, grace_sec=grace_sec)
        _timed_out = bool(getattr(result, "timed_out", False))
        result = subprocess.CompletedProcess(
            args=proc.args, returncode=proc.returncode,
            stdout=result.stdout, stderr=result.stderr,
        )
        exit_code = result.returncode
        stderr_tail = result.stderr[-500:] if result.stderr else ""
    except subprocess.TimeoutExpired:
        exit_code = -1
        stderr_tail = "bench: subprocess timeout"
        _timed_out = True
    finally:
        Path(tmp_config).unlink(missing_ok=True)

    elapsed = round(time.time() - start, 2)

    # 3. 读产物（数据契约：metering.jsonl + meta.json）
    #    精确匹配 subprocess 创建的任务目录：优先从子进程输出解析 task ID，
    #    并校验目录 meta 内容与当前任务一致（防止并发进程/残留目录错配）。
    _expected = task.get("task", "")
    _resolved_td = None
    if result.stdout or result.stderr:
        _combined = (result.stdout or "") + "\n" + (result.stderr or "")
        _m = re.search(r"agent_go\.(task-\d{8}-\d{6}-\d{3}-[0-9a-f]{4})", _combined)
        if _m:
            _candidate = AGENT_GO_DIR / _m.group(1)
            if _candidate.exists() and _dir_matches_task(_candidate, _expected):
                _resolved_td = _candidate
    _new_dirs = set()
    if _resolved_td is None:
        _after_dirs = set(AGENT_GO_DIR.glob("task-*")) if AGENT_GO_DIR.exists() else set()
        _new_dirs = _after_dirs - _before_dirs
    return _collect_result(task_id, model, elapsed, exit_code, stderr_tail, _new_dirs, exact_td=_resolved_td, expected_task=_expected, timed_out=_timed_out, source_batch=source_batch)


def _dir_matches_task(td: Path, expected_task: str) -> bool:
    """校验任务目录的 meta.json 描述是否与期望任务匹配（防止并发进程/残留目录错配）。

    - 期望描述为空 → 无法校验，返回 True（兼容旧调用路径）。
    - 目录不存在或无 meta.json → 返回 False（无法证明匹配）。
    - 描述匹配判定：meta.task 与期望 task 前 30 字符一致，或 meta.task 是
      期望 task 的前缀 / 期望 task 是 meta.task 的前缀（同一任务可能描述略有裁剪）。
    """
    if not expected_task or not td or not td.exists():
        return not expected_task
    meta_path = td / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    meta_task = (meta.get("task") or "").strip()
    expected = expected_task.strip()
    if not meta_task:
        return False
    n = min(len(meta_task), len(expected), 30)
    if n == 0:
        return False
    return meta_task[:n] == expected[:n]


# 动态 timeout 基准参数（基于 v2 bench 实测：每子任务平均耗时 ~70-150s）
# 基准取 150s/subtask，另加 120s 缓冲覆盖收尾（保存 meta/清理/push）与波动。
# 公式：timeout = max(任务YAML配置值, 子任务数 × 基准 + 缓冲)
_DYNAMIC_TIMEOUT_BASE_SEC = 150      # 每子任务基准耗时（秒）
_DYNAMIC_TIMEOUT_BUFFER_SEC = 120    # 收尾/波动缓冲（秒）


def _estimate_subtasks_from_history(task_id: str, results_path: Optional[Path]) -> int:
    """从已有 bench 结果文件推断该任务的历史子任务数。

    Args:
        task_id: 任务 ID
        results_path: bench 结果文件（results.jsonl）。读取其中该 task_id 的
            total_subtasks。

    Returns:
        历史最大子任务数（取最大值比均值更稳妥——Plan 可能产生不同的分解，
        用最大值避免低估导致任务仍被 timeout 截断）；无法推断时返回 0。
    """
    if not results_path or not results_path.exists():
        return 0
    try:
        max_n = 0
        for line in results_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("task_id") == task_id:
                n = rec.get("total_subtasks")
                if isinstance(n, int) and n > 0:
                    max_n = max(max_n, n)
        return max_n
    except OSError:
        return 0


def _dynamic_timeout(task: dict, task_id: str, results_path: Optional[Path] = None) -> int:
    """按子任务数动态计算任务 timeout，解决多子任务任务被 timeout 截断的问题。

    优先级：
      1. 任务 YAML 显式声明 `timeout` → 作为下限（不缩短既有配置）
      2. 历史推断子任务数 × 基准耗时 + 缓冲 → 动态上限

    公式：timeout = max(配置值, 子任务数 × 150s + 120s)
    例：add-caching-layer 5 子任务 → max(900, 5×150+120=870) = 900s
        （历史推断为 5 时仍用配置 900；若子任务更多则自动扩展）
    """
    cfg_timeout = int(task.get("timeout", 1800))
    n = _estimate_subtasks_from_history(task_id, results_path)
    if n <= 0:
        return cfg_timeout
    dynamic = n * _DYNAMIC_TIMEOUT_BASE_SEC + _DYNAMIC_TIMEOUT_BUFFER_SEC
    return max(cfg_timeout, dynamic)


def _subtask_semantic_ok(subtask_result: dict) -> Optional[bool]:
    """判断单个子任务是否通过语义评估（verification_results 中 type=='semantic'）。

    语义评估执行失败被跳过时视为 None（未执行），而不是 False —— 否则会错误地
    「因 API 故障而判失败」，污染 binary_pass。识别跳过的两种信号：
      1. evaluator_skipped=True 字段（新版本 evaluator 返回）
      2. reason 含「API 调用失败 / 已跳过 / 失败（已跳过）」特征（旧版本 executor
         写入 verification_results 时丢弃了 evaluator_skipped，只留 reason 特征）

    Returns:
        True/False 若有有效语义评估结果；None 若未启用、无结果或评估被跳过。
    """
    for vr in (subtask_result.get("verification_results") or []):
        if isinstance(vr, dict) and vr.get("type") == "semantic":
            # 跳过信号 1：evaluator_skipped 字段
            if vr.get("evaluator_skipped"):
                return None
            # 跳过信号 2：reason 特征（API 故障 / 显式跳过）
            reason = (vr.get("reason") or "").lower()
            if any(k in reason for k in ("api 调用失败", "api 请求失败", "调用失败", "已跳过", "failed (skipped)", "评估失败（已跳过）")):
                return None
            return bool(vr.get("passed"))
    return None


# ═══════════════════════════════════════════════════════════════
# S10-P2：代码质量维度（bench-v2-data-requirements.md §4.1）
# ═══════════════════════════════════════════════════════════════

def _git_diff_files(worktree: Path, base_ref: str = "HEAD~1") -> list[str]:
    """获取 worktree 中相对 base 变更的 Python 文件（供 ruff/mypy 检查）。

    base_ref 默认取父提交（HEAD~1）——worktree 分支上每个 subtask 一个 commit。
    返回失败时为空列表（容错，不阻断 bench）。
    """
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            cwd=str(worktree), capture_output=True, text=True, timeout=15,
        )
        return [f for f in r.stdout.splitlines() if f.endswith(".py")]
    except (subprocess.SubprocessError, OSError):
        return []


def _lint_errors_for_worktree(worktree: Path) -> int:
    """对 worktree 中变更的 Python 文件运行 ruff + mypy，返回新增错误数。

    仅统计存在（未删除）的 .py 变更文件；工具缺失/不可用时返回 0（容错）。
    ruff 只查 E/F/W（与 CI lint 一致）；mypy --ignore-missing-imports 忽略缺失 stub。
    """
    files = _git_diff_files(worktree)
    if not files:
        return 0
    errors = 0
    for cmd in (
        ["ruff", "check", "--select=E,F,W", "--output-format=concise"],
        ["mypy", "--ignore-missing-imports", "--no-error-summary"],
    ):
        try:
            r = subprocess.run(
                cmd + files, cwd=str(worktree), capture_output=True, text=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            continue  # 工具缺失 / 超时 → 该维度不计数
        if r.stdout:
            errors += sum(1 for line in r.stdout.splitlines() if line.strip())
    return errors


def _tests_broken_for_worktree(worktree: Path) -> int:
    """运行 repo 原有测试套件（pytest），返回新增测试失败数。

    以「worktree 上 pytest 失败用例数」作为「新增失败」的代理 —— fixture repo
    基线测试应为全绿，失败即代表 subtask 变更引入的回归。pytest 不可用时返回 0。
    """
    try:
        r = subprocess.run(
            ["python", "-m", "pytest", "-q", "--tb=no"],
            cwd=str(worktree), capture_output=True, text=True, timeout=300,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    if r.returncode == 0:
        return 0
    # 解析 "N failed" 或 "N failed, M passed" 中的失败数
    for line in r.stdout.splitlines():
        line = line.strip()
        m = re.match(r"(\d+) failed", line)
        if m:
            return int(m.group(1))
    return 0


def _collect_quality(task_dir: Path) -> dict[str, int]:
    """聚合 task_dir 下全部保留 worktree 的代码质量指标。

    Returns:
        {"lint_errors": int, "tests_broken": int} —— 各 subtask worktree 之和。
        worktree 不保留（已清理）或无 worktree 时返回全 0。
    """
    lint = 0
    broken = 0
    if not task_dir or not task_dir.exists():
        return {"lint_errors": 0, "tests_broken": 0}
    # worktree 布局：task_dir/{sub_id}/work
    for sub_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
        worktree = sub_dir / "work"
        if worktree.exists():
            lint += _lint_errors_for_worktree(worktree)
            broken += _tests_broken_for_worktree(worktree)
    return {"lint_errors": lint, "tests_broken": broken}


def _collect_result(task_id: str, model: str, elapsed: float,
                    exit_code: int, stderr: str,
                    new_dirs: "Optional[set[Path]]" = None,
                    exact_td: "Optional[Path]" = None,
                    expected_task: str = "",
                    timed_out: bool = False,
                    source_batch: str = "") -> dict:
    """从 agent_go 任务目录读 metering + meta，聚合为一条结果。

    exact_td: 精确任务目录（从子进程输出解析，优先）。
    new_dirs: 目录差分结果（回退方案，仅当 exact_td 为 None 时使用）。
    expected_task: 期望任务描述，用于校验目录内容匹配（防止并发/残留错配）。
    timed_out: 任务是否因超时被强制终止（cooperative timeout 触发 SIGTERM/SIGKILL）。
    source_batch: 批次标识（如 baseline / smoke-*），用于跨批次追溯与全量对比。
    若都无法定位匹配目录则返回空数据记录（task_dir=""）。
    """
    td = exact_td if (exact_td and _dir_matches_task(exact_td, expected_task)) else None
    if td is None and new_dirs:
        # 从差集目录中筛选 meta 内容匹配的任务目录，取最新；全部不匹配则降级
        candidates = [d for d in new_dirs if _dir_matches_task(d, expected_task)]
        if candidates:
            td = sorted(candidates, reverse=True)[0]
        elif expected_task:
            td = None
        else:
            td = sorted(new_dirs, reverse=True)[0]
    if td is None and expected_task:
        # 回退：全盘扫描 meta.task 匹配的最近目录（并发进程污染差集时的兜底）
        task_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        td = next((d for d in task_dirs if _dir_matches_task(d, expected_task)), None)
    elif td is None:
        # 兼容旧调用路径：按名称排序取最新
        task_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        td = task_dirs[0] if task_dirs else None

    metering = _read_jsonl(td / "metering.jsonl") if td else []
    meta = _read_json(td / "meta.json") if td else {}

    # cost 聚合
    total_cost = sum(ev.get("cost_usd", 0) or 0 for ev in metering)
    total_latency = sum(ev.get("latency_ms", 0) or 0 for ev in metering)

    # S10-P1：从 metering 提取 Planner / Judge 模型（跨层归因基础）
    planner_model = ""
    judge_model = ""
    for ev in metering:
        _role = ev.get("role", "")
        _am = ev.get("actual_model", "") or ""
        if _role == "planner" and _am:
            planner_model = _am
        elif _role == "evaluator" and _am:
            judge_model = _am

    # 子任务结果
    results = meta.get("results", [])
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    retry_total = sum(r.get("retry_count", 0) for r in results)
    all_passed = all(r.get("verify_ok", False) for r in results if r.get("status") == "completed")

    # S10-P2：代码质量维度（§4.1）—— 从保留 worktree 聚合 lint_errors / tests_broken
    quality = _collect_quality(td) if td else {"lint_errors": 0, "tests_broken": 0}

    # ── S10-P2：P1 字段采集 ──
    # semantic_pass：各子任务的语义评估通过汇总（用 _subtask_semantic_ok，
    #   evaluator_skipped/未启用 → None 不算入，避免「API 故障」误判为失败）。
    #   仅当至少一个子任务有有效语义结果、且全部有效结果为通过 → True；
    #   有任一有效结果为不通过 → False；无任何有效结果 → None。
    _semantic_passed_flags = []
    _semantic_checked = 0
    for _r in results:
        _so = _subtask_semantic_ok(_r)
        if _so is not None:
            _semantic_checked += 1
            _semantic_passed_flags.append(_so)
    semantic_pass: Optional[bool] = None
    if _semantic_checked:
        semantic_pass = all(_semantic_passed_flags)

    # binary_pass：全部子任务 verify_ok，且（语义评估启用时）语义全部通过。
    #   语义评估未启用（semantic_pass is None）时退化为 all_verify_ok 判定。
    binary_pass = all_passed and (semantic_pass is not False)

    # per_subtask：每个子任务的简明细（供按子任务失败模式分析）
    per_subtask = [
        {
            "sub_id": _r.get("subtask_id") or _r.get("id") or "",
            "status": _r.get("status", ""),
            "retries": _r.get("retry_count", 0),
            "verify_ok": bool(_r.get("verify_ok", False)),
            "semantic_ok": _subtask_semantic_ok(_r),
        }
        for _r in results
    ]

    # plan_step_count：执行计划步骤数（subtasks 列表长度；无则 0）
    plan_step_count = len(meta.get("subtasks") or [])

    # ── 进程未自然完成判定 ──
    # bench 用 cooperative timeout：超时先 SIGTERM，grace 后 SIGKILL（-9）。
    # 被 SIGKILL 或非零退出的任务可能有两种情况：
    #   1. 执行中途被杀 → 任务未完成，不应计通过
    #   2. 所有子任务已完成且验证通过，仅收尾阶段（保存 meta/清理 worktree）
    #      被杀 → 实际已完工，应计通过（K1 提升的关键）
    # 判定优先级：
    #   - meta.status == completed → 任务明确成功，绝不判失败（exit_code 非零
    #     可能是收尾命令（如 cleanup/push）的退出码，不代表任务失败）
    #   - 否则若 aborted：区分「收尾被杀（已完工）」与「中途被杀（未完工）」
    meta_status = meta.get("status", "")
    if meta_status == "completed":
        _aborted = False
    else:
        _aborted = exit_code != 0 or meta_status in ("stale_aborted", "aborted", "interrupted", "cancelled")
    if _aborted:
        # 区分「收尾被杀（已完工）」与「中途被杀（未完工）」
        _planned_ids = {st.get("id") for st in (meta.get("subtasks") or [])}
        _result_ids = {r.get("subtask_id") or r.get("id") for r in results if r.get("subtask_id") or r.get("id")}
        _all_resulted = bool(_planned_ids) and _planned_ids.issubset(_result_ids)
        _all_done = bool(results) and all(
            r.get("status") == "completed" and r.get("verify_ok") is True
            for r in results
        )
        if _all_done and _all_resulted:
            # 已完工（收尾阶段被杀）：保持原 completed/all_passed 判定。
            # 仅当 results 覆盖了全部计划子任务时才视为完工——避免无 subtasks
            # 元数据或子任务确实未跑完时误判通过。
            console.debug(f"[collect] {task_id} aborted但全部子任务完成，视为完工 (exit={exit_code}, status={meta_status})")
        else:
            # 中途被杀（未完工）：所有子任务不计通过
            completed = 0
            failed = sum(1 for r in results
                         if r.get("status") in ("failed", "blocked"))
            all_passed = False

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
        "timed_out": timed_out,
        "judge_model": judge_model,
        "planner_model": planner_model,
        "source_batch": source_batch,
        # S10-P2：P1 字段
        "semantic_pass": semantic_pass,
        "binary_pass": binary_pass,
        "per_subtask": per_subtask,
        "plan_step_count": plan_step_count,
        # S10-P2：代码质量维度（§4.1，从保留 worktree 聚合）
        "lint_errors": quality["lint_errors"],
        "tests_broken": quality["tests_broken"],
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

        # S10-P1 统一口径（bench-v2-data-requirements.md §3.1）：
        #   $/pass = sum(total_cost_usd) / sum(pass_rate)
        # 以 raw cost 和 raw pass_rate 为准，不依赖 record 级 dollar_per_pass 字段（跨批次可比）。
        _sum_cost = sum(it.get("total_cost_usd", 0) or 0 for it in items)
        _sum_pass = sum(pass_rates)
        dollar_per_pass = round(_sum_cost / _sum_pass, 6) if _sum_pass > 0 else None

        # S10-P1 K8 修订（§3.4）：K8 = 通过 record 中 zero-retry 占比。
        # 分母只含通过 record（pass_rate > 0），分子是其 total_retries == 0 的子集。
        # binary_pass（P1）落地前以 pass_rate > 0 近似"通过"。
        _passed_records = [it for it in items if (it.get("pass_rate") or 0) > 0]
        _passed_count = len(_passed_records)
        _zero_retry_passed = sum(
            1 for it in _passed_records if (it.get("total_retries") or 0) == 0
        )
        k8 = round(_zero_retry_passed / _passed_count, 4) if _passed_count else None

        # S10-P2 代码质量维度（§3.5 / §4.1）：
        #   - 平均 lint 错误 / 平均测试回归（跨所有 record，含未通过的）
        #   - 代码回归率 = 通过 record（pass_rate>0）中 tests_broken>0 占比
        _lint_all = [it.get("lint_errors") or 0 for it in items]
        _broken_all = [it.get("tests_broken") or 0 for it in items]
        avg_lint = round(sum(_lint_all) / n, 3) if n else 0
        avg_tests_broken = round(sum(_broken_all) / n, 3) if n else 0
        _regression_passed = sum(
            1 for it in _passed_records if (it.get("tests_broken") or 0) > 0
        )
        code_regression_rate = round(_regression_passed / _passed_count, 4) if _passed_count else None

        # 决策规则
        recommendation, roles, reason = _recommend(model, avg_pass_rate, avg_cost, n)

        efficiency_score = _model_efficiency_score(avg_pass_rate, avg_cost)
        cost_per_pass = _model_cost_per_pass(avg_cost, avg_pass_rate)
        models[model] = {
            "sample_size": n,
            "avg_pass_rate": avg_pass_rate,
            "avg_cost_usd": avg_cost,
            "dollar_per_pass": dollar_per_pass,
            "dollar_per_pass_legacy": cost_per_pass,
            "k8_zero_retry_pass_rate": k8,
            "efficiency_score": efficiency_score,
            "avg_lint_errors": avg_lint,
            "avg_tests_broken": avg_tests_broken,
            "code_regression_rate": code_regression_rate,
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
    console.print("─" * 118)
    console.print(f"{'模型':<22} {'样本':>4} {'通过率':>7} {'$/pass':>9} {'K8':>6} {'效率':>8} {'假阳性':>7} {'建议':<12} {'原因'}")
    console.print("─" * 118)

    for model, m in data["models"].items():
        icon = {"recommended": "★", "conditional": "⚠", "discouraged": "✗", "insufficient_data": "?"}[m["recommendation"]]
        fp_str = f"{m.get('false_positive_rate', '?'):>3}%" if isinstance(m.get('false_positive_rate'), (int, float)) else "   ?"
        eff = m.get("efficiency_score", 0)
        eff_str = f"{eff:>7.1f}" if eff > 0 else "     -"
        k8 = m.get("k8_zero_retry_pass_rate")
        k8_str = f"{k8:>5.0%}" if k8 is not None else "    -"
        console.print(f"{icon} {model:<20} {m['sample_size']:>4} {m['avg_pass_rate']:>6.0%} "
                      f"${(m['dollar_per_pass'] or 0):>8.4f} {k8_str} {eff_str} {fp_str:>7} "
                      f"{m['recommendation']:<12} {m['reason']}")
    console.print("─" * 118)
    console.print("效率 = passes/dollar (每美元获得的通过率，越高越经济)")
    console.print("K8   = 通过 record 中 zero-retry 占比（首次验证通过率，§3.4 修订口径）")
    console.print("$/pass = sum(cost) / sum(pass_rate)，raw 口径，跨批次可比（§3.1）")


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════

# _read_jsonl / _read_json 已抽取到 eval.py（共享实现）
