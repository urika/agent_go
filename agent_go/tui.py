"""Curses 状态仪表盘：实时显示并发子任务进度、日志尾随和成本累计。

通过 `agent_go status --watch` 启动，轮询 ~/.agent_go/ 下活跃任务的
meta.json / metering.jsonl / execution.log，渲染 TUI 界面。
"""
import json
import logging
import time
import subprocess as _subprocess
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

__all__ = ["cmd_status_tui"]


class LogTailer:
    """Efficient log file tailer using seek/tell. O(1) per poll after first read."""

    def __init__(self, path: Path, max_lines: int = 500):
        self.path = path
        self.max_lines = max_lines
        self._fp: Any = None
        self._pos = 0
        self._all_lines: list[str] = []

    def close(self) -> None:
        if self._fp:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    def poll(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            cur_size = self.path.stat().st_size
        except OSError:
            return []
        if self._fp is None:
            try:
                self._fp = open(self.path, "r", encoding="utf-8", errors="replace")
            except OSError:
                return []
            self._pos = self._fp.seek(0, 2)
            return []
        if cur_size < self._pos:
            self._pos = 0
            self._fp.seek(0)
            self._all_lines = []
        if cur_size == self._pos:
            return []
        try:
            self._fp.seek(self._pos)
            new_lines = self._fp.readlines()
            self._pos = self._fp.tell()
        except OSError:
            return []
        stripped = [ln.rstrip("\n\r") for ln in new_lines]
        self._all_lines.extend(stripped)
        if len(self._all_lines) > self.max_lines:
            self._all_lines = self._all_lines[-self.max_lines:]
        return stripped

    def get_all(self) -> list[str]:
        return self._all_lines


def _get_tail_lines(log_path: Path, count: int = 10) -> list[str]:
    """Legacy compatibility — simple read without LogTailer."""
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    tail = lines[-30:]
    return [ln.split(" | ")[-1][:100] for ln in tail if "|" in ln][-count:]


def _read_metering_cost(task_dir: Path) -> float:
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
                logger.debug("Failed to parse subtask title from log line")
            break

    elapsed = ""
    created = meta.get("created", "")
    if created:
        try:
            created_clean = created.rsplit("-", 1)[0] if created.count("-") == 2 else created
            start = datetime.strptime(created_clean, "%Y%m%d-%H%M%S")
            end = datetime.now() if status == "running" else (
                datetime.fromtimestamp(log_path.stat().st_mtime) if log_path.exists() else datetime.now())
            delta = end - start
            elapsed = f"{int(delta.total_seconds() // 60)}m{int(delta.total_seconds() % 60)}s"
        except ValueError:
            logger.debug("Failed to parse elapsed time from created timestamp")

    cost = _read_metering_cost(task_dir)
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


STATUS_COLORS = {"completed": 2, "no_changes": 2, "degraded": 3, "running": 3,
                 "failed": 1, "paused": 3, "aborted": 1, "blocked": 5}
ICONS = {"completed": "ok", "no_changes": "--", "degraded": "~", "running": "> ",
         "failed": "!!", "paused": "||", "aborted": "x ", "blocked": "##"}
VC_ABBR = {"deterministic": "det", "heuristic": "heur", "manual": "man", "none": "--"}


def _shorten_log_line(raw: str, max_w: int) -> str:
    """Shorten a log line for TUI display. Extract key info from JSON lines."""
    if "{" in raw:
        try:
            ev = json.loads(raw.split("{", 1)[0].rsplit(" | ", 1)[-1] + "{" + raw.split("{", 1)[1]) if "{" in raw else raw.split(" | ")[-1] if " | " in raw else raw
            return ev.get("event", raw)[:max_w]
        except (json.JSONDecodeError, IndexError):
            pass
    text = raw.split(" | ")[-1] if " | " in raw else raw
    if len(text) > max_w:
        text = text[:max_w - 3] + "..."
    return text


def tui_main(stdscr: Any, task_filter: Optional[str] = None) -> None:
    import curses
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    for i, c in enumerate([curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                           curses.COLOR_CYAN, curses.COLOR_WHITE], 1):
        curses.init_pair(i, c, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)

    stdscr.nodelay(True)
    stdscr.timeout(500)
    selected_idx = 0
    expanded_tasks = set()
    filter_mode = 0
    detail_idx = 0

    # P3-2: Log tailer per task + scroll state
    log_tailers: dict[str, LogTailer] = {}
    log_scroll = 0
    auto_scroll = True
    focus: str = "list"  # "list" | "log"

    while True:
        tasks_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        if task_filter:
            tasks_dirs = [td for td in tasks_dirs if td.name == task_filter]
        if not tasks_dirs:
            if task_filter:
                _safe_addstr(stdscr, 2, 2, f"任务 {task_filter} 不存在或已清理。按 q 退出。")
            else:
                _safe_addstr(stdscr, 2, 2, "暂无任务。按 q 退出。")
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord("q"):
                break
            time.sleep(0.5)
            continue

        rows = [r for r in (_get_task_status(td) for td in tasks_dirs) if r]

        if task_filter and rows:
            _ts = rows[0]["status"]
            if _ts in ("completed", "failed", "aborted"):
                _safe_addstr(stdscr, 2, 2, f"任务 {task_filter}: {_ts}",
                             curses.color_pair(2) if _ts == "completed" else curses.color_pair(1))
                stdscr.refresh()
                time.sleep(3)
                break

        if filter_mode == 1:
            rows = [r for r in rows if r["status"] == "running"]
        elif filter_mode == 2:
            rows = [r for r in rows if r["status"] == "completed"]
        elif filter_mode == 3:
            rows = [r for r in rows if r["status"] == "failed"]

        max_y, max_x = stdscr.getmaxyx()
        if max_y < 8 or max_x < 50:
            key = stdscr.getch()
            if key == ord("q"):
                break
            time.sleep(0.5)
            continue

        if selected_idx >= len(rows) and rows:
            selected_idx = len(rows) - 1

        stdscr.erase()

        # ── P3-2/3: Header with focus indicator ──
        _mode_label = " 执行模式 " if task_filter else " agent_go Status "
        focus_indicator = " [Tab:日志]" if focus == "list" else " [Tab:列表]"
        header = f" {_mode_label} [q]退出 [j/k]选择 [Enter]展开 [←/→]子任务 {focus_indicator}".ljust(max_x - 1)
        _safe_addstr(stdscr, 0, 0, header, curses.color_pair(6))

        list_w = min(max_x - 42, 60)
        detail_x = list_w + 1

        # ── Layout: log panel takes bottom ~35% ──
        log_height = max(3, (max_y - 2) * 35 // 100)
        list_height = max_y - 2 - log_height

        # ── Task list (left column, top section) ──
        line_y = 2
        for i, row in enumerate(rows):
            if line_y >= list_height:
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
                    if line_y >= list_height:
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

        # ── Detail panel (right column) ──
        sel = rows[selected_idx] if rows and selected_idx < len(rows) else None
        if sel:
            res = sel.get("results", [])
            if detail_idx >= len(res):
                detail_idx = max(0, len(res) - 1)
            _safe_addstr(stdscr, 2, detail_x, f"{sel['id'][:24]} {sel['task'][:24]}", curses.color_pair(4))

            summary_y = 3
            _safe_addstr(stdscr, summary_y, detail_x + 1,
                         f"状态: {sel['status']:<10} 进度: {sel['progress']}",
                         curses.color_pair(STATUS_COLORS.get(sel['status'], 5)))
            _safe_addstr(stdscr, summary_y + 1, detail_x + 1,
                         f"耗时: {sel['elapsed']:<8} 成本: ${sel.get('cost_usd', 0):.4f}")
            _safe_addstr(stdscr, summary_y + 2, detail_x + 1,
                         f"ok {sel.get('completed_count', 0)} | fail {sel.get('failed', 0)} | "
                         f"blocked {sel.get('blocked', 0)} | retry成功 {sel.get('retried_success', 0)}")
            dpp = sel.get("dollar_per_pass")
            dpp_str = f"${dpp}" if dpp is not None else "N/A"
            _safe_addstr(stdscr, summary_y + 3, detail_x + 1,
                         f"★ $/pass: {dpp_str}", curses.color_pair(2))

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

        # ── P3-2: Log panel (bottom, full width) ──
        log_y = max_y - log_height
        _safe_addstr(stdscr, log_y - 1, 1, "─── Log ───", curses.color_pair(5))

        if sel:
            tid = sel["id"]
            log_path = AGENT_GO_DIR / tid / "execution.log"

            # Initialize tailer on first encounter
            if tid not in log_tailers:
                log_tailers[tid] = LogTailer(log_path)

            tailer = log_tailers[tid]
            new_lines = tailer.poll()
            if new_lines and auto_scroll:
                log_scroll = 0  # Stay at bottom when auto-scrolling

            all_lines = tailer.get_all()
            max_visible = log_height - 1

            # Clamp scroll to valid range
            if auto_scroll:
                log_scroll = 0
            else:
                max_scroll = max(0, len(all_lines) - max_visible)
                if log_scroll > max_scroll:
                    log_scroll = max_scroll

            visible_lines = all_lines[max(0, len(all_lines) - max_visible - log_scroll):len(all_lines) - log_scroll] if all_lines else []
            visible_lines = visible_lines[:max_visible]

            for k, ln in enumerate(visible_lines):
                if log_y + k >= max_y - 1:
                    break
                display = _shorten_log_line(ln, max_x - 4)
                if focus == "log":
                    attr = curses.A_REVERSE if k == log_scroll % max_visible else 0
                else:
                    attr = 0
                _safe_addstr(stdscr, log_y + k, 1, display[:max_x - 2], attr)

        # ── P3-3: Status bar with shortcuts ──
        running = sum(1 for r in rows if r["status"] == "running")
        done = sum(1 for r in rows if r["status"] == "completed")
        fail = sum(1 for r in rows if r["status"] == "failed")
        focus_label = " [日志]" if focus == "log" else " [列表]"
        bar = f" {_mode_label.strip()}{focus_label} | {len(rows)} tasks | {running} run | {done} done | {fail} fail"
        if not task_filter:
            bar += " | [1]all [2]run [3]done [4]fail"
        bar += " | [Tab]焦点 [←/→]子任务 "
        if task_filter:
            bar += " [r]重试失败 "
        _safe_addstr(stdscr, max_y - 1, 0, bar[:max_x - 1], curses.color_pair(6))

        stdscr.refresh()

        # ── P3-3: Key handling ──
        key = stdscr.getch()

        # Global keys
        if key == ord("q"):
            break
        elif key == 9:  # Tab — toggle focus
            focus = "log" if focus == "list" else "list"
        elif key == ord(" ") and focus == "log":
            auto_scroll = not auto_scroll

        # List-focus keys
        elif focus == "list":
            if key == ord("j") or key == curses.KEY_DOWN:
                selected_idx = min(selected_idx + 1, len(rows) - 1) if rows else 0
            elif key == ord("k") or key == curses.KEY_UP:
                selected_idx = max(selected_idx - 1, 0)
            elif key == 10 and rows:
                tid = rows[selected_idx]["id"]
                expanded_tasks.symmetric_difference_update({tid})
            elif key in (ord("l"), curses.KEY_RIGHT) and rows:
                _sel = rows[selected_idx] if selected_idx < len(rows) else None
                if _sel and _sel.get("results"):
                    detail_idx = min(detail_idx + 1, len(_sel["results"]) - 1)
            elif key in (ord("h"), curses.KEY_LEFT) and rows:
                detail_idx = max(detail_idx - 1, 0)
            elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
                filter_mode = {ord("1"): 0, ord("2"): 1, ord("3"): 2, ord("4"): 3}[key]

        # Log-focus keys
        elif focus == "log":
            if key == ord("j") or key == curses.KEY_DOWN:
                auto_scroll = False
                log_scroll = max(log_scroll - 1, 0)
            elif key == ord("k") or key == curses.KEY_UP:
                auto_scroll = False
                all_count = len(log_tailers.get(rows[selected_idx]["id"], LogTailer(Path("/dev/null"))).get_all()) if rows else 0
                max_scroll = max(0, all_count - (log_height - 1))
                log_scroll = min(log_scroll + 1, max_scroll)
            elif key == ord("G") or key == ord("g"):
                auto_scroll = True
                log_scroll = 0

        # P3-3: Retry key (exec mode only)
        if key == ord("r") and task_filter and rows:
            sel = rows[selected_idx] if selected_idx < len(rows) else None
            if sel:
                _failed_subs = [r for r in sel.get("results", []) if r.get("status") == "failed"]
                if _failed_subs:
                    try:
                        curses.endwin()
                        _subprocess.run(
                            ["agent_go", "resume", sel["id"], "--yes", "--headless"],
                            capture_output=False)
                        # Re-enter curses
                        curses.doupdate()
                    except Exception:
                        pass


def _safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, text, attr)
    except Exception:
        pass


def cmd_status_tui(task_filter: Optional[str] = None) -> None:
    import curses
    try:
        curses.wrapper(lambda stdscr: tui_main(stdscr, task_filter=task_filter))
    except KeyboardInterrupt:
        pass
