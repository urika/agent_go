"""规划阶段辅助：任务耗时预估（M4）。

从 eval.py 迁移而来——estimate_task_duration 逻辑上是「预执行估算」（在线、嵌入 cmd_run），
不是「离线评估」，放在 eval.py 会让核心流程（cli.cmd_run）反向依赖评估模块，违背解耦原则
（核心不依赖增强/评估）。本模块自包含，不 import eval.py，避免循环依赖。

依赖契约：读取 ~/.agent_go/task-*/meta.json 的 results[].duration_sec（核心管线写入）。
"""
import json
import logging
from pathlib import Path
from typing import Any, Optional

__all__ = ["estimate_task_duration"]

logger = logging.getLogger(__name__)

# 无历史数据时的默认子任务耗时（秒），与 eval.py 保持一致
_DEFAULT_SUBTASK_SEC = 240


def _read_meta(task_dir: Path) -> Optional[dict[str, Any]]:
    """读 meta.json（与 eval._read_meta 同构，自包含避免 import eval）。"""
    path = Path(task_dir) / "meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read meta.json from %s: %s", path, e)
        return None


def _scan_task_dirs(base_dir: Path) -> list[Path]:
    """扫描 task-* 目录（与 eval._scan_task_dirs 同构）。"""
    return sorted(Path(base_dir).glob("task-*"), reverse=True)


def estimate_task_duration(subtasks: list[dict], parallel: int, tasks_dir: Path) -> dict[str, Any]:
    """M4 时间预估：历史子任务耗时中位数 × 拓扑波次（考虑并行度）。

    Returns:
        {
            "subtasks": int, "waves": int, "parallel": int,
            "median_subtask_sec": float, "estimated_sec": int,
            "sample_size": int, "confidence": "high|medium|low|none",
        }
    """
    # 1. 拓扑波次数（依赖链深度，带环保护）
    ids = {st["id"] for st in subtasks}
    depth: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset) -> int:
        if sid in depth:
            return depth[sid]
        if sid in seen:
            return 0
        st = next((s for s in subtasks if s["id"] == sid), None)
        if not st:
            return 0
        deps = [d for d in st.get("depends_on", []) if d in ids]
        d = 1 + max((_depth(dep, seen | {sid}) for dep in deps), default=0)
        depth[sid] = d
        return d

    waves = max((_depth(st["id"], frozenset()) for st in subtasks), default=1)

    # 2. 历史子任务耗时中位数
    durations = sorted(
        r["duration_sec"]
        for td in _scan_task_dirs(tasks_dir)
        if (meta := _read_meta(td))
        for r in meta.get("results", [])
        if r.get("status") in ("completed", "no_changes") and r.get("duration_sec")
    )
    sample_size = len(durations)
    median = durations[sample_size // 2] if durations else _DEFAULT_SUBTASK_SEC

    # 3. 估算：受限于「关键路径（波次）」和「并行吞吐」两者较大者
    total_work = median * len(subtasks)
    est_sec = max(waves * median, total_work / max(1, parallel))

    confidence = ("high" if sample_size >= 20 else
                  "medium" if sample_size >= 5 else
                  "low" if sample_size > 0 else "none")
    return {
        "subtasks": len(subtasks),
        "waves": waves,
        "parallel": parallel,
        "median_subtask_sec": median,
        "estimated_sec": round(est_sec),
        "sample_size": sample_size,
        "confidence": confidence,
    }
