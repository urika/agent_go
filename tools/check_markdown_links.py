#!/usr/bin/env python3
"""Check relative Markdown links under a directory."""

from __future__ import annotations

import re
import sys
from pathlib import Path


LINK_RE = re.compile(r"(?<!!)(?:\[[^\]]*\])\(([^)]+)\)")


def find_broken_links(root: str | Path) -> list[tuple[str, str]]:
    base = Path(root).resolve()
    broken: list[tuple[str, str]] = []
    for markdown in base.rglob("*.md"):
        if "archive" in markdown.relative_to(base).parts:
            continue
        text = markdown.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            target = target.strip().split("#", 1)[0].split("?", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (markdown.parent / target).resolve().exists():
                broken.append((str(markdown.relative_to(base)), target))
    return broken


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:] or ["docs"])[0])
    broken = find_broken_links(root)
    for source, target in broken:
        print(f"{source}: broken link: {target}")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
