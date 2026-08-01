import pytest

from src.core.config import PipelineConfig


class TestPipelineConfig:
    def test_from_dict(self):
        data = {
            "stages": [{"name": "extract", "type": "csv_extract"}],
            "global_params": {"filepath": "data.csv"},
        }
        config = PipelineConfig.from_dict(data)
        assert len(config.stages) == 1
        assert config.stages[0]["name"] == "extract"
        assert config.global_params["filepath"] == "data.csv"

    def test_validate_ok(self):
        config = PipelineConfig(stages=[{"name": "s1", "type": "csv_extract"}])
        assert config.validate() == []

    def test_validate_missing_fields(self):
        config = PipelineConfig(stages=[{"name": "s1"}])
        errors = config.validate()
        assert len(errors) == 1
        assert "type" in errors[0]

    def test_validate_multiple_errors(self):
        config = PipelineConfig(stages=[{}, {"name": "s2"}])
        errors = config.validate()
        assert len(errors) == 3

    def test_default_global_params(self):
        config = PipelineConfig(stages=[])
        assert config.global_params == {}

    def test_repr(self):
        config = PipelineConfig(stages=[{"name": "s1"}], global_params={"x": 1})
        r = repr(config)
        assert "PipelineConfig" in r
