import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

__all__ = [
    "SnapshotManager",
    "take_snapshot",
    "list_checkpoints",
    "restore_checkpoint",
]


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


SNAPSHOT_FILENAME = "snapshot.json"
SNAPSHOT_DIR = "checkpoints"
FILES_SUBDIR = "files"


def _checkpoint_dir(task_dir: Path) -> Path:
    return task_dir / SNAPSHOT_DIR


def _sub_checkpoint_dir(task_dir: Path, sub_id: str) -> Path:
    return _checkpoint_dir(task_dir) / sub_id


def _files_dir(task_dir: Path, sub_id: str) -> Path:
    return _sub_checkpoint_dir(task_dir, sub_id) / FILES_SUBDIR


class SnapshotManager:
    """Manages file checkpoints for subtask rollback."""

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)

    def take(self, sub_id: str, worktree: Path, files_hint: str = "") -> Optional[str]:
        """Take a snapshot of files in worktree matching files_hint.

        Returns snapshot name (sub_id) on success, None on failure.
        Files matching the hint pattern are copied to checkpoints/<sub_id>/files/.
        """
        snap_dir = _sub_checkpoint_dir(self.task_dir, sub_id)
        files_dir = _files_dir(self.task_dir, sub_id)
        snap_dir.mkdir(parents=True, exist_ok=True)
        files_dir.mkdir(parents=True, exist_ok=True)

        target_files = self._resolve_files(worktree, files_hint)
        if not target_files:
            logger.debug(f"[checkpoint] {sub_id}: no matching files, skipping snapshot")
            # snap_dir 内已创建 files/ 子目录，rmdir() 对非空目录会抛 OSError → 用 rmtree
            shutil.rmtree(snap_dir, ignore_errors=True)
            return None

        snapshot: dict[str, Any] = {
            "subtask_id": sub_id,
            "timestamp": time.time(),
            "files_hint": files_hint,
            "files": [],
        }

        errors = 0
        for rel_path in target_files:
            src = worktree / rel_path
            if not src.exists() or not src.is_file():
                continue
            dst = files_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dst))
                snapshot["files"].append({
                    "rel_path": rel_path,
                    "size": src.stat().st_size,
                    "sha256_prefix": _file_hash(src),
                })
            except OSError as e:
                logger.warning(f"[checkpoint] {sub_id}: failed to copy {rel_path}: {e}")
                errors += 1

        snapshot["file_count"] = len(snapshot["files"])
        snapshot["errors"] = errors
        (snap_dir / SNAPSHOT_FILENAME).write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.info(f"[checkpoint] {sub_id}: snapshot {len(snapshot['files'])} files ({errors} errors)")
        return sub_id

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List all snapshots for this task."""
        base = _checkpoint_dir(self.task_dir)
        if not base.exists():
            return []
        result: list[dict[str, Any]] = []
        for snap_dir in sorted(base.iterdir()):
            snap_file = snap_dir / SNAPSHOT_FILENAME
            if snap_file.exists():
                try:
                    data = json.loads(snap_file.read_text(encoding="utf-8"))
                    result.append(data)
                except (json.JSONDecodeError, OSError):
                    continue
        return result

    def restore(self, sub_id: str, target: Path) -> int:
        """Restore files from a snapshot back to target directory.

        Returns number of files restored, or 0 if snapshot doesn't exist.
        """
        snap_file = _sub_checkpoint_dir(self.task_dir, sub_id) / SNAPSHOT_FILENAME
        if not snap_file.exists():
            logger.warning(f"[checkpoint] snapshot {sub_id} not found")
            return 0

        try:
            snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.error(f"[checkpoint] failed to read snapshot {sub_id}")
            return 0

        files_dir = _files_dir(self.task_dir, sub_id)
        restored = 0
        for entry in snapshot.get("files", []):
            rel = entry["rel_path"]
            src = files_dir / rel
            dst = target / rel
            if not src.exists():
                logger.warning(f"[checkpoint] {sub_id}: missing cached file {rel}")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src), str(dst))
                restored += 1
            except OSError as e:
                logger.warning(f"[checkpoint] {sub_id}: failed to restore {rel}: {e}")

        logger.info(f"[checkpoint] {sub_id}: restored {restored}/{snapshot.get('file_count', 0)} files to {target}")
        return restored

    def delete(self, sub_id: str) -> bool:
        """Delete a checkpoint snapshot."""
        snap_dir = _sub_checkpoint_dir(self.task_dir, sub_id)
        if not snap_dir.exists():
            return False
        try:
            shutil.rmtree(str(snap_dir))
            logger.info(f"[checkpoint] deleted snapshot {sub_id}")
            return True
        except OSError as e:
            logger.warning(f"[checkpoint] failed to delete {sub_id}: {e}")
            return False

    @staticmethod
    def _resolve_files(worktree: Path, files_hint: str) -> list[str]:
        """Resolve files_hint glob against worktree to get matching relative paths."""
        if not files_hint:
            hint_parts = ["**/*.py", "**/*.js", "**/*.ts", "**/*.rs", "**/*.go",
                          "**/*.java", "**/*.rb", "**/*.c", "**/*.h", "**/*.cpp",
                          "**/*.hpp", "**/*.yaml", "**/*.yml", "**/*.json",
                          "**/*.toml", "**/*.cfg", "**/*.ini", "**/*.sh",
                          "**/*.css", "**/*.html", "**/*.md", "**/Makefile",
                          "**/Dockerfile"]
        else:
            hint_parts = [p.strip() for p in files_hint.replace(",", " ").split() if p.strip()]

        matched: set[str] = set()
        for pattern in hint_parts:
            abs_pattern = str(worktree / pattern)
            try:
                for p in worktree.glob(pattern):
                    if p.is_file():
                        try:
                            rel = p.relative_to(worktree).as_posix()
                            matched.add(rel)
                        except ValueError:
                            pass
            except (OSError, ValueError):
                try:
                    from glob import iglob
                    for p in iglob(abs_pattern, recursive=True):
                        pp = Path(p)
                        if pp.is_file():
                            try:
                                rel = pp.relative_to(worktree).as_posix()
                                matched.add(rel)
                            except ValueError:
                                pass
                except OSError:
                    pass
        return sorted(matched)


def take_snapshot(task_dir: Path, sub_id: str, worktree: Path, files_hint: str = "") -> Optional[str]:
    """Convenience function: take a checkpoint snapshot."""
    return SnapshotManager(task_dir).take(sub_id, worktree, files_hint)


def list_checkpoints(task_id: str) -> list[dict[str, Any]]:
    """Convenience function: list checkpoints for a task by task_id string."""
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        return []
    return SnapshotManager(task_dir).list_snapshots()


def restore_checkpoint(task_id: str, sub_id: str, target: Optional[Path] = None) -> int:
    """Convenience function: restore a checkpoint.

    If target is None, restore to the original worktree (task_dir/sub_id/work).
    Returns number of files restored.
    """
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        return 0
    if target is None:
        target = task_dir / sub_id / "work"
    return SnapshotManager(task_dir).restore(sub_id, target)
