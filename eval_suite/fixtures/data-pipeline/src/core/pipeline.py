from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from src.core.context import Context
from src.core.stage import Stage


class StageError(Exception):
    def __init__(self, stage_name: str, original: Exception) -> None:
        self.stage_name = stage_name
        self.original = original
        super().__init__(f"Stage '{stage_name}' failed: {original}")


class Pipeline:
    def __init__(self, stages: list[Stage]) -> None:
        if not stages:
            raise ValueError("At least one stage is required")
        self.stages = stages
        self._on_stage_start: Callable[[Stage], None] | None = None
        self._on_stage_end: Callable[[Stage, Context], None] | None = None

    @property
    def on_stage_start(self) -> Callable[[Stage], None] | None:
        return self._on_stage_start

    @on_stage_start.setter
    def on_stage_start(self, callback: Callable[[Stage], None] | None) -> None:
        self._on_stage_start = callback

    @property
    def on_stage_end(self) -> Callable[[Stage, Context], None] | None:
        return self._on_stage_end

    @on_stage_end.setter
    def on_stage_end(self, callback: Callable[[Stage, Context], None] | None) -> None:
        self._on_stage_end = callback

    def run(self, context: Context | None = None) -> Context:
        if context is None:
            context = Context()
        for stage in self.stages:
            if self._on_stage_start:
                self._on_stage_start(stage)
            try:
                stage.process(context)
            except Exception as e:
                raise StageError(stage.name, e) from e
            if self._on_stage_end:
                self._on_stage_end(stage, context)
        return context

    def run_async(self, context: Context | None = None) -> Context:
        if context is None:
            context = Context()
        lock = threading.Lock()

        def _run_stage(stage: Stage) -> tuple[Stage, Context]:
            if self._on_stage_start:
                self._on_stage_start(stage)
            try:
                stage.process(context)
            except Exception as e:
                raise StageError(stage.name, e) from e
            if self._on_stage_end:
                with lock:
                    self._on_stage_end(stage, context)
            return stage, context

        with ThreadPoolExecutor(max_workers=len(self.stages)) as executor:
            futures = [executor.submit(_run_stage, s) for s in self.stages]
            for fut in as_completed(futures):
                fut.result()

        return context
