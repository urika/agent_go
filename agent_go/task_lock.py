"""任务级互斥锁工具（M5.2 冲突处理）。

背景：pipeline.py 内部有 task_dir/.task.lock 的 fcntl 非阻塞锁（同一任务
run/resume/recover 交叉修改 worktree/meta 互斥）。本模块将其抽为可复用工具：

- is_task_locked()：探测锁是否被其他进程持有（web 前置 409 检查用，避免
  resume/merge 提交后才发现冲突）
- TaskLock：acquire/release 上下文管理器（cmd_merge 等不经过 pipeline 的
  操作也拿同一把锁，防止 merge 与 run/resume 并发改 worktree）
"""
from __future__ import annotations

from pathlib import Path
from typing import IO, Optional


def is_task_locked(task_dir: Path) -> bool:
    """探测任务锁是否被其他进程持有（非阻塞，不修改锁状态）。"""
    lock_file = task_dir / ".task.lock"
    if not lock_file.exists():
        return False
    try:
        import fcntl
        with lock_file.open("a+", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True  # 被持有
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except (ImportError, OSError):
        return False


class TaskLock:
    """任务级互斥锁（fcntl 非阻塞）。持有失败 raise RuntimeError（与 pipeline 语义一致）。"""

    def __init__(self, task_dir: Path) -> None:
        self._lock_file: Optional[IO[str]] = None
        self._task_dir = task_dir

    def acquire(self) -> "TaskLock":
        import fcntl
        self._task_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self._task_dir / ".task.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError(f"task {self._task_dir.name} is already running")
        return self

    def release(self) -> None:
        if self._lock_file is not None:
            import fcntl
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError, ValueError):
                pass
            self._lock_file.close()
            self._lock_file = None

    def __enter__(self) -> "TaskLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
