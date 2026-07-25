"""Ground-truth tests for the task-mgr fixture project.
These are the canonical tests that agent_go evaluation tasks use as verification commands.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.models import Task
from src.utils import format_timestamp


def test_task_to_dict():
    t = Task(description="Buy milk", task_id="abc123", status="todo", created="2025-01-01T10:00:00")
    d = t.to_dict()
    assert d["id"] == "abc123"
    assert d["description"] == "Buy milk"
    assert d["status"] == "todo"


def test_task_from_dict():
    t = Task.from_dict({"id": "x1", "description": "Test", "status": "done", "created": "2025-01-01"})
    assert t.id == "x1"
    assert t.description == "Test"
    assert t.status == "done"


def test_task_mark_done():
    t = Task(description="Test")
    assert t.status == "todo"
    t.mark_done()
    assert t.status == "done"


def test_format_timestamp():
    assert format_timestamp("2025-01-01T10:00:00") == "2025-01-01 10:00"


def test_format_timestamp_invalid():
    assert format_timestamp("not-a-timestamp") == "not-a-timestamp"
