"""Data model — intentionally minimal to leave room for refactoring tasks."""

import uuid
from datetime import datetime


class Task:
    """A single task item."""

    def __init__(self, description: str, task_id: str = "", status: str = "todo",
                 created: str = ""):
        self.id = task_id or str(uuid.uuid4())[:8]
        self.description = description
        self.status = status  # "todo" or "done"
        self.created = created or datetime.now().isoformat()

    def mark_done(self):
        self.status = "done"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            description=data["description"],
            task_id=data.get("id", ""),
            status=data.get("status", "todo"),
            created=data.get("created", ""),
        )
