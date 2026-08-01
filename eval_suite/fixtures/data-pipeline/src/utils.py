from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Iterator


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def read_file(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8")


def write_file(path: str | Path, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@contextlib.contextmanager
def timer(name: str = "block") -> Iterator[None]:
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    logging.getLogger(__name__).info("%s took %.3fs", name, elapsed)
