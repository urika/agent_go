import json
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

__all__ = ["cmd_status_tui"]

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
            start = datetime.strptime(created, "%Y%m%d-%H%M%S")
            end = datetime.now() if status == "running" else datetime.fromtimestamp(log_path.stat().st_mtime) if log_path.exists() else datetime.now()
            delta = end - start
            elapsed = f"{int(delta.total_seconds() // 60)}m{int(delta.total_seconds() % 60)}s"
        except ValueError:
            # TUI timestamp parsing — invalid format expected in some entries
            logger.debug("Failed to parse elapsed time from created timestamp")

    return {
        "id": task_dir.name, "status": status, "task": meta.get("task", "?")[:50],
        "progress": f"{completed}/{total}" if total > 0 else "-",
        "current": current, "elapsed": elapsed,
        "results": results, "subtasks": meta.get("subtasks", []),
    }


def _get_tail_lines(log_path: Path, count: int = 10) -> list[str]:
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    tail = lines[-30:]
    return [l.split(" | ")[-1][:100] for l in tail if "|" in l][-count:]


STATUS_COLORS = {"completed": 2, "no_changes": 2, "degraded": 3, "running": 3, "failed": 1, "paused": 3, "aborted": 1}
ICONS = {"completed": "ok", "no_changes": "--", "degraded": "~", "running": "> ", "failed": "!!", "paused": "||", "aborted": "x "}


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
        _safe_addstr(stdscr, 0, 0, " agent_go Status  [q]退出 [j/k]选择 [Enter]展开 [1-4]过滤 [r]刷新 ".ljust(max_x - 1), curses.color_pair(6))

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
                    sub_line = f"   {sicon} {sid} {src:>4} {dur:>5}"
                    _safe_addstr(stdscr, line_y, 3, sub_line[:list_w - 4], curses.color_pair(scolor))
                    line_y += 1

        # Detail panel
        sel = rows[selected_idx] if rows and selected_idx < len(rows) else None
        if sel:
            res = sel.get("results", [{}])
            sr = res[0] if res else {}
            _safe_addstr(stdscr, 2, detail_x, f"[{sr.get('subtask_id','?')}] {sel['task'][:30]}", curses.color_pair(4))
            dl = [f"status: {sr.get('status','?')}", f"duration: {sr.get('duration_sec',0)}s",
                  f"retry: {sr.get('retry_count',0)}", f"verify: {'ok' if sr.get('verify_ok') else 'fail'}"]
            cs = sr.get("change_stats")
            if cs:
                dl.append(f"files: {cs.get('files_changed',0)} +{cs.get('insertions',0)}/-{cs.get('deletions',0)}")
            for j, d in enumerate(dl):
                _safe_addstr(stdscr, 4 + j, detail_x + 1, d)

            # Log panel
            log_path = AGENT_GO_DIR / sel["id"] / "execution.log"
            tail = _get_tail_lines(log_path, 6)
            log_y = 10
            _safe_addstr(stdscr, log_y, detail_x, "--- Log ---", curses.color_pair(5))
            for k, tl in enumerate(tail):
                _safe_addstr(stdscr, log_y + 1 + k, detail_x + 1, tl[:max_x - detail_x - 2])

        # Status bar
        running = sum(1 for r in rows if r["status"] == "running")
        done = sum(1 for r in rows if r["status"] == "completed")
        fail = sum(1 for r in rows if r["status"] == "failed")
        bar = f" {len(rows)} tasks | {running} run | {done} done | {fail} fail | [1]all [2]run [3]done [4]fail "
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
