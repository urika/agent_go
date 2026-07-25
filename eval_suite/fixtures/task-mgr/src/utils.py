"""Utility functions — intentionally minimal for task evaluation."""

from datetime import datetime


def format_timestamp(iso_string: str) -> str:
    """Convert ISO timestamp to human-readable format."""
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_string
