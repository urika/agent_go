import sys, os, subprocess, json, threading, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional


from .console import get_default_console
from .executor import run_subtask
from .git_utils import _set_gc_auto, _worktree_remove, _worktree_prune
from .notify import notify_event

logger = logging.getLogger(__name__)

__all__: list[str] = []


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
    """记录 subtask 结果到共享状态、写 result.json、标记完成、失败时通知。

    串行和并发路径共用此函数，避免双分支代码不一致导致的缩进/语义错误。
    返回更新后的 degraded_count（int 不可变，需传值返回）。
    """
    with meta_lock:
        worktree_map[st["id"]] = task_dir / st["id"] / "work"
        results_map[st["id"]] = result
        if result.get("status") == "degraded":
            degraded_count += 1
        # 每个 subtask 独立写 result.json，减少全量覆写
        result_file = task_dir / st["id"] / "result.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
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


def _run_pipeline(confirmed: list[dict[str, Any]], repo: Path, task_dir: Path, logger: logging.Logger, config: dict[str, Any], headless: bool, parallel: int, issue_ref: str, meta: dict[str, Any],
                  worktree_map: Optional[dict[str, Path]] = None, results_map: Optional[dict[str, dict[str, Any]]] = None, completed_ids: Optional[set] = None, remote_url: str = "",
                  preserve_worktrees: Optional[bool] = None) -> None:
    """执行管线：拓扑排序 + 并发/串行执行。恢复模式下传入已有状态。

    preserve_worktrees:
      None  → 默认行为：保留 failed/blocked 的 worktree，其余清理
      True  → 保留所有 worktree
      False → 强制清理所有 worktree
    """
    worktree_map = worktree_map or {}
    console = get_default_console()
    results_map = results_map or {}
    completed_ids = completed_ids or set()
    # M6: 追踪失败和阻断的子任务 ID，下游依赖失败的上游时自动阻断
    failed_ids: set = {r["subtask_id"] for r in results_map.values() if r.get("status") == "failed"}
    blocked_ids: set = {r["subtask_id"] for r in results_map.values() if r.get("status") == "blocked"}
    task_id = meta["task_id"]
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

    # ── 中断标志（信号处理器中仅设置此标志，不执行 I/O） ──
    _interrupted = threading.Event()

    # 注册中断信号处理
    def _on_interrupt(signum: int, frame: Any) -> None:
        _interrupted.set()
        # 立即 kill 子进程（这是 async-signal-safe 的）
        with active_pids_lock:
            pids_to_kill = list(active_pids)
        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    prev_sigint = signal.signal(signal.SIGINT, _on_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _on_interrupt)

    # 跳过已完成的子任务
    remaining = [st for st in confirmed if st["id"] not in completed_ids]
    if not remaining:
        console.print("所有子任务已完成，无需恢复执行")
        meta["status"] = "completed"
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        # 恢复信号处理器与 gc.auto（与其他退出路径一致，避免仓库 config 残留 gc.auto=0）
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)
        if gc_disabled and original_gc_value is not None:
            _, _, _ = _set_gc_auto(repo, original_gc_value)
        return

    wave_num = 0
    if parallel > 1 and total > 1:
        logger.info(f"[并发] max_workers={parallel}, 拓扑调度，剩余 {len(remaining)} 个子任务")

    while remaining:
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

        if actual_workers == 1:
            for st in wave:
                upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                result = run_subtask(task_id, st, repo, task_dir, logger, upstream, headless=headless, issue_ref=issue_ref, active_pids=active_pids, active_pids_lock=active_pids_lock, metering_path=config.get("_metering_path", ""), config=config)
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
                    fut = executor.submit(run_subtask, task_id, st, repo, task_dir, logger, upstream, headless, issue_ref, active_pids, active_pids_lock, metering_path=config.get("_metering_path", ""), config=config)
                    futures[fut] = st
                for fut in as_completed(futures):
                    st = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = {"subtask_id": st["id"], "status": "failed",
                                  "exit_code": -1, "summary": str(e), "worktree": "",
                                  "sandbox_type": "headless", "verify_ok": False, "duration_sec": 0}
                        logger.error(f"并发异常 {st['id']}: {e}")
                    degraded_count = _record_subtask_result(
                        st, result, task_dir, meta,
                        worktree_map, results_map, completed_ids, failed_ids,
                        degraded_count, meta_lock, config,
                    )

        # ── 中断检测：信号处理器已触发，安全地保存状态并退出 ──
        if _interrupted.is_set():
            meta["status"] = "paused"
            (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"任务已暂停 ({len(completed_ids)}/{total})，可通过 agent_go resume {task_id} 恢复")
            signal.signal(signal.SIGINT, prev_sigint)
            signal.signal(signal.SIGTERM, prev_sigterm)
            # 恢复 gc.auto
            if gc_disabled and original_gc_value is not None:
                _, _, _ = _set_gc_auto(repo, original_gc_value)
            sys.exit(0)

        remaining = [st for st in remaining if st["id"] not in completed_ids]
        wave_num += 1

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

            # 判定是否保留此 worktree
            should_preserve = False
            if preserve_worktrees is True:
                should_preserve = True
            elif preserve_worktrees is None:
                if r and r.get("status") in ("failed", "blocked"):
                    should_preserve = True

            if should_preserve:
                preserved_ids.append(sid)
                # 写入标记文件，供 agent_go inspect 识别
                marker = task_dir / sid / ".preserved"
                marker.write_text(json.dumps({
                    "subtask_id": sid,
                    "status": r.get("status", "unknown") if r else "unknown",
                    "failure_reason": r.get("failure_reason", "") if r else "",
                    "branch": f"agent_go/{meta.get('task_id', '')}/{sid}",
                }, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info(f"[worktree] 保留 {sid} 供人工审查 ({r.get('status', '?') if r else '?'})")
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

    # 收集所有结果并写回 meta.json（完整版本，含 results 数组）
    meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
    has_failed = any(r.get("status") in ("failed", "blocked") for r in results_map.values())
    has_blocked = any(r.get("status") == "blocked" for r in results_map.values())
    meta["status"] = "failed" if has_failed else "completed"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    blocked_count = sum(1 for r in results_map.values() if r.get("status") == "blocked")
    summary_icon = "❌" if has_failed else "🎉"
    console.print(f"\n{'='*60}\n{summary_icon} 全部完成 ({len(completed_ids)}/{total})")
    if has_blocked:
        console.print(f"🔗 级联阻断: {blocked_count} 个子任务因上游失败被阻断")
    console.print(f"{'='*60}")
    console.print("\n📦 最终报告")
    console.print("─" * 60)
    for s in confirmed:
        r = results_map.get(s["id"])
        if r:
            icon = {"completed": "✅", "no_changes": "⏭️", "failed": "❌", "blocked": "🔗"}.get(r["status"], "❓")
            console.print(f"{icon} {r['subtask_id']}: {r['summary']}")
        else:
            console.print(f"⏳ {s['id']}: 未执行")
    console.print("─" * 60)

    # ── 级联阻断摘要（M6） ──
    if has_blocked:
        console.print("\n🔗 级联阻断详情")
        console.print("─" * 60)
        for s in confirmed:
            r = results_map.get(s["id"])
            if r and r.get("status") == "blocked":
                console.print(f"  {r['subtask_id']}: {r.get('failure_reason', '上游失败')}")
        console.print("─" * 60)

    # ── 失败原因摘要（M2） ──
    if has_failed:
        console.print("\n❌ 失败原因摘要")
        console.print("─" * 60)
        for s in confirmed:
            r = results_map.get(s["id"])
            if r and r.get("status") == "failed" and r.get("failure_reason"):
                console.print(f"  {r['subtask_id']}: {r['failure_reason']}")
        console.print("─" * 60)

    # ── 验证质量警告（M5） ──
    weak_verify = [
        r for r in results_map.values()
        if r.get("verification_confidence", {}).get("warning")
        and r.get("status") == "completed"
    ]
    if weak_verify:
        console.print("\n⚠️  验证质量警告（可能存在假阳性）")
        console.print("─" * 60)
        for r in weak_verify:
            vc = r.get("verification_confidence", {})
            console.print(f"  {r['subtask_id']}: {vc['warning']} ({vc['level']})")
        console.print("─" * 60)

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
            console.print(f"  {icon} {sid}: {r.get('failure_reason', '?')}")
            console.print(f"     📁 {wt_path}")
            console.print(f"     🔗 git branch: {branch}")
        console.print("─" * 60)
        console.print("  使用 agent_go inspect <task-id> 查看详情")
        console.print(f"  或直接 cd 到对应目录查看")

    # ── 任务完成通知（M1） ──
    # 事件优先级：on_blocked > on_failed > on_complete（一次管线只派发一个事件）
    event = "on_blocked" if has_blocked else "on_failed" if has_failed else "on_complete"
    notify_event(event, {"meta": meta, "results_map": results_map, "task_dir": task_dir}, config)
