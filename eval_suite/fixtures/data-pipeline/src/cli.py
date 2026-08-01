from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.context import Context
from src.core.pipeline import Pipeline
from src.stages.extract import ApiExtractStage, CsvExtractStage
from src.stages.load import ConsoleLoadStage, FileLoadStage
from src.stages.transform import AggregateStage, FilterStage, MapStage
from src.utils import read_file, setup_logging

STAGE_REGISTRY = {
    "csv_extract": CsvExtractStage,
    "api_extract": ApiExtractStage,
    "filter": FilterStage,
    "map": MapStage,
    "aggregate": AggregateStage,
    "file_load": FileLoadStage,
    "console_load": ConsoleLoadStage,
}


def build_pipeline(config_path: str) -> tuple[Pipeline, Context]:
    raw = json.loads(read_file(config_path))
    config = PipelineConfig.from_dict(raw)
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    stages = []
    for stage_cfg in config.stages:
        stype = stage_cfg["type"]
        cls = STAGE_REGISTRY.get(stype)
        if cls is None:
            print(f"Unknown stage type: {stype}", file=sys.stderr)
            sys.exit(1)
        kwargs = {k: v for k, v in stage_cfg.items() if k not in ("type", "name")}
        stage = cls(name=stage_cfg.get("name", stype), **kwargs)
        stages.append(stage)

    context = Context()
    for k, v in config.global_params.items():
        context.set(k, v)

    return Pipeline(stages), context


def cmd_run(args: argparse.Namespace) -> None:
    pipeline, context = build_pipeline(args.config)
    context = pipeline.run(context)
    print(f"Pipeline completed. Context keys: {list(context.data.keys())}")


def cmd_list_stages(args: argparse.Namespace) -> None:
    print("Available stages:")
    for name in sorted(STAGE_REGISTRY):
        print(f"  {name}")


def cmd_validate(args: argparse.Namespace) -> None:
    raw = json.loads(read_file(args.config))
    config = PipelineConfig.from_dict(raw)
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"Validation FAILED: {len(errors)} error(s)", file=sys.stderr)
        sys.exit(1)
    print("Validation OK")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Data Pipeline CLI")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run a pipeline from a config file")
    run_p.add_argument("config", help="Path to pipeline config JSON")

    sub.add_parser("list-stages", help="List available stage types")

    validate_p = sub.add_parser("validate", help="Validate a pipeline config")
    validate_p.add_argument("config", help="Path to pipeline config JSON")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "list-stages":
        cmd_list_stages(args)
    elif args.command == "validate":
        cmd_validate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
