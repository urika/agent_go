from __future__ import annotations

from typing import Any


class PipelineConfig:
    REQUIRED_STAGE_FIELDS = {"name", "type"}

    def __init__(self, stages: list[dict], global_params: dict[str, Any] | None = None) -> None:
        self.stages: list[dict] = stages
        self.global_params: dict[str, Any] = global_params or {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        stages = data.get("stages", [])
        global_params = data.get("global_params", {})
        return cls(stages=stages, global_params=global_params)

    def validate(self) -> list[str]:
        errors: list[str] = []
        for i, stage in enumerate(self.stages):
            missing = self.REQUIRED_STAGE_FIELDS - set(stage.keys())
            for field in missing:
                errors.append(f"Stage {i}: missing required field '{field}'")
        return errors

    def __repr__(self) -> str:
        return f"PipelineConfig(stages={len(self.stages)}, global_params={self.global_params})"
