"""Storage ground-truth tests."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.models import Task
from src.storage import TaskStorage


def test_save_and_load(tmp_path):
    storage = TaskStorage(tmp_path / "tasks.json")
    task = Task(description="Test task", task_id="t1", created="2025-01-01T10:00:00")
    storage.save(task)
    loaded = storage.load_all()
    assert len(loaded) == 1
    assert loaded[0].id == "t1"


def test_load_empty(tmp_path):
    storage = TaskStorage(tmp_path / "nonexistent.json")
    assert storage.load_all() == []
