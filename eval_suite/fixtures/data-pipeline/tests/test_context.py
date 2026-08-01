import threading

import pytest

from src.core.context import Context


class TestContext:
    def test_set_and_get(self):
        ctx = Context()
        ctx.set("key1", "value1")
        assert ctx.get("key1") == "value1"

    def test_get_default(self):
        ctx = Context()
        assert ctx.get("nonexistent") is None
        assert ctx.get("nonexistent", 42) == 42

    def test_has(self):
        ctx = Context()
        assert not ctx.has("key")
        ctx.set("key", "val")
        assert ctx.has("key")

    def test_thread_safety(self):
        ctx = Context()
        n = 100
        errors = []

        def writer(i):
            try:
                ctx.set(f"k{i}", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert ctx.get("k0") == 0
        assert ctx.get("k99") == 99

    def test_metadata_isolation(self):
        ctx = Context()
        ctx.set("a", 1)
        ctx.metadata["foo"] = "bar"
        assert ctx.get("a") == 1
        assert ctx.metadata["foo"] == "bar"
        assert not ctx.has("foo")
