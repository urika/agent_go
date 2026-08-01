from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.core.context import Context


class Stage(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def inputs(self) -> list[str]:
        return []

    @property
    def outputs(self) -> list[str]:
        return []

    @abstractmethod
    def process(self, context: Context) -> None:
        ...

    def validate_inputs(self, context: Context) -> None:
        for key in self.inputs:
            if not context.has(key):
                raise ValueError(f"Missing input key: {key}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name})"
