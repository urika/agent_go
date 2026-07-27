"""Unified output abstraction layer.

Replaces scattered print() calls with a Console that respects
--quiet (headless/CI), --verbose (debug), and --json (JSON Lines) modes.

Usage:
    from agent_go.console import Console, set_default_console, _LazyConsole
    console = Console(quiet=False, verbose=False, json_mode=False)
    console.success("Task completed")
    console.warning("Skill not found")
    console.error("Path not found")
    console.sep()
"""

from __future__ import annotations

import sys
import time as _time
from typing import Any


class Console:
    """Unified output abstraction.

    Three modes:
    - default:  human-readable terminal output
    - quiet:    suppresses non-critical output (--quiet)
    - json:     JSON Lines to stdout, interactive prompts to stderr (--json)
    """

    def __init__(self, quiet: bool = False, verbose: bool = False, json_mode: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.json_mode = json_mode

    # ── Internal ────────────────────────────────────────────────

    def _json_emit(self, event: str, level: str, data: dict[str, Any]) -> None:
        """Write a single JSON Line to stdout."""
        import json as _json
        payload = {
            "event": event,
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "data": data,
        }
        print(_json.dumps(payload, default=str, ensure_ascii=False))

    # ── Raw output ──────────────────────────────────────────────

    def force(self, *args: Any, **kwargs: Any) -> None:
        """Always print. In JSON mode routes to stderr (interactive prompts)."""
        if self.json_mode:
            print(*args, **kwargs, file=sys.stderr)
        else:
            print(*args, **kwargs)

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Drop-in replacement for print(). Respects quiet and json modes."""
        if self.json_mode:
            msg = " ".join(str(a) for a in args)
            self._json_emit("log", "log", {"message": msg})
        elif not self.quiet:
            print(*args, **kwargs)

    # ── Semantic methods ────────────────────────────────────────

    def info(self, msg: str) -> None:
        """Plain informational message."""
        if self.json_mode:
            self._json_emit("info", "info", {"message": msg})
        elif not self.quiet:
            print(msg)

    def success(self, msg: str) -> None:
        """Success message."""
        if self.json_mode:
            self._json_emit("success", "success", {"message": msg})
        elif not self.quiet:
            print(f"✅ {msg}")

    def warning(self, msg: str) -> None:
        """Warning message."""
        if self.json_mode:
            self._json_emit("warning", "warning", {"message": msg})
        elif not self.quiet:
            print(f"⚠️  {msg}")

    def error(self, msg: str) -> None:
        """Error message."""
        if self.json_mode:
            self._json_emit("error", "error", {"message": msg})
        elif not self.quiet:
            print(f"❌ {msg}")

    def debug(self, msg: str) -> None:
        """Debug message — only shown in verbose mode."""
        if self.json_mode:
            self._json_emit("debug", "debug", {"message": msg})
        elif self.verbose and not self.quiet:
            print(f"🔍 {msg}")

    # ── Layout helpers ──────────────────────────────────────────

    def sep(self, char: str = "─", width: int = 50) -> None:
        """Horizontal separator line. Suppressed in JSON mode."""
        if not self.json_mode and not self.quiet:
            print(char * width)

    def title(self, msg: str) -> None:
        """Section title. Suppressed in JSON mode (layout, not data)."""
        if not self.json_mode and not self.quiet:
            print(f"\n{'=' * 60}")
            print(f"  {msg}")
            print(f"{'=' * 60}")

    def subtitle(self, msg: str) -> None:
        """Sub-section header. Suppressed in JSON mode."""
        if not self.json_mode and not self.quiet:
            print(f"\n── {msg} ──")

    # ── Structured output ───────────────────────────────────────

    def table(self, headers: list[str], rows: list[list[str]],
              col_widths: list[int] | None = None) -> None:
        """Print a formatted table or JSON table event."""
        if self.quiet and not self.json_mode:
            return
        if self.json_mode:
            self._json_emit("table", "info", {
                "headers": headers, "rows": rows,
            })
            return
        if not col_widths:
            col_widths = [
                max(len(str(row[i])) if i < len(row) else 0
                    for row in [headers] + rows) + 2
                for i in range(len(headers))
            ]
        header_line = "".join(f"{h:<{w}}" for h, w in zip(headers, col_widths))
        self.print(header_line)
        self.sep(width=sum(col_widths))
        for row in rows:
            row_line = "".join(f"{str(cell):<{w}}" for cell, w in zip(row, col_widths))
            self.print(row_line)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Emit a structured machine-readable lifecycle event.

        In json_mode:  emitted as JSON Lines event with level="event".
        In human mode: emitted as "[event] {data}" on stderr so it doesn't
                       pollute stdout pipelines but is still visible.

        These events enable the MCP server to track progress in real-time
        without polling meta.json.
        """
        if self.json_mode:
            self._json_emit(event, "event", data)
        elif not self.quiet:
            import json as _json
            print(f"[{event}] {_json.dumps(data, default=str)}", file=sys.stderr)

    def data(self, data: Any) -> None:
        """Pretty-print structured data (JSON). In JSON mode emits as event."""
        import json as _json
        if self.json_mode:
            self._json_emit("data", "info", {"data": data})
        elif not self.quiet:
            print(_json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def data_table(self, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
        """Print a list of dicts as a table."""
        if self.quiet or not rows:
            return
        if columns is None:
            columns = list(rows[0].keys())
        if self.json_mode and not self.quiet:
            self._json_emit("table", "info", {"headers": columns, "rows": rows})
            return
        headers = columns
        data_rows = [[str(row.get(c, ""))[:60] for c in columns] for row in rows]
        self.table(headers, data_rows)


# ── Module-level default instance ───────────────────────────────

_default_console = Console()


def set_default_console(console: Console) -> None:
    """Replace the module-level default Console instance."""
    global _default_console
    _default_console = console


def get_default_console() -> Console:
    """Get the current module-level Console instance."""
    return _default_console


class _LazyConsole:
    """Proxy resolving to the current default Console on every attribute access.

    Modules that bind a console at import time should use
    `console = _LazyConsole()` instead of `console = get_default_console()`,
    so a later `set_default_console()` (e.g. cmd_run applying quiet mode)
    takes effect for their output.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_default_console(), name)
