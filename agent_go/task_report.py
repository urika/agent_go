"""Read-only task statistics report generator.

Usage:
    from agent_go.task_report import generate_task_report
    report = generate_task_report(list_of_json_file_paths)
"""

import json
from pathlib import Path

_COMPLETED_STATUSES = {"completed", "done", "success", "closed"}


def _is_completed(task: dict) -> bool:
    """判定任务是否已完成，兼容多种字段形态。"""
    if not isinstance(task, dict):
        return False
    completed = task.get("completed")
    if completed is True:
        return True
    status = task.get("status")
    if isinstance(status, str) and status.strip().lower() in _COMPLETED_STATUSES:
        return True
    return False


def _normalize_tag(tag: str) -> str:
    return tag.strip().lower()


def _extract_tags(task: dict) -> list[str]:
    """从任务中提取标签列表（已归一化）。

    无 tags / 空列表 / 空串 -> ["untagged"]。
    """
    tags = task.get("tags")
    if tags is None:
        return ["untagged"]
    if isinstance(tags, str):
        if tags.strip() == "":
            return ["untagged"]
        return [_normalize_tag(t) for t in tags.split(",") if t.strip() != ""] or ["untagged"]
    if isinstance(tags, (list, tuple)):
        normalized = [_normalize_tag(t) for t in tags if isinstance(t, str) and t.strip() != ""]
        return normalized or ["untagged"]
    return ["untagged"]


def _iter_tasks(payload) -> list[dict]:
    """将文件内容展开为任务记录列表。"""
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            return [t for t in tasks if isinstance(t, dict)]
        return [payload]
    return []


def generate_task_report(task_files: list[str]) -> dict:
    """生成只读任务统计报表。

    入参为任务 JSON 文件路径列表。只读，不修改任何文件。
    任一文件 JSON 解码失败或字段非法 -> 跳过该文件，不抛异常。
    """
    total_count = 0
    completed_count = 0
    tags_distribution: dict[str, int] = {}

    for file_path in task_files:
        try:
            text = Path(file_path).read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError, ValueError):
            continue

        for task in _iter_tasks(payload):
            total_count += 1
            if _is_completed(task):
                completed_count += 1
            for tag in _extract_tags(task):
                tags_distribution[tag] = tags_distribution.get(tag, 0) + 1

    return {
        "total_count": total_count,
        "completed_count": completed_count,
        "uncompleted_count": total_count - completed_count,
        "tags_distribution": tags_distribution,
    }
