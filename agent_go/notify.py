"""M1 通知通道配置化 — 多通道事件通知（desktop / webhook / command）。

设计稿: docs/design/notification-webhook-spec.md

铁律：
- secret 只允许 ${ENV_VAR} 插值，不写 config 明文；插值失败跳过该通道
- payload 白名单制，不含任何密钥
- webhook 必须 https（localhost/127.0.0.1 例外）
- 通知任何失败不得影响管线，全部 catch 后留痕
"""

import json
import logging
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import log_event
from .metrics import aggregate_metering

logger = logging.getLogger(__name__)

__all__ = ["notify_event", "build_payload"]

EVENTS = ("on_complete", "on_failed", "on_blocked", "subtask_failed")

_FAILURE_REASON_MAX = 500   # IM 消息上限保护，超出截断并标注 truncated
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DEFAULT_TIMEOUT = 5
_DEFAULT_RETRY = 1


# ═══════════════════════════════════════════════════════════════
# 配置解析（含 behavior.notify_* 兼容层）
# ═══════════════════════════════════════════════════════════════

def _resolve_notify_config(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    """解析通知配置。返回 None 表示通知整体关闭。

    兼容策略（设计稿 §3）：
    - 无 notify 块 → 走 behavior.notify_on_complete / notify_command 旧配置
    - 有 notify 块 → 以 notify 为准；若 behavior.notify_command 非空则 warning 提示迁移
    """
    behavior = config.get("behavior", {})
    notify = config.get("notify")

    if notify is None:
        if not behavior.get("notify_on_complete", True):
            return None
        channels: list[dict[str, Any]] = [{"type": "desktop"}]
        if behavior.get("notify_command"):
            channels.append({"type": "command", "command": behavior["notify_command"]})
        return {"enabled": True, "timeout_sec": _DEFAULT_TIMEOUT,
                "retry": _DEFAULT_RETRY, "channels": channels}

    if not notify.get("enabled", True):
        return None
    if behavior.get("notify_command"):
        logger.warning("已配置 notify 块，behavior.notify_command 将被忽略，请迁移到 notify.channels")
    return {
        "enabled": True,
        "timeout_sec": notify.get("timeout_sec", _DEFAULT_TIMEOUT),
        "retry": notify.get("retry", _DEFAULT_RETRY),
        "channels": notify.get("channels", []),
    }


# ═══════════════════════════════════════════════════════════════
# Payload 组装（全部为已有数据源：M2 failure_reason / S1 metering / .preserved）
# ═══════════════════════════════════════════════════════════════

def _parse_created(created: str) -> Optional[float]:
    """meta.created 格式为 20260725-030125-545（带毫秒后缀），剥离后解析。"""
    try:
        clean = created.rsplit("-", 1)[0] if created.count("-") == 2 else created
        return datetime.strptime(clean, "%Y%m%d-%H%M%S").timestamp()
    except (ValueError, TypeError):
        return None


def build_payload(event: str, context: dict[str, Any]) -> dict[str, Any]:
    """组装通用 payload（字段白名单制，不接受 config 扩展字段）。"""
    meta = context.get("meta", {})

    # S6: subtask_failed 事件走独立路径（无完整 results_map）
    if event == "subtask_failed":
        subtask = context.get("subtask", {})
        result = context.get("result", {})
        task_dir = Path(context.get("task_dir", "."))
        task_id = meta.get("task_id", "")
        reason = result.get("failure_reason", "") or "未知错误"
        title = subtask.get("title", "")
        return {
            "event": event,
            "task_id": task_id,
            "task": meta.get("task", ""),
            "repo": meta.get("repo", ""),
            "subtask_id": subtask.get("id", ""),
            "subtask_title": title,
            "failure_reason": reason,
            "status": result.get("status", "failed"),
            "duration_sec": result.get("duration_sec", 0),
            "task_dir": str(task_dir),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    results_map = context.get("results_map", {})
    task_dir = Path(context.get("task_dir", "."))
    task_id = meta.get("task_id", "")
    results = [r for r in results_map.values() if isinstance(r, dict)]

    titles = {s.get("id"): s.get("title", "") for s in meta.get("subtasks", [])}

    counts = {"total": len(meta.get("subtasks", [])) or len(results),
              "completed": 0, "failed": 0, "blocked": 0}
    failures = []
    truncated = False
    for r in results:
        status = r.get("status", "")
        if status in ("completed", "no_changes", "degraded"):
            counts["completed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status == "blocked":
            counts["blocked"] += 1
        if status in ("failed", "blocked") and r.get("failure_reason"):
            reason = r["failure_reason"]
            if len(reason) > _FAILURE_REASON_MAX:
                reason = reason[:_FAILURE_REASON_MAX] + "…"
                truncated = True
            failures.append({
                "subtask_id": r.get("subtask_id", ""),
                "title": titles.get(r.get("subtask_id", ""), ""),
                "failure_reason": reason,
            })

    # 保留 worktree：读 .preserved 标记（worktree 清理之后调用，标记为最终态）
    preserved = []
    for marker in sorted(task_dir.glob("*/.preserved")):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            preserved.append({
                "subtask_id": data.get("subtask_id", marker.parent.name),
                "path": str(marker.parent / "work"),
                "branch": data.get("branch", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue

    # 成本：复用 S1 计量聚合
    totals = aggregate_metering(task_dir / "metering.jsonl")
    cost = {
        "total_usd": totals["cost_usd"],
        "by_role": {r: v["cost_usd"] for r, v in totals["by_role"].items()},
    }

    duration_sec = context.get("duration_sec")
    if duration_sec is None:
        created_ts = _parse_created(meta.get("created", ""))
        duration_sec = round(time.time() - created_ts) if created_ts else 0

    payload = {
        "event": event,
        "task_id": task_id,
        "task": meta.get("task", ""),
        "repo": meta.get("repo", ""),
        "status": meta.get("status", ""),
        "subtasks": counts,
        "duration_sec": duration_sec,
        "failures": failures,
        "preserved_worktrees": preserved,
        "cost": cost,
        "task_dir": str(task_dir),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _summary_line(payload: dict[str, Any]) -> str:
    c = payload["subtasks"]
    event = payload["event"]
    if event == "subtask_failed":
        return (f"❌ agent_go {payload['task_id']}: 子任务「{payload.get('subtask_title', '')}」失败"
                f" — {payload.get('failure_reason', '')[:120]}")

    icon = {"on_complete": "🎉", "on_failed": "❌", "on_blocked": "🔗"}.get(event, "🤖")
    parts = [f"{icon} agent_go {payload['task_id']}: {payload['status']} "
             f"({c['completed']}/{c['total']} 完成"]
    if c["failed"]:
        parts.append(f", {c['failed']} 失败")
    if c["blocked"]:
        parts.append(f", {c['blocked']} 阻断")
    parts.append(f", {payload['duration_sec']}s")
    cost = payload.get("cost", {}).get("total_usd", 0)
    if cost:
        parts.append(f", ${cost:.4f}")
    parts.append(")")
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════
# Webhook 适配器（纯渲染函数）
# ═══════════════════════════════════════════════════════════════

def _render_webhook_body(fmt: str, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """按 format 渲染 (body, extra_headers)。"""
    summary = _summary_line(payload)
    failure_lines = "\n".join(
        f"• {f['subtask_id']} {f['title']}: {f['failure_reason']}"
        for f in payload["failures"]
    )

    if fmt == "slack":
        text = summary + (f"\n{failure_lines}" if failure_lines else "")
        body = {"text": text}
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), {}

    if fmt in ("dingtalk", "wecom"):
        md = f"### {summary}"
        if failure_lines:
            md += f"\n\n{failure_lines}"
        if payload["preserved_worktrees"]:
            md += "\n\n保留现场: " + ", ".join(p["subtask_id"] for p in payload["preserved_worktrees"])
        body_md: dict[str, Any] = {"msgtype": "markdown", "markdown": {"title": summary[:80], "text": md}}
        return json.dumps(body_md, ensure_ascii=False).encode("utf-8"), {}

    if fmt == "ntfy":
        text = summary + (f"\n{failure_lines}" if failure_lines else "")
        return text.encode("utf-8"), {"X-Title": f"agent_go {payload['status']}"}

    # generic：通用 payload 原样 POST
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), {}


# ═══════════════════════════════════════════════════════════════
# 通道发送
# ═══════════════════════════════════════════════════════════════

def _interpolate(text: str) -> Optional[str]:
    """${VAR} 环境变量插值。任一变量未设置 → 返回 None（调用方跳过该通道）。"""
    missing = [m for m in _ENV_PATTERN.findall(text) if m not in os.environ]
    if missing:
        logger.warning(f"通知通道跳过：环境变量未设置 {missing}")
        return None
    return _ENV_PATTERN.sub(lambda m: os.environ[m.group(1)], text)


def _is_allowed_url(url: str) -> bool:
    """webhook 必须 https；localhost/127.0.0.1 例外（自建 ntfy 等内网服务）。"""
    if url.startswith("https://"):
        return True
    if url.startswith("http://"):
        host = url[len("http://"):].split("/")[0].split(":")[0]
        return host in ("localhost", "127.0.0.1")
    return False


def _send_desktop(payload: dict[str, Any], timeout: int) -> None:
    """macOS 桌面通知（迁移自原 _notify_complete）。"""
    msg = _summary_line(payload)
    title = f"🤖 agent_go: {payload['task_id']}"
    # 双引号转义防 AppleScript 注入
    script = 'display notification "{}" with title "{}"'.format(
        msg.replace('"', '\\"'), title.replace('"', '\\"'))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        pass  # 非 macOS 环境
    except subprocess.TimeoutExpired:
        logger.debug("桌面通知超时")


def _send_webhook(payload: dict[str, Any], channel: dict[str, Any],
                  timeout: int, retry: int) -> None:
    url = _interpolate(channel.get("url", ""))
    if not url:
        return
    if not _is_allowed_url(url):
        logger.warning(f"webhook URL 必须是 https（localhost 例外），已跳过: {url[:60]}")
        return

    headers = {"Content-Type": "application/json"}
    for k, v in channel.get("headers", {}).items():
        iv = _interpolate(str(v))
        if iv is None:
            return  # header 插值失败 → 跳过该通道
        headers[k] = iv

    body, extra = _render_webhook_body(channel.get("format", "generic"), payload)
    headers.update(extra)

    attempts = 1 + max(0, retry)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                logger.info(f"webhook 通知已发送 ({channel.get('name', url[:40])}): HTTP {resp.status}")
            return
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                logger.warning(f"webhook 4xx 不重试 ({e.code}): {url[:60]}")
                return
            logger.debug(f"webhook 5xx ({e.code})，第 {attempt + 1}/{attempts} 次")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.debug(f"webhook 网络错误，第 {attempt + 1}/{attempts} 次: {e}")
    logger.warning(f"webhook 通知失败（{attempts} 次尝试）: {url[:60]}")


def _send_command(payload: dict[str, Any], channel: dict[str, Any], timeout: int) -> None:
    """自定义命令通道。模板变量只暴露系统生成的安全标量
    （不含 failure_reason——LLM 输出属不可信输入，防 shell 注入）。"""
    cmd_tpl = _interpolate(channel.get("command", ""))
    if not cmd_tpl:
        return
    c = payload["subtasks"]
    safe_vars = {
        "event": payload["event"],
        "task_id": payload["task_id"],
        "status": payload["status"],
        "completed": c["completed"],
        "total": c["total"],
        "failed": c["failed"],
        "blocked": c["blocked"],
        "duration_sec": payload["duration_sec"],
        "cost_usd": payload["cost"]["total_usd"],
        "message": _summary_line(payload),
    }
    try:
        formatted = cmd_tpl.format(**safe_vars)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"通知命令模板变量错误: {e}")
        return
    try:
        subprocess.run(shlex.split(formatted), capture_output=True, timeout=timeout)
    except Exception as e:
        logger.debug(f"自定义通知命令失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def notify_event(event: str, context: dict[str, Any], config: dict[str, Any]) -> None:
    """派发通知事件。任何通道失败都不影响调用方。"""
    if event not in EVENTS:
        logger.debug(f"未知通知事件: {event}")
        return
    cfg = _resolve_notify_config(config)
    if cfg is None:
        return

    payload = build_payload(event, context)
    log_event(logger, "notify_event", {
        "event": event, "task_id": payload["task_id"],
        "channels": len(cfg["channels"]),
    })

    timeout = cfg["timeout_sec"]
    for channel in cfg["channels"]:
        try:
            if event not in channel.get("events", EVENTS):
                continue
            ctype = channel.get("type", "")
            if ctype == "desktop":
                _send_desktop(payload, timeout)
            elif ctype == "webhook":
                _send_webhook(payload, channel, timeout, cfg["retry"])
            elif ctype == "command":
                _send_command(payload, channel, timeout)
            else:
                logger.warning(f"未知通知通道类型: {ctype}")
        except Exception as e:
            # 故障隔离：通知失败绝不阻断管线
            log_event(logger, "notify_error", {"type": channel.get("type", "?"), "error": str(e)[:200]})
