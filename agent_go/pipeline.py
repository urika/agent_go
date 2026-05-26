import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .executor import run_subtask
from .git_utils import _set_gc_auto, _worktree_remove, _worktree_prune

def _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta,
                  worktree_map=None, results_map=None, completed_ids=None):
    """执行管线：拓扑排序 + 并发/串行执行。恢复模式下传入已有状态。"""
    worktree_map = worktree_map or {}
    results_map = results_map or {}
    completed_ids = completed_ids or set()
    task_id = meta["task_id"]
    meta_lock = threading.Lock()
    degraded_count = sum(1 for r in results_map.values() if r.get("status") in ("no_changes", "degraded"))
    total = len(confirmed)

    # 禁用 git gc.auto — worktree 并发操作共享对象库时避免竞态
    original_gc_value = None
    gc_disabled = False
    if (repo / ".git").exists():
        original_gc_value, ok = _set_gc_auto(repo, "0")
        if ok:
            gc_disabled = True
            logger.info(f"[worktree] gc.auto 已禁用 (原值: {original_gc_value})")

    # 注册中断信号处理
    def _on_interrupt(signum, frame):
        meta["status"] = "paused"
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"任务已暂停 ({len(completed_ids)}/{total})，可通过 agent_go resume {task_id} 恢复")
        sys.exit(0)
    prev_sigint = signal.signal(signal.SIGINT, _on_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _on_interrupt)

    # 跳过已完成的子任务
    remaining = [st for st in confirmed if st["id"] not in completed_ids]
    if not remaining:
        print("所有子任务已完成，无需恢复执行")
        meta["status"] = "completed"
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return

    wave_num = 0
    if parallel > 1 and total > 1:
        logger.info(f"[并发] max_workers={parallel}, 拓扑调度，剩余 {len(remaining)} 个子任务")

    while remaining:
        wave = [st for st in remaining
                if all(dep in completed_ids for dep in st.get("depends_on", []))]
        if not wave:
            logger.error("依赖循环或无法满足的依赖！")
            break

        logger.info(f"[Wave {wave_num}] {', '.join(st['id'] for st in wave)}")
        actual_workers = min(parallel, len(wave)) if parallel > 1 else 1

        if actual_workers == 1:
            for st in wave:
                upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                result = run_subtask(task_id, st, repo, task_dir, logger, upstream, headless=headless, issue_ref=issue_ref)
                with meta_lock:
                    worktree_map[st["id"]] = task_dir / st["id"] / "work"
                    results_map[st["id"]] = result
                    if result.get("status") == "degraded":
                        degraded_count += 1
                    meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
                    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                completed_ids.add(st["id"])
        else:
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {}
                for st in wave:
                    upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                    fut = executor.submit(run_subtask, task_id, st, repo, task_dir, logger, upstream, headless, issue_ref)
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
                        meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
                        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                    completed_ids.add(st["id"])

        remaining = [st for st in remaining if st["id"] not in completed_ids]
        wave_num += 1

    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)

    # ── Worktree 清理 ──
    if (repo / ".git").exists():
        errors = 0
        for st in confirmed:
            wt_path = task_dir / st["id"] / "work"
            if wt_path.exists():
                if _worktree_remove(repo, wt_path):
                    logger.info(f"[worktree] removed: {st['id']}")
                else:
                    errors += 1
                    logger.warning(f"[worktree] 无法移除: {st['id']}")
        _worktree_prune(repo)
        logger.info(f"[worktree] cleanup ({errors} errors)")
        if gc_disabled and original_gc_value is not None:
            _set_gc_auto(repo, original_gc_value)

    # 如果有子任务 failed 则整体为 failed，否则为 completed
    has_failed = any(r.get("status") == "failed" for r in results_map.values())
    meta["status"] = "failed" if has_failed else "completed"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'='*60}\n🎉 全部完成 ({len(completed_ids)}/{total})\n{'='*60}")
    print("\n📦 最终报告")
    print("─" * 60)
    for s in confirmed:
        r = results_map.get(s["id"])
        if r:
            icon = {"completed": "✅", "no_changes": "⏭️", "failed": "❌"}.get(r["status"], "❓")
            print(f"{icon} {r['subtask_id']}: {r['summary']}")
        else:
            print(f"⏳ {s['id']}: 未执行")
    print("─" * 60)
    print(f"\n📁 {task_dir}")
    print(f"📝 {task_dir}/execution.log")
