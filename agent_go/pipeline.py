import sys, os, subprocess, json, threading, signal, logging, inspect
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional


from .console import _LazyConsole
from .config import write_censored_event
from .executor import run_subtask
from .git_utils import _set_gc_auto, _worktree_remove, _worktree_prune
# 解耦：notify 是可选增强，删除模块级 import 以匹配 architecture.md 解耦原则
# （让函数内动态 import 统一拦截，便于测试 mock 和 disable notify 而不破坏 import）

logger = logging.getLogger(__name__)

console = _LazyConsole()

__all__: list[str] = []

# 心跳周期（秒）：pipeline 运行期间周期 touch {task_dir}/heartbeat。
# meta.json 只在子任务完成时写，长时间运行的合法任务会被 _cleanup_stale_tasks
# 的"meta.json mtime > 1h"判活误杀；heartbeat 文件是"父进程存活"的外部信号。
HEARTBEAT_INTERVAL = 30


def _start_heartbeat(task_dir: Path, logger: logging.Logger) -> threading.Event:
    """启动心跳线程：任务运行期间周期刷新 {task_dir}/heartbeat 的 mtime。

    进程在跑 → 每 HEARTBEAT_INTERVAL 秒 touch 一次；SIGKILL 后文件冻结，
    下一次 stale 清理可用其 mtime 准确区分"长任务运行中"与"进程已死"。
    返回 stop Event；调用方在所有退出路径调用 _stop_heartbeat 收尾。
    """
    hb_path = task_dir / "heartbeat"
    stop_event = threading.Event()

    def _beat() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                hb_path.touch()
            except OSError as _e:
                logger.warning(f"[heartbeat] 写入失败（心跳停止）: {_e}")
                break

    _t = threading.Thread(target=_beat, daemon=True, name=f"heartbeat-{task_dir.name}")
    _t.start()
    try:
        hb_path.touch()
    except OSError as _e:
        logger.warning(f"[heartbeat] 初始写入失败: {_e}")
    logger.debug(f"[heartbeat] 已启动（interval={HEARTBEAT_INTERVAL}s）")
    return stop_event


def _stop_heartbeat(task_dir: Path, stop_event: Optional[threading.Event]) -> None:
    """停止心跳线程并清理 heartbeat 文件（正常/中断退出路径调用）。"""
    if stop_event is not None:
        stop_event.set()
    try:
        (task_dir / "heartbeat").unlink()
    except FileNotFoundError:
        pass


def _invoke_run_subtask(task_id, st, repo, task_dir, logger, upstream, headless,
                        issue_ref, active_pids, active_pids_lock, metering_path, config,
                        interrupt_event):
    """调用 worker；兼容外部集成方仍使用旧版 run_subtask mock/signature。"""
    kwargs = {
        "headless": headless, "issue_ref": issue_ref,
        "active_pids": active_pids, "active_pids_lock": active_pids_lock,
        "metering_path": metering_path, "config": config,
    }
    try:
        if "interrupt_event" in inspect.signature(run_subtask).parameters:
            kwargs["interrupt_event"] = interrupt_event
    except (TypeError, ValueError):
        kwargs["interrupt_event"] = interrupt_event
    return run_subtask(task_id, st, repo, task_dir, logger, upstream, **kwargs)


def _save_meta_atomic(meta: dict, task_dir: Path) -> None:
    """原子写 meta.json：先写 .tmp，再 rename（POSIX 保证原子性）。

    用于 P0 Layer 4 — 每个 subtask 完成后立即持久化。
    即使后续进程被 SIGKILL，partial write 也不会损坏 meta.json。
    """
    meta_path = task_dir / "meta.json"
    tmp_path = task_dir / "meta.json.tmp"
    tmp_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(tmp_path, meta_path)


def _meter_total_cost(metering_path: str) -> float:
    """聚合 metering.jsonl 的累计成本（任务级，用于成本控制 L3 熔断）。"""
    if not metering_path:
        return 0.0
    total = 0.0
    try:
        with open(metering_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # cost_censored 是控制审计事件，cost_usd 表示累计下限，不是新的消费。
                if ev.get("event") != "cost_censored":
                    total += ev.get("cost_usd", 0.0) or 0.0
    except OSError:
        return 0.0
    return total


def _metering_available(metering_path: str) -> bool:
    if not metering_path:
        return False
    try:
        with open(metering_path, encoding="utf-8"):
            return True
    except OSError:
        return False


def _dynamic_task_budget(cc_cfg: dict, subtasks: list[dict]) -> float:
    """S12-P1 G3：动态计算任务级 L3 预算（默认值，config 未显式指定时用）。

    公式：Σ(per_subtask_budget_usd[difficulty] × subtask_multiplier × 该难度子任务数)
    避免 hard 多子任务与 easy 少子任务共用同一全局上限导致 hard 过早熔断。
    返回 0 表示无法计算（将回退到静态 max_budget_usd 或禁用）。
    """
    _budgets = cc_cfg.get("per_subtask_budget_usd") or {}
    _mult = cc_cfg.get("subtask_multiplier", 2.5) or 2.5
    total = 0.0
    for st in subtasks:
        _diff = st.get("difficulty", "medium")
        _b = _budgets.get(_diff) or _budgets.get("medium", 0.0) or 0.0
        total += float(_b) * float(_mult)
    return round(total, 4)


def _subtask_budget_reservation(cc_cfg: dict, subtask: dict) -> float:
    """返回单个子任务的预算 reservation；未知配置返回 0（不做错误阻断）。"""
    budgets = cc_cfg.get("per_subtask_budget_usd") or {}
    difficulty = subtask.get("difficulty", "medium")
    if isinstance(budgets, dict):
        value = budgets.get(difficulty) or budgets.get("medium", 0.0)
    else:
        value = budgets
    try:
        return max(0.0, float(value or 0.0) * float(cc_cfg.get("subtask_multiplier", 2.5) or 2.5))
    except (TypeError, ValueError):
        return 0.0


def _estimate_wave_count(subtasks: list[dict], completed_ids: set = frozenset()) -> int:
    """估算拓扑波次总数（仅计算不执行，P1-4 波次进度卡片用）。

    对剩余子任务重复分层：每轮取出所有依赖已满足的子任务为一个 wave。
    依赖循环时提前终止，返回已算出的波次数。
    """
    remaining = [s for s in subtasks if s["id"] not in completed_ids]
    done = set(completed_ids)
    waves = 0
    while remaining:
        wave = [s for s in remaining if all(d in done for d in s.get("depends_on", []))]
        if not wave:
            break  # 依赖循环或无法满足
        done.update(s["id"] for s in wave)
        remaining = [s for s in remaining if s["id"] not in done]
        waves += 1
    return waves


def _record_subtask_result(
    st: dict,
    result: dict,
    task_dir: Path,
    meta: dict,
    worktree_map: dict,
    results_map: dict,
    completed_ids: set,
    failed_ids: set,
    degraded_count: int,
    meta_lock: threading.Lock,
    config: dict,
) -> int:
    """记录 subtask 结果到共享状态、写 result.json + meta.json、标记完成、失败时通知。

    串行和并发路径共用此函数，避免双分支代码不一致导致的缩进/语义错误。
    返回更新后的 degraded_count（int 不可变，需传值返回）。

    关键改进（P0 Layer 4）：每个 subtask 完成立即原子写 meta.json。
    即使后续被 SIGKILL，已完成的 subtask 状态也不会丢失，recover 不再需要。
    """
    from .failure import classify_failure
    result.setdefault("failure_class", classify_failure(result, meta))
    with meta_lock:
        worktree_map[st["id"]] = task_dir / st["id"] / "work"
        results_map[st["id"]] = result
        if result.get("status") == "degraded" or result.get("degraded"):
            degraded_count += 1
            # S12-P1 G4 安全阀：degrade 模式下统计连续失败（失败递增 / 成功清零）
            _is_degrade_fail = result.get("status") == "failed" or not result.get("verify_ok")
            if isinstance(config, dict) and config.get("_degraded"):
                _cur_streak = int(config.get("_degrade_fail_streak", 0) or 0)
                config["_degrade_fail_streak"] = _cur_streak + 1 if _is_degrade_fail else 0
        # 每个 subtask 独立写 result.json，减少全量覆写
        result_file = task_dir / st["id"] / "result.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        # P0 Layer 4：原子写 meta.json（write tmp + rename），保证 partial write 不损坏
        # 把当前 subtask 结果也存进 meta.json 的 results 数组
        meta.setdefault("results", [])
        # 替换已有的（如果重跑同 id），否则追加
        existing_idx = next((i for i, r in enumerate(meta["results"]) if r.get("subtask_id") == st["id"]), None)
        if existing_idx is not None:
            meta["results"][existing_idx] = result
        else:
            meta["results"].append(result)
        _save_meta_atomic(meta, task_dir)
        completed_ids.add(st["id"])
        if result.get("status") == "failed":
            failed_ids.add(st["id"])
        # S6 失败通知增强：子任务失败时主动推送（即使整体未完成）
        from .notify import notify_event as _notify_event
        try:
            _notify_event("subtask_failed", {
                "subtask": st,
                "result": result,
                "meta": meta,
                "task_dir": str(task_dir),
            }, config)
        except Exception:
            pass
    return degraded_count


def _sanitize_preserved_worktree(wt_path: Path) -> None:
    """S12 失败清理 #2：净化保留的 worktree 现场，移除运行时缓存垃圾，
    避免 .pytest_cache / __pycache__ / *.pyc 污染人工审查现场。
    删除失败静默（净化是增强，不阻断保留）。
    """
    if not wt_path or not wt_path.exists():
        return
    import shutil
    for _root, _dirs, _files in os.walk(str(wt_path)):
        _r = Path(_root)
        for _d in list(_dirs):
            if _d in (".pytest_cache", "__pycache__"):
                try:
                    shutil.rmtree(str(_r / _d), ignore_errors=True)
                    _dirs.remove(_d)
                except Exception:
                    pass
        for _f in _files:
            if _f.endswith(".pyc"):
                try:
                    (_r / _f).unlink(missing_ok=True)
                except OSError:
                    pass


def _run_pipeline_impl(confirmed: list[dict[str, Any]], repo: Path, task_dir: Path, logger: logging.Logger, config: dict[str, Any], headless: bool, parallel: int, issue_ref: str, meta: dict[str, Any],
                  worktree_map: Optional[dict[str, Path]] = None, results_map: Optional[dict[str, dict[str, Any]]] = None, completed_ids: Optional[set] = None, remote_url: str = "",
                  preserve_worktrees: Optional[bool] = None, interrupted: Optional[threading.Event] = None, step_confirm: bool = False) -> None:
    """执行管线：拓扑排序 + 并发/串行执行。恢复模式下传入已有状态。

    preserve_worktrees:
      None  → 默认行为：保留 failed/blocked 的 worktree，其余清理
      True  → 保留所有 worktree
      False → 强制清理所有 worktree
    interrupted:
      外部中断 Event（TUI 模式由主线程维护）。None 时内部创建并注册 OS 信号处理器。
    step_confirm:
      每波执行前暂停，确认继续/跳过/退出。仅非 TUI 模式有效。
    """
    worktree_map = worktree_map or {}
    results_map = results_map or {}
    completed_ids = completed_ids or set()
    # M6: 追踪失败和阻断的子任务 ID，下游依赖失败的上游时自动阻断
    failed_ids: set = {r["subtask_id"] for r in results_map.values() if r.get("status") == "failed"}
    blocked_ids: set = {r["subtask_id"] for r in results_map.values() if r.get("status") == "blocked"}
    task_id = meta["task_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    if meta.get("base_commit"):
        config["_base_commit"] = meta["base_commit"]
    # 同一 task 不允许 run/resume/recover 交叉修改 worktree 和 meta。
    _task_lock_file = None
    try:
        import fcntl
        _task_lock_file = (task_dir / ".task.lock").open("a+", encoding="utf-8")
        fcntl.flock(_task_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError, OSError):
        if _task_lock_file is not None:
            _task_lock_file.close()
        raise RuntimeError(f"task {task_id} is already running")

    def _release_task_lock() -> None:
        if _task_lock_file is not None:
            try:
                fcntl.flock(_task_lock_file.fileno(), fcntl.LOCK_UN)
            except (NameError, OSError):
                pass
            _task_lock_file.close()

    meta_lock = threading.Lock()
    active_pids = set()
    active_pids_lock = threading.Lock()
    degraded_count = sum(1 for r in results_map.values() if r.get("status") in ("no_changes", "degraded"))
    total = len(confirmed)

    # 禁用 git gc.auto — worktree 并发操作共享对象库时避免竞态
    original_gc_value = None
    gc_disabled = False
    if (repo / ".git").exists():
        original_gc_value, ok, _ = _set_gc_auto(repo, "0")
        if ok:
            gc_disabled = True
            logger.info(f"[worktree] gc.auto 已禁用 (原值: {original_gc_value})")

    # ── 中断标志 ──
    # TUI 模式：main 线程维护 Event，后台线程只检查
    # CLI 模式：内部创建 Event 并注册 OS 信号处理器
    if interrupted is not None:
        _interrupted = interrupted
        _own_signal = False
    else:
        _interrupted = threading.Event()
        _own_signal = True

    # 注册信号处理器：设置中断标志 + SIGTERM 转发到子进程
    # （无论 _own_signal 如何都需要转发子进程，覆盖 TUI/Cli 两种模式）
    def _on_interrupt(signum: int, frame: Any) -> None:
        _interrupted.set()
        # 先保存 meta.json（尽最大努力 — async-signal-safe 约束下仅写入 pipe 或小文件）
        with active_pids_lock:
            pids_to_kill = list(active_pids)
        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    signals_installed = False
    prev_sigint = prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        prev_sigint = signal.signal(signal.SIGINT, _on_interrupt)
        prev_sigterm = signal.signal(signal.SIGTERM, _on_interrupt)
        signals_installed = True
    config["_pipeline_signal_state"] = (signals_installed, prev_sigint, prev_sigterm)
    config["_pipeline_gc_state"] = (repo, gc_disabled, original_gc_value)

    # 跳过已完成的子任务
    remaining = [st for st in confirmed if st["id"] not in completed_ids]
    if not remaining:
        console.print("所有子任务已完成，无需恢复执行")
        meta["status"] = "DELIVERY_READY" if meta.get("status_schema_version") else "completed"
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        # 恢复信号处理器（仅 CLI 模式自有）与 gc.auto
        if signals_installed:
            signal.signal(signal.SIGINT, prev_sigint)
            signal.signal(signal.SIGTERM, prev_sigterm)
        if gc_disabled and original_gc_value is not None:
            _, _, _ = _set_gc_auto(repo, original_gc_value)
        _release_task_lock()
        return

    # ── 心跳：pipeline 存活信号（供 stale 清理判活，防误杀长任务）──
    _heartbeat_stop = _start_heartbeat(task_dir, logger)

    # ── MCP 消费层（S9-A）：启动外部 MCP server 连接池 ──
    # 解耦：可选增强，启动失败降级 warning 不阻断 pipeline（与 notify/skills 同级）
    mcp_pool = None
    _mcp_cfg = config.get("mcp_servers") if config else None
    if _mcp_cfg:
        try:
            from .mcp_client import MCPClientPool
            mcp_pool = MCPClientPool(_mcp_cfg)
            mcp_pool.start_all()
            config["_mcp_pool"] = mcp_pool  # 透传给子任务（agent_loop / subtask）
            _tool_count = len(mcp_pool.tool_definitions())
            if _tool_count:
                logger.info(f"[mcp] 已连接 {len(mcp_pool._servers)} 个 MCP server，{_tool_count} 个工具可用")
        except Exception as _mcp_err:
            logger.warning(f"MCP 消费层启动失败，跳过（不中断核心）: {_mcp_err}")
            mcp_pool = None

    def _stop_mcp_pool() -> None:
        """pipeline 退出时回收 MCP server 进程（3 个退出点共用）。"""
        if mcp_pool is not None:
            try:
                mcp_pool.stop_all()
            except Exception:
                logger.debug("MCP 池停止异常（已忽略）", exc_info=True)

    console.emit("pipeline_start", {
        "task_id": task_id,
        "total_subtasks": total,
        "parallel": parallel,
    })
    wave_num = 0
    # P1-4: 预估算总波次（拓扑分层，仅计算不执行），用于波次进度卡片
    total_waves = _estimate_wave_count(confirmed, completed_ids) or 1
    if parallel > 1 and total > 1:
        logger.info(f"[并发] max_workers={parallel}, 拓扑调度，剩余 {len(remaining)} 个子任务")

    while remaining:
        # S12-P1 G4 安全阀：degrade 模式下，降级子任务连续失败 ≥3 个 → 回退 stop，
        # 避免在便宜模型上无限烧钱（降级模型 verify 必败时 degrade 只是"延长死亡时间"）。
        # CR-H1 修复：置 `_degrade_aborted` 哨兵，防止同轮 L3 检查（成本只增不减）把
        # 刚置 False 的 _degraded 又改回 True，导致"回退 stop"永不生效。
        if isinstance(config, dict) and config.get("_degraded"):
            _streak = int(config.get("_degrade_fail_streak", 0) or 0)
            if _streak >= 3:
                logger.error(
                    f"[degrade] 连续 {_streak} 个降级子任务失败，回退 stop（不再降级烧钱）")
                config["_degraded"] = False
                config["_degrade_fail_streak"] = 0
                config["_degrade_aborted"] = True
        # M6: 分离已阻断的子任务（上游失败 → 下游不执行）
        # block_on_failure=false（--no-verify-block）时跳过阻断，下游照常调度
        block_on_failure = config.get("verification", {}).get("block_on_failure", True)
        newly_blocked = []
        if block_on_failure:
            for st in remaining:
                deps = st.get("depends_on", [])
                blockers = [d for d in deps if d in failed_ids or d in blocked_ids]
                if blockers:
                    newly_blocked.append(st)
                    blocked_ids.add(st["id"])
                    results_map[st["id"]] = {
                        "subtask_id": st["id"], "status": "blocked",
                        "exit_code": -1, "summary": f"上游失败，已阻断: {blockers}",
                        "blocked_by": blockers,
                        "failure_reason": "上游依赖失败，级联阻断",
                        "worktree": "", "sandbox_type": "headless",
                        "verify_ok": False, "duration_sec": 0,
                    }
                    completed_ids.add(st["id"])
        if newly_blocked:
            logger.info(f"[级联阻断] {', '.join(st['id'] for st in newly_blocked)} — 上游失败，已阻断")

        # 已被阻断的子任务不得进入 wave（blocked_ids 中的子任务虽然 deps 已满足，
        # 但必须跳过执行，否则阻断形同虚设、blocked 结果会被真实执行覆盖）
        wave = [st for st in remaining
                if st["id"] not in blocked_ids
                and all(dep in completed_ids for dep in st.get("depends_on", []))]

        # S10 成本控制 L3：任务级熔断（跨子任务）。每次调度新 wave 前聚合 metering
        # 累计成本，超 max_budget_usd 则停止调度并将剩余子任务标记 blocked。
        # 默认关闭（cost_control.enabled=False 不检查）。
        # S12-P1 G3：budget_mode 三态（strict=block / degrade=降级继续 / ignore=跳过 L3）
        # + 动态默认预算（max_budget_usd 为空时用 Σ per_subtask_budget×mult×子任务数）。
        _cc_cfg = (config or {}).get("cost_control") or {}
        if _cc_cfg.get("enabled") and wave:
            _budget_mode = _cc_cfg.get("budget_mode", "strict")
            _max_budget = _cc_cfg.get("max_budget_usd") or 0.0
            if not _max_budget:
                # CR-M1 修复：任务级预算应在规划时一次性确定（confirmed 全量），
                # 不随波次 remaining 缩短而下降，否则任务越接近完成越容易误触 L3。
                _max_budget = _dynamic_task_budget(_cc_cfg, confirmed)
            _meter_path = (config or {}).get("_metering_path", "")
            if _budget_mode != "ignore" and _max_budget > 0 and _meter_path:
                if not _metering_available(_meter_path):
                    logger.error("成本计量不可用，strict/degrade 模式停止调度以避免无上限消费")
                    for st in remaining:
                        if st["id"] not in results_map:
                            results_map[st["id"]] = {
                                "subtask_id": st["id"], "status": "blocked", "exit_code": -1,
                                "summary": "成本计量不可用，已停止调度", "blocked_by": ["metering"],
                                "failure_reason": "metering.jsonl 不可读", "kill_reason": "metering_unavailable",
                                "worktree": "", "sandbox_type": "headless", "verify_ok": False,
                                "duration_sec": 0,
                            }
                            completed_ids.add(st["id"])
                    break
                _spent = _meter_total_cost(_meter_path)
                if _spent >= _max_budget:
                    # CR-H1 修复：_degrade_aborted 哨兵置位后，降级已回退 stop，
                    # 不再因 _spent≥_max_budget（成本只增不减）重复进入降级分支。
                    _can_degrade = (_budget_mode == "degrade"
                                    and not (isinstance(config, dict)
                                             and config.get("_degrade_aborted")))
                    if _can_degrade:
                        # G4 优雅降级：不硬停，给剩余子任务打降级标记（切便宜模型继续），
                        # 保留部分产出。run_subtask 读到 config["_degraded"] 后降档模型。
                        logger.warning(
                            f"[cost_control L3] 任务累计成本 ${_spent:.4f} ≥ 预算 ${_max_budget:.4f}，"
                            f"切换降级模式（budget_mode=degrade），剩余 {len(remaining)} 个子任务降档模型")
                        write_censored_event(_meter_path, level="L3", sub_id="",
                                             spent=_spent, budget=_max_budget,
                                             reason=f"成本超预算降级：${_spent:.4f} ≥ ${_max_budget:.4f}")
                        if isinstance(config, dict):
                            config["_degraded"] = True
                            config["_degrade_budget"] = _max_budget
                        # 不 break：继续调度（剩余子任务将以降级模型执行）
                    else:
                        logger.warning(
                            f"[cost_control L3] 任务累计成本 ${_spent:.4f} ≥ 预算 ${_max_budget:.4f}，"
                            f"停止调度剩余 {len(remaining)} 个子任务 (budget_mode={_budget_mode})")
                        write_censored_event(_meter_path, level="L3", sub_id="",
                                             spent=_spent, budget=_max_budget,
                                             reason=f"任务累计成本 ${_spent:.4f} ≥ 预算 ${_max_budget:.4f}")
                        for st in remaining:
                            if st["id"] not in results_map:
                                results_map[st["id"]] = {
                                    "subtask_id": st["id"], "status": "blocked",
                                    "exit_code": -1, "summary": f"成本熔断（累计 ${_spent:.4f} ≥ 预算 ${_max_budget:.4f}）",
                                    "blocked_by": ["cost_control"],
                                    "failure_reason": "任务成本超预算熔断",
                                    "kill_reason": "over_budget_l3",
                                    "worktree": "", "sandbox_type": "headless",
                                    "verify_ok": False, "duration_sec": 0,
                                }
                                completed_ids.add(st["id"])
                        break

        # 并发预算 reservation：strict 模式下先预留每个 worker 的 L2 上限，
        # 避免同一 wave 在一次检查后同时启动并超过任务预算。实际成本仍由 L3
        # 在下一 wave 复核，reservation 只针对有明确 per-subtask 预算的任务。
        if _cc_cfg.get("enabled") and _cc_cfg.get("budget_mode", "strict") == "strict" and wave:
            _reservation_budget = _cc_cfg.get("max_budget_usd") or _dynamic_task_budget(_cc_cfg, confirmed)
            _reserved_available = max(0.0, float(_reservation_budget or 0.0) - _meter_total_cost(config.get("_metering_path", "")))
            _reserved_wave = []
            _reservation_blocked = []
            for _st in wave:
                _need = _subtask_budget_reservation(_cc_cfg, _st)
                if _need <= 0 or _need <= _reserved_available:
                    _reserved_wave.append(_st)
                    _reserved_available -= _need
                else:
                    _reservation_blocked.append(_st)
            for _st in _reservation_blocked:
                blocked_ids.add(_st["id"])
                completed_ids.add(_st["id"])
                results_map[_st["id"]] = {
                    "subtask_id": _st["id"], "status": "blocked", "exit_code": -1,
                    "summary": "预算 reservation 不足，未启动子任务",
                    "blocked_by": ["cost_control"], "failure_reason": "并发启动前预算不足",
                    "kill_reason": "over_budget_l3", "worktree": "", "sandbox_type": "headless",
                    "verify_ok": False, "duration_sec": 0,
                }
            wave = _reserved_wave

        if not wave:
            logger.error("依赖循环或无法满足的依赖！")
            # 将无法调度的子任务标记为失败，避免收尾时 meta 误标 completed
            for st in remaining:
                if st["id"] not in results_map:
                    results_map[st["id"]] = {
                        "subtask_id": st["id"], "status": "failed",
                        "exit_code": -1, "summary": "依赖循环或无法满足的依赖，未执行",
                        "worktree": "", "sandbox_type": "headless",
                        "verify_ok": False, "duration_sec": 0,
                    }
            break

        logger.info(f"[Wave {wave_num}] {', '.join(st['id'] for st in wave)}")
        actual_workers = min(parallel, len(wave)) if parallel > 1 else 1
        console.emit("wave_start", {
            "wave_idx": wave_num,
            "total_waves": total_waves,
            "subtask_ids": [st["id"] for st in wave],
            "parallel": actual_workers,
        })

        # P1-4: 波次进度卡片 — 人类可读分组显示（JSON 模式下由 console 转为事件）
        _wave_labels = [f"{st['id']} ({st.get('title', '?')[:40]})" for st in wave]
        console.subtitle(f"═══ Wave {wave_num + 1}/{total_waves} ({actual_workers} 并行) ═══")
        for _w in _wave_labels:
            console.print(f"  ▶ {_w}")

        # P1-4: 每波前确认（仅 CLI 非 TUI 模式，step_confirm=True）
        if step_confirm and not _interrupted.is_set():
            _wave_titles = [f"{st['id']} ({st.get('title', '?')})" for st in wave]
            console.force(f"\n⏸ Wave {wave_num} — 准备执行: {', '.join(_wave_titles)}")
            try:
                _resp = input("  [Enter]继续  [s]跳过本波  [q]暂停: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _resp = "q"
            if _resp == "q":
                console.force("⏹ 用户选择暂停")
                _interrupted.set()
                break
            elif _resp == "s":
                logger.info(f"[skip] 用户跳过 Wave {wave_num}")
                for st in wave:
                    if st["id"] not in results_map:
                        results_map[st["id"]] = {
                            "subtask_id": st["id"], "status": "no_changes",
                            "exit_code": 0, "summary": "用户跳过",
                            "worktree": "", "sandbox_type": "headless",
                            "verify_ok": False, "duration_sec": 0,
                        }
                    completed_ids.add(st["id"])
                remaining = [st for st in remaining if st["id"] not in completed_ids]
                console.emit("wave_complete", {"wave_idx": wave_num, "skipped": True})
                wave_num += 1
                continue

        if actual_workers == 1:
            for st in wave:
                upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                try:
                    result = _invoke_run_subtask(task_id, st, repo, task_dir, logger, upstream, headless,
                                                 issue_ref, active_pids, active_pids_lock,
                                                 config.get("_metering_path", ""), config, _interrupted)
                except Exception as e:
                    # 异常隔离（与并发分支对齐）：内部 bug 崩溃不击穿整个进程，
                    # 记为 failed + kill_reason=system_error（区别于能力失败）
                    result = {"subtask_id": st["id"], "status": "failed",
                              "exit_code": -1, "summary": f"system_error: {e}",
                              "failure_reason": f"内部异常: {type(e).__name__}: {e}",
                              "kill_reason": "system_error",
                              "worktree": "", "sandbox_type": "headless",
                              "verify_ok": False, "duration_sec": 0}
                    logger.error(f"串行异常 {st['id']}: {type(e).__name__}: {e}")
                degraded_count = _record_subtask_result(
                    st, result, task_dir, meta,
                    worktree_map, results_map, completed_ids, failed_ids,
                    degraded_count, meta_lock, config,
                )
        else:
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {}
                for st in wave:
                    upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                    fut = executor.submit(_invoke_run_subtask, task_id, st, repo, task_dir, logger, upstream,
                                          headless, issue_ref, active_pids, active_pids_lock,
                                          config.get("_metering_path", ""), config, _interrupted)
                    futures[fut] = st
                for fut in as_completed(futures):
                    st = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        # 异常隔离（与串行分支对齐）：内部 bug 崩溃不击穿进程，
                        # 标 kill_reason=system_error 区别于能力失败
                        result = {"subtask_id": st["id"], "status": "failed",
                                  "exit_code": -1, "summary": f"system_error: {e}",
                                  "failure_reason": f"内部异常: {type(e).__name__}: {e}",
                                  "kill_reason": "system_error",
                                  "worktree": "",
                                  "sandbox_type": "headless", "verify_ok": False, "duration_sec": 0}
                        logger.error(f"并发异常 {st['id']}: {type(e).__name__}: {e}")
                    degraded_count = _record_subtask_result(
                        st, result, task_dir, meta,
                        worktree_map, results_map, completed_ids, failed_ids,
                        degraded_count, meta_lock, config,
                    )
                    # 若中断已触发，取消剩余 futures，加速退出
                    if _interrupted.is_set():
                        remaining_futs = [f for f in futures if not f.done()]
                        for f in remaining_futs:
                            f.cancel()
                        break

        # P1-4: 波次完成汇总（人类可读 + 结构化事件）
        _wave_done = sum(1 for st in wave if st["id"] in completed_ids)
        _wave_fail = sum(1 for st in wave if st["id"] in failed_ids or st["id"] in blocked_ids)
        console.emit("wave_complete", {
            "wave_idx": wave_num, "total_waves": total_waves,
            "done": _wave_done, "failed": _wave_fail,
        })
        if _wave_fail:
            console.print(f"  ⚠️ Wave {wave_num + 1} 完成: {_wave_done - _wave_fail} ✅ / {_wave_fail} ❌")
        elif _wave_done:
            console.print(f"  ✅ Wave {wave_num + 1} 完成: {_wave_done} 个子任务")
        console.sep("─", 50)

        # ── 中断检测：信号处理器已触发，安全地保存状态并退出 ──
        # M0 语义修复：中断暂停写 PAUSED（可恢复锚点），不再是 PLAN_REVIEW（规划审查门）。
        # 能力失败优先（m0-state-machine.md §状态定义）：中断时若已有 failed 子任务（确定性
        # 能力失败，非被中断打断），终态为 VERIFICATION_FAILED 而非 PAUSED——PAUSED 暗示
        # "恢复后能继续"，但能力失败恢复后大概率仍失败，PAUSED 会误导用户。
        if _interrupted.is_set():
            _has_failed = bool(failed_ids)
            if meta.get("status_schema_version"):
                meta["status"] = "VERIFICATION_FAILED" if _has_failed else "PAUSED"
            else:
                meta["status"] = "failed" if _has_failed else "paused"
            (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            if _has_failed:
                logger.info(f"任务中断且已有失败子任务 ({len(completed_ids)}/{total})，终态 VERIFICATION_FAILED（能力失败优先）")
            else:
                logger.info(f"任务已暂停 ({len(completed_ids)}/{total})，可通过 agent_go resume {task_id} 恢复")
            _stop_heartbeat(task_dir, _heartbeat_stop)
            if signals_installed:
                signal.signal(signal.SIGINT, prev_sigint)
                signal.signal(signal.SIGTERM, prev_sigterm)
            # 恢复 gc.auto
            if gc_disabled and original_gc_value is not None:
                _, _, _ = _set_gc_auto(repo, original_gc_value)
            _stop_mcp_pool()
            _release_task_lock()
            sys.exit(0)

        remaining = [st for st in remaining if st["id"] not in completed_ids]
        wave_num += 1

    if signals_installed:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    # ── 远程推送 worktree 分支（可选）──
    if remote_url and (repo / ".git").exists():
        logger.info(f"[remote] 推送 worktree 分支到 {remote_url}")
        push_errors = 0
        for st in confirmed:
            branch = f"agent_go/{task_id}/{st['id']}"
            # 检查分支是否存在（可能 worktree 创建失败走了 clone 降级）
            branch_check = subprocess.run(
                ["git", "branch", "--list", branch],
                cwd=str(repo), capture_output=True, text=True)
            if branch_check.stdout.strip():
                push_result = subprocess.run(
                    ["git", "push", remote_url, f"{branch}:{branch}"],
                    cwd=str(repo), capture_output=True)
                if push_result.returncode == 0:
                    logger.info(f"[remote] pushed: {branch}")
                else:
                    push_errors += 1
                    logger.warning(f"[remote] 推送失败 {branch}: {push_result.stderr.strip()[:200]}")
        if push_errors == 0:
            logger.info(f"[remote] 所有分支推送成功")
        else:
            logger.warning(f"[remote] {push_errors} 个分支推送失败")

    # ── S9-B 产物导出：清理 worktree 前扫描 __artifacts__/ 收集到 artifact_dir ──
    # 声明制：只有写入 __artifacts__/ 的文件视为交付物；不指定 --artifact-dir 时跳过（向后兼容）
    export_result: Optional[dict[str, Any]] = None
    _artifact_dir = config.get("artifact_dir") if config else None
    if _artifact_dir:
        try:
            from .artifacts import export as _export_artifacts
            export_result = _export_artifacts(task_id, results_map, _artifact_dir, task_dir)
        except Exception as _art_err:
            logger.warning(f"[artifacts] 产物导出失败（不中断任务）: {_art_err}")

    # ── Worktree 清理（跳过保留的 worktree） ──
    preserved_ids: list[str] = []
    if (repo / ".git").exists():
        errors = 0
        for st in confirmed:
            r = results_map.get(st["id"])
            sid = st["id"]
            wt_path = task_dir / sid / "work"
            if not wt_path.exists():
                continue

            # 判定是否保留此 worktree。
            # S12 改进（失败清理策略）：保留判定从"只看 status"升级为结合 kill_reason 与降级标记。
            #  - cleanup_race（S12-P0 已修正为实际成功）不再保留 → 避免浪费
            #  - over_budget_l2/l3、stuck、hard_timeout → 保留（真失败，需审查）
            #  - degraded=True（S12-P2 降级产物）→ 强制保留（最需重点审查）
            should_preserve = False
            _kill_reason = r.get("kill_reason") if r else None
            _is_degraded = bool(r.get("degraded")) if r else False
            if preserve_worktrees is True:
                should_preserve = True
            elif preserve_worktrees is None:
                if r and r.get("status") in ("failed", "blocked"):
                    if _kill_reason == "cleanup_race":
                        # 实际已成功的收尾竞态 → 不保留（结果已修正为通过）
                        logger.info(f"[worktree] {sid} kill_reason=cleanup_race（实际成功），不保留")
                    else:
                        should_preserve = True
                # degraded 降级产物无论 status 都强制保留（需重点审查）
                if _is_degraded:
                    should_preserve = True

            if should_preserve:
                preserved_ids.append(sid)
                # 写入标记文件，供 agent_go inspect 识别
                marker = task_dir / sid / ".preserved"
                _marker_data: dict[str, Any] = {
                    "subtask_id": sid,
                    "status": r.get("status", "unknown") if r else "unknown",
                    "failure_reason": r.get("failure_reason", "") if r else "",
                    "kill_reason": _kill_reason or "",
                    "degraded": _is_degraded,
                    "branch": f"agent_go/{meta.get('task_id', '')}/{sid}",
                }
                marker.write_text(json.dumps(_marker_data, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
                _preserve_reason = "degraded(需审查)" if _is_degraded else (r.get("status", "?") if r else "?")
                if _kill_reason:
                    _preserve_reason += f"/{_kill_reason}"
                logger.info(f"[worktree] 保留 {sid} 供人工审查 ({_preserve_reason})")
                # S12 失败清理 #2：净化保留现场（移除运行时缓存，避免污染审查现场）
                _sanitize_preserved_worktree(wt_path)
                continue

            ok, err = _worktree_remove(repo, wt_path)
            if ok:
                logger.info(f"[worktree] removed: {sid}")
            else:
                errors += 1
                logger.warning(f"[worktree] 无法移除 {sid}: {err}")

        ok_prune, err_prune = _worktree_prune(repo)
        if not ok_prune:
            logger.warning(f"[worktree] prune 失败: {err_prune}")

        if preserved_ids:
            logger.info(f"[worktree] 共保留 {len(preserved_ids)} 个 worktree: {', '.join(preserved_ids)}")
        logger.info(f"[worktree] cleanup ({errors} errors)")

        # ── Tag 清理 ──
        tag_errors = 0
        for st in confirmed:
            tag_name = f"{task_id}/{st['id']}"
            tag_result = subprocess.run(
                ["git", "tag", "-d", tag_name],
                cwd=str(repo), capture_output=True)
            if tag_result.returncode == 0:
                logger.debug(f"[tag] deleted: {tag_name}")
            else:
                tag_errors += 1
                logger.debug(f"[tag] 删除失败 {tag_name}: {tag_result.stderr.strip()[:100]}")
        if tag_errors:
            logger.warning(f"[tag] {tag_errors} 个 tag 删除失败")
        else:
            logger.info(f"[tag] 任务 tags 已清理")

        if gc_disabled and original_gc_value is not None:
            _, _, _ = _set_gc_auto(repo, original_gc_value)

    # 正常结束：回收 MCP 池（中断退出已在上面 sys.exit 前处理）
    _stop_mcp_pool()

    # 心跳收尾：清除存活信号（状态将改为 completed/failed，不再需要判活）
    _stop_heartbeat(task_dir, _heartbeat_stop)
    _release_task_lock()

    # 收集所有结果并写回 meta.json（完整版本，含 results 数组）
    meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
    # 完成边界一致性检查：commit 是唯一完成边界（architecture.md）。
    # 标记 completed 但 commit_hash 为空且 failure_reason 非空（如"Git 提交或 tag 失败"）
    # 的是"假 completed"——验证可能通过了但完成边界没成功，应修正为 failed。
    for _res in results_map.values():
        if (_res.get("status") == "completed" and not _res.get("commit_hash")
                and _res.get("failure_reason")):
            _res["status"] = "failed"
            logger.warning(
                f"[完成边界] sub-{_res.get('subtask_id')} 标记 completed 但无 commit_hash "
                f"且 failure_reason 非空（{_res.get('failure_reason','')[:40]}），修正为 failed"
            )
    has_failed = any(r.get("status") in ("failed", "blocked") for r in results_map.values())
    has_blocked = any(r.get("status") == "blocked" for r in results_map.values())
    # 能力失败优先（M0 修复）：有 failed 子任务（含其级联 blocked 下游）→ VERIFICATION_FAILED；
    # BLOCKED 仅保留给纯约束阻断（cost/metering/依赖环，无 failed 子任务）。
    # 级联 blocked（有 blocked_by，因上游 failed 而阻断）也算能力失败——即便上游后来被
    # 改成 completed（如假 completed），blocked 下游的存在证明执行过程有能力失败痕迹。
    has_capability_failure = any(
        r.get("status") == "failed" or (r.get("status") == "blocked" and r.get("blocked_by"))
        for r in results_map.values()
    )
    if meta.get("status_schema_version"):
        meta["status"] = "VERIFICATION_FAILED" if has_capability_failure else ("BLOCKED" if has_blocked else "DELIVERY_READY")
    else:
        meta["status"] = "failed" if has_failed else "completed"
    from .delivery import apply_delivery_result
    from .failure import aggregate_failure_class
    delivery = apply_delivery_result(meta, repo)
    meta["failure_class"] = aggregate_failure_class(
        [r.get("failure_class") for r in results_map.values()], meta
    )
    if meta.get("status_schema_version") and not has_failed and delivery["accepted_delivery"]:
        meta["status"] = "ACCEPTED_DELIVERY"
    elif meta.get("status_schema_version") and not has_failed and delivery["delivery_failed"]:
        meta["status"] = "DELIVERY_FAILED"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    _total_time = sum(r.get("duration_sec", 0) for r in results_map.values())
    _total_cost = sum(r.get("claude_cost", 0) for r in results_map.values() if r.get("claude_cost"))
    console.emit("pipeline_complete", {
        "task_id": task_id,
        "status": meta["status"],
        "total_subtasks": total,
        "completed": len(completed_ids),
        "failed": sum(1 for r in results_map.values() if r.get("status") == "failed"),
        "blocked": sum(1 for r in results_map.values() if r.get("status") == "blocked"),
        "duration_sec": round(_total_time, 2),
        "cost_usd": round(_total_cost, 4) if _total_cost else 0.0,
    })

    blocked_count = sum(1 for r in results_map.values() if r.get("status") == "blocked")
    summary_icon = "❌" if has_failed else "🎉"

    # ── P1-3 增强报告 ──────────────────────────────────────
    console.sep()
    console.title(f"{summary_icon} 全部完成 ({len(completed_ids)}/{total})")
    if has_blocked:
        console.print(f"🔗 级联阻断: {blocked_count} 个子任务因上游失败被阻断")

    # 总体统计
    _total_time = sum(r.get("duration_sec", 0) for r in results_map.values())
    _total_cost = sum(r.get("claude_cost", 0) for r in results_map.values() if r.get("claude_cost"))
    _total_changed = sum(r.get("change_stats", {}).get("files_changed", 0) for r in results_map.values())
    _total_insertions = sum(r.get("change_stats", {}).get("insertions", 0) for r in results_map.values())
    _total_deletions = sum(r.get("change_stats", {}).get("deletions", 0) for r in results_map.values())

    console.subtitle("汇总")
    console.print(f"  子任务: {len(completed_ids)}/{total}  总耗时: {_total_time:.0f}s  总变更: +{_total_insertions}/-{_total_deletions} ({_total_changed} 文件)  ", end="")
    if _total_cost:
        console.print(f" 总成本: ${_total_cost:.4f}")
    else:
        console.print("")

    # 逐子任务详情表
    console.subtitle("子任务明细")
    _tbl_headers = ["id", "状态", "耗时", "变更", "摘要"]
    _tbl_rows: list[list[str]] = []
    for s in confirmed:
        r = results_map.get(s["id"])
        if not r:
            _tbl_rows.append([s["id"], "⏳", "-", "-", "未执行"])
            continue
        _icon = {"completed": "✅", "no_changes": "⏭️", "failed": "❌", "blocked": "🔗"}.get(r["status"], "❓")
        _dur = f"{r.get('duration_sec', 0):.0f}s" if r.get("duration_sec") else "-"
        _cs = r.get("change_stats", {})
        if _cs:
            _chg = f"+{_cs.get('insertions', 0)}/-{_cs.get('deletions', 0)} ({_cs.get('files_changed', 0)}f)"
        else:
            _chg = "-"
        _tbl_rows.append([s["id"], _icon, _dur, _chg, r["summary"]])
    if _tbl_rows:
        console.table(_tbl_headers, _tbl_rows)

    # ── 级联阻断摘要 ──
    if has_blocked:
        console.subtitle("🔗 级联阻断详情")
        for s in confirmed:
            r = results_map.get(s["id"])
            if r and r.get("status") == "blocked":
                console.print(f"  {r['subtask_id']}: {r.get('failure_reason', '上游失败')}")

    # ── 失败原因摘要 ──
    if has_failed:
        console.subtitle("❌ 失败原因摘要")
        for s in confirmed:
            r = results_map.get(s["id"])
            if r and r.get("status") == "failed" and r.get("failure_reason"):
                console.print(f"  {r['subtask_id']}: {r['failure_reason']}")

    # ── 验证质量警告 ──
    weak_verify = [
        r for r in results_map.values()
        if r.get("verification_confidence", {}).get("warning")
        and r.get("status") == "completed"
    ]
    if weak_verify:
        console.subtitle("⚠️  验证质量警告（可能存在假阳性）")
        for r in weak_verify:
            vc = r.get("verification_confidence", {})
            console.print(f"  {r['subtask_id']}: {vc['warning']} ({vc['level']})")

    console.print(f"\n📁 {task_dir}")
    console.print(f"📝 {task_dir}/execution.log")

    # ── 保留 worktree 提示 ──
    if preserved_ids:
        console.print("\n🔍 以下 worktree 已保留供人工审查:")
        console.print("─" * 60)
        for sid in preserved_ids:
            r = results_map.get(sid, {})
            icon = {"failed": "❌", "blocked": "🔗"}.get(r.get("status", ""), "❓")
            wt_path = task_dir / sid / "work"
            branch = f"agent_go/{meta.get('task_id', '')}/{sid}"
            # S12 失败清理 #4：degraded 降级产物突出"需 review"标记；kill_reason 展示
            _degraded_flag = " ⚠️ 降级产物（需重点 review）" if r.get("degraded") else ""
            _kr = r.get("kill_reason") or ""
            _kr_suffix = f" | kill_reason={_kr}" if _kr else ""
            console.print(f"  {icon} {sid}: {r.get('failure_reason', '?')}{_degraded_flag}{_kr_suffix}")
            console.print(f"     📁 {wt_path}")
            console.print(f"     🔗 git branch: {branch}")
        console.print("─" * 60)
        console.print("  使用 agent_go inspect <task-id> 查看详情")
        console.print(f"  或直接 cd 到对应目录查看")

    # ── S9-B 产物导出清单（与保留 worktree 清单并列） ──
    if export_result:
        try:
            from .artifacts import render_export_summary as _render_export_summary
            _summary = _render_export_summary(export_result)
            if _summary:
                console.print(_summary)
        except Exception as _render_err:
            logger.debug(f"[artifacts] 导出清单渲染失败（忽略）: {_render_err}")

    # ── P0-5 失败恢复闭环引导 ──
    # 失败后给出可复制执行的完整操作路径，避免用户手动拼接 task_id
    task_id_str = meta.get("task_id", task_dir.name)
    if has_failed or has_blocked:
        console.sep("=", 68)
        console.title("🔧 失败恢复指引")
        console.print("推荐操作（可直接复制执行）:")
        if preserved_ids:
            console.print(f"  📋 查看失败现场:     agent_go inspect {task_id_str}")
        if has_failed:
            console.print(f"  📝 审查已完成部分:   agent_go review --task {task_id_str}")
            console.print(f"  🔄 修复后继续执行:   agent_go resume {task_id_str}")
            console.print(f"  🚫 不阻断下游重试:   agent_go resume {task_id_str} --no-verify-block")
        if has_blocked:
            console.print(f"  🚫 跳过阻断重试:     agent_go resume {task_id_str} --no-verify-block")
        console.sep("=", 68)

    # ── P1-3 后续操作卡片 ──
    task_id_str = meta.get("task_id", task_dir.name)
    console.print("\n📋 后续操作:")
    console.print(f"  📋 审查变更     agent_go review --task {task_id_str} --deep")
    console.print(f"  ✅ 审查+批准    agent_go review --task {task_id_str} --deep --approve")
    console.print(f"  🔀 创建 PR      agent_go pr {task_id_str} --push")
    console.print(f"  🔄 恢复执行     agent_go resume {task_id_str}")

    # ── 任务完成通知（M1） ──
    # 事件优先级：on_blocked > on_failed > on_complete（一次管线只派发一个事件）
    event = "on_blocked" if has_blocked else "on_failed" if has_failed else "on_complete"
    # 解耦：动态 import + try/except——notify 是可选增强，失败不中断
    try:
        from .notify import notify_event
        notify_event(event, {"meta": meta, "results_map": results_map, "task_dir": task_dir}, config)
    except Exception as e:
        logger.warning(f"notify 加载/调用失败，跳过任务完成通知（不中断）: {e}")


def _run_pipeline(*args, **kwargs) -> None:
    """运行 pipeline，并为非预期异常提供最后一道资源清理兜底。"""
    config = args[4] if len(args) > 4 else kwargs.get("config", {})
    task_dir = args[2] if len(args) > 2 else kwargs.get("task_dir")
    logger = args[3] if len(args) > 3 else kwargs.get("logger", logging.getLogger(__name__))
    try:
        return _run_pipeline_impl(*args, **kwargs)
    except BaseException:
        try:
            state = config.get("_pipeline_signal_state", (False, None, None))
            if state[0] and threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, state[1])
                signal.signal(signal.SIGTERM, state[2])
            gc_state = config.get("_pipeline_gc_state")
            if gc_state and gc_state[1] and gc_state[2] is not None:
                _set_gc_auto(gc_state[0], gc_state[2])
            pool = config.get("_mcp_pool")
            if pool is not None:
                pool.stop_all()
            if task_dir is not None:
                (Path(task_dir) / "heartbeat").unlink(missing_ok=True)
        except Exception:
            logger.debug("pipeline 异常兜底清理失败", exc_info=True)
        raise
