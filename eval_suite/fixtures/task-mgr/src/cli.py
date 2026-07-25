"""Task Manager CLI — minimal fixture project for agent_go eval benchmarks.

Intentionally simple: enough complexity to test multi-file refactoring,
not so much that it distracts from the agent evaluation goals.
"""

import sys
import json
from pathlib import Path
from .models import Task
from .storage import TaskStorage
from .utils import format_timestamp


DATA_DIR = Path.home() / ".task-mgr"
DATA_FILE = DATA_DIR / "tasks.json"


def cmd_add(description: str) -> int:
    """Add a new task. Usage: python -m src add 'Buy groceries'"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = TaskStorage(DATA_FILE)
    task = Task(description=description)
    storage.save(task)
    print(f"Added task #{task.id}: {task.description}")
    return 0


def cmd_list(status: str = "") -> int:
    """List tasks. Usage: python -m src list [todo|done|all]"""
    if not DATA_FILE.exists():
        print("No tasks yet.")
        return 0
    storage = TaskStorage(DATA_FILE)
    tasks = storage.load_all()
    if status and status != "all":
        tasks = [t for t in tasks if t.status == status]
    for t in tasks:
        print(f"#{t.id} [{t.status}] {t.description}")
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src <add|list> [args...]")
        return 1
    cmd = sys.argv[1]
    if cmd == "add":
        return cmd_add(" ".join(sys.argv[2:]))
    elif cmd == "list":
        return cmd_list(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        print(f"Unknown command: {cmd}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
