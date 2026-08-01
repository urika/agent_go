from __future__ import annotations

import json
from typing import Any

from src.core.context import Context
from src.core.stage import Stage
from src.utils import write_file


class FileLoadStage(Stage):
    def __init__(self, output_key: str = "records", name: str = "file_load") -> None:
        self._name = name
        self._output_key = output_key

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return [self._output_key, "output_path"]

    @property
    def outputs(self) -> list[str]:
        return []

    def process(self, context: Context) -> None:
        data = context.get(self._output_key)
        output_path = context.get("output_path")
        if output_path is None:
            raise ValueError("No output_path provided in context")
        write_file(output_path, json.dumps(data, indent=2))


class ConsoleLoadStage(Stage):
    def __init__(self, output_key: str = "records", name: str = "console_load") -> None:
        self._name = name
        self._output_key = output_key

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return [self._output_key]

    @property
    def outputs(self) -> list[str]:
        return []

    def process(self, context: Context) -> None:
        data = context.get(self._output_key)
        print(json.dumps(data, indent=2, default=str))
