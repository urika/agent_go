import sys, os, subprocess, json, threading, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional


from .console import get_default_console
from .executor import run_subtask
from .git_utils import _set_gc_auto, _worktree_remove, _worktree_prune

logger = logging.getLogger(__name__)

__all__: list[str] = []

def _run_pipeline(confirmed: list[dict[str, Any]], repo: Path, task_dir: Path, logger: logging.Logger, config: dict[str, Any], headless: bool, parallel: int, issue_ref: str, meta: dict[str, Any],
                  worktree_map: Optional[dict[str, Path]] = None, results_map: Optional[dict[str, dict[str, Any]]] = None, completed_ids: Optional[set] = None, remote_url: str = "") -> None:
    """执行管线：拓扑排序 + 并发/串行执行。恢复模式下传入已有状态。"""
    worktree_map = worktree_map or {}
    console = get_default_console()
    results_map = results_map or {}
    completed_ids = completed_ids or set()
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
        wave = [st for st in remaining
                if all(dep in completed_ids for dep in st.get("depends_on", []))]
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
                result = run_subtask(task_id, st, repo, task_dir, logger, upstream, headless=headless, issue_ref=issue_ref, active_pids=active_pids, active_pids_lock=active_pids_lock)
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
        else:
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {}
                for st in wave:
                    upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                    fut = executor.submit(run_subtask, task_id, st, repo, task_dir, logger, upstream, headless, issue_ref, active_pids, active_pids_lock)
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
                    with meta_lock:
                        worktree_map[st["id"]] = task_dir / st["id"] / "work"
                        results_map[st["id"]] = result
                        if result.get("status") == "degraded":
                            degraded_count += 1
                        # 每个 subtask 独立写 result.json
                        result_file = task_dir / st["id"] / "result.json"
                        result_file.parent.mkdir(parents=True, exist_ok=True)
                        result_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                    completed_ids.add(st["id"])

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

    # ── Worktree 清理 ──
    if (repo / ".git").exists():
        errors = 0
        for st in confirmed:
            wt_path = task_dir / st["id"] / "work"
            if wt_path.exists():
                ok, err = _worktree_remove(repo, wt_path)
                if ok:
                    logger.info(f"[worktree] removed: {st['id']}")
                else:
                    errors += 1
                    logger.warning(f"[worktree] 无法移除 {st['id']}: {err}")
        ok_prune, err_prune = _worktree_prune(repo)
        if not ok_prune:
            logger.warning(f"[worktree] prune 失败: {err_prune}")
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
    has_failed = any(r.get("status") == "failed" for r in results_map.values())
    meta["status"] = "failed" if has_failed else "completed"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    console.print(f"\n{'='*60}\n🎉 全部完成 ({len(completed_ids)}/{total})\n{'='*60}")
    console.print("\n📦 最终报告")
    console.print("─" * 60)
    for s in confirmed:
        r = results_map.get(s["id"])
        if r:
            icon = {"completed": "✅", "no_changes": "⏭️", "failed": "❌"}.get(r["status"], "❓")
            console.print(f"{icon} {r['subtask_id']}: {r['summary']}")
        else:
            console.print(f"⏳ {s['id']}: 未执行")
    console.print("─" * 60)
    console.print(f"\n📁 {task_dir}")
    console.print(f"📝 {task_dir}/execution.log")
