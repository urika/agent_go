"""AST-based static checks for suspicious Python code patterns.

Detects:
- For loops with suspiciously small bodies (≤2 statements) where subsequent
  code at the same indentation level references loop-local variables — a common
  indentation error pattern (code accidentally placed outside the loop body).

Zero external dependencies — uses only stdlib ``ast``.
"""

import ast
from pathlib import Path
from typing import Optional, Union

_MAX_SMALL_BODY = 1
# A variable leaking outside a tiny for-loop is suspicious only when consumed
# by ≥2 distinct subsequent siblings — a single use is usually a legit
# accumulator/checker pattern; the bug pattern has the loop variable read in
# 2+ sibling statements (e.g. try + with, or with + function-call).
_MIN_SIBLING_USES = 2


def check_path(path: Union[str, Path]) -> list[dict]:
    """Run all checks on a Python file or directory.

    Returns a list of report dicts with keys:
        file (str): source file path
        line (int): line number
        message (str): description of the issue
        severity (str): ``"warning"`` or ``"error"``
    """
    p = Path(path)
    if p.is_file():
        return _check_file(p)
    reports: list[dict] = []
    for pyfile in sorted(p.rglob("*.py")):
        if pyfile.name == "__init__.py":
            continue
        reports.extend(_check_file(pyfile))
    return reports


def _check_file(filepath: Path) -> list[dict]:
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except SyntaxError:
        return []
    return _check_suspicious_for_loops(tree, str(filepath))


# ── AST helpers ──────────────────────────────────────────────────────────────

def _collect_assigned(node: ast.AST, names: set[str]) -> None:
    """Collect variable names that are assigned (stored) in *node*."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        names.add(node.id)
    for child in ast.iter_child_nodes(node):
        _collect_assigned(child, names)


def _collect_used(node: ast.AST, names: set[str]) -> None:
    """Collect variable names that are referenced (loaded) in *node*."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        names.add(node.id)
    for child in ast.iter_child_nodes(node):
        _collect_used(child, names)


def _find_containing_list(parent: ast.AST, node: ast.AST) -> Optional[list]:
    """Find which list attribute in *parent* contains *node*.

    Checks ``body``, ``orelse``, ``finalbody``, and ``ExceptHandler.body``.
    """
    if isinstance(parent, ast.ExceptHandler):
        if node in parent.body:
            return parent.body
    for attr in ("body", "orelse", "finalbody"):
        lst = getattr(parent, attr, None)
        if isinstance(lst, list) and node in lst:
            return lst
    return None


# ── Checkers ─────────────────────────────────────────────────────────────────

def _check_suspicious_for_loops(tree: ast.AST, filepath: str) -> list[dict]:
    """Find for-loops with a tiny body where subsequent sibling statements
    reference loop-local variables — a common indentation error pattern."""
    issues: list[dict] = []

    # Build parent lookup: id(child) -> parent
    parent_map: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[id(child)] = node

    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if len(node.body) > _MAX_SMALL_BODY:
            continue

        # Skip "search-and-break" loops: a `break` anywhere in the body signals
        # an intentional early-exit pattern (find first match), where using the
        # result after the loop is correct, not an indentation error.
        if any(isinstance(s, ast.Break) for s in ast.walk(node)):
            continue

        # Skip accumulator loops: body is a single AugAssign (x += y) — the
        # variable is intentionally accumulated then consumed after the loop.
        if (len(node.body) == 1
                and isinstance(node.body[0], ast.AugAssign)
                and isinstance(node.body[0].target, ast.Name)):
            continue

        # Collect variables assigned inside the for body
        # PLUS the loop target itself (e.g. `for fut in ...` → `fut`)
        body_vars: set[str] = set()
        _collect_assigned(node.target, body_vars)
        for stmt in node.body:
            _collect_assigned(stmt, body_vars)
        if not body_vars:
            continue

        # Locate the containing block to find subsequent siblings.
        # Walk up at most 2 levels: if the for is the last statement in its
        # container, the buggy code may be a sibling of the parent (e.g.
        # try/with accidentally outside a ``with ThreadPoolExecutor`` block).
        # Only walk up through scope-like parents (With/Try/AsyncWith) —
        # walking through FunctionDef/Module would match unrelated siblings.
        # Count how many DISTINCT siblings consume each loop var: a single use
        # is a common accumulator/checker pattern; the bug pattern has the
        # variable read in 3+ siblings (try + with + function-call chain).
        sibling_uses: dict[str, int] = {v: 0 for v in body_vars}
        current: ast.AST = node
        for _level in range(3):
            parent = parent_map.get(id(current))
            if parent is None:
                break
            container = _find_containing_list(parent, current)
            if container is None:
                break
            try:
                idx = container.index(current)
            except ValueError:
                break

            # Tally distinct siblings using each body var
            for sibling in container[idx + 1 : idx + 4]:
                sibling_vars: set[str] = set()
                _collect_used(sibling, sibling_vars)
                for v in body_vars & sibling_vars:
                    sibling_uses[v] += 1

            # For is last in its container — walk up only if parent is a
            # scope-like compound statement (with/try) whose siblings could
            # be code accidentally leaked outside the block.
            if isinstance(parent, (ast.With, ast.AsyncWith, ast.Try)):
                current = parent
            else:
                break

        # Report only vars consumed by ≥ _MIN_SIBLING_USES distinct siblings
        leaky = sorted(v for v, n in sibling_uses.items() if n >= _MIN_SIBLING_USES)
        if leaky:
            var_list = ", ".join(leaky)
            uses_str = ", ".join(f"{v}={sibling_uses[v]}" for v in leaky)
            issues.append({
                "file": filepath,
                "line": node.lineno,
                "message": (
                    f"For loop body has only {len(node.body)} statement(s) "
                    f"but loop variable(s) leak to {len(leaky)} var(s) used "
                    f"in ≥{_MIN_SIBLING_USES} siblings ({uses_str}). "
                    "Possible indentation error \u2014 code may be "
                    "accidentally outside the loop."
                ),
                "severity": "warning",
            })

    return issues
