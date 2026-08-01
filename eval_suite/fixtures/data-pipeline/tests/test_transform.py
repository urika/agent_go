import pytest

from src.core.context import Context
from src.stages.transform import AggregateStage, FilterStage, MapStage


class TestFilterStage:
    def test_filter_predicate(self):
        records = [{"v": 1}, {"v": 2}, {"v": 3}]
        stage = FilterStage(predicate=lambda r: r["v"] > 1)
        ctx = Context()
        ctx.set("records", records)
        stage.process(ctx)
        assert ctx.get("records") == [{"v": 2}, {"v": 3}]

    def test_filter_empty(self):
        stage = FilterStage(predicate=lambda r: False)
        ctx = Context()
        ctx.set("records", [{"a": 1}])
        stage.process(ctx)
        assert ctx.get("records") == []

    def test_name(self):
        stage = FilterStage(predicate=lambda r: True, name="my_filter")
        assert stage.name == "my_filter"


class TestMapStage:
    def test_map_transform(self):
        records = [{"x": 1}, {"x": 2}]
        stage = MapStage(mapping=lambda r: {"x": r["x"] * 2})
        ctx = Context()
        ctx.set("records", records)
        stage.process(ctx)
        assert ctx.get("records") == [{"x": 2}, {"x": 4}]

    def test_name(self):
        stage = MapStage(mapping=lambda r: r, name="double")
        assert stage.name == "double"
        assert "records" in stage.inputs
        assert "records" in stage.outputs


class TestAggregateStage:
    def test_aggregate_by_key(self):
        records = [
            {"group": "a", "val": 1},
            {"group": "a", "val": 2},
            {"group": "b", "val": 3},
        ]
        stage = AggregateStage(key="group", aggregator=lambda rs: sum(r["val"] for r in rs))
        ctx = Context()
        ctx.set("records", records)
        stage.process(ctx)
        result = ctx.get("aggregated")
        assert result == {"a": 3, "b": 3}

    def test_name_and_io(self):
        stage = AggregateStage(key="k", aggregator=len)
        assert stage.name == "aggregate"
        assert "records" in stage.inputs
        assert "aggregated" in stage.outputs
