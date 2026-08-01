from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from src.core.context import Context
from src.core.stage import Stage


class FilterStage(Stage):
    def __init__(self, predicate: Callable[[dict[str, Any]], bool], name: str = "filter") -> None:
        self._name = name
        self._predicate = predicate

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return ["records"]

    @property
    def outputs(self) -> list[str]:
        return ["records"]

    def process(self, context: Context) -> None:
        records: list[dict[str, Any]] = context.get("records", [])
        filtered = [r for r in records if self._predicate(r)]
        context.set("records", filtered)


class MapStage(Stage):
    def __init__(self, mapping: Callable[[dict[str, Any]], dict[str, Any]], name: str = "map") -> None:
        self._name = name
        self._mapping = mapping

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return ["records"]

    @property
    def outputs(self) -> list[str]:
        return ["records"]

    def process(self, context: Context) -> None:
        records: list[dict[str, Any]] = context.get("records", [])
        mapped = [self._mapping(r) for r in records]
        context.set("records", mapped)


class AggregateStage(Stage):
    def __init__(self, key: str, aggregator: Callable[[list[dict[str, Any]]], Any], name: str = "aggregate") -> None:
        self._name = name
        self._key = key
        self._aggregator = aggregator

    @property
    def name(self) -> str:
        return self._name

    @property
    def inputs(self) -> list[str]:
        return ["records"]

    @property
    def outputs(self) -> list[str]:
        return ["aggregated"]

    def process(self, context: Context) -> None:
        records: list[dict[str, Any]] = context.get("records", [])
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            groups[r.get(self._key, "__none__")].append(r)
        result = {k: self._aggregator(v) for k, v in groups.items()}
        context.set("aggregated", result)
