"""Web 看板（Kanban）数据层：单文件 ~/.agent_go/kanban.json，纯函数 + mtime 缓存。

设计要点（docs/design/kanban-board.md）：
  - 看板 stage 是需求管理层，与 status.py 的执行状态机正交；
    卡片通过 task_ids[] 软链接执行任务，执行状态实时从 meta.json 派生，不冗余在卡片上。
  - 5 阶段列：brainstorm → requirements → design → implementation → operations
  - 3 类卡片：discussion（人+AI 讨论）/ implementation（AI 落地实施）/ periodic（周期任务）
  - 周期任务 = 外部触发：卡片只存 cron 表达式（展示用），系统 crontab 定时调 dispatch API。

卡片模型（dict）：
  {
    "id": "card-<12位小写字母数字>",
    "title": str,
    "stage": "brainstorm",            # STAGES 之一
    "type": "discussion|implementation|periodic",
    "repo": str,                      # 目标仓库路径（implementation/periodic 必填）
    "description": str,               # markdown，沉淀人+AI 讨论内容
    "spec_path": str,                 # 可选，关联 Task Spec 文件
    "cron": str,                      # periodic 专用，展示用 cron 表达式
    "task_ids": [str],                # 软链接 agent_go 执行任务
    "archived": False,
    "created": iso, "updated": iso,
    "history": [{"ts", "action", "from"?, "to"?, "note"?}]
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import string
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

# 阶段定义（顺序即列顺序）
STAGES = [
    ("brainstorm", "💡 头脑风暴"),
    ("requirements", "📝 需求生成"),
    ("design", "🎨 设计方案讨论"),
    ("implementation", "🔨 落地实现"),
    ("operations", "📈 运营优化"),
]
STAGE_KEYS = {k for k, _ in STAGES}

CARD_TYPES = {"discussion": "💬 讨论", "implementation": "🤖 实施", "periodic": "🔁 周期"}

# 必须填 repo 的卡片类型（可派发执行的前提）
_REPO_REQUIRED_TYPES = {"implementation", "periodic"}

# card_id 合法格式（防路径穿越/注入，与 web 层 _TASK_ID_RE 同思路）
_CARD_ID_RE = re.compile(r"^card-[A-Za-z0-9-]+$")

_CARD_ID_ALPHABET = string.ascii_lowercase + string.digits

# update_card 允许修改的字段白名单
_UPDATABLE_FIELDS = ("title", "description", "repo", "cron", "spec_path")

BOARD_VERSION = 1


class KanbanError(Exception):
    """看板业务异常（与 ProfileError/TaskRunnerError 同级，web 层映射 422）。"""


def board_path() -> Path:
    """看板数据文件路径。运行时读模块级 AGENT_GO_DIR（测试可 monkeypatch）。"""
    return AGENT_GO_DIR / "kanban.json"


# ── 加载 / 保存（mtime 缓存 + 原子写）────────────────────────

_board_cache: Optional[dict] = None
_board_mtime: float = 0.0
_lock = threading.Lock()  # web 是 ThreadingHTTPServer，读写改需串行化


def _empty_board() -> dict:
    return {"version": BOARD_VERSION, "cards": []}


def load_board(force: bool = False) -> dict:
    """加载看板（mtime 缓存，仿 models_registry.load_registry）。

    文件不存在/损坏 → 空看板（fallback，不阻断 web）。
    """
    global _board_cache, _board_mtime
    path = board_path()
    if not path.exists():
        _board_cache = _empty_board()
        _board_mtime = 0.0
        return _board_cache
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _board_cache or _empty_board()
    if not force and _board_cache is not None and mtime == _board_mtime:
        return _board_cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("kanban.json 读取失败（%s），回退空看板: %s", path, e)
        _board_cache = _empty_board()
        _board_mtime = 0.0
        return _board_cache
    if not isinstance(raw, dict) or not isinstance(raw.get("cards"), list):
        logger.warning("kanban.json 结构非法（缺 cards 列表），回退空看板")
        _board_cache = _empty_board()
        _board_mtime = 0.0
        return _board_cache
    _board_cache = raw
    _board_mtime = mtime
    return _board_cache


def _save_board(board: dict) -> None:
    """原子写看板（tmp + os.replace）+ 清缓存。调用方须持 _lock。"""
    global _board_cache, _board_mtime
    path = board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _board_cache = None
    _board_mtime = 0.0


# ── 校验与查找 ───────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_card_id() -> str:
    return "card-" + "".join(secrets.choice(_CARD_ID_ALPHABET) for _ in range(12))


def _validate_card_id(card_id: str) -> None:
    if not _CARD_ID_RE.match(card_id or ""):
        raise KanbanError(f"非法 card_id: {card_id!r}")


def _validate_stage(stage: str) -> None:
    if stage not in STAGE_KEYS:
        raise KanbanError(f"非法 stage: {stage!r}（须为 {'/'.join(k for k, _ in STAGES)}）")


def _validate_type(card_type: str) -> None:
    if card_type not in CARD_TYPES:
        raise KanbanError(f"非法 type: {card_type!r}（须为 {'/'.join(CARD_TYPES)}）")


def _validate_repo(card_type: str, repo: str) -> None:
    if card_type in _REPO_REQUIRED_TYPES and not repo.strip():
        raise KanbanError(f"type={card_type} 的卡片必须填写 repo（目标仓库路径）")


def _find_card(board: dict, card_id: str) -> Optional[dict]:
    for card in board.get("cards", []):
        if isinstance(card, dict) and card.get("id") == card_id:
            return card
    return None


def get_card(card_id: str) -> Optional[dict]:
    """按 id 查卡片。id 格式非法抛 KanbanError；不存在返回 None（web 层映射 404）。"""
    _validate_card_id(card_id)
    return _find_card(load_board(), card_id)


def _require_card(board: dict, card_id: str) -> dict:
    card = _find_card(board, card_id)
    if card is None:
        raise KanbanError(f"卡片不存在: {card_id}")
    return card


def _append_history(card: dict, action: str, **extra: str) -> None:
    entry: dict = {"ts": _now_iso(), "action": action}
    entry.update({k: v for k, v in extra.items() if v})
    card.setdefault("history", []).append(entry)


# ── 卡片操作（读-改-写在锁内完成）────────────────────────────


def create_card(title: str, type: str, stage: str = "brainstorm", repo: str = "",
                description: str = "", cron: str = "", spec_path: str = "") -> dict:
    """新建卡片。type/stage/repo 校验失败抛 KanbanError。"""
    title = (title or "").strip()
    if not title:
        raise KanbanError("title 不能为空")
    _validate_type(type)
    _validate_stage(stage)
    repo = (repo or "").strip()
    _validate_repo(type, repo)
    with _lock:
        board = load_board()
        now = _now_iso()
        card = {
            "id": _new_card_id(),
            "title": title,
            "stage": stage,
            "type": type,
            "repo": repo,
            "description": description or "",
            "spec_path": spec_path or "",
            "cron": (cron or "").strip(),
            "task_ids": [],
            "archived": False,
            "created": now,
            "updated": now,
            "history": [],
        }
        _append_history(card, "create", note=f"创建于 {stage}")
        board["cards"].append(card)
        _save_board(board)
    return card


def update_card(card_id: str, **fields: str) -> dict:
    """更新卡片字段（白名单：title/description/repo/cron/spec_path）。"""
    _validate_card_id(card_id)
    bad = [k for k in fields if k not in _UPDATABLE_FIELDS]
    if bad:
        raise KanbanError(f"不可更新字段: {', '.join(sorted(bad))}")
    with _lock:
        board = load_board()
        card = _require_card(board, card_id)
        for k, v in fields.items():
            card[k] = v
        if not (card.get("title") or "").strip():
            raise KanbanError("title 不能为空")
        _validate_repo(card.get("type", ""), card.get("repo") or "")
        card["updated"] = _now_iso()
        _append_history(card, "update", note=f"更新字段: {', '.join(sorted(fields))}")
        _save_board(board)
    return card


def move_card(card_id: str, to_stage: str, note: str = "") -> dict:
    """阶段流转（MVP 允许任意方向流转，PM 灵活性优先，history 留痕）。"""
    _validate_card_id(card_id)
    _validate_stage(to_stage)
    with _lock:
        board = load_board()
        card = _require_card(board, card_id)
        from_stage = card.get("stage", "")
        card["stage"] = to_stage
        card["updated"] = _now_iso()
        _append_history(card, "move", **{"from": from_stage, "to": to_stage, "note": note})
        _save_board(board)
    return card


def archive_card(card_id: str, archived: bool = True) -> dict:
    """归档/取消归档（归档卡片默认不在看板展示）。"""
    _validate_card_id(card_id)
    with _lock:
        board = load_board()
        card = _require_card(board, card_id)
        card["archived"] = bool(archived)
        card["updated"] = _now_iso()
        _append_history(card, "archive" if archived else "unarchive")
        _save_board(board)
    return card


def link_task(card_id: str, task_id: str) -> dict:
    """软链接执行任务（去重追加 task_ids，history 记 link）。"""
    _validate_card_id(card_id)
    if not task_id:
        raise KanbanError("task_id 不能为空")
    with _lock:
        board = load_board()
        card = _require_card(board, card_id)
        if task_id not in card.setdefault("task_ids", []):
            card["task_ids"].append(task_id)
        card["updated"] = _now_iso()
        _append_history(card, "link", note=task_id)
        _save_board(board)
    return card


def delete_card(card_id: str) -> None:
    """物理删除卡片。已派发过任务（task_ids 非空）的卡片拒绝删除（防御数据堆积，
    保留追溯链；不要了请归档）。"""
    _validate_card_id(card_id)
    with _lock:
        board = load_board()
        card = _require_card(board, card_id)
        if card.get("task_ids"):
            raise KanbanError("卡片已派发过任务（存在关联 task_id），不可删除；可改归档")
        board["cards"] = [c for c in board["cards"] if c.get("id") != card_id]
        _save_board(board)
