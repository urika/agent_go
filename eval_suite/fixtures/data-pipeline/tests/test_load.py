import json

import pytest

from src.core.context import Context
from src.stages.load import ConsoleLoadStage, FileLoadStage


class TestFileLoadStage:
    def test_file_output(self, tmp_path):
        output_path = tmp_path / "out.json"
        ctx = Context()
        ctx.set("records", [{"a": 1}])
        ctx.set("output_path", str(output_path))
        stage = FileLoadStage()
        stage.process(ctx)
        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data == [{"a": 1}]

    def test_missing_output_path(self):
        ctx = Context()
        ctx.set("records", [])
        stage = FileLoadStage()
        with pytest.raises(ValueError, match="No output_path"):
            stage.process(ctx)

    def test_name_and_io(self):
        stage = FileLoadStage(output_key="aggregated", name="file_save")
        assert stage.name == "file_save"
        assert "aggregated" in stage.inputs
        assert "output_path" in stage.inputs


class TestConsoleLoadStage:
    def test_console_output(self, capsys):
        ctx = Context()
        ctx.set("records", [{"x": 42}])
        stage = ConsoleLoadStage()
        stage.process(ctx)
        captured = capsys.readouterr()
        assert '"x"' in captured.out
        assert "42" in captured.out

    def test_name_and_io(self):
        stage = ConsoleLoadStage(output_key="aggregated")
        assert "aggregated" in stage.inputs
        assert stage.outputs == []
