import json
import sys

import pytest

from src.cli import STAGE_REGISTRY, build_pipeline, cmd_list_stages, cmd_validate


class TestCliListStages:
    def test_list_stages(self, capsys):
        class Args:
            pass

        cmd_list_stages(Args())
        captured = capsys.readouterr()
        for name in STAGE_REGISTRY:
            assert name in captured.out


class TestCliValidate:
    def test_validate_ok(self, tmp_path, capsys):
        config_path = tmp_path / "config.json"
        config = {"stages": [{"name": "s1", "type": "csv_extract"}], "global_params": {}}
        config_path.write_text(json.dumps(config))
        class Args:
            config = str(config_path)
        cmd_validate(Args())
        captured = capsys.readouterr()
        assert "Validation OK" in captured.out

    def test_validate_fail(self, tmp_path, capsys):
        config_path = tmp_path / "bad.json"
        config = {"stages": [{"name": "s1"}]}
        config_path.write_text(json.dumps(config))
        class Args:
            config = str(config_path)
        with pytest.raises(SystemExit):
            cmd_validate(Args())
        captured = capsys.readouterr()
        assert "FAILED" in captured.err


class TestBuildPipeline:
    def test_build_pipeline_ok(self, tmp_path):
        config_path = tmp_path / "pipeline.json"
        config = {
            "stages": [
                {"name": "extract", "type": "csv_extract"},
                {"name": "load", "type": "console_load"},
            ],
            "global_params": {"filepath": "dummy.csv"},
        }
        config_path.write_text(json.dumps(config))
        pipeline, context = build_pipeline(str(config_path))
        assert len(pipeline.stages) == 2
        assert context.get("filepath") == "dummy.csv"

    def test_build_pipeline_unknown_stage(self, tmp_path):
        config_path = tmp_path / "bad.json"
        config = {
            "stages": [{"name": "x", "type": "nonexistent"}],
            "global_params": {},
        }
        config_path.write_text(json.dumps(config))
        with pytest.raises(SystemExit):
            build_pipeline(str(config_path))

    def test_build_pipeline_validation_error(self, tmp_path):
        config_path = tmp_path / "bad.json"
        config = {
            "stages": [{"name": "x"}],
            "global_params": {},
        }
        config_path.write_text(json.dumps(config))
        with pytest.raises(SystemExit):
            build_pipeline(str(config_path))
