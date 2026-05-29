import subprocess
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "collect_timing", "collect_change_stats",
    "collect_merge_result", "extract_usage",
]

def collect_timing(worktree_create_ms: float, merge_upstream_ms: float, claude_execute_ms: float,
                   verification_ms: float, git_commit_ms: float) -> dict[str, float]:
    return {
        "worktree_create_ms": round(worktree_create_ms),
        "merge_upstream_ms": round(merge_upstream_ms),
        "claude_execute_ms": round(claude_execute_ms),
        "verification_ms": round(verification_ms),
        "git_commit_ms": round(git_commit_ms),
    }


def collect_change_stats(worktree_path: Path) -> dict[str, Any]:
    files_changed = 0
    insertions = 0
    deletions = 0
    actual_files = []

    numstat = subprocess.run(
        ["git", "diff", "--numstat", "HEAD"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    for line in numstat.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            insertions += int(parts[0]) if parts[0] != "-" else 0
            deletions += int(parts[1]) if parts[1] != "-" else 0
            actual_files.append(parts[2])

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    new_files = 0
    for line in status_result.stdout.strip().split("\n"):
        if line.startswith("??"):
            new_files += 1
            filename = line[3:]
            if filename not in actual_files:
                actual_files.append(filename)

    return {
        "files_changed": len(actual_files),
        "insertions": insertions,
        "deletions": deletions,
        "new_files": new_files,
        "modified_files": len(actual_files) - new_files,
        "actual_files": actual_files,
    }


def collect_merge_result(upstream_id: str, success: bool, conflict_files: Optional[list[str]] = None) -> dict[str, Any]:
    result = {"upstream": upstream_id, "status": "success" if success else "conflict"}
    if conflict_files:
        result["conflict_files"] = conflict_files
    return result


def extract_usage(api_response: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    usage = api_response.get("usage", {})
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "model": model,
        "provider": provider,
    }
