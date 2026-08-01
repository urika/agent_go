import pytest

from src.core.context import Context
from src.core.pipeline import Pipeline, StageError
from src.core.stage import Stage


class AddStage(Stage):
    def __init__(self, name="add", value=1):
        self._name = name
        self._value = value

    @property
    def name(self):
        return self._name

    @property
    def inputs(self):
        return ["value"]

    @property
    def outputs(self):
        return ["value"]

    def process(self, context):
        v = context.get("value", 0)
        context.set("value", v + self._value)


class ErrorStage(Stage):
    @property
    def name(self):
        return "error_stage"

    def process(self, context):
        raise RuntimeError("boom")


class TestPipeline:
    def test_create_pipeline(self):
        p = Pipeline([AddStage()])
        assert len(p.stages) == 1

    def test_empty_pipeline_raises(self):
        with pytest.raises(ValueError, match="At least one stage"):
            Pipeline([])

    def test_run_sequential(self):
        p = Pipeline([AddStage(value=1), AddStage(value=2)])
        ctx = Context()
        ctx.set("value", 0)
        p.run(ctx)
        assert ctx.get("value") == 3

    def test_run_default_context(self):
        p = Pipeline([AddStage(name="a", value=10)])
        ctx = p.run()
        assert ctx.get("value") == 10

    def test_run_async(self):
        p = Pipeline([AddStage(value=1), AddStage(value=2)])
        ctx = Context()
        ctx.set("value", 0)
        ctx = p.run_async(ctx)
        assert ctx.get("value") == 3

    def test_error_propagation(self):
        p = Pipeline([AddStage(value=1), ErrorStage(), AddStage(value=1)])
        ctx = Context()
        ctx.set("value", 0)
        with pytest.raises(StageError) as excinfo:
            p.run(ctx)
        assert excinfo.value.stage_name == "error_stage"
        assert isinstance(excinfo.value.original, RuntimeError)

    def test_callbacks(self):
        p = Pipeline([AddStage(value=1)])
        events = []

        def on_start(stage):
            events.append(("start", stage.name))

        def on_end(stage, ctx):
            events.append(("end", stage.name))

        p.on_stage_start = on_start
        p.on_stage_end = on_end
        p.run(Context())
        assert events == [("start", "add"), ("end", "add")]
