"""TaskEvent 最小词汇 + 每任务 append-only 事件日志（ADR-010 阶段 2）。

每 task 一份 ``<task_dir>/events.jsonl``，平台**编排级**事件的唯一真源（骨架期）：
轨迹（trajectory/）覆盖执行级（backend 内部 turn/step/tool），events.jsonl 覆盖
编排级（plan/decompose/verify/retry/commit）——dsh 臂 plan_gate_blocked 类规划级
失败只有这里能归因（见 docs/design/adr010-phase2-value-check.md 边界案例）。

契约：
- 每事件 ``{"seq", "ts", "type", "data"}``；seq 每任务单调递增，ts 为 epoch 毫秒；
- **fail-open**：任何写入失败只记 debug 日志，绝不影响任务执行；
- 线程安全：pipeline 并发子任务共用同一文件，per-path 锁保护；
- meta.json 维持双写一个版本周期（投影化切换不在本阶段）。

词汇（ADR-010 最小集 + task_end）：

| type | data 关键字段 | 发射点 |
|---|---|---|
| plan | steps, source(api/local/rule), iteration | cli.cmd_run |
| decompose | subtasks, waves | cli.cmd_run |
| subtask_start | sub_id, title, difficulty, backend, model | executor.run_subtask |
| model_attempt | sub_id, attempt, backend, model, returncode, kill_reason, elapsed_sec | executor / run_repair 后 |
| verify | sub_id, ok, failed_commands, rejected_commands, semantic_fail | executor._verify_changes 后 |
| commit | sub_id, tag, has_changes | executor tag 写入后 |
| retry | sub_id, attempt, max_retries, reason | executor 修复重试 |
| subtask_end | sub_id, status, failure_class, retries | executor.run_subtask 末尾 |
| task_end | status_counts, total_subtasks | pipeline 完成 |
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent_go")

EVENTS_FILENAME = "events.jsonl"

# per-path 锁：pipeline ThreadPoolExecutor 下多子任务并发写同一 events.jsonl。
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def _next_seq(path: Path) -> int:
    """读文件尾部最后一行取 seq+1（骨架期事件量小，无需索引）。"""
    try:
        size = path.stat().st_size
        if size == 0:
            return 1
        with path.open("rb") as f:
            f.seek(max(0, size - 4096), os.SEEK_SET)
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return int(json.loads(line).get("seq", 0)) + 1
            except (ValueError, TypeError):
                continue
        return 1
    except OSError:
        return 1


def emit_event(task_dir: Optional[str], etype: str, **data) -> None:
    """追加一条 TaskEvent 到 <task_dir>/events.jsonl（fail-open）。"""
    if not task_dir:
        return
    try:
        path = Path(task_dir) / EVENTS_FILENAME
        with _lock_for(path):
            event = {"seq": _next_seq(path), "ts": int(time.time() * 1000),
                     "type": etype, "data": data}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug(f"[events] 事件写入失败（忽略）: {etype} {exc}")


def read_events(task_dir: str) -> list:
    """读取全部事件（消费端/测试用；坏行跳过）。"""
    out: list = []
    try:
        for line in (Path(task_dir) / EVENTS_FILENAME).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out
