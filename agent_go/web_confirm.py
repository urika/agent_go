"""Web 计划确认协议（R5b）：pending/decision 文件协议 + 阻塞轮询。

背景：CLI 的 confirm_plan/confirm_subtasks 是 input() 阻塞式交互，web 后台子进程
无法应答。本模块提供第三种确认通道（v2 §3.4 的可注入确认函数）：

  子进程（agent_go run --confirm-mode web）：
    写 <task_dir>/pending_confirmation.json（stage + payload + 超时）
    → 每 2s 轮询 confirmation_decision.json → 读到匹配 stage 的决策 → 清理并继续

  web 端（POST /api/tasks/<id>/confirm）：
    校验 pending 存在 + stage 匹配 → 写 confirmation_decision.json

  超时（默认 30min）：自动按 "N"（取消）处理，任务退出，避免挂起（v2 风险表）。

decision 取值：stage=plan → Y（确认）/ R（重新生成）/ N（取消）；
              stage=subtasks → Y / N。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PENDING_FILE = "pending_confirmation.json"
DECISION_FILE = "confirmation_decision.json"
DEFAULT_TIMEOUT_SEC = 1800  # 30 分钟
POLL_INTERVAL_SEC = 2.0


def read_pending(task_dir: Path) -> Optional[dict]:
    """当前待确认项（web GET 端点数据源）。无则 None。"""
    pf = task_dir / PENDING_FILE
    if not pf.exists():
        return None
    try:
        data = json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def web_confirm(stage: str, payload: Any, task_dir: Path, logger: logging.Logger,
                timeout: float = DEFAULT_TIMEOUT_SEC) -> str:
    """写 pending 并阻塞等待 web 决策，返回 "Y"/"R"/"N"（超时按 "N"）。

    payload：plan dict（stage=plan）或 {"subtasks": [...]}（stage=subtasks），
    原样序列化供前端渲染。
    """
    pending = {
        "stage": stage,
        "payload": payload,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timeout_sec": int(timeout),
    }
    pending_path = task_dir / PENDING_FILE
    decision_path = task_dir / DECISION_FILE
    pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("web 确认等待中: stage=%s timeout=%ds（%s）", stage, timeout, pending_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if decision_path.exists():
            try:
                data = json.loads(decision_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            if isinstance(data, dict) and data.get("stage") == stage and data.get("decision"):
                decision = str(data["decision"]).upper()
                for p in (decision_path, pending_path):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                logger.info("web 确认收到决策: stage=%s decision=%s", stage, decision)
                return decision
        time.sleep(POLL_INTERVAL_SEC)
    # 超时：按取消处理（v2 风险表：超时自动 cancel）
    try:
        pending_path.unlink()
    except OSError:
        pass
    logger.warning("web 确认超时（%ds），按取消处理", timeout)
    return "N"
