"""Unified output abstraction layer.

Replaces scattered print() calls with a Console that respects
--quiet (headless/CI) and --verbose (debug) modes.

Usage:
    from agent_go.console import Console
    console = Console(quiet=False, verbose=False)
    console.success("Task completed")
    console.warning("Skill not found")
    console.error("Path not found")
    console.sep()
"""

from __future__ import annotations

import sys
from typing import Any


class Console:
    """Unified output abstraction.

    Respects --quiet (suppress output) and --verbose (debug info) flags.
    In quiet mode, only force() bypasses suppression (for interactive prompts).
    """

    def __init__(self, quiet: bool = False, verbose: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose

    # ── Raw output ──────────────────────────────────────────────

    def force(self, *args: Any, **kwargs: Any) -> None:
        """Always print — bypasses quiet mode. For interactive prompts."""
        print(*args, **kwargs)

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Drop-in replacement for print(). Respects quiet mode."""
        if not self.quiet:
            print(*args, **kwargs)

    # ── Semantic methods ────────────────────────────────────────

    def info(self, msg: str) -> None:
        """Plain informational message."""
        if not self.quiet:
            print(msg)

    def success(self, msg: str) -> None:
        """✅ Success message."""
        if not self.quiet:
            print(f"✅ {msg}")

    def warning(self, msg: str) -> None:
        """⚠️  Warning message."""
        if not self.quiet:
            print(f"⚠️  {msg}")

    def error(self, msg: str) -> None:
        """❌ Error message."""
        if not self.quiet:
            print(f"❌ {msg}")

    def debug(self, msg: str) -> None:
        """🔍 Debug message — only shown in verbose mode."""
        if self.verbose and not self.quiet:
            print(f"🔍 {msg}")

    # ── Layout helpers ──────────────────────────────────────────

    def sep(self, char: str = "─", width: int = 50) -> None:
        """Horizontal separator line."""
        if not self.quiet:
            print(char * width)

    def title(self, msg: str) -> None:
        """Section title with decorative separators."""
        if not self.quiet:
            print(f"\n{'=' * 60}")
            print(f"  {msg}")
            print(f"{'=' * 60}")

    def subtitle(self, msg: str) -> None:
        """Sub-section header."""
        if not self.quiet:
            print(f"\n── {msg} ──")

    # ── Structured output ───────────────────────────────────────

    def table(self, headers: list[str], rows: list[list[str]], col_widths: list[int] | None = None) -> None:
        """Print a formatted table.

        Args:
            headers: Column header strings.
            rows: List of row lists (each cell is a string).
            col_widths: Optional explicit column widths; auto-calculated if None.
        """
        if self.quiet:
            return
        if not col_widths:
            col_widths = [max(len(str(row[i])) if i < len(row) else 0 for row in [headers] + rows) + 2 for i in range(len(headers))]
        header_line = "".join(f"{h:<{w}}" for h, w in zip(headers, col_widths))
        self.print(header_line)
        self.sep(width=sum(col_widths))
        for row in rows:
            row_line = "".join(f"{str(cell):<{w}}" for cell, w in zip(row, col_widths))
            self.print(row_line)

    def data(self, data: Any) -> None:
        """Pretty-print structured data (JSON, dict, etc.)."""
        import json as _json
        if not self.quiet:
            print(_json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def data_table(self, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
        """Print a list of dicts as a table.

        Args:
            rows: List of dicts with consistent keys.
            columns: Ordered column keys; uses all keys from first row if None.
        """
        if self.quiet or not rows:
            return
        if columns is None:
            columns = list(rows[0].keys())
        headers = columns
        data_rows = [[str(row.get(c, ""))[:60] for c in columns] for row in rows]
        self.table(headers, data_rows)


# ── Module-level default instance ───────────────────────────────
# Imported by modules that don't receive a Console via dependency injection.
# The default is non-quiet; cmd_run() replaces it with a configured instance.

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
