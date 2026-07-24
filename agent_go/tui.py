import json
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

__all__ = ["cmd_status_tui"]

def _read_metering_cost(task_dir: Path) -> float:
    """从 metering.jsonl 汇总 cost_usd（轻量版，TUI 用）。"""
    metering_path = task_dir / "metering.jsonl"
    if not metering_path.exists():
        return 0.0
    total = 0.0
    try:
        for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                ev = json.loads(line)
                total += ev.get("cost_usd", 0.0) or 0.0
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return round(total, 6)


def _get_task_status(task_dir: Path) -> Optional[dict[str, Any]]:
    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    status = meta.get("status", "unknown")
    log_path = task_dir / "execution.log"

    if status == "running" and log_path.exists():
        if time.time() - log_path.stat().st_mtime > 600:
            status = "failed"

    results = meta.get("results", [])
    completed = sum(1 for r in results if r.get("status") in ("completed", "no_changes", "degraded"))
    failed = sum(1 for r in results if r.get("status") == "failed")
    blocked = sum(1 for r in results if r.get("status") == "blocked")
    retried_success = sum(1 for r in results if r.get("retry_count", 0) > 0 and r.get("status") == "completed")
    total = len(meta.get("subtasks", []))
    current = ""
    for line in reversed(log_path.read_text(encoding="utf-8").strip().split("\n")[-10:]) if log_path.exists() else []:
        if "subtask_start" in line:
            try:
                current = json.loads(line.split(" | ")[-1]).get("title", "")
            except (json.JSONDecodeError, IndexError, KeyError):
                # TUI log parsing — malformed lines are expected
                logger.debug("Failed to parse subtask title from log line")
            break

    elapsed = ""
    created = meta.get("created", "")
    if created:
        try:
            # created 格式为 "20260725-030125-545"（带毫秒后缀），剥离后解析
            created_clean = created.rsplit("-", 1)[0] if created.count("-") == 2 else created
            start = datetime.strptime(created_clean, "%Y%m%d-%H%M%S")
            end = datetime.now() if status == "running" else datetime.fromtimestamp(log_path.stat().st_mtime) if log_path.exists() else datetime.now()
            delta = end - start
            elapsed = f"{int(delta.total_seconds() // 60)}m{int(delta.total_seconds() % 60)}s"
        except ValueError:
            # TUI timestamp parsing — invalid format expected in some entries
            logger.debug("Failed to parse elapsed time from created timestamp")

    cost = _read_metering_cost(task_dir)
    # $/pass rate = 总成本 / 成功完成的子任务数
    completed_count = sum(1 for r in results if r.get("status") == "completed")
    dollar_per_pass = round(cost / completed_count, 4) if completed_count > 0 else None

    return {
        "id": task_dir.name, "status": status, "task": meta.get("task", "?")[:50],
        "progress": f"{completed}/{total}" if total > 0 else "-",
        "current": current, "elapsed": elapsed,
        "results": results, "subtasks": meta.get("subtasks", []),
        "failed": failed, "blocked": blocked, "retried_success": retried_success,
        "cost_usd": cost, "dollar_per_pass": dollar_per_pass,
        "completed_count": completed_count,
    }


def _get_tail_lines(log_path: Path, count: int = 10) -> list[str]:
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    tail = lines[-30:]
    return [l.split(" | ")[-1][:100] for l in tail if "|" in l][-count:]


STATUS_COLORS = {"completed": 2, "no_changes": 2, "degraded": 3, "running": 3, "failed": 1, "paused": 3, "aborted": 1, "blocked": 5}
ICONS = {"completed": "ok", "no_changes": "--", "degraded": "~", "running": "> ", "failed": "!!", "paused": "||", "aborted": "x ", "blocked": "##"}

# 验证置信度缩写
VC_ABBR = {"deterministic": "det", "heuristic": "heur", "manual": "man", "none": "--"}


def tui_main(stdscr: Any) -> None:
    import curses
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    for i, c in enumerate([curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_CYAN, curses.COLOR_WHITE], 1):
        curses.init_pair(i, c, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)

    stdscr.nodelay(True)
    stdscr.timeout(500)
    selected_idx = 0
    expanded_tasks = set()
    filter_mode = 0
    detail_idx = 0  # 详情面板子任务轮播索引

    while True:
        tasks_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        rows = [r for r in (_get_task_status(td) for td in tasks_dirs) if r]
        if filter_mode == 1:
            rows = [r for r in rows if r["status"] == "running"]
        elif filter_mode == 2:
            rows = [r for r in rows if r["status"] == "completed"]
        elif filter_mode == 3:
            rows = [r for r in rows if r["status"] == "failed"]

        max_y, max_x = stdscr.getmaxyx()
        if max_y < 8 or max_x < 50:
            key = stdscr.getch()
            if key == ord('q'):
                break
            time.sleep(0.5)
            continue

        if selected_idx >= len(rows) and rows:
            selected_idx = len(rows) - 1

        stdscr.erase()

        # Header
        _safe_addstr(stdscr, 0, 0, " agent_go Status  [q]退出 [j/k]选择 [Enter]展开 [←/→]子任务 [1-4]过滤 ".ljust(max_x - 1), curses.color_pair(6))

        list_w = min(max_x - 42, 60)
        detail_x = list_w + 1

        # Task list
        line_y = 2
        for i, row in enumerate(rows):
            if line_y >= max_y - 4:
                break
            is_sel = (i == selected_idx)
            color = STATUS_COLORS.get(row["status"], 5)
            icon = ICONS.get(row["status"], "?")
            prefix = ">" if is_sel else " "
            task_line = f"{prefix}{icon} {row['id'][:20]} {row['progress']:>5} {row['elapsed']:>6}"
            attr = curses.color_pair(color) | (curses.A_REVERSE if is_sel else 0)
            _safe_addstr(stdscr, line_y, 1, task_line[:list_w - 2], attr)
            line_y += 1

            if row["id"] in expanded_tasks:
                for sr in row.get("results", []):
                    if line_y >= max_y - 4:
                        break
                    sid = sr.get("subtask_id", "?")
                    sstat = sr.get("status", "?")
                    scolor = STATUS_COLORS.get(sstat, 5)
                    sicon = ICONS.get(sstat, "?")
                    dur = f"{sr.get('duration_sec', 0):.0f}s"
                    src = sr.get("agent_type_source", "?")[:4]
                    rc = sr.get("retry_count", 0)
                    vc = sr.get("verification_confidence", {}).get("level", "none")
                    vc_short = VC_ABBR.get(vc, "?")
                    sub_line = f"   {sicon} {sid} {src:>4} {dur:>5} r:{rc} {vc_short}"
                    _safe_addstr(stdscr, line_y, 3, sub_line[:list_w - 4], curses.color_pair(scolor))
                    line_y += 1

        # Detail panel — 任务级摘要 + 子任务轮播
        sel = rows[selected_idx] if rows and selected_idx < len(rows) else None
        if sel:
            res = sel.get("results", [])
            # 子任务轮播索引边界
            if detail_idx >= len(res):
                detail_idx = max(0, len(res) - 1)
            _safe_addstr(stdscr, 2, detail_x, f"{sel['id'][:24]} {sel['task'][:24]}", curses.color_pair(4))

            # 任务级摘要
            summary_y = 3
            _safe_addstr(stdscr, summary_y, detail_x + 1,
                         f"状态: {sel['status']:<10} 进度: {sel['progress']}", curses.color_pair(STATUS_COLORS.get(sel['status'], 5)))
            _safe_addstr(stdscr, summary_y + 1, detail_x + 1,
                         f"耗时: {sel['elapsed']:<8} 成本: ${sel.get('cost_usd', 0):.4f}")
            _safe_addstr(stdscr, summary_y + 2, detail_x + 1,
                         f"ok {sel.get('completed_count', 0)} | fail {sel.get('failed', 0)} | "
                         f"blocked {sel.get('blocked', 0)} | retry成功 {sel.get('retried_success', 0)}")
            dpp = sel.get('dollar_per_pass')
            dpp_str = f"${dpp}" if dpp is not None else "N/A"
            _safe_addstr(stdscr, summary_y + 3, detail_x + 1,
                         f"★ $/pass: {dpp_str}", curses.color_pair(2))

            # 子任务轮播详情
            sub_detail_y = summary_y + 5
            if res:
                sr = res[detail_idx]
                idx_label = f"[{detail_idx + 1}/{len(res)}]"
                _safe_addstr(stdscr, sub_detail_y, detail_x,
                             f"{idx_label} {sr.get('subtask_id', '?')}", curses.color_pair(3))
                vc = sr.get("verification_confidence", {}).get("level", "none")
                dl = [
                    f"status: {sr.get('status', '?')}",
                    f"duration: {sr.get('duration_sec', 0):.0f}s",
                    f"retry: {sr.get('retry_count', 0)}",
                    f"verify: {'ok' if sr.get('verify_ok') else 'fail'} ({VC_ABBR.get(vc, '?')})",
                ]
                cs = sr.get("change_stats")
                if cs:
                    dl.append(f"files: {cs.get('files_changed', 0)} +{cs.get('insertions', 0)}/-{cs.get('deletions', 0)}")
                fr = sr.get("failure_reason")
                if fr:
                    dl.append(f"原因: {fr[:40]}")
                for j, d in enumerate(dl):
                    _safe_addstr(stdscr, sub_detail_y + 1 + j, detail_x + 1, d[:max_x - detail_x - 3])

            # Log panel
            log_y = sub_detail_y + 8
            log_path = AGENT_GO_DIR / sel["id"] / "execution.log"
            tail = _get_tail_lines(log_path, max(3, max_y - log_y - 2))
            _safe_addstr(stdscr, log_y, detail_x, "--- Log ---", curses.color_pair(5))
            for k, tl in enumerate(tail):
                _safe_addstr(stdscr, log_y + 1 + k, detail_x + 1, tl[:max_x - detail_x - 2])

        # Status bar
        running = sum(1 for r in rows if r["status"] == "running")
        done = sum(1 for r in rows if r["status"] == "completed")
        fail = sum(1 for r in rows if r["status"] == "failed")
        bar = f" {len(rows)} tasks | {running} run | {done} done | {fail} fail | [1]all [2]run [3]done [4]fail | [←/→]子任务 "
        _safe_addstr(stdscr, max_y - 1, 0, bar[:max_x - 1], curses.color_pair(6))

        stdscr.refresh()
        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == ord('j') or key == curses.KEY_DOWN:
            selected_idx = min(selected_idx + 1, len(rows) - 1) if rows else 0
        elif key == ord('k') or key == curses.KEY_UP:
            selected_idx = max(selected_idx - 1, 0)
        elif key == 10 and rows:
            tid = rows[selected_idx]["id"]
            expanded_tasks.symmetric_difference_update({tid})
        elif key in (ord('l'), curses.KEY_RIGHT) and rows:
            sel = rows[selected_idx] if selected_idx < len(rows) else None
            if sel and sel.get("results"):
                detail_idx = min(detail_idx + 1, len(sel["results"]) - 1)
        elif key in (ord('h'), curses.KEY_LEFT) and rows:
            detail_idx = max(detail_idx - 1, 0)
        elif key in (ord('1'), ord('2'), ord('3'), ord('4')):
            # 状态栏提示 [1]all [2]run [3]done [4]fail → filter_mode 0/1/2/3
            filter_mode = {ord('1'): 0, ord('2'): 1, ord('3'): 2, ord('4'): 3}[key]


def _safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, text, attr)
    except Exception:
        # curses addstr throws on boundary/resize — intentionally silent
        pass


def cmd_status_tui() -> None:
    import curses
    try:
        curses.wrapper(tui_main)
    except KeyboardInterrupt:
        pass
