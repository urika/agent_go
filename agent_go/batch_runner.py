"""本地后台队列批量执行（roadmap §7.13 后续）——把看板当队列，串行跑批。

设计（与既有纪律一致）：
  - **队列＝看板本身**：不新增持久化队列文件，取 implementation 列（或显式 card-id 列表）
    的 implementation/periodic 卡片，按创建顺序串行执行；跑完即回流（成功 → operations）。
  - **显式串行**：一次只跑一个任务（`--parallel 1`）——本地模型经 Semaphore(1) 本就串行，
    批跑再显式串行，避免代理/后端争用（jev 预试批的教训：共享代理并发 → 全员超时）。
  - **默认 dry-run**：`--yes` 才真正启动任务（启动长跑任务属外向/不可逆动作，需人闸门）。
  - **失败即停**：默认一条失败就停下（`--keep-going` 继续）；失败卡片留在 implementation
    列（与 web 派发一致：看板无 blocked 列，人工介入后手动流转）。
  - **fail-open**：卡片流转/链接失败只警告，不回滚已跑任务。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import kanban
from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

SUCCESS_STAGES = ("DELIVERY_READY", "ACCEPTED_DELIVERY")
RUNNABLE_TYPES = ("implementation", "periodic")
DEFAULT_TIMEOUT = 3600


@dataclass
class BatchItem:
    card_id: str
    title: str
    repo: str
    task: str
    stage: str = ""
    automation: str = "pending"

    def as_dict(self) -> Dict[str, Any]:
        return {"card_id": self.card_id, "title": self.title, "repo": self.repo,
                "stage": self.stage, "automation": self.automation}


@dataclass
class BatchResult:
    ran: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    items: List[Dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {"dry_run": self.dry_run, "ran": self.ran, "succeeded": self.succeeded,
                "failed": self.failed, "skipped": self.skipped, "items": self.items}


def _card_task_text(card: Dict[str, Any]) -> str:
    text = str(card.get("title") or "")
    if card.get("description"):
        text += "\n\n" + str(card["description"])
    return text[:4000]


def select_cards(*, stage: str = "implementation", limit: int = 5,
                 card_ids: Optional[Sequence[str]] = None,
                 automation: str = "") -> List[BatchItem]:
    """从看板取批跑候选（显式 card_ids 时忽略列过滤，但仍校验可执行性与 repo 存在）。"""
    board = kanban.load_board(force=True)
    wanted = set(card_ids or [])
    items: List[BatchItem] = []
    for card in board.get("cards", []):
        if not isinstance(card, dict) or card.get("archived"):
            continue
        card_id = str(card.get("id") or "")
        if wanted and card_id not in wanted:
            continue
        if not wanted and str(card.get("stage") or "") != stage:
            continue
        if str(card.get("type") or "") not in RUNNABLE_TYPES:
            continue
        if automation and str(card.get("automation") or "") != automation:
            continue
        repo = str(card.get("repo") or "")
        if not repo or not Path(repo).is_dir():
            logger.warning("[batch] 跳过卡片 %s：repo 为空或不存在（%s）", card_id, repo or "<空>")
            continue
        items.append(BatchItem(card_id=card_id, title=str(card.get("title") or ""),
                               repo=repo, task=_card_task_text(card),
                               stage=str(card.get("stage") or ""),
                               automation=str(card.get("automation") or "pending")))
        if limit and len(items) >= int(limit):
            break
    return items


def _task_status(task_id: str) -> str:
    meta_path = AGENT_GO_DIR / task_id / "meta.json"
    if not meta_path.exists():
        return ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    try:
        from .status import normalize_task_status
        return str(normalize_task_status(meta.get("status", ""), meta))
    except Exception:
        return str(meta.get("status") or "")


def _parse_task_id(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = ev.get("task_id") or (ev.get("data") or {}).get("task_id")
        if tid:
            return str(tid)
    return ""


def run_batch(items: Sequence[BatchItem], *, dry_run: bool = True, keep_going: bool = False,
              timeout: int = DEFAULT_TIMEOUT, parallel: int = 1) -> BatchResult:
    """串行执行批跑；返回汇总（dry-run 只列计划，不启动任何进程）。"""
    result = BatchResult(dry_run=dry_run)
    if not items:
        return result
    for item in items:
        record: Dict[str, Any] = item.as_dict()
        if dry_run:
            record["status"] = "planned"
            result.items.append(record)
            continue
        argv = [sys.executable, "-m", "agent_go", "--json", "run", item.repo, item.task,
                "--yes", "--parallel", str(max(1, int(parallel)))]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            stdout, returncode = proc.stdout or "", proc.returncode
        except subprocess.TimeoutExpired:
            record.update({"status": "timeout", "timeout_sec": timeout})
            result.failed += 1
            result.items.append(record)
            if not keep_going:
                break
            continue
        except OSError as e:
            record.update({"status": "spawn_failed", "error": str(e)[:200]})
            result.failed += 1
            result.items.append(record)
            if not keep_going:
                break
            continue
        task_id = _parse_task_id(stdout)
        status = _task_status(task_id) if task_id else ""
        record.update({"task_id": task_id, "returncode": returncode,
                       "status": status or ("failed" if returncode else "unknown")})
        result.ran += 1
        if status in SUCCESS_STAGES:
            result.succeeded += 1
            try:
                kanban.dispatch_card(item.card_id, task_id, to_stage="operations",
                                     note=f"批跑完成（{status}）")
            except Exception as e:  # noqa: BLE001 - 流转失败不回滚任务
                logger.warning("[batch] 卡片 %s 回流失败: %s", item.card_id, e)
        else:
            result.failed += 1
            try:
                if task_id:
                    kanban.link_task(item.card_id, task_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[batch] 卡片 %s 链接失败: %s", item.card_id, e)
        result.items.append(record)
        if result.failed and not keep_going:
            break
    return result
