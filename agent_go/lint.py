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

_MAX_SMALL_BODY = 2


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

        # Collect variables assigned inside the for body
        body_vars: set[str] = set()
        for stmt in node.body:
            _collect_assigned(stmt, body_vars)
        if not body_vars:
            continue

        # Locate the containing block to find subsequent siblings
        parent = parent_map.get(id(node))
        if parent is None:
            continue
        container = _find_containing_list(parent, node)
        if container is None:
            continue

        try:
            idx = container.index(node)
        except ValueError:
            continue

        # Check up to 3 subsequent sibling statements
        for sibling in container[idx + 1 : idx + 4]:
            sibling_vars: set[str] = set()
            _collect_used(sibling, sibling_vars)
            overlap = body_vars & sibling_vars
            if overlap:
                var_list = ", ".join(sorted(overlap))
                issues.append({
                    "file": filepath,
                    "line": node.lineno,
                    "message": (
                        f"For loop body has only {len(node.body)} statement(s) "
                        f"but subsequent code uses loop variable(s): {var_list}. "
                        "Possible indentation error \u2014 code may be "
                        "accidentally outside the loop."
                    ),
                    "severity": "warning",
                })
                break  # one report per suspicious for-loop

    return issues
