"""模型生产力评估编排器（S8 P0）。

设计原则（解耦 §3）：不 import pipeline/executor 等核心模块，通过 subprocess 调 CLI。
核心与评估的唯一接口是 metering.jsonl + meta.json 的数据契约。

CLI:
  agent_go eval bench --tasks eval_suite/ --candidate-models m1,m2 --repeat 3 --output results.jsonl
  agent_go eval models  # 读 results.jsonl 输出决策矩阵
"""

import hashlib
import json
import logging
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # yaml 不是 stdlib，bench 启动时提示安装

from .console import _LazyConsole
from .config import AGENT_GO_DIR, CONFIG_PATH
from .eval import _read_jsonl, _read_json
from .assessment import load_all as load_all_assessments, compute_false_positive_rate
from .pricing import model_tier, validate_worker_tier
from .failure import classify_failure
from .bench_schema import validate_record

__all__ = ["cmd_bench", "cmd_baseline", "analyze_model_productivity"]
console = _LazyConsole()


def _task_version(task: dict) -> str:
    """Stable content version until M0-5 adds catalog-managed task versions."""
    if task.get("_bench_task_version"):
        return str(task["_bench_task_version"])
    payload = json.dumps(task, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


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
    class _R:
        stdout: str
        stderr: str
        timed_out: bool = False
    _r = _R()
    _r.stdout = "".join(stdout_lines)
    _r.stderr = "".join(stderr_lines)
    _r.timed_out = _timed_out
    return _r

# 默认 agent_go 入口：用脚本绝对路径（不依赖 pip 安装或 PYTHONPATH，
# 避免子进程 cwd 在 fixture repo 内时找不到 agent_go 包）
_AGENT_GO_ENTRY = ["-m", "agent_go"]


# ═══════════════════════════════════════════════════════════════
# 编排器
# ═══════════════════════════════════════════════════════════════

# ── S12 运行前预检：实际模型探测 + 定价完整性校验 ──

def _probe_actual_model(model: str, timeout: int = 45) -> str:
    """探测路由名实际解析到的后端模型。

    用一次轻量 claude 调用（-p "hi" + --model <路由名> + stream-json），
    从响应 message.model 解析真实模型（如 claude-haiku-4-5 → glm-4.7）。
    调用失败或超时返回空串（调用方降级为"未知"，仅告警不阻断）。
    """
    if not model:
        return ""
    cmd = ["claude", "-p", "hi",
           "--permission-mode", "bypassPermissions",
           "--no-session-persistence",
           "--output-format", "stream-json",
           "--verbose",
           "--include-partial-messages"]
    if model:
        cmd.extend(["--model", model])
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            cwd=str(Path(__file__).resolve().parent.parent))
        for _line in (cp.stdout or "").splitlines():
            try:
                _ev = json.loads(_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(_ev, dict):
                _msg_model = _ev.get("message", {}).get("model", "")
                if not _msg_model:
                    # stream_event 包装：真实模型在 event.message.model
                    _inner = _ev.get("event", {}) if _ev.get("type") == "stream_event" else {}
                    _msg_model = _inner.get("message", {}).get("model", "")
                if _msg_model:
                    return str(_msg_model).strip()
        return ""
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""


def _preflight_model_pricing(models: list[str], interactive: bool = True) -> bool:
    """运行前模型-价格预检：探测每个候选模型的实际后端 + 校验定价覆盖。

    返回 True=可继续（所有模型有定价，或仅告警不阻断）；False=中止。
    缺定价时：interactive=True 询问用户继续/中止；False（--yes）仅告警继续。
    """
    from .pricing import resolve_price, format_price_for_report

    missing_actual: list[str] = []
    unknown: list[str] = []
    console.print("\n🔍 运行前模型-价格预检（探测实际后端 + 校验定价）…")
    for _m in models:
        _actual = _probe_actual_model(_m)
        if _actual:
            _has = resolve_price(_actual) is not None
            console.print(f"  {_m} → 实际 {_actual} "
                          f"[{'✅ 有定价 ' + format_price_for_report(_actual) if _has else '⚠️ 缺定价'}]")
            if not _has:
                missing_actual.append(f"{_m} → {_actual}")
        else:
            _has_route = resolve_price(_m) is not None
            console.print(f"  {_m} → 探测失败 [{'✅ 路由名有定价（沿用）' if _has_route else '⚠️ 缺定价'}]")
            if not _has_route:
                unknown.append(_m)

    if not missing_actual and not unknown:
        console.print("  ✅ 全部模型有定价，可安全运行")
        return True

    console.warning("⚠️ 以下模型缺少定价，成本将按 claude 报告价（可能虚高）:")
    for _x in missing_actual + unknown:
        console.warning(f"    - {_x}")
    console.warning("   建议：联网抓取确认定价后更新 pricing.py MODEL_PRICES")
    if interactive:
        try:
            _resp = input("  继续运行（成本可能虚高）? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _resp = "n"
        if _resp != "y":
            console.error("预检未通过，中止运行（请先补充定价）")
            return False
    return True


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
        shutil.copytree(repo, work, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "node_modules", "dist", ".vite"))

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
        # kill_reason（baseline 单子任务路径，与 _collect_result 同口径）
        if verification_ok:
            _bl_kill_reason = "none"
        elif exit_code == -1:
            _bl_kill_reason = "stuck_or_hardtimeout"
        elif (cost_usd or 0) == 0:
            _bl_kill_reason = "infra"
        else:
            _bl_kill_reason = "interrupted_or_unknown"

        return {
            "bench_schema_version": 1,
            "task_id": task_id,
            "task_version": _task_version(task),
            "suite": task.get("_bench_suite", "canonical"),
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
            "difficulty": task.get("difficulty", "medium"),
            "accepted_delivery": False,
            "delivery_branch_created": False,
            "pr_created": False,
            "spec_compliance": None,
            "architecture_compliance": None,
            "semantic_pass": None,
            "binary_pass": verification_ok,
            "kill_reason": _bl_kill_reason,
            "failure_class": classify_failure(
                {"status": "completed" if verification_ok else "failed",
                 "verify_ok": verification_ok, "exit_code": exit_code,
                 "kill_reason": _bl_kill_reason},
                timed_out=exit_code == -1,
            ),
            "per_subtask": [],
            "plan_step_count": 0,
            "lint_errors": quality["lint_errors"],
            "tests_broken": quality["tests_broken"],
            "baseline": True,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def cmd_bench(args=None) -> None:
    """eval bench 编排器：tasks × candidate_models × repeat → results.jsonl。

    走完整 agent_go harness（subprocess 隔离），与 cmd_baseline（claude -p 裸跑）对照，
    量化 harness 在真实 plan→decompose→execute 流程下的通过率/成本/质量。
    复用 _run_one_task（已含 subprocess 隔离 + _collect_result），不重写执行逻辑。

    CLI: agent_go eval bench --tasks eval_suite/ --candidate-models m1,m2 --repeat 3 --output results.jsonl
    """
    if yaml is None:
        console.warning("需要 PyYAML 以解析任务文件：pip install pyyaml")
        sys.exit(1)

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
    source_batch = getattr(args, "source_batch", None) or "bench"
    no_skills = bool(getattr(args, "no_skills", False))
    suite = getattr(args, "bench_suite", "") or ""

    if not models:
        console.error("至少指定一个 --candidate-models（逗号分隔）")
        sys.exit(1)

    # S12 运行前预检：校验实际后端 + 定价覆盖
    _no_confirm = bool(getattr(args, "yes", False)) or bool(getattr(args, "eval_all", False))
    if not _preflight_model_pricing(models, interactive=not _no_confirm):
        sys.exit(1)

    task_files = sorted(tasks_dir.glob("tasks/*.yaml"))
    if not task_files:
        console.error(f"未找到任务文件: {tasks_dir}/tasks/*.yaml")
        sys.exit(1)

    catalog_path = tasks_dir / "task_catalog.json"
    catalog = {}
    if catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            console.warning(f"任务目录 catalog 读取失败，将运行全部任务: {exc}")
    if suite:
        task_files = [
            tf for tf in task_files
            if suite in (catalog.get(tf.stem.split("-", 1)[-1], {}).get("suites", []))
            or suite in (catalog.get(yaml.safe_load(tf.read_text(encoding="utf-8")).get("id", ""), {}).get("suites", []))
        ]
        if not task_files:
            console.error(f"suite={suite} 没有匹配任务")
            sys.exit(1)

    total = len(task_files) * len(models) * repeat
    console.print(f"🧪 bench 开始（走 harness）: {len(task_files)} 任务 × {len(models)} 模型 × {repeat} 重复 = {total} 次执行")
    console.print(f"   模型: {', '.join(models)}")
    console.print(f"   输出: {output_path}")

    # S12 bench 并行：默认并发 2（--bench-parallel 可调）。
    # 并行单元 = (task, model, repeat) 组合，每个独立 subprocess + worktree，
    # 数据隔离（pass_rate/cost 不受影响）。受 API rate-limit 与本地验证资源约束。
    _bench_parallel = int(getattr(args, "bench_parallel", 2) or 1)
    _bench_parallel = max(1, _bench_parallel)
    console.print(f"   并发度: {_bench_parallel}（--bench-parallel 可调）")

    # 预加载所有任务（读取 YAML 与 repo），线程池内只执行 _run_one_task
    _all_jobs: list[tuple[dict, Path, str, str, int]] = []
    for tf in task_files:
        task = yaml.safe_load(tf.read_text(encoding="utf-8"))
        task_id = task["id"]
        task_meta = catalog.get(task_id, {})
        task["_bench_suite"] = suite or "canonical"
        task["_bench_task_version"] = task_meta.get("task_version", _task_version(task))
        task["_bench_risk_types"] = task_meta.get("risk_types", [])
        task["_bench_high_variance"] = bool(task_meta.get("high_variance", False))
        repo = Path(task["repo"])
        if not repo.is_absolute():
            repo = Path.cwd() / repo
        for model in models:
            for r in range(repeat):
                _all_jobs.append((task, repo, model, task_id, r + 1))

    results: list[dict] = []
    _progress_lock = threading.Lock()
    _current = [0]

    def _run_one_wrapper(job: tuple[dict, Path, str, str, int]) -> dict:
        _task, _repo, _model, _task_id, _r = job
        with _progress_lock:
            _current[0] += 1
            _n = _current[0]
            console.print(f"\n[{_n}/{total}] {_task_id} | {_model} | repeat={_r}")
        _rec = _run_one_task(_task, _repo, _model, _task_id,
                             no_skills=no_skills, source_batch=source_batch,
                             results_path=output_path,
                             hard_model=getattr(args, "hard_model", "") or "")
        # ISSUE-38：任务结束后清理 fixture 源仓库失效 worktree 注册
        # （timeout/SIGKILL 打断时 pipeline 清理不执行，注册项残留）
        _prune_fixture_worktrees(_repo)
        # _run_one_task 返回单条 record（dict）；防御历史 list[dict] 签名
        _recs = _rec if isinstance(_rec, list) else [_rec]
        for _r2 in _recs:
            if not isinstance(_r2, dict):
                continue
            _r2["model"] = _model
            _r2["repeat"] = _r
            _r2.setdefault("bench_schema_version", 1)
            _r2.setdefault("task_version", _task_version(_task))
            _r2.setdefault("source_batch", source_batch)
            _r2.setdefault("planner_model", "")
            _r2.setdefault("judge_model", "")
            _r2.setdefault("difficulty", _task.get("difficulty", "medium"))
            _r2.setdefault("failure_class", None)
            _r2.setdefault("accepted_delivery", False)
            _r2.setdefault("delivery_branch_created", False)
            _r2.setdefault("pr_created", False)
            _r2.setdefault("spec_compliance", None)
            _r2.setdefault("architecture_compliance", None)
            _r2.setdefault("total_cost_usd", 0.0)
            _r2.setdefault("elapsed_sec", 0.0)
            _r2["suite"] = _task.get("_bench_suite", "canonical")
            _r2["bench_schema_version"] = 1
            _r2["task_version"] = _task_version(_task)
            _r2["difficulty"] = _task.get("difficulty", "medium")
            _r2["risk_types"] = _task.get("_bench_risk_types", [])
            _r2["high_variance"] = bool(_task.get("_bench_high_variance", False))
            _schema_errors = validate_record(_r2)
            if _schema_errors:
                raise ValueError(f"invalid Bench record for {_task_id}: {'; '.join(_schema_errors)}")
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(_r2, ensure_ascii=False) + "\n")
        return _rec if isinstance(_rec, dict) else {}

    if _bench_parallel > 1 and len(_all_jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=_bench_parallel) as executor:
            _futures = {executor.submit(_run_one_wrapper, j): j for j in _all_jobs}
            for _fut in as_completed(_futures):
                try:
                    _res = _fut.result()
                    if _res:
                        results.append(_res)
                except Exception as _exc:
                    _job = _futures[_fut]
                    console.error(f"[bench] {_job[3]} | {_job[2]} 执行异常: {_exc}")
    else:
        for _job in _all_jobs:
            _res = _run_one_wrapper(_job)
            if _res:
                results.append(_res)

    console.print(f"\n✅ bench 完成: {len(results)} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


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

    # S12 运行前预检：对照基线同样校验实际后端 + 定价覆盖
    _no_confirm = bool(getattr(args, "yes", False)) or bool(getattr(args, "eval_all", False))
    if not _preflight_model_pricing(models, interactive=not _no_confirm):
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
        catalog_path = tasks_dir / "task_catalog.json"
        if catalog_path.exists():
            try:
                task["_bench_task_version"] = json.loads(catalog_path.read_text(encoding="utf-8")).get(task_id, {}).get("task_version", "")
            except (OSError, json.JSONDecodeError):
                pass
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
                entry["bench_schema_version"] = 1
                entry["task_version"] = _task_version(task)
                entry["suite"] = task.get("_bench_suite", "canonical")
                entry["difficulty"] = task.get("difficulty", "medium")
                _schema_errors = validate_record(entry)
                if _schema_errors:
                    raise ValueError(f"invalid baseline record for {task_id}: {'; '.join(_schema_errors)}")
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    console.print(f"\n✅ baseline 完成: {len(task_files) * len(models) * repeat} 条结果 → {output_path}")
    console.print(f"   下一步: agent_go eval models --results {output_path}")


def _run_one_task(task: dict, repo: Path, model: str, task_id: str,
                  preserve: bool = False, no_skills: bool = False,
                  source_batch: str = "", results_path: Optional[Path] = None,
                  hard_model: str = "") -> list[dict]:
    """跑一次任务 → 读产物 → 返回每子任务的结构化结果列表。

    hard_model: CR-建议#5——hard 难度子任务使用的更强模型（留空 = 与候选 model 相同）。

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
        config: dict[str, Any] = {
            "plan_api": plan_api,
            # CR-建议#5：hard 难度可用更强模型（--hard-model），easy/medium 用候选模型
            "worker_models": {"easy": model, "medium": model,
                               "hard": hard_model or model},
            # 任务级难度下限：优先按任务 yaml 的 difficulty 标注子任务（"优先输入标注，
            # 无输入自行判定"）。hard 任务确保子任务 ≥ hard，触发混合路由 hard→强模型。
            "min_difficulty": str(task.get("difficulty", "") or ""),
            # 任务级验证命令（端到端模式 e2e 的 subtask.verification 来源）：
            # difficulty=hard 触发端到端时，单子任务用任务级 verification 验收。
            "task_verification": list(task.get("verification", []) or []),
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
            # 混合模式（--hard-model）：evaluator 也用强模型（hard_model→云端），
            # 避免"强 worker 产出 + 弱 evaluator 误判"错配（本地 35B 评估把含核心
            # 代码的完整 diff 误判为"仅测试文件"，confidence 0.3 仍判 failed）。
            if hard_model:
                evaluator_cfg["model"] = hard_model
            config["evaluator"] = evaluator_cfg
        # 继承用户的 skills / agent_loop / verification / goal 配置（skill 自动发现等），
        # 否则 bench 默认关闭
        for _k in ("skills", "agent_loop", "verification", "goal"):
            if user_config.get(_k):
                config[_k] = dict(user_config[_k])
        # S10 成本控制：透传任务 YAML 的 cost_control（每任务开关，默认 enabled=false）
        if task.get("cost_control"):
            config["cost_control"] = dict(task["cost_control"])
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
             "--no-cache",        # 质量校验：禁用 plan cache，确保 planner metering 完整采集（planner_model 字段）
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
    return _collect_result(task_id, model, elapsed, exit_code, stderr_tail, _new_dirs, exact_td=_resolved_td, expected_task=_expected, timed_out=_timed_out, source_batch=source_batch)  # type: ignore[return-value]


def _prune_fixture_worktrees(repo: Path) -> None:
    """清理 fixture 源仓库的失效 worktree 注册（ISSUE-38）。

    bench 直接对 fixture 源仓库（含 .git）跑 `agent_go run`，executor 的
    `git worktree add` 把 worktree 注册到 fixture 源仓库 `.git/worktrees/`。
    正常收尾 pipeline 会 prune，但被 timeout/SIGKILL 打断的 bench 任务会残留
    注册项（路径指向 ~/.agent_go/task-*/sub-*/work，目录已删除或清理）。
    `git worktree prune` 会清除指向不存在目录的注册——廉价且安全，任务完成后调用。
    """
    try:
        from .git_utils import _worktree_prune
        ok, err = _worktree_prune(repo)
        if not ok:
            logging.getLogger(__name__).warning(f"[bench] fixture worktree prune 失败 ({repo}): {err}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"[bench] fixture worktree prune 异常 ({repo}): {e}")


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
# S12-P2 G6：动态 timeout 改为按难度（此前按子任务数——耗时由难度驱动而非子任务数）。
# mult 复用 retry_timeout 的难度倍数表（与执行侧口径一致，避免测量/执行两套逻辑分叉）。
_DIFFICULTY_TIMEOUT_BASE_SEC = 150   # 每难度基准耗时（秒）
_DYNAMIC_TIMEOUT_BUFFER_SEC = 120    # 收尾/波动缓冲（秒）
_DIFFICULTY_MULT = {"easy": 1, "medium": 1.5, "hard": 2.5}  # 与 executor retry_timeout 倍数表一致


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


def _measure_elapsed_p95(task_id: str, results_path: Optional[Path]) -> Optional[float]:
    """CR-timeout 模型：从 results 读取该任务的实测耗时，返回 P95（近似最慢典型值）。

    数据驱动 timeout——有效值 = P95 × 余量。样本 <3 时 P95 不可靠，返回 None
    （调用方回退难度公式）。
    """
    if not results_path or not results_path.exists():
        return None
    elapsed = []
    for line in results_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if r.get("task_id") == task_id and r.get("elapsed_sec"):
            try:
                elapsed.append(float(r["elapsed_sec"]))
            except (TypeError, ValueError):
                continue
    if len(elapsed) < 3:
        return None
    elapsed.sort()
    idx = min(len(elapsed) - 1, int(0.95 * len(elapsed)))
    return elapsed[idx]


def _dynamic_timeout(task: dict, task_id: str, results_path: Optional[Path] = None) -> int:
    """按难度 + 实测耗时动态计算任务 timeout，解决多子任务/高难度任务被 timeout 截断的问题。

    S12-P2 G6：耗时由难度驱动，不再按子任务数（控制变量指错方向）。
    mult 复用 retry_timeout 难度倍数表 {easy:1, med:1.5, hard:2.5}。

    优先级（取最大）：
      1. 任务 YAML 显式声明 `timeout` → 作为下限（不缩短既有配置）
      2. 难度基准 × mult + 缓冲 → 动态下限
      3. 多子任务 hard 任务：max(子任务数 × 基准 + 缓冲, 难度动态值)
      4. 实测 P95 × 余量（CR-timeout 模型：数据驱动，随批次收敛到任务真实耗时）
         余量 = 1.5（high_variance） / 1.3（默认）

    公式：timeout = max(YAML, 难度公式, 实测P95 × 余量)
    """
    cfg_timeout = int(task.get("timeout", 1800))
    difficulty = task.get("difficulty", "medium")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    mult = _DIFFICULTY_MULT.get(difficulty, 1.5)
    dynamic = _DIFFICULTY_TIMEOUT_BASE_SEC * mult + _DYNAMIC_TIMEOUT_BUFFER_SEC
    # 多子任务 hard 任务兼容：历史子任务数多时按子任务数上浮（取大者）
    n = _estimate_subtasks_from_history(task_id, results_path)
    if n > 0:
        per_sub = _DIFFICULTY_TIMEOUT_BASE_SEC * n + _DYNAMIC_TIMEOUT_BUFFER_SEC
        dynamic = max(dynamic, per_sub)
    # 实测 P95 × 余量（数据驱动；无历史回退难度公式）
    _p95 = _measure_elapsed_p95(task_id, results_path)
    if _p95:
        _margin = 1.5 if task.get("high_variance") else 1.3
        dynamic = max(dynamic, int(_p95 * _margin))
    return int(max(cfg_timeout, dynamic))


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

    # cost 聚合：本地模型事件（is_local=True 且 cost=0）按 local_model_cost TCO 折算，
    # 使 metric-freeze/gate 的本地基线 $/pass 含真实 TCO（电费+折旧），不视为免费。
    total_cost = sum(ev.get("cost_usd", 0) or 0 for ev in metering)
    _tco: Optional[Callable[[str], float]] = None
    try:
        from .metrics import local_tco_usd as _tco  # type: ignore[assignment]
    except Exception:
        _tco = None
    if _tco is not None:
        for ev in metering:
            if ev.get("is_local") and not (ev.get("cost_usd") or 0):
                _tco_amt = _tco(ev.get("actual_model", "") or "")
                if _tco_amt > 0:
                    total_cost += _tco_amt
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
    # CR-#1：no_changes（成功态：任务本不需改动、验证通过）计为通过，不再拉低 pass_rate。
    completed = sum(1 for r in results if r.get("status") in ("completed", "no_changes"))
    failed = sum(1 for r in results if r.get("status") == "failed")
    retry_total = sum(r.get("retry_count", 0) for r in results)
    # S12-P0：修 all([]) 空真值陷阱——「零个 completed」时 all() 返回 True 会把全失败
    # 误判为通过。要求至少一个 completed 才可能 all_passed。
    _completed_results = [r for r in results if r.get("status") in ("completed", "no_changes")]
    all_passed = bool(_completed_results) and all(r.get("verify_ok", False) for r in _completed_results)

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

    # binary_pass 在下方 aborted 修正之后计算（S12-P0：修时序错位——
    # 原先在 aborted 分支之前冻结，导致 completed=0 但 binary_pass=True 的矛盾）。

    # per_subtask：每个子任务的简明细（供按子任务失败模式分析）
    per_subtask = [
        {
            "sub_id": _r.get("subtask_id") or _r.get("id") or "",
            "status": _r.get("status", ""),
            "retries": _r.get("retry_count", 0),
            "verify_ok": bool(_r.get("verify_ok", False)),
            "semantic_ok": _subtask_semantic_ok(_r),
            # S12-P0 G1：子任务级 kill_reason（运行时写入，度量侧归因）
            "kill_reason": _r.get("kill_reason"),
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
    _cleanup_race = False  # 收尾竞态：子任务全完成已验证，仅进程被杀
    if _aborted:
        # 区分「收尾被杀（已完工）」与「中途被杀（未完工）」
        _planned_ids = {st.get("id") for st in (meta.get("subtasks") or [])}
        _result_ids = {r.get("subtask_id") or r.get("id") for r in results if r.get("subtask_id") or r.get("id")}
        _all_resulted = bool(_planned_ids) and _planned_ids.issubset(_result_ids)
        # _all_results_done：所有已落盘 result 都 completed/no_changes + verify_ok
        # （不要求覆盖计划全集）。CR-#1：no_changes 计为完成。
        _all_results_done = bool(results) and all(
            r.get("status") in ("completed", "no_changes") and r.get("verify_ok") is True
            for r in results
        )
        if _all_results_done and timed_out:
            # cleanup_race（S12-P0）：子任务全完成已验证，进程在收尾阶段被杀 → 计为通过。
            # 不再依赖 _all_resulted（计划 id 覆盖）——SIGKILL 常导致 meta.subtasks 未完整
            # 落盘，旧逻辑因此把已完工任务误判失败（v3 65 条假失败 / 通过率被腰斩的根因）。
            _cleanup_race = True
            completed = len(results)
            failed = 0
            all_passed = True
            console.debug(f"[collect] {task_id} cleanup_race：全部子任务完成已验证，收尾被杀计为通过 (exit={exit_code}, status={meta_status})")
        elif _all_results_done and _all_resulted:
            # 已完工（非超时收尾被杀）：保持原 completed/all_passed 判定
            console.debug(f"[collect] {task_id} aborted但全部子任务完成，视为完工 (exit={exit_code}, status={meta_status})")
        else:
            # 中途被杀（未完工）：所有子任务不计通过
            completed = 0
            failed = sum(1 for r in results
                         if r.get("status") in ("failed", "blocked"))
            all_passed = False

    # binary_pass 在 aborted 修正之后计算（S12-P0 修时序错位）；
    # all_passed 已要求至少一个 completed，故 all([]) 陷阱不再成立。
    binary_pass = all_passed and (semantic_pass is not False)

    # kill_reason 分类（S12-P0）：把"进程被杀"与"任务失败"解耦——
    # cleanup_race 计通过、预算熔断单列、infra 不计能力失败。
    # 优先采用运行时写入的子任务级 kill_reason（G1：subtask/executor/pipeline 决策点写），
    # 无运行时记录时按数据反推（方案 B 兜底）。
    _runtime_kill = [r.get("kill_reason") for r in results if r.get("kill_reason")]
    if _runtime_kill:
        # 任务级取子任务 kill_reason 中最"严重"的一个。
        # system_error 最严重（内部 bug 崩溃，非能力失败）→ 优先标识，供审计/告警。
        if "system_error" in _runtime_kill:
            kill_reason = "system_error"
        elif any(k in ("over_budget_l2", "over_budget_l3") for k in _runtime_kill):
            kill_reason = "over_budget_l2" if "over_budget_l2" in _runtime_kill else "over_budget_l3"
        elif any(k in ("stuck", "hard_timeout", "goal_timeout", "goal_turns_exceeded") for k in _runtime_kill):
            kill_reason = next(k for k in ("stuck", "hard_timeout", "goal_timeout", "goal_turns_exceeded")
                               if k in _runtime_kill)
        elif _cleanup_race:
            kill_reason = "cleanup_race"
        elif all_passed:
            kill_reason = "none"
        else:
            kill_reason = "interrupted_or_unknown"
    elif _cleanup_race:
        kill_reason = "cleanup_race"
    elif all_passed:
        kill_reason = "none"
    elif timed_out:
        kill_reason = "stuck_or_hardtimeout"
    elif (total_cost or 0) == 0:
        kill_reason = "infra"
    else:
        kill_reason = "interrupted_or_unknown"

    from .delivery import evaluate_accepted_delivery
    from .failure import aggregate_failure_class, classify_failure
    delivery = evaluate_accepted_delivery(meta, meta.get("repo") or None)
    failure_class = aggregate_failure_class(
        [classify_failure(r, meta, timed_out=timed_out) for r in results],
        {**meta, "delivery_failed": delivery["delivery_failed"]},
    )
    if not failure_class and timed_out and not _cleanup_race:
        failure_class = "timeout"
    if not failure_class and exit_code != 0 and not results:
        failure_class = "infrastructure_failure" if not total_cost else "system_error"

    return {
        "bench_schema_version": 1,
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
        "task_version": str(meta.get("task_version") or "unversioned"),
        "suite": str(meta.get("suite") or "canonical"),
        "repeat": int(meta.get("repeat") or 1),
        "difficulty": str(meta.get("difficulty") or "medium"),
        # S10-P2：P1 字段
        "semantic_pass": semantic_pass,
        "binary_pass": binary_pass,
        "kill_reason": kill_reason,
        "per_subtask": per_subtask,
        "plan_step_count": plan_step_count,
        "plan_quality_status": meta.get("plan_quality_status"),
        "plan_requirement_coverage": meta.get("plan_requirement_coverage"),
        "plan_acceptance_coverage": meta.get("plan_acceptance_coverage"),
        "plan_conflict_count": meta.get("plan_conflict_count", 0),
        "plan_warning_count": meta.get("plan_warning_count", 0),
        # S10-P2：代码质量维度（§4.1，从保留 worktree 聚合）
        "lint_errors": quality["lint_errors"],
        "tests_broken": quality["tests_broken"],
        # 产品交付维度：不参与旧 pass_rate 计算，仅用于 Accepted Delivery 分析。
        "delivery_branch_created": bool(meta.get("delivery_branch")),
        "pr_created": bool(meta.get("pr_url")),
        "accepted_delivery": delivery["accepted_delivery"],
        "delivery_failed": delivery["delivery_failed"],
        "accepted_delivery_reasons": delivery["accepted_delivery_reasons"],
        "spec_compliance": meta.get("spec_compliance"),
        "architecture_compliance": meta.get("architecture_compliance"),
        "failure_class": failure_class,
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
    from .metrics import compute_frozen_metrics

    ordinary_results = [
        r for r in results
        if r.get("suite", "canonical") != "stress" and not r.get("high_variance", False)
    ]
    stress_results = [
        r for r in results
        if r.get("suite") == "stress" or r.get("high_variance", False)
    ]
    by_model: dict[str, list[dict]] = {}

    for r in ordinary_results:
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

        # S12-P0 修正口径：对历史记录（per_subtask 已含真实子任务状态），用 per_subtask
        # 重算"修正通过率"——cleanup_race（全部子任务完成已验证但收尾被杀）计为通过，
        # 使旧采集器的 pass_rate 与修正后 _collect_result 口径一致（v3 34%→~67%）。
        # 新采集器产出的记录 pass_rate 已含 cleanup_race 修正，重算结果与之等价。
        # 任务级判定：headline 通过 OR 全部子任务 completed+verify_ok（cleanup_race）。
        def _corrected_pass(it: dict) -> float:
            if (it.get("pass_rate") or 0) > 0:
                return 1.0
            _ps = it.get("per_subtask") or []
            if _ps and all(p.get("status") == "completed" and p.get("verify_ok") for p in _ps):
                return 1.0  # cleanup_race：全完成已验证，计为通过
            return 0.0

        corrected_pass_rates = [_corrected_pass(it) for it in items]
        avg_corrected_pass_rate = round(sum(corrected_pass_rates) / n, 4) if n else 0
        _sum_corr_pass = sum(corrected_pass_rates)

        # S10-P1 统一口径（bench-v2-data-requirements.md §3.1）：
        #   $/pass = sum(total_cost_usd) / sum(pass_rate)
        # 以 raw cost 和 raw pass_rate 为准，不依赖 record 级 dollar_per_pass 字段（跨批次可比）。
        _sum_cost = sum(it.get("total_cost_usd", 0) or 0 for it in items)
        _sum_pass = sum(pass_rates)
        dollar_per_pass = round(_sum_cost / _sum_pass, 6) if _sum_pass > 0 else None

        # CR-P1-2：任务级 $/pass（PRD 分母缺陷修正）——分母 = 任务级成功计数
        # （全部子任务通过才算 1 个交付；cleanup_race 计通过、over_budget/infra 不计）。
        # 现有 sum(pass_rate) 分母把部分通过也计入，系统性低估真实每交付成本（K4 偏乐观）。
        _sum_delivered = sum(1 for it in items if _task_delivered(it))
        legacy_task_level_dollar_per_pass = (
            round(_sum_cost / _sum_delivered, 6) if _sum_delivered > 0 else None
        )

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

        efficiency_score = _model_efficiency_score(avg_pass_rate, avg_cost)
        cost_per_pass = _model_cost_per_pass(avg_cost, avg_pass_rate)
        _delivery_metrics = compute_frozen_metrics(items)
        # New schema records use the frozen Accepted Delivery denominator.
        # Historical exploratory records lack accepted_delivery and retain the
        # old all-subtasks fallback for backward-compatible analysis only.
        _has_delivery_field = any("accepted_delivery" in it for it in items)
        task_level_dollar_per_pass = (
            _delivery_metrics["cost_per_accepted_delivery_usd"]
            if _has_delivery_field
            else legacy_task_level_dollar_per_pass
        )
        models[model] = {
            "sample_size": n,
            "avg_pass_rate": avg_pass_rate,
            "avg_corrected_pass_rate": avg_corrected_pass_rate,  # S12-P0 修正口径（cleanup_race 计入）
            "avg_cost_usd": avg_cost,
            "dollar_per_pass": dollar_per_pass,
            "task_level_dollar_per_pass": task_level_dollar_per_pass,  # CR-P1-2 任务级口径（all-or-nothing）
            "task_level_dollar_per_pass_legacy": legacy_task_level_dollar_per_pass,
            "dollar_per_pass_legacy": cost_per_pass,
            "k8_zero_retry_pass_rate": k8,
            "efficiency_score": efficiency_score,
            "avg_lint_errors": avg_lint,
            "avg_tests_broken": avg_tests_broken,
            "code_regression_rate": code_regression_rate,
            "completed_subtasks": completed_total,
            "total_subtasks": subtask_total,
        }
        models[model].update({
            "accepted_delivery_rate": _delivery_metrics["accepted_delivery_rate"],
            "pr_creation_rate": _delivery_metrics["pr_creation_rate"],
            "delivery_failure_rate": _delivery_metrics["delivery_failure_rate"],
            "cost_per_accepted_delivery_usd": _delivery_metrics["cost_per_accepted_delivery_usd"],
            "valid_task_count": _delivery_metrics["valid_task_count"],
            "failure_class_summary": _delivery_metrics["failure_class_summary"],
        })

        # 从 agent_go 任务目录读取评估事件计算假阳性率
        fp_data = _compute_fp_for_model(model, AGENT_GO_DIR)
        if fp_data:
            models[model]["false_positive_rate"] = fp_data["fp_rate"]
            models[model]["avg_confidence"] = fp_data["avg_confidence"]

    # CR-G1 成本感知推荐（两遍）：先算跨模型 $/pass 中位数 + best_value，再带成本调 _recommend。
    # 此前 _recommend 纯通过率门控，贵 5× 的模型与便宜的拿到相同 recommended。
    _dpp_values = [m["dollar_per_pass"] for m in models.values() if m["dollar_per_pass"]]
    _dpp_median = _median(_dpp_values)
    # best_value：≥70% 通过者中 efficiency_score（passes/$）最高者，回答"性价比最优选哪个"
    _bv_candidates = [(mdl, m) for mdl, m in models.items()
                      if m["avg_pass_rate"] >= 0.70 and (m["efficiency_score"] or 0) > 0]
    _best_value_model = (max(_bv_candidates, key=lambda kv: kv[1]["efficiency_score"])[0]
                         if _bv_candidates else None)
    for _mdl, _m in models.items():
        _rec, _roles, _reason = _recommend(
            _mdl, _m["avg_pass_rate"], _m["avg_cost_usd"], _m["sample_size"],
            dollar_per_pass=_m["dollar_per_pass"], dpp_median=_dpp_median)
        # CR-P1-1：小样本 low_confidence（PRD 铁律：样本<5 不决策，不参与自动路由）。
        # 不改 recommendation 类别（pass 仍反映能力），仅标 flag + reason 注记，
        # 并在 _recommend_worker_models（cmd_recommend）排除，避免小样本噪声进 config。
        _low_n = _m["sample_size"] < 5
        _m["low_confidence"] = _low_n
        if _low_n and _rec != "insufficient_data":
            _reason += "（⚠ 小样本 n<5，low_confidence，不参与自动路由）"
        _m["recommendation"] = _rec
        _m["recommended_roles"] = _roles
        _m["reason"] = _reason
        _m["best_value"] = (_mdl == _best_value_model)

    return {
        "models": models,
        "total_runs": len(results),
        "ordinary_runs": len(ordinary_results),
        "stress_runs": len(stress_results),
        "frozen_metrics": compute_frozen_metrics(ordinary_results),
        "stress_metrics": compute_frozen_metrics(stress_results),
    }


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


def _task_delivered(it: dict) -> bool:
    """CR-P1-2：任务级"交付"判定（PRD 分母缺陷修正）——全部子任务通过才算 1 个交付。

    与 _corrected_pass 的区别：_corrected_pass 把"任何部分通过"(pass_rate>0) 记为通过，
    偏乐观；此处要求 **全部** 子任务 completed+verify_ok（all-or-nothing）才算交付。
    kill_reason 过滤已隐含：cleanup_race（全完成已验证）→ 交付；over_budget（有 blocked/
    failed 子任务）→ 不交付；infra（子任务未全完成）→ 不交付。
    """
    _pr = it.get("pass_rate")
    if _pr is not None and _pr >= 1.0:
        return True  # headline 全部通过（新采集器 pass_rate 已含 cleanup_race 修正）
    _ps = it.get("per_subtask") or []
    if _ps:
        return all(p.get("status") == "completed" and p.get("verify_ok") for p in _ps)
    return False

def _median(values: list[float]) -> Optional[float]:
    """中位数（None-safe）。用于跨模型 $/pass 成本基线。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 6)


def _recommend(model: str, pass_rate: float, avg_cost: float, n: int,
               dollar_per_pass: Optional[float] = None,
               dpp_median: Optional[float] = None) -> tuple[str, list[str], str]:
    """决策规则（PRD §3.7 对齐：60/70/75/80 四档阈值）+ 成本维度（CR-G1）。

    CR-G1 成本降档：$/pass > 2× 同批中位数时，recommended 降为 conditional——
    贵模型不默认拿 ★（与便宜的同通过率模型区分），但不否定其能力（roles 不变）。
    仅 recommended 受降档影响：conditional/discouraged 不再往下压（成本不该把
    能力达标的模型压成"垃圾"）。
    """
    if n < 3:
        return ("insufficient_data", [], f"仅 {n} 样本，需 ≥3 才可决策")
    if pass_rate < 0.60:
        return ("discouraged", [], f"通过率 {pass_rate:.0%} <60%，省钱产出垃圾（PRD 反指标）")
    if pass_rate >= 0.80:
        cat, roles, reason = ("recommended", ["worker_easy", "worker_medium", "worker_hard"],
                              f"通过率 {pass_rate:.0%} ≥80%，全角色可用")
    elif pass_rate >= 0.75:
        cat, roles, reason = ("conditional", ["worker_easy", "worker_medium", "worker_hard"],
                              f"通过率 {pass_rate:.0%} ≥75%，全角色可用（注意 hard 任务表现）")
    elif pass_rate >= 0.70:
        cat, roles, reason = ("conditional", ["worker_easy", "worker_medium"],
                              f"通过率 {pass_rate:.0%} ≥70%，easy/medium 可用")
    else:
        cat, roles, reason = ("conditional", ["worker_easy"],
                              f"通过率 {pass_rate:.0%} ≥60%，仅 easy 可用")
    if (cat == "recommended" and dollar_per_pass and dpp_median
            and dollar_per_pass > 2 * dpp_median):
        cat = "conditional"
        reason += f"；成本过高（$/pass ${dollar_per_pass:.4f} > 2× 批次中位数 ${dpp_median:.4f}）"
    return (cat, roles, reason)


def cmd_models(args=None) -> None:
    """打印模型生产力决策矩阵。"""
    results_path = Path(getattr(args, "results", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")
    data = analyze_model_productivity(results_path)

    if "error" in data:
        console.warning(f"{data['error']} → 先跑 agent_go eval bench")
        return

    console.print(f"\n📊 模型生产力评估（{data['total_runs']} 次执行）")
    console.print("─" * 118)
    console.print(f"{'模型':<22} {'样本':>4} {'档':>3} {'通过率':>7} {'Accepted':>8} {'PR':>6} {'交付失败':>8} {'交付$':>8} {'建议':<12} {'原因'}")
    console.print("─" * 118)

    for model, m in data["models"].items():
        icon = {"recommended": "★", "conditional": "⚠", "discouraged": "✗", "insufficient_data": "?"}[m["recommendation"]]
        bv = "💰" if m.get("best_value") else "  "  # CR-G1：性价比最优标记
        tier_str = {"frontier": "F", "value": "V", "lite": "L"}.get(model_tier(str(model)) or "", "-")  # CR-G2：tier 展示
        capd = m.get("cost_per_accepted_delivery_usd")
        capd_str = f"${capd:>7.4f}" if capd is not None else "      -"
        adr = m.get("accepted_delivery_rate")
        adr_str = f"{adr:>7.0%}" if adr is not None else "      -"
        prr = m.get("pr_creation_rate")
        prr_str = f"{prr:>5.0%}" if prr is not None else "     -"
        dfr = m.get("delivery_failure_rate")
        dfr_str = f"{dfr:>7.0%}" if dfr is not None else "      -"
        console.print(f"{icon}{bv}{model:<19} {m['sample_size']:>4} {tier_str:>3} {m['avg_pass_rate']:>6.0%} "
                      f"{adr_str} {prr_str} {dfr_str} {capd_str} "
                      f"{m['recommendation']:<12} {m['reason']}")
    console.print("─" * 118)
    console.print("效率 = passes/dollar (每美元获得的通过率，越高越经济)")
    console.print("K8   = 通过 record 中 zero-retry 占比（首次验证通过率，§3.4 修订口径）")
    console.print("Accepted = accepted_delivery_count / valid_task_count；交付$ = Cost per Accepted Delivery")
    console.print("$/pass = 历史诊断指标，仅限同 suite + source_batch 比较")
    console.print("修正   = S12-P0 修正口径通过率（cleanup_race 计通过，历史数据按 per_subtask 重算）")
    console.print("💰    = CR-G1 性价比最优（≥70% 通过者中 efficiency_score 最高；贵模型 $/pass>2× 中位数时 ★ 降 ⚠）")
    console.print("档    = CR-G2 模型分级（F=frontier 旗舰 / V=value 主力 / L=lite 轻量 / -=未分级自定义）")
    console.print("交付$ = CR-P1-2 任务级口径（全部子任务通过才算 1 个交付；cleanup_race 计通过、over_budget/infra 不计）——K4 北极星口径")


# ═══════════════════════════════════════════════════════════════
# CR-G5：bench 推荐 → worker_models 自动衔接（dry-run / --apply）
# ═══════════════════════════════════════════════════════════════

def _recommend_worker_models(models: dict[str, Any]) -> dict[str, Optional[dict]]:
    """按 recommended_roles + 通过率/$/pass 把模型分配到 easy/medium/hard 槽。

    分配规则（确定性、可审计）：
      - hard：recommended_roles 含 worker_hard 者，取通过率最高（tie → best_value → min $/pass）。
      - medium / easy：含对应 role 者，取 $/pass 最低（easy/medium 槽优先省钱；tie → max 通过率）。
      - 无合格候选 → 该槽 None（留空，不退而塞弱模型）。
    依赖 G1（成本感知推荐）+ G2（tier 校验）先落地，推荐才可信。
    """
    items = list(models.items())  # (name, metrics)

    def _pick(role: str, criterion: str) -> Optional[dict]:
        # CR-P1-1：排除 low_confidence（小样本 n<5），避免噪声进 worker_models 配置
        cands = [(n, m) for n, m in items
                 if role in (m.get("recommended_roles") or [])
                 and not m.get("low_confidence")]
        if not cands:
            return None
        if role == "worker_hard":
            # 能力优先：max 通过率 → best_value → min $/pass
            cands.sort(key=lambda nm: (-nm[1]["avg_pass_rate"],
                                       0 if nm[1].get("best_value") else 1,
                                       nm[1].get("dollar_per_pass") or 9))
        else:
            # 省钱优先：min $/pass → max 通过率
            cands.sort(key=lambda nm: ((nm[1].get("dollar_per_pass") or 9),
                                       -nm[1]["avg_pass_rate"]))
        n, m = cands[0]
        return {"model": n, "criterion": criterion,
                "avg_pass_rate": m["avg_pass_rate"],
                "dollar_per_pass": m.get("dollar_per_pass"),
                "recommendation": m["recommendation"],
                "best_value": bool(m.get("best_value"))}

    return {
        "hard": _pick("worker_hard", "通过率最高"),
        "medium": _pick("worker_medium", "$/pass 最低"),
        "easy": _pick("worker_easy", "$/pass 最低"),
    }


def _recommend_roles(models: dict[str, Any]) -> dict[str, Any]:
    """CR-G5 / P1：基于模型生产力指标推荐完整角色路由（planner/worker/reviewer + fallback）。

    与 _recommend_worker_models（只推荐难度槽模型名）不同，本函数产出 config 可直接
    写入的 router.roles 结构（provider + model + fallback），并内建两项 PRD 铁律：
      - planner 不降级：不配 fallback（规划 token 是全局成本前置变量，省小钱 Worker 膨胀数倍）
      - reviewer 不同源：reviewer 与 worker 不得同 provider（视角低相关，防自评偏差）

    角色选择规则（确定性、可审计）：
      - planner：通过率最高者（能力优先；不可能是性价比理由降级）→ 无 fallback
      - worker ：性价比最优者（best_value 或 dollar_per_pass 最低；能力 ≥70%）→
                 fallback 取次优候选（不同 provider，可用性降级）
      - reviewer：与 worker 不同 provider 的通过率最高者 → 无 fallback（低频角色不降级）
    低置信（sample_size<5）仍可展示但标注，不自动写入（与 CR-P1-1 一致）。

    Args:
        models: analyze_model_productivity 输出的 {"模型名": metrics, ...}。

    Returns:
        {"planner": {"provider","model","reason",...}|None, "worker": ..., "reviewer": ...}
    """
    from .pricing import infer_provider

    # 候选池：通过率达标 + 可用（含低置信，标注）
    cands = [(n, m) for n, m in models.items()
             if (m.get("avg_pass_rate") or 0) >= 0.60 and m.get("sample_size", 0) >= 3]
    if not cands:
        return {"planner": None, "worker": None, "reviewer": None,
                "note": "无通过率≥60% 且样本≥3 的模型，无法推荐"}

    def _provider(n: str) -> str:
        return infer_provider(n) or "unknown"

    def _desc(n: str, m: dict) -> dict:
        return {
            "model": n,
            "provider": _provider(n),
            "avg_pass_rate": m["avg_pass_rate"],
            "dollar_per_pass": m.get("dollar_per_pass"),
            "recommendation": m["recommendation"],
            "best_value": bool(m.get("best_value")),
            "low_confidence": m.get("low_confidence", False),
        }

    _cands_desc = [(n, m, _desc(n, m)) for n, m in cands]

    # ── planner：通过率最高（能力优先），无 fallback ──
    _planner = max(_cands_desc, key=lambda t: t[1]["avg_pass_rate"])
    planner = _planner[2]
    planner["reason"] = f"通过率最高 {planner['avg_pass_rate']:.0%}（Planner 铁律：不配置 fallback）"

    # ── worker：性价比优先（≥70% 能力门槛） ──
    _worker_cands = [t for t in _cands_desc if t[1]["avg_pass_rate"] >= 0.70]
    if _worker_cands:
        _worker = min(_worker_cands, key=lambda t: (
            0 if t[2]["best_value"] else 1, t[2]["dollar_per_pass"] if t[2]["dollar_per_pass"] is not None else 999))
    else:
        _worker = max(_cands_desc, key=lambda t: t[1]["avg_pass_rate"])
    worker = dict(_worker[2])
    worker["reason"] = f"最佳性价比（通过率 {worker['avg_pass_rate']:.0%}，$/pass ${worker['dollar_per_pass'] or 0:.4f}）"

    # worker fallback：不同 provider 的次优候选（可用性降级）
    _fb_cands = [t for t in _cands_desc
                 if t[0] != worker["model"] and _provider(t[0]) != worker["provider"]]
    if _fb_cands:
        _fb = max(_fb_cands, key=lambda t: t[1]["avg_pass_rate"])
        worker["fallback"] = {"provider": _fb[2]["provider"], "model": _fb[0]}

    # ── reviewer：与 worker 不同 provider 的通过率最高者（不同源铁律） ──
    _rev_cands = [t for t in _cands_desc
                  if t[0] != worker["model"] and _provider(t[0]) != worker["provider"]]
    if _rev_cands:
        _rev = max(_rev_cands, key=lambda t: t[1]["avg_pass_rate"])
        reviewer = dict(_rev[2])
        reviewer["reason"] = (f"通过率最高 {reviewer['avg_pass_rate']:.0%} 且与 worker "
                              f"({worker['provider']}) 不同源（Reviewer 铁律）")
    else:
        reviewer = None

    return {"planner": planner, "worker": worker, "reviewer": reviewer}


def identify_deterministic_issues(models: dict[str, Any], results_records: list[dict],
                                  budget_per_pass: float = 0.10) -> list[dict[str, Any]]:
    """规则初筛：识别 4 类确定性问题候选（Q4 收敛 649c36e），供 LLM 精排。

    1. $/pass 超预算：模型 dollar_per_pass > budget_per_pass
    2. failure_class 集中：某 failure_class 占失败记录 >50%
    3. 环境漂移：metering actual_model 与声明模型不一致（实际后端变化）
    4. problems 复发：problems.py 中 opened/复发问题（跨任务失败记忆）

    返回 [{"type": ..., "severity": high|medium, "detail": ..., "evidence": [...]}]
    """
    issues: list[dict[str, Any]] = []

    # 1. $/pass 超预算
    for name, m in models.items():
        dpp = m.get("dollar_per_pass")
        if dpp is not None and dpp > budget_per_pass:
            issues.append({
                "type": "cost_over_budget", "severity": "high",
                "detail": f"模型 {name} 的 $/pass=${dpp:.4f} 超过预算 ${budget_per_pass:.4f}",
                "evidence": [f"models/{name}/dollar_per_pass"],
                "model": name, "value": dpp,
            })

    # 2. failure_class 集中（records 中某 failure_class 占比 >50%）
    fails = [r for r in results_records if not (r.get("accepted_delivery") or r.get("binary_pass"))]
    if fails:
        from collections import Counter
        fc = Counter(r.get("failure_class", "unknown") for r in fails)
        top_class, top_count = fc.most_common(1)[0]
        ratio = top_count / len(fails)
        if ratio > 0.5:
            issues.append({
                "type": "failure_class_concentration", "severity": "medium",
                "detail": f"失败集中于 {top_class}（{top_count}/{len(fails)} = {ratio:.0%}），提示系统性问题",
                "evidence": [f"failure_class/{top_class}"],
                "failure_class": top_class, "ratio": round(ratio, 3),
            })

    # 3. 环境漂移：metering actual_model 与声明 routed_model 不一致
    drift = []
    for r in results_records:
        actual = r.get("actual_model")
        routed = r.get("routed_model")
        if actual and routed and actual != routed:
            drift.append((routed, actual))
    if drift:
        from collections import Counter
        drift_count = Counter(drift)
        top = drift_count.most_common(1)[0]
        issues.append({
            "type": "environment_drift", "severity": "high",
            "detail": f"路由模型与实际后端不一致：{top[0][0]} → {top[0][1]}（{top[1]} 次），"
                      "疑似代理/环境漂移（如后端被改、key 失效回退）",
            "evidence": [f"environment_drift/{top[0][0]}->{top[0][1]}"],
            "routed_model": top[0][0], "actual_model": top[0][1], "count": top[1],
        })

    # 4. problems 复发（opened/analyzed 状态的跨任务失败记忆）
    try:
        from .problems import load as load_problems
        from .config import AGENT_GO_DIR
        probs = load_problems(AGENT_GO_DIR / "problems.jsonl")
        recurrent = [p for p in probs if p.status in ("opened", "analyzed") and p.occurrence_count > 1]
        if recurrent:
            top_p = max(recurrent, key=lambda p: p.occurrence_count)
            issues.append({
                "type": "problem_recurrence", "severity": "medium",
                "detail": f"问题复发：{top_p.failure_pattern}（复发 {top_p.occurrence_count} 次，根因 {top_p.root_cause}）",
                "evidence": [f"problems/{top_p.id}"],
                "problem_id": top_p.id, "recurrence": top_p.occurrence_count,
            })
    except Exception:
        pass  # problems 不可用不影响推荐

    return issues


def build_recommendation(models: dict[str, Any]) -> dict[str, Any]:
    """统一推荐入口：从模型生产力指标一次产出 worker_models + router.roles 全套推荐。

    整合 CR-G5（eval recommend）与 P1（router recommend）：两者此前各写一份
    config（worker_models / router.roles），本函数把推荐计算收敛到一处，供两个
    CLI 入口共享展示与写入，避免重复计算与两次 config 读写的互相覆盖风险。

    Returns:
        {"worker_models": {"easy","medium","hard": {slot 详情}|None},
         "roles": {"planner","worker","reviewer": {role 详情}|None},
         "note": str|None}
    """
    _wm = _recommend_worker_models(models)
    _roles = _recommend_roles(models)
    _note = _roles.get("note")
    return {"worker_models": _wm, "roles": {k: _roles[k] for k in ("planner", "worker", "reviewer")},
            "note": _note}


def llm_rerank_recommendation(rec: dict[str, Any], issues: list[dict[str, Any]],
                              models: dict[str, Any], config: dict[str, Any],
                              logger) -> tuple[dict[str, Any], dict[str, Any]]:
    """LLM 精排（M6.4）：规则初筛候选 + 确定性问题证据 → LLM 跨维权衡精排。

    规则初筛（build_recommendation）产出确定候选；本函数把候选 + identify_deterministic_issues
    识别的 4 类问题 + 模型指标喂给 LLM，让它做跨维权衡精排并给出理由。
    输出在 rec 基础上增加 llm_ranking（精排排序+理由）字段；--apply 仍走 apply_recommendation（人工确认）。
    """
    from .api import call_api

    # 构造证据上下文
    wm = rec.get("worker_models", {})
    roles = rec.get("roles", {})
    model_summary = []
    for name, m in list(models.items())[:12]:
        model_summary.append(
            f"- {name}: pass_rate={m.get('avg_pass_rate',0):.0%}, "
            f"$/pass=${m.get('dollar_per_pass') or 0:.4f}, "
            f"recommended_roles={m.get('recommended_roles', [])}"
        )
    issue_summary = "\n".join(
        f"- [{i['severity']}] {i['type']}: {i['detail']}" for i in issues
    ) or "（无确定性问题）"

    prompt = f"""你是模型选型与配置优化顾问。基于以下**真实证据**给出精排建议。

## 模型生产力指标（bench 实测）
{chr(10).join(model_summary)}

## 规则初筛候选（确定性规则产出）
worker_models: {json.dumps({k: (v if isinstance(v, str) else (v or {}).get('model')) for k, v in wm.items()}, ensure_ascii=False)}
roles: {json.dumps({k: (v if isinstance(v, str) else (v or {}).get('model')) for k, v in roles.items() if v}, ensure_ascii=False)}

## 确定性问题（规则识别）
{issue_summary}

## 任务
对上述候选做**跨维权衡精排**，只输出合法 JSON（无 markdown、无解释）：
{{
  "worker_models": {{"easy": "...", "medium": "...", "hard": "..."}},
  "roles": {{"planner": "...", "evaluator": "..."}},
  "ranking": [{{"model": "...", "role": "...", "reason": "精排理由（引用证据）", "confidence": 0-1}}],
  "issues_addressed": ["对应解决的问题类型"],
  "cautions": ["风险提醒"]
}}

要求：
- 精排必须基于证据（模型指标 + 确定性问题），不凭空推荐
- 若初筛候选合理可沿用；若证据显示需调整（如 $/pass 超预算、failure_class 集中），给出替代模型
- reason 必须引用证据（如 models/<name>/dollar_per_pass、failure_class/<class>）
"""
    try:
        content = call_api(config, [{"role": "user", "content": prompt}], logger)
    except Exception as e:
        logger.warning(f"[llm_rerank] LLM 调用失败，回退规则候选: {e}")
        rec["llm_ranking"] = None
        rec["llm_error"] = str(e)
        return rec, {"llm_ranking": None, "llm_error": str(e), "issues_addressed": [], "cautions": []}

    # 解析 JSON（剥离 markdown 代码块）
    import re as _re
    c = content.strip()
    c = _re.sub(r"^```(?:json)?\s*|\s*```$", "", c, flags=_re.MULTILINE).strip()
    try:
        start = c.find("{"); end = c.rfind("}")
        data = json.loads(c[start:end+1])
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[llm_rerank] 响应解析失败，回退规则候选: {e}")
        rec["llm_ranking"] = None
        rec["llm_error"] = f"parse: {e}"
        return rec, {"llm_ranking": None, "llm_error": str(e), "issues_addressed": [], "cautions": []}

    # 用 LLM 精排覆盖 worker_models/roles（若 LLM 给出合法值）
    llm_wm = data.get("worker_models") or {}
    for slot in ("easy", "medium", "hard"):
        mname = llm_wm.get(slot)
        if mname and mname in models:
            existing = rec["worker_models"].get(slot)
            if isinstance(existing, str):
                if existing:
                    rec["worker_models"][slot] = mname
            elif existing:
                rec["worker_models"][slot]["model"] = mname
    llm_roles = data.get("roles") or {}
    for role in ("planner", "evaluator"):
        mname = llm_roles.get(role)
        if mname and mname in models:
            rec["roles"][role] = {"provider": "anthropic" if role == "planner" else "openai",
                                   "model": mname, "base_url": "", "api_key": ""}

    rec["llm_ranking"] = data.get("ranking", [])
    rec["issues_addressed"] = data.get("issues_addressed", [])
    rec["cautions"] = data.get("cautions", [])
    rec["issues"] = issues
    return rec, {"llm_ranking": rec["llm_ranking"], "llm_error": None,
                 "issues_addressed": rec["issues_addressed"], "cautions": rec["cautions"]}


def apply_recommendation(rec: dict[str, Any], apply_roles: bool = True,
                         apply_worker_models: bool = True) -> list[str]:
    """一次原子写：把推荐写入 config.json 的 router.roles 与/或 worker_models。

    整合 _apply_roles + _apply_worker_models 的重复读写——两者此前各自读一次
    config 再写回，若被先后调用存在互相覆盖风险；本函数单次读+单次写保证一致性。

    Args:
        rec: build_recommendation 的输出。
        apply_roles: 是否写入 router.roles（planner/worker/reviewer + fallback）。
        apply_worker_models: 是否写入 worker_models（easy/medium/hard）。

    Returns:
        skipped 角色列表（provider 无法推断或 None 而跳过）。
    """
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    _skipped: list[str] = []

    if apply_worker_models:
        _wm = rec.get("worker_models") or {}
        cfg["worker_models"] = {
            "easy": (_wm.get("easy") or {}).get("model", ""),
            "medium": (_wm.get("medium") or {}).get("model", ""),
            "hard": (_wm.get("hard") or {}).get("model", ""),
        }

    if apply_roles:
        _router = cfg.setdefault("router", {})
        _roles = _router.setdefault("roles", {})
        for _role in ("planner", "worker", "reviewer"):
            _p = (rec.get("roles") or {}).get(_role)
            if not _p or not _p.get("provider") or not _p.get("model"):
                _skipped.append(_role)
                continue
            _role_cfg = {"provider": _p["provider"], "model": _p["model"]}
            _fb = _p.get("fallback")
            if _fb and _fb.get("provider") and _fb.get("model"):
                _role_cfg["fallback"] = {"provider": _fb["provider"], "model": _fb["model"]}
            _roles[_role] = _role_cfg

    _tmp = CONFIG_PATH.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _tmp.replace(CONFIG_PATH)
    return _skipped


def _apply_roles(roles_proposal: dict[str, Optional[dict]]) -> list[str]:
    """[deprecated 薄委托] 写 router.roles。请改用 apply_recommendation（整合原子写）。"""
    return apply_recommendation({"worker_models": {}, "roles": roles_proposal},
                                apply_roles=True, apply_worker_models=False)


def _apply_worker_models(proposal: dict[str, Optional[dict]]) -> None:
    """[deprecated 薄委托] 写 worker_models。请改用 apply_recommendation（整合原子写）。"""
    apply_recommendation({"worker_models": proposal, "roles": {}},
                         apply_roles=False, apply_worker_models=True)


def cmd_recommend(args=None) -> None:
    """CR-G5：读 bench 结果 → 推荐 worker_models{easy,medium,hard} → dry-run / --apply 写 config。

    CLI: agent_go eval recommend [--results FILE] [--apply] [--force]
    依赖 G1（成本感知推荐）+ G2（tier 校验）落地后才可信。绝不静默改 config：
    默认 dry-run 只展示；--apply 写入前跑 G2 校验，tier 错配时拒绝（--force 覆盖）。
    """
    logger = logging.getLogger(__name__)
    results_path = Path(getattr(args, "results", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")
    data = analyze_model_productivity(results_path)
    if "error" in data:
        console.warning(f"{data['error']} → 先跑 agent_go eval bench --output {results_path}")
        return

    rec = build_recommendation(data["models"])
    proposal = rec["worker_models"]

    # M6.4：--llm 触发规则初筛 + LLM 精排（候选 + 4 类确定性问题证据 → LLM 跨维权衡）
    if getattr(args, "llm", False):
        from .config import load_config
        _records = _read_jsonl(results_path)
        _issues = identify_deterministic_issues(data["models"], _records)
        console.print(f"\n🔍 规则初筛确定性问题：{len(_issues)} 类")
        for _iss in _issues:
            console.print(f"  [{_iss['severity']}] {_iss['type']}: {_iss['detail'][:90]}")
        console.print("\n🤖 LLM 精排推理中...")
        rec, _llm_info = llm_rerank_recommendation(rec, _issues, data["models"], load_config(), logger)
        proposal = rec["worker_models"]
        if rec.get("llm_ranking"):
            console.print("\n📊 LLM 精排理由：")
            for _r in rec["llm_ranking"][:5]:
                console.print(f"  {_r.get('model','')}（{_r.get('role','')}）: {_r.get('reason','')[:100]}")
        if rec.get("cautions"):
            for _c in rec["cautions"][:3]:
                console.warning(f"⚠ {_c[:100]}")

    # dry-run 展示
    console.print(f"\n🎯 worker_models 推荐（基于 {data['total_runs']} 次执行）")
    console.print("─" * 90)
    for _slot in ("hard", "medium", "easy"):
        _p = proposal.get(_slot)
        if not _p:
            console.print(f"  {_slot:<7} → （无合格候选，留空）")
            continue
        _bv = " 💰" if _p["best_value"] else ""
        _dpp = _p["dollar_per_pass"]
        console.print(f"  {_slot:<7} → {_p['model']}{_bv}  "
                      f"(通过率 {_p['avg_pass_rate']:.0%}, $/pass ${_dpp or 0:.4f}, "
                      f"{_p['criterion']}, {_p['recommendation']})")
    console.print("─" * 90)

    # G2 tier 校验（advisory，但 --apply 时可作为拒绝条件）
    _proposed_wm = {k: (proposal.get(k) or {}).get("model", "") for k in ("easy", "medium", "hard")}
    _tier_issues = validate_worker_tier(_proposed_wm)
    if _tier_issues:
        for _slot, _mdl, _tier, _msg in _tier_issues:
            console.warning(f"⚠ tier 错配：{_msg}")

    if not getattr(args, "apply", False):
        console.print("（dry-run，未写入。用 --apply 写入 config.json，tier 错配时加 --force 覆盖）")
        return

    if _tier_issues and not getattr(args, "force", False):
        console.error("tier 错配，拒绝写入。复查 bench 数据或用 --force 覆盖。")
        sys.exit(1)
    apply_recommendation(rec, apply_roles=False, apply_worker_models=True)
    console.print(f"✅ 已写入 {CONFIG_PATH} 的 worker_models：{_proposed_wm}")


# ═══════════════════════════════════════════════════════════════
# S10 成本基线（测量/控制解耦 + 删失校正）
# ═══════════════════════════════════════════════════════════════

def compute_cost_baseline(results_paths: list, tasks_dir: str = "eval_suite",
                          exclude_timed_out: bool = True,
                          tolerance: float = 1.5) -> dict:
    """基于 bench 结果计算删失校正的成本基线（P90 × tolerance 预算）。

    测量/控制解耦原则：
      - 基线只统计「自然成本」——排除被 timeout/熔断截断（timed_out=True）的
        记录，避免右删失把真实成本系统性压低。
      - 被截断记录属于「已知下限」（censored），不参与 mean/P90 计算。
      - 预测因子：difficulty × model × plan_step_count（子任务数）。

    Args:
        results_paths: 一个或多个 results*.jsonl 路径
        tasks_dir: eval_suite 目录（用于按 task_id 映射 difficulty）
        exclude_timed_out: 排除 timed_out=True 记录（默认 True，删失校正）
        tolerance: 预算 = P90 × tolerance

    Returns:
        {"per_difficulty_model": {diff: {model: {n, mean, p90, budget}}},
         "per_difficulty": {diff: {n, mean, p90, budget}},
         "summary": {...}}
    """
    from pathlib import Path

    records: list[dict] = []
    for _p in (results_paths if isinstance(results_paths, (list, tuple)) else [results_paths]):
        for line in Path(_p).read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not records:
        return {"error": "无数据"}

    # 按 task_id 映射 difficulty（从任务 YAML 读取）
    task_diff: dict[str, str] = {}
    tasks_dir_p = Path(tasks_dir)
    if tasks_dir_p.is_dir():
        for tf in sorted((tasks_dir_p / "tasks").glob("*.yaml")):
            try:
                _t = yaml.safe_load(tf.read_text(encoding="utf-8"))
                if _t and _t.get("id"):
                    task_diff[_t["id"]] = _t.get("difficulty", "medium")
            except Exception:
                continue

    # 删失校正：排除被截断记录（timed_out=True → 右删失，真实成本 ≥ 记录值）
    if exclude_timed_out:
        _censored = [r for r in records if r.get("timed_out")]
        records = [r for r in records if not r.get("timed_out")]
    else:
        _censored = []

    def _p90(vals: list) -> float:
        if not vals:
            return 0.0
        vals = sorted(vals)
        _i = min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))
        return vals[_i]

    per_dm: dict[str, dict] = {}
    per_diff: dict[str, list] = {}
    _steps: list[int] = []

    for r in records:
        model = r.get("model", "unknown")
        tid = r.get("task_id", "")
        diff = task_diff.get(tid, "medium")
        cost = r.get("total_cost_usd", 0.0) or 0.0
        steps = r.get("plan_step_count") or r.get("total_subtasks") or 0
        _steps.append(int(steps))
        per_dm.setdefault(diff, {}).setdefault(model, []).append(cost)
        per_diff.setdefault(diff, []).append(cost)

    out: dict[str, Any] = {"per_difficulty_model": {}, "per_difficulty": {}}
    for diff, by_model in per_dm.items():
        out["per_difficulty_model"][diff] = {}
        for model, costs in by_model.items():
            out["per_difficulty_model"][diff][model] = {
                "n": len(costs),
                "mean": round(sum(costs) / len(costs), 6),
                "p90": round(_p90(costs), 6),
                "budget": round(_p90(costs) * tolerance, 6),
            }
    for diff, costs in per_diff.items():
        out["per_difficulty"][diff] = {
            "n": len(costs),
            "mean": round(sum(costs) / len(costs), 6),
            "p90": round(_p90(costs), 6),
            "budget": round(_p90(costs) * tolerance, 6),
        }

    _all = [c for diff in per_diff.values() for c in diff]
    out["summary"] = {
        "total_records": len(records),
        "censored_records": len(_censored),
        "exclude_timed_out": exclude_timed_out,
        "tolerance": tolerance,
        "overall_mean": round(sum(_all) / len(_all), 6) if _all else 0.0,
        "overall_p90": round(_p90(_all), 6) if _all else 0.0,
        "overall_budget": round(_p90(_all) * tolerance, 6) if _all else 0.0,
        "avg_plan_step_count": round(sum(_steps) / len(_steps), 2) if _steps else 0.0,
    }
    return out


def cmd_cost_baseline(args=None) -> None:
    """输出删失校正的成本基线表（agent_go eval cost-baseline）。"""
    _workspace = Path(__file__).resolve().parent.parent
    paths_arg = getattr(args, "results", None) or "eval_suite/results.jsonl"
    results_paths = [p.strip() for p in paths_arg.split(",") if p.strip()]
    results_paths = [_workspace / p if not Path(p).is_absolute() else Path(p) for p in results_paths]
    tasks_dir = _workspace / (getattr(args, "tasks", None) or "eval_suite")
    tolerance = float(getattr(args, "tolerance", 1.5) or 1.5)

    baseline = compute_cost_baseline(results_paths, tasks_dir=str(tasks_dir),
                                     exclude_timed_out=True, tolerance=tolerance)
    if "error" in baseline:
        console.error(baseline["error"])
        return

    console.print(f"成本基线（P90×{tolerance}，排除超时删失 {baseline['summary']['censored_records']} 条）")
    console.print(f"总计 {baseline['summary']['total_records']} 条自然成本记录")
    console.print("")
    console.print("┌ 按难度 × 模型 ─────────────────────────────────────┐")
    console.print(f"{'难度':<8} {'模型':<18} {'n':>4} {'mean':>8} {'P90':>8} {'预算':>8}")
    for diff in sorted(baseline["per_difficulty_model"]):
        for model, m in sorted(baseline["per_difficulty_model"][diff].items()):
            console.print(f"{diff:<8} {model:<18} {m['n']:>4} ${m['mean']:>7.4f} "
                          f"${m['p90']:>7.4f} ${m['budget']:>7.4f}")
    console.print("")
    console.print("┌ 按难度（整体）────────────────────────────────────┐")
    console.print(f"{'难度':<8} {'n':>4} {'mean':>8} {'P90':>8} {'预算':>8}")
    for diff in sorted(baseline["per_difficulty"]):
        m = baseline["per_difficulty"][diff]
        console.print(f"{diff:<8} {m['n']:>4} ${m['mean']:>7.4f} ${m['p90']:>7.4f} ${m['budget']:>7.4f}")
    console.print(f"\n总体预算（P90×{tolerance}）: ${baseline['summary']['overall_budget']:.4f}")
    console.print(f"平均子任务数: {baseline['summary']['avg_plan_step_count']}")
    console.print("注：预算基于自然成本（排除 timed_out 右删失）。被熔断记录会写入 "
                  "metering.jsonl 的 cost_censored 事件继续累计（测量与控制解耦）。")


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════

# _read_jsonl / _read_json 已抽取到 eval.py（共享实现）

# ═══════════════════════════════════════════════════════════════
# P2 难度自动校准（任务集扩充配套）：基于 bench 实测校准难度标签
# ═══════════════════════════════════════════════════════════════

_DIFF_ORDER = ["easy", "medium", "hard"]
_DIFF_UP_THRESHOLD = 0.40       # pass_rate 低于此 → 建议升档（实际比标注难）
_DIFF_DOWN_THRESHOLD = 0.85     # pass_rate 高于此 → 候选降档（结合耗时确认）
_DIFF_ELAPSED_FACTOR = 2.0      # avg_elapsed > 同难度中位数 × 此值 → 升档信号
_DIFF_CROSS_DIFF_RATIO = 1.2    # avg_elapsed ≤ 更简单档中位数 × 此值 → 降档信号


def calibrate_task_difficulty(results_paths: list, tasks_dir: str = "eval_suite") -> dict:
    """基于 bench 实测数据校准任务难度标签（P2）。

    设计原则（与 _recommend 决策阈值对齐，PRD 铁律）：
      - 通过率是主信号：pass_rate 高 → 任务实际偏简单；低 → 偏难。
      - 耗时为辅助信号：结合"同难度中位数"做相对判断，避免绝对阈值误判
        （硬任务本就更耗时）。
      - 同难度比较：升/降档都基于该难度在**本批数据中的中位数**，
        而非拍脑袋的绝对秒数。

    判定规则（对每个有数据的任务）：
      - 升档（up）：pass_rate < 0.40（通过率低，能力不足）
        或 avg_elapsed > 同难度中位数 × 2.0（异常耗时）
      - 降档（down）：pass_rate >= 0.85 且
        avg_elapsed < 同难度中位数 × 0.35（又快又准 → 标难了）
      - 维持（keep）：其余

    Args:
        results_paths: 一个或多个 results*.jsonl 路径
        tasks_dir: eval_suite 目录（读 tasks/*.yaml 的标注难度）

    Returns:
        {"tasks": {task_id: {labeled, suggested, pass_rate, avg_elapsed,
                              n, reason, action}},
         "by_difficulty_elapsed_median": {diff: median_sec},
         "summary": {"total", "up", "down", "keep", "no_data"}}
    """
    if yaml is None:
        return {"error": "yaml 未安装，无法读取任务配置"}

    records: list[dict] = []
    for _p in (results_paths if isinstance(results_paths, (list, tuple)) else [results_paths]):
        _f = Path(_p)
        if not _f.exists():
            continue
        for line in _f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not records:
        return {"error": "无数据"}

    # 读任务标注难度（task_id → labeled difficulty）
    labeled: dict[str, str] = {}
    _td = Path(tasks_dir)
    if (_td / "tasks").is_dir():
        for tf in sorted((_td / "tasks").glob("*.yaml")):
            try:
                _t = yaml.safe_load(tf.read_text(encoding="utf-8"))
                if _t and _t.get("id"):
                    labeled[_t["id"]] = _t.get("difficulty", "medium")
            except Exception:
                continue

    # 聚合每任务实测指标（仅自然记录；timed_out 右删失不计入耗时均值）
    agg: dict[str, dict] = {}
    for r in records:
        tid = r.get("task_id", "")
        if not tid or tid.startswith("task-"):  # 跳过探索期临时任务
            continue
        a = agg.setdefault(tid, {"elapsed": [], "pass": [], "n": 0})
        a["pass"].append(1.0 if (r.get("pass_rate") or 0) > 0 else 0.0)
        if not r.get("timed_out"):
            _el = r.get("elapsed_sec") or r.get("elapsed") or 0
            if _el:
                a["elapsed"].append(float(_el))
        a["n"] += 1

    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        m = len(s) // 2
        return s[m] if len(s) % 2 else round((s[m - 1] + s[m]) / 2, 2)

    # 各难度 elapsed 中位数（用于相对判断）
    diff_elapsed: dict[str, list[float]] = {}
    for tid, a in agg.items():
        diff = labeled.get(tid, "medium")
        diff_elapsed.setdefault(diff, []).extend(a["elapsed"])
    by_diff_median = {d: _median(v) for d, v in diff_elapsed.items()}

    tasks_out: dict[str, dict] = {}
    summary = {"total": 0, "up": 0, "down": 0, "keep": 0, "no_data": 0}
    for tid, a in agg.items():
        labeled_diff = labeled.get(tid, "medium")
        avg_pass = round(sum(a["pass"]) / a["n"], 4) if a["n"] else 0.0
        avg_elapsed = round(sum(a["elapsed"]) / len(a["elapsed"]), 1) if a["elapsed"] else None
        diff_median = by_diff_median.get(labeled_diff, 0.0)

        suggested = labeled_diff
        reason = ""
        action = "keep"
        # 降档参照：更简单一档的中位数（如 hard→medium 时参照 medium 档中位数）。
        # 避免"同难度中位数被自身数据主导"导致的自我参照失效。
        _lower_diff = _next_diff(labeled_diff, -1)
        _lower_median = by_diff_median.get(_lower_diff, 0.0) if _lower_diff != labeled_diff else 0.0
        if avg_pass < _DIFF_UP_THRESHOLD:
            suggested = _next_diff(labeled_diff, 1)
            action = "up"
            reason = f"通过率 {avg_pass:.0%} < {_DIFF_UP_THRESHOLD:.0%}，实际比标注难"
        elif (avg_pass >= _DIFF_DOWN_THRESHOLD and avg_elapsed is not None
              and _lower_median > 0 and avg_elapsed <= _lower_median * _DIFF_CROSS_DIFF_RATIO):
            suggested = _lower_diff
            action = "down"
            reason = (f"通过率 {avg_pass:.0%} ≥ {_DIFF_DOWN_THRESHOLD:.0%} 且耗时 "
                      f"{avg_elapsed:.0f}s ≤ {_lower_diff}档中位数 {_lower_median:.0f}s × "
                      f"{_DIFF_CROSS_DIFF_RATIO:.1f}，实际比标注简单")
        elif (avg_elapsed is not None and diff_median > 0
              and avg_elapsed > diff_median * _DIFF_ELAPSED_FACTOR):
            suggested = _next_diff(labeled_diff, 1)
            action = "up"
            reason = (f"耗时 {avg_elapsed:.0f}s > 同难度中位数 {diff_median:.0f}s × "
                      f"{_DIFF_ELAPSED_FACTOR:.1f}，疑似低估")

        summary[action] += 1
        summary["total"] += 1
        tasks_out[tid] = {
            "labeled": labeled_diff,
            "suggested": suggested,
            "pass_rate": avg_pass,
            "avg_elapsed": avg_elapsed,
            "n": a["n"],
            "action": action,
            "reason": reason,
        }

    # 有 YAML 但无实测数据的任务 → no_data
    for tid, diff in labeled.items():
        if tid not in tasks_out:
            tasks_out[tid] = {
                "labeled": diff, "suggested": diff, "pass_rate": None,
                "avg_elapsed": None, "n": 0, "action": "no_data",
                "reason": "无实测数据（本次 results 未覆盖）",
            }
            summary["no_data"] += 1

    return {
        "tasks": tasks_out,
        "by_difficulty_elapsed_median": by_diff_median,
        "summary": summary,
    }


def _next_diff(current: str, delta: int) -> str:
    """难度档位移动（easy→medium→hard），边界处保持不变。"""
    _i = _DIFF_ORDER.index(current) if current in _DIFF_ORDER else 1
    _j = max(0, min(len(_DIFF_ORDER) - 1, _i + delta))
    return _DIFF_ORDER[_j]


def _apply_difficulty_calibration(cal: dict, tasks_dir: str = "eval_suite") -> list[str]:
    """把校准建议写回任务 YAML 的 difficulty 字段（原子写：tmp + rename）。

    仅更新 action in ("up", "down") 的任务；keep/no_data 不动。
    返回实际修改的任务 id 列表。
    """
    if yaml is None:
        return []
    _td = Path(tasks_dir) / "tasks"
    _changed: list[str] = []
    if not _td.is_dir():
        return _changed
    for tf in sorted(_td.glob("*.yaml")):
        try:
            _t = yaml.safe_load(tf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not _t or not _t.get("id"):
            continue
        _c = cal.get("tasks", {}).get(_t["id"])
        if not _c or _c.get("action") not in ("up", "down"):
            continue
        _new = _c["suggested"]
        if _t.get("difficulty") != _new:
            _t["difficulty"] = _new
            _tmp = tf.with_suffix(".yaml.tmp")
            _tmp.write_text(yaml.safe_dump(_t, allow_unicode=True, sort_keys=False), encoding="utf-8")
            _tmp.replace(tf)
            _changed.append(_t["id"])
    return _changed


def cmd_calibrate_difficulty(args=None) -> None:
    """P2：难度自动校准命令（agent_go eval calibrate-difficulty）。

    CLI: agent_go eval calibrate-difficulty [--results FILE[,FILE...]]
         [--tasks eval_suite] [--apply] [--threshold-up F] [--threshold-down F]
    默认 dry-run 展示建议表；--apply 写回任务 YAML 的 difficulty。
    """
    _workspace = Path(__file__).resolve().parent.parent
    results_arg = getattr(args, "results", None) or "eval_suite/results.jsonl"
    results_paths = [p.strip() for p in results_arg.split(",") if p.strip()]
    results_paths = [_workspace / p if not Path(p).is_absolute() else Path(p) for p in results_paths]
    tasks_dir = _workspace / (getattr(args, "tasks", None) or "eval_suite")

    cal = calibrate_task_difficulty(results_paths, tasks_dir=str(tasks_dir))
    if "error" in cal:
        console.error(cal["error"])
        return

    s = cal["summary"]
    console.print("难度校准建议（基于 bench 实测）")
    console.print(f"总计 {s['total']} 个有数据任务: 升档 {s['up']} / 降档 {s['down']} / 维持 {s['keep']} / 无数据 {s['no_data']}")
    console.print("")
    console.print(f"{'任务':<40} {'标注':<7} {'建议':<7} {'通过率':>7} {'耗时':>8} {'n':>3}  原因")
    console.print("─" * 110)
    for tid in sorted(cal["tasks"]):
        t = cal["tasks"][tid]
        _mark = {"up": "⬆", "down": "⬇", "keep": "·", "no_data": "-"}[t["action"]]
        _pr = f"{t['pass_rate']:.0%}" if t["pass_rate"] is not None else "  -"
        _el = f"{t['avg_elapsed']:.0f}s" if t["avg_elapsed"] is not None else " -"
        console.print(f"{_mark} {tid:<38} {t['labeled']:<7} {t['suggested']:<7} "
                      f"{_pr:>7} {_el:>8} {t['n']:>3}  {t['reason']}")
    console.print("─" * 110)
    console.print("中位数参考: " + ", ".join(
        f"{d}={v:.0f}s" for d, v in sorted(cal["by_difficulty_elapsed_median"].items())))

    if not getattr(args, "apply", False):
        console.print("（dry-run，未写入。用 --apply 写回任务 YAML 的 difficulty）")
        return
    _changed = _apply_difficulty_calibration(cal, tasks_dir=str(tasks_dir))
    if _changed:
        console.success(f"已更新 {len(_changed)} 个任务难度: {', '.join(_changed)}")
    else:
        console.print("无需要更新的任务")
