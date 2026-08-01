from __future__ import annotations

import csv
import io
from typing import Any

from src.core.context import Context
from src.core.stage import Stage


class CsvExtractStage(Stage):
    def __init__(self, name: str = "csv_extract") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return ["filepath"]

    @property
    def outputs(self) -> list[str]:
        return ["records", "columns"]

    def process(self, context: Context) -> None:
        filepath = context.get("filepath")
        if filepath is None:
            raise ValueError("No filepath provided in context")
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            records = list(reader)
            columns = reader.fieldnames or []
        context.set("records", records)
        context.set("columns", columns)


class ApiExtractStage(Stage):
    def __init__(self, name: str = "api_extract") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return ["api_url"]

    @property
    def outputs(self) -> list[str]:
        return ["records"]

    def process(self, context: Context) -> None:
        api_url = context.get("api_url")
        if api_url is None:
            raise ValueError("No api_url provided in context")
        data = self._fetch(api_url)
        context.set("records", data)

    def _fetch(self, url: str) -> list[dict[str, Any]]:
        import json
        from urllib.request import urlopen

        response = urlopen(url)
        return json.loads(response.read().decode())
