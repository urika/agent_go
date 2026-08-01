from __future__ import annotations

import threading
from typing import Any


class Context:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self.data

    def __repr__(self) -> str:
        return f"Context(data={self.data}, metadata={self.metadata})"
