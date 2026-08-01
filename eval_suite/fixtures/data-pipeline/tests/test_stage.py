import pytest

from src.core.context import Context
from src.core.stage import Stage


class ConcreteStage(Stage):
    def __init__(self, name="test_stage"):
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def inputs(self):
        return ["input_a"]

    @property
    def outputs(self):
        return ["output_a"]

    def process(self, context):
        context.set("output_a", context.get("input_a") * 2)


class TestStage:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Stage()

    def test_concrete_stage_creation(self):
        s = ConcreteStage("my_stage")
        assert s.name == "my_stage"

    def test_inputs_and_outputs(self):
        s = ConcreteStage()
        assert s.inputs == ["input_a"]
        assert s.outputs == ["output_a"]

    def test_process(self):
        s = ConcreteStage()
        ctx = Context()
        ctx.set("input_a", 21)
        s.process(ctx)
        assert ctx.get("output_a") == 42

    def test_validate_inputs_pass(self):
        s = ConcreteStage()
        ctx = Context()
        ctx.set("input_a", 1)
        s.validate_inputs(ctx)

    def test_validate_inputs_fail(self):
        s = ConcreteStage()
        ctx = Context()
        with pytest.raises(ValueError, match="Missing input key"):
            s.validate_inputs(ctx)

    def test_repr(self):
        s = ConcreteStage("foo")
        assert "ConcreteStage" in repr(s)
        assert "foo" in repr(s)
