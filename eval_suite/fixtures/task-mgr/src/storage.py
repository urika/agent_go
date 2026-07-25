"""Simple JSON file storage — intentionally has a bug (missing default) for fix-bug task."""

import json
from pathlib import Path
from .models import Task


class TaskStorage:
    """Load and save tasks to a JSON file."""

    def __init__(self, filepath: Path):
        self.filepath = Path(filepath)

    def load_all(self) -> list[Task]:
        if not self.filepath.exists():
            return []
        data = json.loads(self.filepath.read_text(encoding="utf-8"))
        return [Task.from_dict(item) for item in data]

    def save(self, task: Task) -> None:
        tasks = self.load_all()
        tasks.append(task)
        self._write(tasks)

    def _write(self, tasks: list[Task]) -> None:
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.filepath.write_text(
            json.dumps([t.to_dict() for t in tasks], indent=2, ensure_ascii=False),
            encoding="utf-8")
