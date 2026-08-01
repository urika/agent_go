import csv
import io
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from src.core.context import Context
from src.stages.extract import ApiExtractStage, CsvExtractStage


class TestCsvExtractStage:
    def test_extract_csv(self, tmp_path):
        csv_path = tmp_path / "test.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "age"])
            writer.writerow(["Alice", "30"])
            writer.writerow(["Bob", "25"])

        stage = CsvExtractStage()
        ctx = Context()
        ctx.set("filepath", str(csv_path))
        stage.process(ctx)

        assert ctx.has("records")
        assert ctx.has("columns")
        assert len(ctx.get("records")) == 2
        assert ctx.get("columns") == ["name", "age"]

    def test_missing_filepath(self):
        stage = CsvExtractStage()
        ctx = Context()
        with pytest.raises(ValueError, match="No filepath"):
            stage.process(ctx)

    def test_name_and_io(self):
        stage = CsvExtractStage("my_csv")
        assert stage.name == "my_csv"
        assert "filepath" in stage.inputs
        assert "records" in stage.outputs


class TestApiExtractStage:
    @mock.patch("urllib.request.urlopen")
    def test_extract_api(self, mock_urlopen):
        fake_data = [{"id": 1, "name": "Alice"}]
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(fake_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        stage = ApiExtractStage()
        ctx = Context()
        ctx.set("api_url", "http://example.com/data")
        stage.process(ctx)

        assert ctx.has("records")
        assert ctx.get("records") == fake_data

    def test_missing_api_url(self):
        stage = ApiExtractStage()
        ctx = Context()
        with pytest.raises(ValueError, match="No api_url"):
            stage.process(ctx)

    def test_name(self):
        stage = ApiExtractStage("api_stage")
        assert stage.name == "api_stage"
