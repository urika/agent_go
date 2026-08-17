"""Web 观察平台：以只读 HTTP 接口展示 agent_go 任务执行数据。

设计目标：
  - 只读：全部 GET，不触碰 worktree/git，不修改任何任务数据
  - 无框架：仅 stdlib http.server + 单文件 HTML/JS 前端
  - 复用现有解析：meta.json / metering.jsonl / execution.log / replay 时间线

CLI 入口: `agent_go web [--host 127.0.0.1] [--port 8091] [--token <secret>]`

API 一览（前缀 /api）：
  GET /api/tasks                      任务清单
  GET /api/tasks/<id>                 任务详情（subtasks + results）
  GET /api/tasks/<id>/<sub>/detail    子任务验证结果/改动统计
  GET /api/tasks/<id>/<sub>/log       子任务执行日志段
  GET /api/tasks/<id>/metering        metering 按 role 聚合 + 明细
  GET /api/tasks/<id>/replay          执行时间线（复用 replay.py）
  GET /api/tasks/<id>/plan            PLAN.md + plans/
  GET /api/tasks/<id>/assessment      假阳性评估事件（assessment.jsonl）
  GET /api/overview                   总览大盘：KPI + 近 7 天成本趋势
  GET /api/cost                       全局成本：by_model/by_role + Top 任务
  GET /api/models                     模型生产力：生产 metering + bench 对照
  GET /api/cross-judge                交叉评判矩阵（cross_judge_scores.jsonl）
  GET /api/bench-results              bench 模型对照结果
  GET /api/baseline                   claude 裸跑基线 + $/pass 门禁基线
  GET /api/config                     用户配置只读展示（api_key 脱敏）
  GET /api/storage                    磁盘占用 + 孤儿目录检测
  GET /api/events                     SSE：任务状态变化实时推送
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .status import task_status, normalize_task_status
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .config import AGENT_GO_DIR, CONFIG_PATH, load_config
from .console import _LazyConsole
from .profiles import (
    ProfileError,
    activate_cloud,
    activate_local,
    activate_profile,
    health_check,
    list_profiles,
    probe_local_models,
    read_current_profile,
)
from .task_runner import TaskRunnerError, task_runner
from . import kanban
from .kanban import KanbanError
# 复用 eval 的 JSONL/JSON 读取（与 bench/cross_judge 同源，避免实现漂移）
from .eval import _read_jsonl, _read_json

logger = logging.getLogger(__name__)
console = _LazyConsole()

MAX_LOG_LINE = 2000  # execution.log 单行截断长度（防大响应）

# 任务目录前缀：只有匹配 task-* 的目录才纳入观察（与 cmd_list 一致）
_TASK_PREFIX = "task-"

# task_id 合法格式：task-YYYYMMDD-HHMMSS[-mmm-hhhh]（兼容新旧两种格式）
# 严格校验防止路径穿越（../../etc/passwd 之类）
_TASK_ID_RE = re.compile(r"^task-\d{8}-\d{6}(?:-\d{3}-[0-9a-f]{4})?$")
# sub_id 合法格式：字母数字 + 连字符/下划线，禁止路径分隔符
_SUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _valid_task_id(task_id: str) -> bool:
    """校验 task_id 格式，防止路径穿越。"""
    return bool(_TASK_ID_RE.match(task_id))


def _valid_sub_id(sub_id: str) -> bool:
    """校验 sub_id 格式，禁止路径分隔符与 ..。"""
    return bool(_SUB_ID_RE.match(sub_id))


def _task_dir(task_id: str) -> Optional[Path]:
    """返回校验通过的任务目录 Path，不合法或不存在返回 None。"""
    if not _valid_task_id(task_id):
        return None
    td = AGENT_GO_DIR / task_id
    return td if td.is_dir() else None


def _list_task_dirs() -> list[Path]:
    if not AGENT_GO_DIR.exists():
        return []
    return sorted(
        d for d in AGENT_GO_DIR.iterdir()
        if d.is_dir() and d.name.startswith(_TASK_PREFIX) and (d / "meta.json").exists()
    )


def _task_meta(task_dir: Path) -> dict:
    return _read_json(task_dir / "meta.json")


def api_tasks(include_legacy: bool = False) -> list[dict]:
    """任务清单（轻量，不含 subtasks 明细）。

    include_legacy=False（默认）：仅展示新规范状态任务（status_schema_version 存在）。
    include_legacy=True：仅展示历史归档任务（无 status_schema_version），状态值经
        normalize_task_status 归一化后展示，供归档页面查询。
    """
    out = []
    for td in _list_task_dirs():
        meta = _task_meta(td)
        if not meta:  # eval._read_json 不存在/坏文件返回 {}
            continue
        has_schema = bool(meta.get("status_schema_version"))
        # 默认视图与归档视图互斥：新规范 vs legacy
        if include_legacy and has_schema:
            continue
        if not include_legacy and not has_schema:
            continue
        results = meta.get("results", []) or []
        # 从 results 汇总执行指标
        total_elapsed = 0.0
        retries = 0
        for r in results:
            total_elapsed += r.get("duration_sec", 0) or 0
            retries += r.get("retry_count", 0) or 0
        # 成本从 metering.jsonl 聚合（meta.json results 不含 cost）
        metering_records = _read_jsonl(td / "metering.jsonl")
        cost = sum(r.get("cost_usd", 0) or 0 for r in metering_records)
        try:
            mtime = td.joinpath("meta.json").stat().st_mtime
        except OSError:
            continue  # 目录刚被 clean 清理的竞态窗口，跳过
        out.append({
            "id": td.name,
            "task": meta.get("task", ""),
            # 归档视图：legacy 状态归一化展示（completed→DELIVERY_READY 等），复用筛选/颜色
            "status": normalize_task_status(meta.get("status"), meta) if include_legacy else task_status(meta),
            "repo": meta.get("repo", ""),
            "subtask_count": len(meta.get("subtasks", []) or []),
            "completed": sum(1 for r in results if r.get("status") == "completed"),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
            "blocked": sum(1 for r in results if r.get("status") == "blocked"),
            "cost_usd": round(cost, 4),
            "total_elapsed_sec": round(total_elapsed, 1),
            "total_retries": retries,
            "created": meta.get("created", ""),
            "mtime": mtime,
            # U1：web 确认模式待确认标记（pending_confirmation.json 存在 → 列表 🔔）
            "pending_confirmation": (td / "pending_confirmation.json").exists(),
        })
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def api_task(task_id: str) -> Optional[dict]:
    td = _task_dir(task_id)
    if td is None:
        return None
    meta = _task_meta(td)
    if not meta:
        return None
    subtasks = meta.get("subtasks", []) or []
    results = meta.get("results", []) or []
    # 按 subtask_id 匹配：运行中/崩溃恢复的任务 results 是完成顺序而非
    # 子任务顺序（pipeline 结束时才重排），下标配对会张冠李戴
    results_by_id = {r.get("subtask_id"): r for r in results
                     if isinstance(r, dict) and r.get("subtask_id")}
    items = []
    for i, st in enumerate(subtasks):
        r = results_by_id.get(st.get("id")) or {}
        items.append({
            "id": st.get("id", f"sub-{i+1}"),
            "title": st.get("title", ""),
            "difficulty": st.get("difficulty", ""),
            "task_type": st.get("task_type", ""),  # CR-G3：任务类型（security/bugfix/...）
            "depends_on": st.get("depends_on", []) or [],
            "skills": st.get("skills", []) or [],
            "agent_type": st.get("agent_type", ""),
            "verification": st.get("verification", []) or [],
            "status": r.get("status", "pending"),
            "duration_sec": r.get("duration_sec"),
            "retry_count": r.get("retry_count"),
            "verify_ok": r.get("verify_ok"),
            "exit_code": r.get("exit_code"),
            "summary": r.get("summary", ""),
            "failure_reason": r.get("failure_reason", ""),
            "worktree": r.get("worktree", ""),
            "agent_type_source": r.get("agent_type_source",
                                       st.get("_agent_type_source", "")),
        })
    return {
        "id": td.name,
        "task": meta.get("task", ""),
            "status": task_status(meta),
        "repo": meta.get("repo", ""),
        "created_at": meta.get("created", ""),
        "subtasks": items,
        # U5：本 web 实例是否托管该任务进程（cancel 边界标识数据源）
        "managed": task_runner.is_running(td.name),
        "meta": {
            k: meta.get(k) for k in ("planner_model", "source_batch")
            if meta.get(k)
        },
        # 谦逊层（#51）：已知盲区 + 未覆盖视角 + 层间归因（纯透传 meta 已聚合数据）
        "blind_spots": meta.get("blind_spots") or {},
        "uncovered_perspectives": meta.get("uncovered_perspectives") or [],
        "layer_attribution": meta.get("layer_attribution") or {},
    }


def api_subtask_detail(task_id: str, sub_id: str) -> Optional[dict]:
    if not _valid_sub_id(sub_id):
        return None
    td = _task_dir(task_id)
    if td is None:
        return None
    meta = _task_meta(td)
    if not meta:
        return None
    subtasks = meta.get("subtasks", []) or []
    results = meta.get("results", []) or []
    # 按 subtask_id 匹配（同 api_task：results 顺序不保证等于子任务顺序）
    results_by_id = {r.get("subtask_id"): r for r in results
                     if isinstance(r, dict) and r.get("subtask_id")}
    idx = next((i for i, st in enumerate(subtasks)
                if st.get("id") == sub_id), None)
    if idx is None:
        return None
    st = subtasks[idx]
    r = results_by_id.get(sub_id) or {}
    return {
        "id": sub_id,
        "title": st.get("title", ""),
        "description": st.get("description", ""),
        "difficulty": st.get("difficulty", ""),
        "task_type": st.get("task_type", ""),  # CR-G3：任务类型（security/bugfix/...）
        "depends_on": st.get("depends_on", []) or [],
        "files_hint": st.get("files_hint", []) or [],
        "risks": st.get("risks", []) or [],
        "skills": st.get("skills", []) or [],
        "agent_type": st.get("agent_type", ""),
        "verification": st.get("verification", []) or [],
        "agent_prompt": (st.get("agent_prompt") or "")[:2000],
        "result": {
            "status": r.get("status"),
            "duration_sec": r.get("duration_sec"),
            "retry_count": r.get("retry_count"),
            "verify_ok": r.get("verify_ok"),
            "exit_code": r.get("exit_code"),
            "summary": r.get("summary", ""),
            "failure_reason": r.get("failure_reason", ""),
            "worktree": r.get("worktree", ""),
            "change_stats": r.get("change_stats", {}),
            "verification_results": r.get("verification_results", []),
            "merge_results": r.get("merge_results", []),
            "sandbox_type": r.get("sandbox_type", ""),
            "agent_type_source": r.get("agent_type_source", ""),
            "skills_unresolved": r.get("skills_unresolved", []) or [],
        },
    }


def _extract_subtask_log(task_id: str, sub_id: str, limit: int = 400) -> list[dict]:
    """从 execution.log 提取该子任务的日志段。

    匹配规则：以子任务启动/提交标记为段落边界（如含 sub_id 的 INFO/DEBUG 行）。
    返回 [{line_no, text}]，每行截断 MAX_LOG_LINE 字符。
    """
    if not _valid_sub_id(sub_id):
        return []
    td = _task_dir(task_id)
    if td is None:
        return []
    log_path = td / "execution.log"
    if not log_path.exists():
        return []
    lines_out = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    # 边界匹配：避免 sub-1 误命中 sub-10/sub-11（子任务 >9 个时）
    sub_pat = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(sub_id)
                         + r"(?![A-Za-z0-9_-])")
    # 找子任务相关行：行内含 sub_id（作为 sub-N 出现的部分）
    start_idx = None
    for i, ln in enumerate(all_lines):
        if sub_pat.search(ln):
            start_idx = i
            break
    if start_idx is None:
        return []
    # 段落上边界：从 start_idx 往前回退到本子任务的启动标记行；
    # 遇到其他子任务的 "[subtask]" 标记即停（不得跨段）
    begin = start_idx
    for i in range(start_idx - 1, max(-1, start_idx - 200), -1):
        line = all_lines[i]
        if "[subtask]" in line or "start subtask" in line.lower():
            if sub_pat.search(line):
                begin = i
            break
    # 下边界：往后到下一个子任务启动标记或文件尾
    end = len(all_lines)
    for i in range(start_idx + 1, min(len(all_lines), start_idx + 3000)):
        if "[subtask]" in all_lines[i] and not sub_pat.search(all_lines[i]):
            end = i
            break
    for i in range(begin, min(end, len(all_lines))):
        ln = all_lines[i].rstrip("\n")
        lines_out.append({
            "line_no": i + 1,
            "text": ln[:MAX_LOG_LINE],
        })
        if len(lines_out) >= limit:
            break
    return lines_out


def api_metering(task_id: str) -> Optional[dict]:
    td = _task_dir(task_id)
    if td is None:
        return None
    records = _read_jsonl(td / "metering.jsonl")
    by_role: dict[str, dict] = {}
    rows = []
    for rec in records:
        role = rec.get("role", "unknown")
        cost = rec.get("cost_usd", 0) or 0
        prompt = rec.get("prompt_tokens", 0) or 0
        completion = rec.get("completion_tokens", 0) or 0
        latency = rec.get("latency_ms", 0) or 0
        agg = by_role.setdefault(role, {"count": 0, "cost": 0.0, "prompt": 0,
                                        "completion": 0, "latency": 0.0})
        agg["count"] += 1
        agg["cost"] += cost
        agg["prompt"] += prompt
        agg["completion"] += completion
        agg["latency"] += latency
        rows.append({
            "role": role,
            "virtual_model": rec.get("virtual_model", ""),
            "actual_model": rec.get("actual_model", ""),
            "routed_model": rec.get("routed_model", ""),
            "difficulty": rec.get("difficulty", ""),
            "subtask_id": rec.get("subtask_id", ""),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cost_usd": cost,
            "latency_ms": latency,
            "result": rec.get("result", ""),
            "ts": rec.get("ts", ""),
        })
    summary = {}
    for role, agg in by_role.items():
        summary[role] = {
            "count": agg["count"],
            "cost_usd": round(agg["cost"], 4),
            "prompt_tokens": agg["prompt"],
            "completion_tokens": agg["completion"],
            "latency_ms": round(agg["latency"], 1),
        }
    return {"summary": summary, "rows": rows}


def api_replay(task_id: str) -> Optional[dict]:
    from .replay import _build_timeline, _collect_summary
    td = _task_dir(task_id)
    if td is None:
        return None
    data = _read_json(td / "meta.json")
    if not data:
        return None
    try:
        timeline = _build_timeline(data)
        summary = _collect_summary(data)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("replay build failed: %s", exc)
        return {"timeline": [], "summary": {}, "error": str(exc)}
    return {"timeline": timeline, "summary": summary}


def api_plan(task_id: str) -> Optional[dict]:
    td = _task_dir(task_id)
    if td is None:
        return None
    plan_md = ""
    plan_md_path = td / "PLAN.md"
    if plan_md_path.exists():
        try:
            plan_md = plan_md_path.read_text(encoding="utf-8")[:20000]
        except OSError:
            plan_md = ""
    versions = []
    plans_dir = td / "plans"
    if plans_dir.is_dir():
        for p in sorted(plans_dir.glob("v*.json")):
            versions.append({
                "version": p.stem,
                "mtime": p.stat().st_mtime,
                "content": (p.read_text(encoding="utf-8", errors="replace") or "")[:20000],
            })
    return {"plan_md": plan_md, "versions": versions}


# ═══════════════════════════════════════════════════════════════
# 全局视图（跨任务聚合）— 解决观测缺口 B：无全局聚合视图
# ═══════════════════════════════════════════════════════════════

def _parse_date(ts: str) -> str:
    """从 metering ts（ISO 格式）提取 YYYY-MM-DD，失败返回空。"""
    if not ts or not isinstance(ts, str):
        return ""
    # 形如 2026-08-02T16:04:20 或 2026-08-02 16:04:20
    return ts[:10] if len(ts) >= 10 and ts[4] == "-" else ""


def api_overview() -> dict:
    """总览大盘：KPI 卡片 + 近 7 天成本趋势 + 健康告警。

    数据源：遍历所有任务的 meta.json + metering.jsonl。
    """
    today = time.strftime("%Y-%m-%d")
    # 新规范状态分组：13 个 canonical state 按阶段聚合为大盘可读的分类
    # DELIVERY_READY = 旧 completed（交付物就绪）；ACCEPTED_DELIVERY = 已验收
    task_counts = {"total": 0, "in_progress": 0, "delivered": 0,
                   "failed": 0, "blocked": 0, "today_delivered": 0, "today_cost": 0.0}
    # canonical → 大盘分组映射
    _IN_PROGRESS = {"EXECUTING", "PAUSED"}
    _DELIVERED = {"DELIVERY_READY", "ACCEPTED_DELIVERY"}
    _FAILED = {"VERIFICATION_FAILED", "DELIVERY_FAILED"}
    _BLOCKED = {"BLOCKED"}
    _CANCELLED = {"CANCELLED"}
    cost_by_day: dict[str, float] = {}  # YYYY-MM-DD -> cost
    dollar_per_pass_rate = None
    completed_with_cost = 0
    total_cost_for_pass = 0.0

    for td in _list_task_dirs():
        meta = _task_meta(td)
        if not meta:
            continue
        # 历史数据归档：跳过未迁移到新规范状态的任务（与 api_tasks 一致）
        if not meta.get("status_schema_version"):
            continue
        task_counts["total"] += 1
        status = task_status(meta)
        if status in _IN_PROGRESS:
            task_counts["in_progress"] += 1
        elif status in _DELIVERED:
            task_counts["delivered"] += 1
            created = meta.get("created", "")
            if created.startswith(today):
                task_counts["today_delivered"] += 1
        elif status in _FAILED:
            task_counts["failed"] += 1
        elif status in _BLOCKED:
            task_counts["blocked"] += 1
        # CANCELLED 不计入任何正/负向 KPI（用户主动取消）

        results = meta.get("results", []) or []
        completed = sum(1 for r in results if r.get("status") == "completed")

        # 成本按天聚合（从 metering 的 ts）
        metering = _read_jsonl(td / "metering.jsonl")
        task_cost = 0.0
        for rec in metering:
            cost = rec.get("cost_usd", 0) or 0
            task_cost += cost
            day = _parse_date(rec.get("ts", ""))
            if day:
                cost_by_day[day] = cost_by_day.get(day, 0.0) + cost

        # $/pass rate（completed > 0 时才计入）
        if completed > 0 and task_cost > 0:
            completed_with_cost += completed
            total_cost_for_pass += task_cost

    if completed_with_cost > 0:
        dollar_per_pass_rate = round(total_cost_for_pass / completed_with_cost, 6)

    task_counts["today_cost"] = round(cost_by_day.get(today, 0.0), 4)

    # 近 7 天趋势
    days_7 = []
    for i in range(6, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        days_7.append({"date": d, "cost": round(cost_by_day.get(d, 0.0), 4)})

    return {
        "kpi": task_counts,
        "dollar_per_pass_rate": dollar_per_pass_rate,
        "cost_trend_7d": days_7,
    }


def api_cost() -> dict:
    """全局成本分析：by_model / by_role 分解 + Top N 任务。

    数据源：全部任务的 metering.jsonl（按 actual_model / role 聚合）。
    """
    by_model: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    task_costs: list[dict] = []
    total_cost = 0.0

    for td in _list_task_dirs():
        metering = _read_jsonl(td / "metering.jsonl")
        task_cost = sum(r.get("cost_usd", 0) or 0 for r in metering)
        if task_cost > 0:
            task_costs.append({"task_id": td.name, "cost": round(task_cost, 4)})
        total_cost += task_cost
        for rec in metering:
            # 过滤验证重试占位符（非真实模型调用，cost=0/tokens=null，避免污染模型列表）
            if rec.get("actual_model") == "verify_retry" or rec.get("result") == "retry":
                continue
            cost = rec.get("cost_usd", 0) or 0
            model = rec.get("actual_model") or rec.get("virtual_model") or "unknown"
            role = rec.get("role", "unknown")
            m = by_model.setdefault(model, {"cost": 0.0, "calls": 0,
                                            "prompt_tokens": 0, "completion_tokens": 0,
                                            "routed_model": "", "resolved": True})
            m["cost"] += cost
            m["calls"] += 1
            m["prompt_tokens"] += rec.get("prompt_tokens", 0) or 0
            m["completion_tokens"] += rec.get("completion_tokens", 0) or 0
            # 记录路由名（用于 UI 标注路由别名→真实后端）+ 解析置信度
            rm = rec.get("routed_model", "") or ""
            if rm and not m.get("routed_model"):
                m["routed_model"] = rm
            if rec.get("actual_model_resolved") is False:
                m["resolved"] = False  # 任一记录回退则标记整组未解析
            r = by_role.setdefault(role, {"cost": 0.0, "calls": 0})
            r["cost"] += cost
            r["calls"] += 1

    # 排序 + 占比
    def _with_pct(d: dict, total: float) -> list[dict]:
        items = [{"name": k, **{kk: round(vv, 4) if isinstance(vv, float) else vv
                                for kk, vv in v.items()},
                  "pct": round(v["cost"] / total * 100, 2) if total > 0 else 0}
                 for k, v in d.items()]
        return sorted(items, key=lambda x: x["cost"], reverse=True)

    task_costs.sort(key=lambda x: x["cost"], reverse=True)
    return {
        "total_cost": round(total_cost, 4),
        "by_model": _with_pct(by_model, total_cost),
        "by_role": _with_pct(by_role, total_cost),
        "top_tasks": task_costs[:20],
    }


def api_models() -> dict:
    """模型生产力对比：生产 metering 聚合 + bench results.jsonl 对照。

    生产数据：从全部任务 metering 聚合每模型（worker 角色）的 cost/calls/task 数。
    Bench 数据：读 eval_suite/results.jsonl（bench 产物），按 model 聚合。
    """
    # ── 生产数据 ──
    prod: dict[str, dict] = {}
    for td in _list_task_dirs():
        for rec in _read_jsonl(td / "metering.jsonl"):
            # 过滤验证重试占位符（非真实模型调用）
            if rec.get("actual_model") == "verify_retry" or rec.get("result") == "retry":
                continue
            model = rec.get("actual_model") or rec.get("virtual_model") or "unknown"
            # 只统计 worker 角色的（代表实际执行模型）
            if rec.get("role") != "worker":
                continue
            m = prod.setdefault(model, {"cost": 0.0, "calls": 0, "tasks": set(),
                                        "routed_model": "", "resolved": True})
            m["cost"] += rec.get("cost_usd", 0) or 0
            m["calls"] += 1
            m["tasks"].add(td.name)
            rm = rec.get("routed_model", "") or ""
            if rm and not m.get("routed_model"):
                m["routed_model"] = rm
            if rec.get("actual_model_resolved") is False:
                m["resolved"] = False
    prod_rows = [{
        "model": k,
        "cost": round(v["cost"], 4),
        "calls": v["calls"],
        "task_count": len(v["tasks"]),
        "avg_cost_per_call": round(v["cost"] / v["calls"], 6) if v["calls"] else 0,
        "routed_model": v.get("routed_model", ""),
        "resolved": v.get("resolved", True),
    } for k, v in prod.items()]
    prod_rows.sort(key=lambda x: x["cost"], reverse=True)

    # ── Bench 数据 ──
    bench_rows = []
    bench_path = _bench_results_path()
    if bench_path.exists():
        from .bench import analyze_model_productivity
        analysis = analyze_model_productivity(bench_path)
        if "models" in analysis:
            for model, info in analysis["models"].items():
                bench_rows.append({
                    "model": model,
                    "sample_size": info.get("sample_size", 0),
                    "avg_pass_rate": info.get("avg_pass_rate", 0),
                    "avg_cost_usd": info.get("avg_cost_usd", 0),
                    "dollar_per_pass": info.get("dollar_per_pass", 0),
                    "recommendation": info.get("recommendation", ""),
                })

    return {"production": prod_rows, "bench": bench_rows}


def _bench_results_path() -> Path:
    """定位 eval_suite/results.jsonl（优先 cwd，回退仓库根）。"""
    cwd_candidate = Path.cwd() / "eval_suite" / "results.jsonl"
    if cwd_candidate.exists():
        return cwd_candidate
    # 回退：web_server.py 所在包的上两级（仓库根）
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "eval_suite" / "results.jsonl"


def _resolve_workspace_file(name: str) -> Path:
    """定位工作区下的文件（优先 cwd，回退仓库根）。"""
    cwd_candidate = Path.cwd() / name
    if cwd_candidate.exists():
        return cwd_candidate
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / name


# ═══════════════════════════════════════════════════════════════
# 数据对象黑洞（P1）— assessment / cross_judge / bench-results / baseline
# ═══════════════════════════════════════════════════════════════

def api_assessment(task_id: str) -> Optional[dict]:
    """假阳性评估事件（task 级 assessment.jsonl）。

    返回逐事件明细 + 聚合（按 passed/confidence 分布）。
    """
    td = _task_dir(task_id)
    if td is None:
        return None
    records = _read_jsonl(td / "assessment.jsonl")
    passed = sum(1 for r in records if r.get("passed"))
    failed = sum(1 for r in records if not r.get("passed"))
    by_model: dict[str, int] = {}
    for r in records:
        m = r.get("evaluator_model", "unknown")
        by_model[m] = by_model.get(m, 0) + 1
    return {
        "total": len(records),
        "passed": passed,
        "failed": failed,
        "false_positive_rate": round(failed / len(records), 4) if records else 0,
        "by_evaluator_model": by_model,
        "records": records[:200],  # 限制响应大小
    }


def api_cross_judge() -> dict:
    """交叉评判矩阵（cross_judge_scores.jsonl）。

    返回 candidate × judge 的评分矩阵 + 自评拦截统计。
    """
    path = _resolve_workspace_file("cross_judge_scores.jsonl")
    records = _read_jsonl(path)
    # 按 candidate_model × judge_model 聚合
    matrix: dict[tuple[str, str], list[float]] = {}
    self_blocked = 0
    errors = 0
    for r in records:
        if r.get("error"):
            if "自评禁止" in (r.get("error") or ""):
                self_blocked += 1
            else:
                errors += 1
            continue
        cand = r.get("candidate_model", "?")
        judge = r.get("judge_model", "?")
        score = r.get("semantic_score", 0)
        matrix.setdefault((cand, judge), []).append(score)

    matrix_rows = [{
        "candidate": cand, "judge": judge,
        "avg_score": round(sum(scores) / len(scores), 2),
        "samples": len(scores),
        "avg_false_positive": None,  # 单独算
    } for (cand, judge), scores in matrix.items()]

    return {
        "total_records": len(records),
        "self_blocked": self_blocked,
        "errors": errors,
        "matrix": matrix_rows[:200],
        "source_path": str(path),
    }


def api_bench_results() -> dict:
    """bench 模型对照结果（eval_suite/results.jsonl）。"""
    path = _bench_results_path()
    records = _read_jsonl(path)
    # 按 model 聚合
    by_model: dict[str, dict] = {}
    for r in records:
        m = r.get("model", "?")
        agg = by_model.setdefault(m, {"runs": 0, "completed": 0, "failed": 0,
                                       "total_cost": 0.0, "pass_rates": []})
        agg["runs"] += 1
        agg["completed"] += r.get("completed", 0) or 0
        agg["failed"] += r.get("failed", 0) or 0
        agg["total_cost"] += r.get("total_cost_usd", 0) or 0
        if r.get("pass_rate") is not None:
            agg["pass_rates"].append(r["pass_rate"])

    model_rows = [{
        "model": m,
        "runs": v["runs"],
        "avg_pass_rate": round(sum(v["pass_rates"]) / len(v["pass_rates"]), 4)
                         if v["pass_rates"] else 0,
        "total_cost": round(v["total_cost"], 4),
    } for m, v in by_model.items()]
    model_rows.sort(key=lambda x: x["total_cost"], reverse=True)

    return {
        "total_runs": len(records),
        "by_model": model_rows,
        "records": records[:200],  # 最近 200 条
        "source_path": str(path),
    }


def api_baseline() -> dict:
    """claude 裸跑基线（eval_suite/baseline.jsonl）+ $/pass 门禁基线。

    合并两个 baseline 概念：
    1. bench baseline.jsonl（claude -p 裸跑对照）
    2. cost_baseline.json（$/pass 门禁回归基线，全局）
    """
    # 1. bench baseline
    bench_path = _resolve_workspace_file("eval_suite/baseline.jsonl")
    bench_records = _read_jsonl(bench_path)

    # 2. cost 门禁基线
    cost_baseline_path = CONFIG_PATH.parent / "cost_baseline.json"
    cost_baseline = _read_json(cost_baseline_path)

    return {
        "bench_baseline": {
            "total_runs": len(bench_records),
            "records": bench_records[:100],
            "source_path": str(bench_path),
        },
        "cost_gate_baseline": {
            "data": cost_baseline,
            "source_path": str(cost_baseline_path),
        },
    }


# ═══════════════════════════════════════════════════════════════
# 配置查看（P2-1）+ 磁盘运维（P2-2）
# ═══════════════════════════════════════════════════════════════

def api_profiles() -> dict:
    """配置中心数据源：profile 列表 + 当前模式（R3）。"""
    return list_profiles()


def api_health() -> dict:
    """模型端点健康检查：plan/worker/evaluator/本地代理 + mismatch（R4）。"""
    return health_check()


def api_proxy_policies() -> dict:
    """代理路由策略可视（R9 消费）：GET <代理>/api/route/policies。

    配置中心展示代理的模型→后端路由偏好/云端模型/智能路由阈值，替代盲猜
    （部署拓扑 ③ 对 agent_go 可视）。代理不可达/未实现时返回 ok=False。
    """
    import urllib.request as _ur
    from .profiles import DEFAULT_LOCAL_URL
    # R9 由本地代理（llama.cpp）提供，无论当前配置云端/本地都指向本地代理
    proxy_url = DEFAULT_LOCAL_URL
    url = f"{proxy_url}/api/route/policies"
    try:
        with _ur.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e), "proxy_url": proxy_url}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"代理返回非对象结构: {type(data).__name__}", "proxy_url": proxy_url}
    data["ok"] = True
    data["proxy_url"] = proxy_url
    return data


def _audit(op: str, params: dict, result: Any, ok: bool, token: str = "") -> None:
    """写操作审计行（R16）：append ~/.agent_go/web_audit.jsonl。

    params 摘要截断（任务描述可能很长）；token 只存 sha256 前 8 位（操作者区分，不存明文）。
    """
    import hashlib

    def _clip(v: Any) -> Any:
        if isinstance(v, str):
            return v[:200]
        if isinstance(v, dict):
            return {k: _clip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clip(x) for x in v[:10]]
        return v

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "op": op,
        "params": _clip(params),
        "ok": ok,
        "result": _clip(result) if not isinstance(result, str) else result[:300],
        "auth": hashlib.sha256(token.encode()).hexdigest()[:8] if token else "",
    }
    try:
        with (AGENT_GO_DIR / "web_audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("audit write failed: %s", e)


def _run_cli(argv: list[str], timeout: float = 180) -> dict[str, Any]:
    """同步执行 agent_go 子命令（快操作：clean/review-decision/merge），返回结构化结果。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_go"] + argv,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"命令超时（{timeout}s）"}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def _task_status_of(task_id: str) -> str:
    """任务当前状态（meta.json 唯一事实源，经 status.py 归一化）。"""
    td = _task_dir(task_id)
    if td is None:
        return ""
    meta_path = td / "meta.json"
    if not meta_path.exists():
        return ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return normalize_task_status(meta.get("status", ""), meta)


_RUNNING_STATES = {"EXECUTING", "PLANNING"}


def api_task_review(task_id: str) -> Optional[dict]:
    """读取任务 review.json（R9 决策持久化结果）。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    rj = td / "review.json"
    if not rj.exists():
        return {"task_id": task_id, "decision": None}
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data["task_id"] = task_id
    return data


def api_merge_preview(task_id: str) -> Optional[dict]:
    """merge 确认弹窗数据（D5）：delivery/target 分支、ahead 数、可合并性、冲突文件。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    try:
        meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    repo = meta.get("repo", "")
    delivery_branch = meta.get("delivery_branch") or ""
    target = meta.get("target_branch") or meta.get("base_branch") or "main"
    result: dict[str, Any] = {
        "task_id": task_id,
        "repo": repo,
        "delivery_branch": delivery_branch,
        "target_branch": target,
        "pr_url": meta.get("pr_url") or "",
        "explicit_merge_commit": meta.get("explicit_merge_commit") or "",
    }
    if not delivery_branch or not repo or not Path(repo).exists():
        result["mergeable"] = False
        result["error"] = "无 delivery_branch 或仓库不存在"
        return result
    from .delivery import check_mergeability
    mc = check_mergeability(repo, delivery_branch, target)
    result.update({
        "mergeable": bool(mc.get("mergeable")),
        "ahead": mc.get("ahead", 0),
        "conflicts": mc.get("conflicts") or [],
    })
    if mc.get("error"):
        result["error"] = mc["error"]
    return result


def api_deviation(task_id: str) -> Optional[dict]:
    """任务偏差记录聚合（R12）：类型/根因分布 + 事件列表。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    from .deviation import load as load_deviations, aggregate_deviations
    from dataclasses import asdict
    events = load_deviations(td)
    agg = aggregate_deviations(events)
    agg["task_id"] = task_id
    agg["events"] = [
        {k: d.get(k) for k in ("deviation_type", "root_cause_category", "summary",
                               "subtask_id", "timestamp", "failure_class")}
        for e in events[-50:]
        for d in [asdict(e)]
    ]
    return agg


def api_local_tco() -> dict:
    """本地模型 TCO 面板（R13）：is_local metering 调用数 × local_model_cost。

    D1：返回 estimated: true，前端必须显著标注"估算成本，非真实账单"。
    """
    from .metrics import local_tco_usd
    per_model: dict[str, dict[str, Any]] = {}
    total_calls = 0
    for mj in AGENT_GO_DIR.glob("task-*/metering.jsonl"):
        try:
            with mj.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not rec.get("is_local"):
                        continue
                    model = rec.get("actual_model") or rec.get("routed_model") or "unknown"
                    slot = per_model.setdefault(model, {"calls": 0, "tokens": 0})
                    slot["calls"] += 1
                    slot["tokens"] += (rec.get("prompt_tokens") or 0) + (rec.get("completion_tokens") or 0)
                    total_calls += 1
        except OSError:
            continue
    rows = []
    total_tco = 0.0
    for model, slot in sorted(per_model.items(), key=lambda kv: -kv[1]["calls"]):
        unit = local_tco_usd(model)
        tco = round(unit * slot["calls"], 6)
        total_tco += tco
        rows.append({"model": model, "calls": slot["calls"], "tokens": slot["tokens"],
                     "unit_cost": unit, "tco_usd": tco,
                     "configured": unit > 0})
    unconfigured = [r["model"] for r in rows if not r["configured"]]
    return {
        "estimated": True,
        "total_calls": total_calls,
        "total_tco_usd": round(total_tco, 6),
        "by_model": rows,
        "unconfigured_models": unconfigured,
        "note": "未配置 local_model_cost 的模型按 0 计（配置中心可编辑 local_model_cost）" if unconfigured else "",
    }


def api_config_diff(name: str) -> Optional[dict]:
    """当前生效配置 vs 目标 profile 的字段级差异（R15）。"""
    from .profiles import profile_path
    path = profile_path(name)
    if not path.exists():
        return None
    try:
        target_saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    current = load_config()
    # 目标 profile 同样走 DEFAULT merge（与 load_config 语义一致）
    from .config import DEFAULT_CONFIG
    target = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in target_saved.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            target[k].update(v)
        else:
            target[k] = v

    diffs: list[dict[str, Any]] = []

    def _walk(prefix: str, cur: Any, tgt: Any) -> None:
        if isinstance(cur, dict) and isinstance(tgt, dict):
            for key in sorted(set(cur) | set(tgt)):
                _walk(f"{prefix}{key}" if not prefix else f"{prefix}.{key}",
                      cur.get(key), tgt.get(key))
            return
        if cur != tgt:
            diffs.append({"field": prefix, "current": cur, "target": tgt})

    _walk("", current, target)
    return {"profile": name, "diff_count": len(diffs), "diffs": diffs[:100]}


_CONFIG_EDIT_WHITELIST = {
    "worker_models": dict, "worker_backends": dict, "local_models": list,
    "local_model_cost": dict, "goal": dict, "evaluator": dict,
    "plan_api.worker_base_url": str, "planner_api.base_url": str,
}


def put_config_field(field: str, value: Any) -> dict:
    """白名单字段编辑（R14）：写入当前生效配置文件（profile 或 config.json）。

    校验：字段在白名单 + 值类型与白名单声明一致；api_key 等敏感字段永不接受。
    保存后新任务生效（load_config 每次读文件，天然热生效）。
    """
    if field not in _CONFIG_EDIT_WHITELIST:
        raise ProfileError(
            f"字段不在白名单: {field}（允许: {', '.join(sorted(_CONFIG_EDIT_WHITELIST))}）")
    expected = _CONFIG_EDIT_WHITELIST[field]
    if not isinstance(value, expected):
        raise ProfileError(f"字段 {field} 值类型应为 {expected.__name__}，收到 {type(value).__name__}")
    from .profiles import active_config_source
    target = active_config_source()
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except json.JSONDecodeError as e:
        raise ProfileError(f"配置文件损坏: {target}: {e}") from e
    if "." in field:
        top, sub = field.split(".", 1)
        data.setdefault(top, {})[sub] = value
    else:
        data[field] = value
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"field": field, "saved_to": str(target), "effective": "新任务生效"}


def api_task_report(task_id: str, fmt: str) -> Optional[dict]:
    """任务报告（M5.2.1）：复用 CLI `agent_go report --output -`（单一实现，行为一致）。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    result = _run_cli(["report", task_id, "--format", fmt, "--output", "-"], timeout=60)
    if not result["ok"]:
        return {"ok": False, "error": result["stderr"][-200:] or result["stdout"][-200:]}
    return {"ok": True, "content": result["stdout"]}


def api_worktrees(task_id: str) -> Optional[dict]:
    """保留 worktree 清单（R17，inspect 同逻辑）。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    meta_path = td / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        meta = {}
    entries = []
    for st in meta.get("subtasks", []):
        sid = st.get("id", "")
        sub_dir = td / sid
        wt_path = sub_dir / "work"
        preserved_file = sub_dir / ".preserved"
        result_file = sub_dir / "result.json"
        result: dict[str, Any] = {}
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        status = result.get("status", "unknown")
        worktree_exists = wt_path.exists() and (wt_path / ".git").exists()
        is_preserved = preserved_file.exists()
        if not is_preserved and status not in ("failed", "blocked") and not worktree_exists:
            continue
        preserved_data: dict[str, Any] = {}
        if is_preserved:
            try:
                preserved_data = json.loads(preserved_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        entries.append({
            "subtask_id": sid,
            "status": status,
            "worktree": str(wt_path) if worktree_exists else "",
            "branch": preserved_data.get("branch", f"agent_go/{task_id}/{sid}"),
            "preserved": is_preserved,
            "failure_reason": result.get("failure_reason", preserved_data.get("failure_reason", "")),
        })
    return {"task_id": task_id, "worktrees": entries}


def api_audit(limit: int = 100) -> dict:
    """写操作审计记录（U6/R16 消费端）：web_audit.jsonl 尾部 limit 行倒序。"""
    path = AGENT_GO_DIR / "web_audit.jsonl"
    if not path.exists():
        return {"records": [], "total": 0}
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return {"records": [], "total": 0}
    records = []
    for line in reversed(lines[-limit:]):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"records": records, "total": len(lines)}


def api_config() -> dict:
    """只读展示用户配置（config.json + role_skill_map.json + 熔断器状态）。"""
    config = load_config()
    sensitive_keys = ("api_key", "token")

    # 脱敏：api_key/token 字段一律隐藏（长 key 保留前后 4 字符便于辨认，
    # 短 key 完全遮蔽）；递归处理嵌套 dict 与 list
    def _mask(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: _mask(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_mask(item, key) for item in value]
        if key in sensitive_keys and isinstance(value, str) and value:
            return value[:4] + "..." + value[-4:] if len(value) > 8 else "***"
        return value

    config = _mask(config)

    # role_skill_map
    rsmap_path = CONFIG_PATH.parent / "role_skill_map.json"
    role_skill_map = _read_json(rsmap_path) if rsmap_path.exists() else None

    return {
        "config": config,
        "config_path": str(CONFIG_PATH),
        "role_skill_map": role_skill_map,
        "role_skill_map_path": str(rsmap_path),
    }


def api_storage() -> dict:
    """磁盘占用分析：task 目录大小排行 + 孤儿目录（无 meta.json）检测。

    数据源：遍历 AGENT_GO_DIR。用于运维（748+ 任务累积监控）。
    """
    if not AGENT_GO_DIR.exists():
        return {"total_size": 0, "total_size_mb": 0.0,
                "task_count": 0, "orphan_count": 0,
                "top_tasks": [], "orphans": []}

    def _dir_size(path: Path) -> int:
        total = 0
        for p in path.iterdir():
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
            elif p.is_dir():
                total += _dir_size(p)
        return total

    tasks = []
    orphans = []
    total = 0
    for d in AGENT_GO_DIR.iterdir():
        if not d.is_dir() or not d.name.startswith(_TASK_PREFIX):
            continue
        try:
            size = _dir_size(d)
        except OSError:
            continue
        total += size
        has_meta = (d / "meta.json").exists()
        entry: dict[str, Any] = {"name": d.name, "size": size, "has_meta": has_meta}
        if has_meta:
            tasks.append(entry)
        else:
            orphans.append(entry)

    tasks.sort(key=lambda x: int(x["size"] or 0), reverse=True)
    return {
        "total_size": total,
        "total_size_mb": round(total / 1024 / 1024, 2),
        "task_count": len(tasks),
        "orphan_count": len(orphans),
        "top_tasks": tasks[:20],  # 最大的 20 个
        "orphans": orphans,
    }


# ── 任务状态快照（api_kanban 复用，meta 签名缓存避免每请求全量解析）──

_task_status_cache: dict[str, dict] = {}
_task_status_sig: str = ""
_task_status_lock = threading.Lock()


def _task_status_snapshot() -> dict[str, dict]:
    """task_id → {task_id, status, task} 快照，按各 meta.json 的 mtime:size 签名
    缓存；任务无变化时 /api/kanban 复用，避免每次请求全量 open+json.loads（O(tasks)）。
    任务目录/meta 变化（含新增/清理）都会使签名失效。
    """
    global _task_status_cache, _task_status_sig
    parts: list[str] = []
    dirs: list[Path] = []
    for td in _list_task_dirs():
        dirs.append(td)
        meta_p = td / "meta.json"
        try:
            parts.append(f"{td.name}:{meta_p.stat().st_mtime:.6f}:{meta_p.stat().st_size}")
        except OSError:
            continue
    sig = f"{AGENT_GO_DIR}\n" + "|".join(parts)
    if sig == _task_status_sig:
        return _task_status_cache
    status_map: dict[str, dict] = {}
    for td in dirs:
        meta = _task_meta(td)
        if not meta:
            continue
        status_map[td.name] = {
            "task_id": td.name,
            "status": task_status(meta),
            "task": meta.get("task", ""),
        }
    with _task_status_lock:
        _task_status_cache = status_map
        _task_status_sig = sig
    return status_map


def api_kanban(include_archived: bool = False) -> dict:
    """看板数据（Kanban）：卡片按 stage 分组 + 关联任务实时状态派生。

    看板 stage 与执行状态正交：卡片只存 task_ids 软链接，latest_task 从
    meta.json 实时派生（复用 _list_task_dirs/task_status，mtime 签名缓存），
    不冗余在卡片上。archived 卡片默认不返回；include_archived=True 时按其
    stage 分组返回（卡片带 archived:true，供前端归档视图/取消归档）。
    """
    board = kanban.load_board()
    status_map = _task_status_snapshot()
    grouped: dict[str, list] = {key: [] for key, _ in kanban.STAGES}
    for card in board.get("cards", []):
        if card.get("archived") and not include_archived:
            continue
        stage = card.get("stage", "")
        if stage not in grouped:
            continue  # 历史脏数据（非法 stage）防御性跳过
        c = dict(card)
        tids = c.get("task_ids") or []
        latest = None
        for tid in reversed(tids):  # 最近一次派发优先
            if tid in status_map:
                latest = status_map[tid]
                break
        if latest is None and tids:
            # 关联任务已被清理 → 标记 unknown（不丢弃链接信息）
            latest = {"task_id": tids[-1], "status": "unknown", "task": ""}
        c["latest_task"] = latest
        grouped[stage].append(c)
    return {
        "stages": [{"key": k, "label": label} for k, label in kanban.STAGES],
        "card_types": kanban.CARD_TYPES,
        "cards": grouped,
        "total": sum(len(v) for v in grouped.values()),
    }


class WebHandler(BaseHTTPRequestHandler):
    """只读观察 API handler。"""

    protocol_version = "HTTP/1.1"
    server_version = "agent_go-web/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静默 access log

    # ── 鉴权 ─────────────────────────────────────────────────

    def _auth_role(self, query: str = "") -> str:
        """请求角色：admin / viewer / open（无鉴权配置，全开放）/ ""（未认证）。

        P1.2 多用户角色：--admin-token（或兼容 --token）全部操作；--viewer-token 只读。
        """
        admin = getattr(self.server, "admin_token", None) or ""
        viewer = getattr(self.server, "viewer_token", None) or ""
        if not admin and not viewer:
            return "open"  # 未配置任何 token → 向后兼容（全开放）
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-Api-Key", "")
        token = auth[7:] if auth.startswith("Bearer ") else (api_key or "")
        # EventSource 无法自定义请求头，允许 ?token= query 传递（仅 SSE 等场景）
        if not token:
            for pair in query.split("&"):
                if pair.startswith("token="):
                    token = unquote(pair[6:])
        if admin and token == admin:
            return "admin"
        if viewer and token == viewer:
            return "viewer"
        return ""

    def _auth_ok(self, query: str = "") -> bool:
        return self._auth_role(query) != ""

    def _auth_guard(self, query: str = "", required: str = "read") -> bool:
        """鉴权守卫。required=read：admin/viewer 均可；required=admin：仅 admin（写操作）。

        401 = 未认证（无有效 token）；403 = 认证但角色权限不足（viewer 访问写操作）。
        """
        role = self._auth_role(query)
        if role == "open":
            return True
        if role == "":
            self._reply_json(401, {"error": "unauthorized"})
            return False
        if required == "admin" and role != "admin":
            self._reply_json(403, {"error": "forbidden: viewer 角色无写操作权限"})
            return False
        return True

    # ── 工具 ─────────────────────────────────────────────────

    def _reply(self, code: int, content_type: str, body: bytes,
               extra_headers: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _reply_json(self, code: int, payload: Any,
                    extra_headers: Optional[dict] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._reply(code, "application/json; charset=utf-8", body, extra_headers)

    def _reply_html(self, body: str) -> None:
        self._reply(200, "text/html; charset=utf-8", body.encode("utf-8"))

    # ── 路由 ─────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in ("/", "/index.html"):
            self._reply_html(_SPA_HTML)
            return
        if path == "/health":
            self._reply_json(200, {"status": "ok", "server": "agent_go-web"})
            return
        if path.startswith("/api/"):
            if not self._auth_guard(parsed.query, required="read"):
                return
            self._route_api(path, parsed.query)
            return
        self._reply_json(404, {"error": f"not found: {path}"})

    def _route_api(self, path: str, query: str) -> None:
        parts = [p for p in path.split("/") if p]
        # parts[0] == "api"
        try:
            if len(parts) == 2 and parts[1] == "tasks":
                self._reply_json(200, {"tasks": api_tasks()})
                return
            if len(parts) == 2 and parts[1] == "archive":
                # 历史归档任务（无 status_schema_version），状态归一化展示
                self._reply_json(200, {"tasks": api_tasks(include_legacy=True)})
                return
            if len(parts) == 3 and parts[1] == "tasks":
                data = api_task(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "metering":
                data = api_metering(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "replay":
                data = api_replay(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "plan":
                data = api_plan(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 5 and parts[1] == "tasks" and parts[4] == "detail":
                data = api_subtask_detail(parts[2], parts[3])
                if data is None:
                    self._reply_json(404, {"error": "subtask not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 5 and parts[1] == "tasks" and parts[4] == "log":
                data_log = _extract_subtask_log(parts[2], parts[3])
                self._reply_json(200, {"lines": data_log})
                return
            # ── 审批/交付数据（M2/R9-R10）──
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "review":
                data = api_task_review(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "report":
                fmt = next((unquote(p[7:]) for p in query.split("&")
                            if p.startswith("format=")), "html")
                if fmt not in ("md", "html"):
                    self._reply_json(400, {"error": "format 须为 md/html"})
                    return
                data = api_task_report(parts[2], fmt)
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                elif not data.get("ok"):
                    self._reply_json(500, {"error": data.get("error", "报告生成失败")})
                else:
                    content = data["content"]
                    ctype = "text/html; charset=utf-8" if fmt == "html" else "text/markdown; charset=utf-8"
                    self._reply(200, ctype, content.encode("utf-8"))
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "merge-preview":
                data = api_merge_preview(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            # ── 全局视图（P0-2）──
            if len(parts) == 2 and parts[1] == "overview":
                self._reply_json(200, api_overview())
                return
            if len(parts) == 2 and parts[1] == "cost":
                self._reply_json(200, api_cost())
                return
            if len(parts) == 2 and parts[1] == "models":
                self._reply_json(200, api_models())
                return
            # ── 数据对象黑洞（P1）──
            if len(parts) == 3 and parts[1] == "assessment":
                data = api_assessment(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 2 and parts[1] == "cross-judge":
                self._reply_json(200, api_cross_judge())
                return
            if len(parts) == 2 and parts[1] == "bench-results":
                self._reply_json(200, api_bench_results())
                return
            if len(parts) == 2 and parts[1] == "baseline":
                self._reply_json(200, api_baseline())
                return
            # ── 配置查看（P2-1）──
            if len(parts) == 2 and parts[1] == "config":
                self._reply_json(200, api_config())
                return
            # ── 磁盘运维（P2-2）──
            if len(parts) == 2 and parts[1] == "storage":
                self._reply_json(200, api_storage())
                return
            # ── 看板（Kanban）──
            if len(parts) == 2 and parts[1] == "kanban":
                # ?archived=1 → 归档视图（含已归档卡片，供取消归档）
                include_archived = "archived" in query
                self._reply_json(200, api_kanban(include_archived=include_archived))
                return
            # ── 配置中心 + 健康检查（M1/R3-R4）──
            if len(parts) == 2 and parts[1] == "profiles":
                self._reply_json(200, api_profiles())
                return
            if len(parts) == 2 and parts[1] == "health":
                self._reply_json(200, api_health())
                return
            # ── M3 观测端点（R12/R13/R15/R17）──
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "deviation":
                data = api_deviation(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 2 and parts[1] == "local-tco":
                self._reply_json(200, api_local_tco())
                return
            if len(parts) == 2 and parts[1] == "audit":
                self._reply_json(200, api_audit())
                return
            if len(parts) == 2 and parts[1] == "proxy-policies":
                self._reply_json(200, api_proxy_policies())
                return
            if len(parts) == 3 and parts[1] == "config" and parts[2] == "diff":
                name = next((unquote(p[5:]) for p in query.split("&")
                             if p.startswith("name=")), "")
                if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
                    self._reply_json(400, {"error": "invalid profile name"})
                    return
                data = api_config_diff(name)
                if data is None:
                    self._reply_json(404, {"error": "profile not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "worktrees":
                data = api_worktrees(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "task not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "pending-confirmation":
                td = _task_dir(parts[2])
                if td is None:
                    self._reply_json(404, {"error": "task not found"})
                    return
                pf = td / "pending_confirmation.json"
                if not pf.exists():
                    self._reply_json(200, {"pending": None})
                    return
                try:
                    self._reply_json(200, {"pending": json.loads(pf.read_text(encoding="utf-8"))})
                except (json.JSONDecodeError, OSError):
                    self._reply_json(500, {"error": "pending 文件损坏"})
                return
            if len(parts) == 2 and parts[1] == "events":
                self._stream_events(query)
                return
        except Exception as exc:  # pragma: no cover - 防御性
            logger.exception("api error on %s", path)
            self._reply_json(500, {"error": str(exc)})
            return
        self._reply_json(404, {"error": f"not found: {path}"})

    # ── 写操作（M1/R3/R8）：token 鉴权 + JSON body 校验 ─────

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            self._reply_json(404, {"error": f"not found: {path}"})
            return
        if not self._auth_guard(parsed.query, required="admin"):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024:
            self._reply_json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except json.JSONDecodeError:
            self._reply_json(400, {"error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._reply_json(400, {"error": "JSON body must be an object"})
            return
        self._route_write_api(path, body)

    def _route_write_api(self, path: str, body: dict) -> None:
        parts = [p for p in path.split("/") if p]
        token = getattr(self.server, "admin_token", None) or ""  # type: ignore[attr-defined]
        try:
            # POST /api/profile/local {url?}  一键本地（R3）
            if len(parts) == 3 and parts[1] == "profile" and parts[2] == "local":
                url = str(body.get("url") or "http://localhost:4000")
                result = activate_local(url)
                _audit("profile.local", {"url": url}, result, True, token)
                self._reply_json(200, result)
                return
            # POST /api/profile/cloud  恢复云端（R3）
            if len(parts) == 3 and parts[1] == "profile" and parts[2] == "cloud":
                result = activate_cloud()
                _audit("profile.cloud", {}, result, True, token)
                self._reply_json(200, result)
                return
            # POST /api/profile/activate {name}  激活任意 profile（R3）
            if len(parts) == 3 and parts[1] == "profile" and parts[2] == "activate":
                name = str(body.get("name") or "")
                if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
                    self._reply_json(400, {"error": "invalid profile name"})
                    return
                result = activate_profile(name)
                _audit("profile.activate", {"name": name}, result, True, token)
                self._reply_json(200, result)
                return
            # ── M2 任务生命周期 ──────────────────────────────
            # POST /api/tasks/run {repo, task, parallel?, goal?}（R5a，confirm_mode=auto）
            if len(parts) == 3 and parts[1] == "tasks" and parts[2] == "run":
                self._op_run(body, token)
                return
            # POST /api/tasks/<id>/resume（R6）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "resume":
                self._op_resume(parts[2], body, token)
                return
            # POST /api/tasks/<id>/cancel（R6）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "cancel":
                self._op_cancel(parts[2], token)
                return
            # POST /api/tasks/clean-old {days, confirm}（R7）
            if len(parts) == 3 and parts[1] == "tasks" and parts[2] == "clean-old":
                self._op_clean_old(body, token)
                return
            # POST /api/tasks/<id>/review {deep?}（R9 触发审查）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "review":
                self._op_review(parts[2], body, token)
                return
            # POST /api/tasks/<id>/review/decision {decision, comment?}（R9 审批，D4 必审计）
            if len(parts) == 5 and parts[1] == "tasks" and parts[3] == "review" and parts[4] == "decision":
                self._op_review_decision(parts[2], body, token)
                return
            # POST /api/tasks/<id>/merge {push?, remote?}（R10）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "merge":
                self._op_merge(parts[2], body, token)
                return
            # POST /api/tasks/<id>/pr {push?, remote?}（R11）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "pr":
                self._op_pr(parts[2], body, token)
                return
            # POST /api/tasks/<id>/confirm {stage, decision}（R5b 计划确认回执）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "confirm":
                self._op_confirm(parts[2], body, token)
                return
            # ── 看板（Kanban）────────────────────────────────
            # POST /api/kanban/cards {title, type, stage?, repo?, description?, cron?}
            if len(parts) == 3 and parts[1] == "kanban" and parts[2] == "cards":
                self._op_kanban_create(body, token)
                return
            # POST /api/kanban/cards/<id>/<update|move|archive|delete|dispatch>
            if len(parts) == 5 and parts[1] == "kanban" and parts[2] == "cards":
                card_id, action = parts[3], parts[4]
                if action == "update":
                    self._op_kanban_update(card_id, body, token)
                    return
                if action == "move":
                    self._op_kanban_move(card_id, body, token)
                    return
                if action == "archive":
                    self._op_kanban_archive(card_id, body, token)
                    return
                if action == "delete":
                    self._op_kanban_delete(card_id, token)
                    return
                if action == "dispatch":
                    self._op_kanban_dispatch(card_id, body, token)
                    return
        except KanbanError as e:
            _audit("error", {"path": path}, str(e), False, token)
            self._reply_json(422, {"error": str(e)})
            return
        except ProfileError as e:
            _audit("error", {"path": path}, str(e), False, token)
            self._reply_json(422, {"error": str(e)})
            return
        except TaskRunnerError as e:
            _audit("error", {"path": path}, str(e), False, token)
            self._reply_json(422, {"error": str(e)})
            return
        except Exception as exc:  # pragma: no cover - 防御性
            logger.exception("write api error on %s", path)
            self._reply_json(500, {"error": str(exc)})
            return
        self._reply_json(404, {"error": f"not found: {path}"})

    # ── M2 写操作实现 ────────────────────────────────────────

    def _op_run(self, body: dict, token: str) -> None:
        repo = str(body.get("repo") or "").strip()
        task = str(body.get("task") or "").strip()
        try:
            parallel = int(body.get("parallel") or 1)
        except (TypeError, ValueError):
            self._reply_json(400, {"error": "parallel 必须为正整数"})
            return
        goal = body.get("goal")  # None/True/False
        confirm_mode = str(body.get("confirm_mode") or "auto")
        if confirm_mode not in ("auto", "web"):
            self._reply_json(400, {"error": "confirm_mode 须为 auto/web"})
            return
        if not repo or not Path(repo).is_absolute() or not Path(repo).is_dir():
            self._reply_json(400, {"error": f"repo 必须是存在的绝对路径: {repo or '<空>'}"})
            return
        if not task:
            self._reply_json(400, {"error": "task 描述不能为空"})
            return
        # D3/R5a：本地模式下代理不可达 → 启动即报错（不放行到任务失败才发现）
        if read_current_profile() == "local":
            from .profiles import DEFAULT_LOCAL_URL
            backends = (load_config().get("worker_backends") or {})
            local_url = next((v for v in backends.values()
                              if isinstance(v, str) and ("localhost" in v or "127.0.0.1" in v)),
                             DEFAULT_LOCAL_URL)
            try:
                probe_local_models(local_url)
            except ProfileError as e:
                _audit("tasks.run", {"repo": repo, "task": task}, str(e), False, token)
                self._reply_json(422, {
                    "error": f"本地模式代理不可达，未启动任务。请先启动代理或检查配置中心健康面板。\n原因: {e}"})
                return
        task_id = task_runner.start_run(repo, task, parallel=parallel, goal=goal,
                                        confirm_mode=confirm_mode)
        note = ("已跳过计划确认（--yes）" if confirm_mode == "auto"
                else "Plan 生成后将在本页面请求确认（30 分钟超时自动取消）")
        result = {"task_id": task_id, "status": "started",
                  "confirm_mode": confirm_mode, "note": note}
        _audit("tasks.run", {"repo": repo, "task": task, "parallel": parallel}, result, True, token)
        self._reply_json(200, result)

    def _op_resume(self, task_id: str, body: dict, token: str) -> None:
        td = _task_dir(task_id)
        if td is None:
            self._reply_json(404, {"error": "task not found"})
            return
        # M5.2 冲突处理：任务锁被其他进程持有（CLI run/resume/merge）→ 409 前置拦截
        from .task_lock import is_task_locked
        if is_task_locked(td):
            _audit("tasks.resume", {"task_id": task_id}, "冲突: 任务锁被持有", False, token)
            self._reply_json(409, {"error": "任务被其他进程持有（正在执行/恢复/合并），无法 resume。"
                                            "请稍后再试。"})
            return
        # D3/R6：运行中任务 resume → 409 冲突
        status = _task_status_of(task_id)
        if status in _RUNNING_STATES or task_runner.is_running(task_id):
            _audit("tasks.resume", {"task_id": task_id}, f"冲突: 状态 {status}", False, token)
            self._reply_json(409, {"error": f"任务正在运行中（{status}），无法 resume。可先 cancel。",
                                   "status": status})
            return
        parallel = int(body.get("parallel") or 1)
        task_runner.start_resume(task_id, parallel=parallel)
        result = {"task_id": task_id, "status": "resumed"}
        _audit("tasks.resume", {"task_id": task_id}, result, True, token)
        self._reply_json(200, result)

    def _op_cancel(self, task_id: str, token: str) -> None:
        if _task_dir(task_id) is None:
            self._reply_json(404, {"error": "task not found"})
            return
        if not task_runner.cancel(task_id):
            _audit("tasks.cancel", {"task_id": task_id}, "句柄不存在", False, token)
            self._reply_json(409, {
                "error": "任务进程不受本 web 实例管理（可能已结束或 web 重启过）。"
                         "若为孤儿进程，请用 CLI kill；状态以任务页为准。"})
            return
        result = {"task_id": task_id, "status": "cancelling",
                  "note": "已发送 SIGINT（与 Ctrl+C 同义），pipeline 收尾中"}
        _audit("tasks.cancel", {"task_id": task_id}, result, True, token)
        self._reply_json(200, result)

    def _op_clean_old(self, body: dict, token: str) -> None:
        if body.get("confirm") is not True:
            self._reply_json(400, {"error": "需 confirm: true 二次确认"})
            return
        try:
            days = int(body.get("days") or 0)
        except (TypeError, ValueError):
            self._reply_json(400, {"error": "days 必须为正整数"})
            return
        if days <= 0:
            self._reply_json(400, {"error": "days 必须为正整数"})
            return
        cutoff = time.time() - days * 86400
        victims = [t for t in AGENT_GO_DIR.glob("task-*")
                   if t.is_dir() and t.stat().st_mtime < cutoff]
        if not victims:
            self._reply_json(200, {"removed": [], "note": f"无早于 {days} 天的任务"})
            return
        from .cli import clean_task_dirs
        result = clean_task_dirs(victims)
        _audit("tasks.clean_old", {"days": days}, result, True, token)
        self._reply_json(200, result)

    def _op_review(self, task_id: str, body: dict, token: str) -> None:
        if _task_dir(task_id) is None:
            self._reply_json(404, {"error": "task not found"})
            return
        if body.get("deep"):
            key = task_runner.start_review_deep(task_id)
            result = {"task_id": task_id, "status": "review_started", "deep": True, "op_key": key}
            _audit("tasks.review", {"task_id": task_id, "deep": True}, result, True, token)
            self._reply_json(200, result)
            return
        cli_result = _run_cli(["review", "--task", task_id, "--yes"], timeout=300)
        _audit("tasks.review", {"task_id": task_id, "deep": False},
               cli_result["stdout"][-300:] or cli_result["stderr"][-300:], cli_result["ok"], token)
        self._reply_json(200 if cli_result["ok"] else 422, cli_result)

    def _op_review_decision(self, task_id: str, body: dict, token: str) -> None:
        if _task_dir(task_id) is None:
            self._reply_json(404, {"error": "task not found"})
            return
        decision = str(body.get("decision") or "")
        flag = {"approve": "--approve", "reject": "--reject",
                "changes-requested": "--changes-requested"}.get(decision)
        if not flag:
            self._reply_json(400, {"error": "decision 须为 approve/reject/changes-requested"})
            return
        argv = ["review", "--task", task_id, flag, "--yes"]
        comment = str(body.get("comment") or "").strip()
        if comment and decision in ("reject", "changes-requested"):
            argv += ["--comment-text", comment]
        result = _run_cli(argv, timeout=120)
        # D4：审批决策必写审计（含决策与评论）
        _audit("tasks.review.decision", {"task_id": task_id, "decision": decision,
                                         "comment": comment},
               result["stdout"][-300:] or result["stderr"][-300:], result["ok"], token)
        self._reply_json(200 if result["ok"] else 422, result)

    def _op_merge(self, task_id: str, body: dict, token: str) -> None:
        td = _task_dir(task_id)
        if td is None:
            self._reply_json(404, {"error": "task not found"})
            return
        # M5.2 冲突处理：任务锁被持有（运行/恢复中）→ 409 前置拦截
        from .task_lock import is_task_locked
        if is_task_locked(td):
            _audit("tasks.merge", {"task_id": task_id}, "冲突: 任务锁被持有", False, token)
            self._reply_json(409, {"error": "任务被其他进程持有（正在执行/恢复），无法 merge。请稍后再试。"})
            return
        push = bool(body.get("push"))
        remote = str(body.get("remote") or "origin")
        argv = ["merge", task_id, "--remote", remote]
        if push:
            argv.append("--push")
        result = _run_cli(argv, timeout=180)
        # D3：冲突时把冲突信息带给前端（cmd_merge 的 mergeability 预检输出在 stdout/stderr）
        payload = dict(result)
        if not result["ok"]:
            preview = api_merge_preview(task_id)
            if preview:
                payload["conflicts"] = preview.get("conflicts") or []
                payload["mergeable"] = preview.get("mergeable")
        _audit("tasks.merge", {"task_id": task_id, "push": push, "remote": remote},
               result["stdout"][-300:] or result["stderr"][-300:], result["ok"], token)
        self._reply_json(200 if result["ok"] else 422, payload)

    def _op_pr(self, task_id: str, body: dict, token: str) -> None:
        if _task_dir(task_id) is None:
            self._reply_json(404, {"error": "task not found"})
            return
        push = bool(body.get("push"))
        remote = str(body.get("remote") or "origin")
        argv = ["pr", task_id, "--remote", remote]
        if push:
            argv.append("--push")
        else:
            argv.append("--offline")  # 默认只生成 PR.md，不实际创建（安全默认）
        result = _run_cli(argv, timeout=300)
        payload: dict[str, Any] = dict(result)
        m = re.search(r"https://\S+/pull/\d+", result["stdout"])
        if m:
            payload["pr_url"] = m.group(0)
        _audit("tasks.pr", {"task_id": task_id, "push": push, "remote": remote},
               result["stdout"][-300:] or result["stderr"][-300:], result["ok"], token)
        self._reply_json(200 if result["ok"] else 422, payload)

    def _op_confirm(self, task_id: str, body: dict, token: str) -> None:
        """R5b 计划确认回执：校验 pending 存在 → 写 decision 文件（子进程轮询读取）。"""
        td = _task_dir(task_id)
        if td is None:
            self._reply_json(404, {"error": "task not found"})
            return
        pending_path = td / "pending_confirmation.json"
        if not pending_path.exists():
            self._reply_json(409, {"error": "任务当前无待确认项（pending_confirmation.json 不存在）"})
            return
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._reply_json(500, {"error": "pending 文件损坏"})
            return
        stage = str(body.get("stage") or "")
        decision = str(body.get("decision") or "").upper()
        if stage != pending.get("stage"):
            self._reply_json(409, {"error": f"stage 不匹配：当前待确认 stage={pending.get('stage')}，"
                                           f"收到 {stage or '<空>'}"})
            return
        allowed = {"plan": {"Y", "N", "R"}, "subtasks": {"Y", "N"}}.get(stage, set())
        if decision not in allowed:
            self._reply_json(400, {"error": f"stage={stage} 的 decision 须为 {'/'.join(sorted(allowed))}"})
            return
        decision_path = td / "confirmation_decision.json"
        decision_path.write_text(json.dumps({
            "stage": stage, "decision": decision,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
        result = {"task_id": task_id, "stage": stage, "decision": decision, "status": "accepted"}
        _audit("tasks.confirm", {"task_id": task_id, "stage": stage, "decision": decision},
               result, True, token)
        self._reply_json(200, result)

    # ── 看板（Kanban）写操作 ─────────────────────────────────

    def _kanban_card_or_reply(self, card_id: str) -> Optional[dict]:
        """看板写端点共用前置：id 格式非法 → 400；不存在 → 404；通过返回卡片。"""
        try:
            card = kanban.get_card(card_id)
        except KanbanError as e:
            self._reply_json(400, {"error": str(e)})
            return None
        if card is None:
            self._reply_json(404, {"error": f"卡片不存在: {card_id}"})
            return None
        return card

    def _op_kanban_create(self, body: dict, token: str) -> None:
        title = str(body.get("title") or "").strip()
        ctype = str(body.get("type") or "")
        if not title:
            self._reply_json(400, {"error": "title 不能为空"})
            return
        if ctype not in kanban.CARD_TYPES:
            self._reply_json(400, {"error": f"type 须为 {'/'.join(kanban.CARD_TYPES)}"})
            return
        # 非法 stage / implementation 缺 repo → KanbanError → 422（except 链）
        card = kanban.create_card(
            title=title, type=ctype,
            stage=str(body.get("stage") or "brainstorm"),
            repo=str(body.get("repo") or "").strip(),
            description=str(body.get("description") or ""),
            cron=str(body.get("cron") or "").strip(),
            spec_path=str(body.get("spec_path") or "").strip())
        _audit("kanban.create", {"card_id": card["id"], "title": title, "type": ctype},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_update(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        # null 安全（与 create 一致）：null 视为未传，跳过；title/repo/cron/spec_path strip，
        # description 保留原文（markdown 缩进/首尾空格可能是有意为之）。
        fields = {}
        for k in ("title", "description", "repo", "cron", "spec_path"):
            if k in body and body[k] is not None:
                v = body[k]
                fields[k] = str(v).strip() if k != "description" else str(v)
        if not fields:
            self._reply_json(400, {"error": "无可更新字段（title/description/repo/cron/spec_path）"})
            return
        card = kanban.update_card(card_id, **fields)
        _audit("kanban.update", {"card_id": card_id, "fields": sorted(fields)},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_move(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        stage = str(body.get("stage") or "")
        if not stage:
            self._reply_json(400, {"error": "stage 不能为空"})
            return
        card = kanban.move_card(card_id, stage, note=str(body.get("note") or ""))
        _audit("kanban.move", {"card_id": card_id, "stage": stage}, card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_archive(self, card_id: str, body: dict, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        archived = body.get("archived", True)
        if not isinstance(archived, bool):
            self._reply_json(400, {"error": "archived 必须为布尔值（true/false）"})
            return
        card = kanban.archive_card(card_id, archived=archived)
        _audit("kanban.archive", {"card_id": card_id, "archived": archived},
               card, True, token)
        self._reply_json(200, {"ok": True, "card": card})

    def _op_kanban_delete(self, card_id: str, token: str) -> None:
        if self._kanban_card_or_reply(card_id) is None:
            return
        # 已派发过任务的卡片 → KanbanError → 422（except 链）
        kanban.delete_card(card_id)
        _audit("kanban.delete", {"card_id": card_id}, {"deleted": card_id}, True, token)
        self._reply_json(200, {"ok": True, "deleted": card_id})

    def _op_kanban_dispatch(self, card_id: str, body: dict, token: str) -> None:
        """派发执行：implementation/periodic 卡片 → task_runner.start_run，
        成功后原子 link_task + 自动流转到 implementation 列（dispatch_card 单锁）。"""
        card = self._kanban_card_or_reply(card_id)
        if card is None:
            return
        if card.get("type") not in ("implementation", "periodic"):
            self._reply_json(422, {"error": "仅 implementation/periodic 卡片可派发执行"})
            return
        repo = (card.get("repo") or "").strip()
        if not repo or not Path(repo).is_dir():
            self._reply_json(422, {"error": f"卡片 repo 为空或路径不存在: {repo or '<空>'}"})
            return
        try:
            parallel = int(body.get("parallel") or 1)
        except (TypeError, ValueError):
            self._reply_json(400, {"error": "parallel 必须为正整数"})
            return
        confirm_mode = str(body.get("confirm_mode") or "auto")
        if confirm_mode not in ("auto", "web"):
            self._reply_json(400, {"error": "confirm_mode 须为 auto/web"})
            return
        # 幂等防护：卡片已有运行中任务（EXECUTING/PLANNING 或托管句柄存活）→ 拒绝重复派发
        for tid in reversed(card.get("task_ids") or []):
            st = _task_status_of(tid)
            if st in _RUNNING_STATES or task_runner.is_running(tid):
                self._reply_json(409, {
                    "error": f"卡片已有运行中任务（{tid}，状态 {st}），不可重复派发。"
                             "可先取消或等待其结束。",
                    "task_id": tid,
                })
                return
        # 任务文本 = 标题 + 描述（截断防御，防超长 argv）
        task_text = card.get("title", "")
        if card.get("description"):
            task_text += "\n\n" + card["description"]
        task_text = task_text[:4000]
        task_id = task_runner.start_run(repo, task_text, parallel=parallel,
                                        confirm_mode=confirm_mode)
        card = kanban.dispatch_card(card_id, task_id, note=f"派发任务 {task_id}")
        result = {"ok": True, "task_id": task_id, "card": card}
        _audit("kanban.dispatch", {"card_id": card_id, "task_id": task_id, "repo": repo},
               {"task_id": task_id}, True, token)
        self._reply_json(200, result)

    def do_PUT(self) -> None:
        """PUT /api/config {field, value}（R14 白名单字段编辑）。"""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            self._reply_json(404, {"error": f"not found: {path}"})
            return
        if not self._auth_guard(parsed.query, required="admin"):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024:
            self._reply_json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except json.JSONDecodeError:
            self._reply_json(400, {"error": "invalid JSON body"})
            return
        token = getattr(self.server, "admin_token", None) or ""  # type: ignore[attr-defined]
        if path == "/api/config":
            field = str(body.get("field") or "")
            value = body.get("value")
            try:
                result = put_config_field(field, value)
            except ProfileError as e:
                _audit("config.put", {"field": field}, str(e), False, token)
                self._reply_json(422, {"error": str(e)})
                return
            _audit("config.put", {"field": field}, result, True, token)
            self._reply_json(200, result)
            return
        self._reply_json(404, {"error": f"not found: {path}"})

    def do_DELETE(self) -> None:
        """DELETE /api/tasks/<id>  {confirm: true}（R7 单任务清理）。"""
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            self._reply_json(404, {"error": f"not found: {path}"})
            return
        if not self._auth_guard(parsed.query, required="admin"):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024:
            self._reply_json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except json.JSONDecodeError:
            self._reply_json(400, {"error": "invalid JSON body"})
            return
        parts = [p for p in path.split("/") if p]
        token = getattr(self.server, "admin_token", None) or ""  # type: ignore[attr-defined]
        if len(parts) == 3 and parts[1] == "tasks":
            task_id = parts[2]
            td = _task_dir(task_id)
            if td is None:
                self._reply_json(404, {"error": "task not found"})
                return
            if body.get("confirm") is not True:
                self._reply_json(400, {"error": "需 confirm: true 二次确认"})
                return
            status = _task_status_of(task_id)
            if status in _RUNNING_STATES or task_runner.is_running(task_id):
                self._reply_json(409, {"error": f"任务运行中（{status}），先 cancel 再清理"})
                return
            from .cli import clean_task_dirs
            result = clean_task_dirs([td])
            _audit("tasks.delete", {"task_id": task_id}, result, True, token)
            self._reply_json(200, result)
            return
        self._reply_json(404, {"error": f"not found: {path}"})

    # ── SSE：任务状态实时刷新 ───────────────────────────────

    def _stream_events(self, query: str) -> None:
        """SSE：周期轮询任务目录 mtime，有变化即推送 refresh 事件。"""
        params = {}
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = unquote(v)
        try:
            interval = max(2, min(30, int(params.get("interval", "5"))))
        except ValueError:
            interval = 5
        last_signature = self._tasks_signature()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                time.sleep(interval)
                sig = self._tasks_signature()
                if sig != last_signature:
                    last_signature = sig
                    body = json.dumps({"type": "refresh"}, ensure_ascii=False)
                    self.wfile.write(f"event: message\ndata: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _tasks_signature() -> str:
        sigs = []
        for td in _list_task_dirs():
            meta_p = td / "meta.json"
            try:
                sigs.append(f"{td.name}:{meta_p.stat().st_mtime:.0f}:{meta_p.stat().st_size}")
            except OSError:
                continue
        # 看板数据变化也触发 SSE refresh（kanban.json 不在任务目录内）
        try:
            kb = kanban.board_path()
            sigs.append(f"kanban:{kb.stat().st_mtime:.0f}:{kb.stat().st_size}")
        except OSError:
            pass
        return "|".join(sigs)


def serve_web(host: str = "127.0.0.1", port: int = 8091,
              token: Optional[str] = None,
              viewer_token: Optional[str] = None) -> None:
    """启动 Web 操作台服务（阻塞）。

    鉴权（P1.2 多用户角色）：
      --token/--admin-token：admin 角色（全部操作）
      --viewer-token：viewer 角色（只读 GET；写操作 403）
      两者均未配置 → 全开放（向后兼容）

    U4 失控防护：
      - 启动时扫描疑似孤儿任务（EXECUTING 但无托管句柄）并警告
      - 关闭时 atexit → task_runner.kill_all()（SIGINT 优雅收尾，超时 SIGKILL）
    """
    import atexit

    httpd = ThreadingHTTPServer((host, port), WebHandler)
    httpd.admin_token = token or ""  # type: ignore[attr-defined]
    httpd.viewer_token = viewer_token or ""  # type: ignore[attr-defined]

    orphans = task_runner.orphan_tasks()
    if orphans:
        console.warning(
            f"⚠️ 检测到 {len(orphans)} 个疑似孤儿任务（状态 EXECUTING 但非本实例托管）: "
            f"{', '.join(orphans[:5])}{' …' if len(orphans) > 5 else ''}。"
            "若为残留进程请手工 kill，再用 resume 续跑。"
        )

    @atexit.register
    def _kill_children() -> None:
        n = task_runner.kill_all()
        if n:
            console.print(f"🛑 web 关闭：已终止 {n} 个托管任务进程（SIGINT 收尾）")

    console.print(f"🌐 agent_go web 观察平台: http://{host}:{port}")
    if token:
        console.print("🔐 token 鉴权已启用（Authorization: Bearer <token>）")
    console.print("⏹️  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n⏹️  web 服务已停止")
        httpd.server_close()


# ═══════════════════════════════════════════════════════════════
# 单文件前端 SPA（内嵌 HTML，无外部资源依赖）
# ═══════════════════════════════════════════════════════════════

_SPA_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent_go 观察平台</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --border:#2a2f3a; --text:#e6e8eb;
          --dim:#8b93a1; --green:#3fb950; --red:#f85149; --yellow:#d29922;
          --blue:#58a6ff; --purple:#bc8cff; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); font-size:14px; }
  header { padding:14px 20px; border-bottom:1px solid var(--border);
           display:flex; align-items:center; gap:16px; }
  header h1 { font-size:16px; margin:0; }
  .status { margin-left:auto; color:var(--dim); }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
           background:var(--panel); border:1px solid var(--border); }
  .container { padding:16px 20px; }
  .filters { display:flex; gap:12px; margin-bottom:12px; align-items:center; flex-wrap:wrap; }
  .filters input[type=text], .filters select {
    background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:6px 10px; border-radius:6px; font-size:13px; }
  .filter-btn { background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:5px 10px; border-radius:6px; cursor:pointer; font-size:13px; }
  .filter-btn.active { border-color:var(--blue); color:var(--blue); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border);
           vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:12px; position:sticky; top:0;
       background:var(--bg); }
  tr.task-row { cursor:pointer; }
  tr.task-row:hover td { background:rgba(88,166,255,0.06); }
  .st-completed { color:var(--green); } .st-failed { color:var(--red); }
  .st-aborted { color:var(--yellow); } .st-blocked { color:var(--red); }
  .st-pending, .st-running { color:var(--dim); }
  .st-cancelled { color:var(--dim); }
  .dim { color:var(--dim); font-size:11px; }
  .task-detail { display:none; }
  .task-detail.open { display:table-row; }
  .detail-box { padding:16px; background:var(--panel); border:1px solid var(--border);
                border-radius:8px; }
  .sub-item { border:1px solid var(--border); border-radius:8px; margin-bottom:8px;
              background:#1a1e26; }
  .sub-head { padding:10px 14px; cursor:pointer; display:flex; gap:10px; align-items:center;
              flex-wrap:wrap; }
  .sub-head .icon { width:18px; }
  .sub-head .title { font-weight:500; }
  .tag { font-size:11px; padding:1px 7px; border-radius:8px; background:var(--border);
         color:var(--dim); }
  .sub-body { display:none; padding:12px 14px; border-top:1px solid var(--border); }
  .sub-body.open { display:block; }
  .tabs { display:flex; gap:4px; margin:10px 0; }
  .tab-btn { background:transparent; border:1px solid var(--border); color:var(--dim);
             padding:4px 12px; border-radius:6px; cursor:pointer; font-size:13px; }
  .tab-btn.active { color:var(--text); border-color:var(--blue); }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }
  pre, .log { background:#0b0d11; border:1px solid var(--border); border-radius:6px;
              padding:10px; overflow:auto; font-size:12px; line-height:1.5;
              font-family:"SF Mono",Menlo,Consolas,monospace; white-space:pre-wrap; }
  .kv { display:grid; grid-template-columns:150px 1fr; gap:4px 12px; margin-bottom:8px; }
  .kv dt { color:var(--dim); } .kv dd { margin:0; }
  .meter-summary { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:10px; }
  .meter-card { background:#0b0d11; border:1px solid var(--border); border-radius:8px;
                padding:10px 14px; }
  .meter-card .label { color:var(--dim); font-size:12px; }
  .meter-card .val { font-size:16px; font-weight:600; }
  .loading { color:var(--dim); text-align:center; padding:40px; }
  .err { color:var(--red); padding:20px; text-align:center; }
  .kv-table td { padding:4px 8px; font-size:12px; }
  .diff-stats { font-size:12px; color:var(--dim); }
  .vline { width:1px; height:14px; background:var(--border); }
  .nav-tabs { display:flex; gap:4px; margin-left:12px; }
  .nav-tab { background:transparent; border:none; color:var(--dim);
             padding:6px 12px; cursor:pointer; font-size:14px;
             border-bottom:2px solid transparent; }
  .nav-tab:hover { color:var(--text); }
  .nav-tab.active { color:var(--blue); border-bottom-color:var(--blue); }
  .kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
              gap:12px; margin-bottom:20px; }
  .kpi-card { background:var(--panel); border:1px solid var(--border);
              border-radius:8px; padding:14px 18px; }
  .kpi-card .label { color:var(--dim); font-size:12px; margin-bottom:4px; }
  .kpi-card .val { font-size:22px; font-weight:600; }
  .kpi-card .val.green { color:var(--green); }
  .kpi-card .val.red { color:var(--red); }
  .kpi-card .val.yellow { color:var(--yellow); }
  .section-title { font-size:15px; font-weight:600; margin:20px 0 10px;
                   color:var(--text); border-left:3px solid var(--blue);
                   padding-left:10px; }
  .trend-chart { background:#0b0d11; border:1px solid var(--border);
                 border-radius:8px; padding:16px; margin:10px 0; }
  .json-view { background:#0b0d11; border:1px solid var(--border); border-radius:6px;
               padding:10px; overflow:auto; font-size:12px;
               font-family:"SF Mono",Menlo,Consolas,monospace;
               white-space:pre-wrap; max-height:600px; }
  .warn-banner { background:rgba(210,153,34,0.1); border:1px solid var(--yellow);
                 border-radius:8px; padding:10px 14px; margin-bottom:16px;
                 color:var(--yellow); }
  .mode-badge { display:inline-block; padding:4px 14px; border-radius:14px;
                font-size:13px; font-weight:600; }
  .mode-badge.local { background:rgba(63,185,80,0.15); color:var(--green);
                      border:1px solid var(--green); }
  .mode-badge.cloud { background:rgba(88,166,255,0.12); color:var(--blue);
                      border:1px solid var(--blue); }
  .mode-badge.custom { background:rgba(210,153,34,0.12); color:var(--yellow);
                       border:1px solid var(--yellow); }
  .btn { background:var(--panel); border:1px solid var(--border); color:var(--text);
         padding:6px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn:hover { border-color:var(--blue); }
  .btn.primary { border-color:var(--green); color:var(--green); }
  .btn:disabled { opacity:0.5; cursor:not-allowed; }
  .health-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
                 gap:12px; margin:12px 0; }
  .health-card { background:var(--panel); border:1px solid var(--border);
                 border-radius:8px; padding:12px 14px; font-size:13px; }
  .health-card .role { color:var(--dim); font-size:12px; margin-bottom:4px; }
  .health-card .st-ok { color:var(--green); font-weight:600; }
  .health-card .st-bad { color:var(--red); font-weight:600; }
  .health-card .st-skip { color:var(--dim); }
  .health-card .url { font-size:11px; color:var(--dim); word-break:break-all;
                      margin-top:4px; font-family:Menlo,Consolas,monospace; }
  .run-form { display:flex; gap:8px; align-items:center; margin-bottom:14px;
              background:var(--panel); border:1px solid var(--border);
              border-radius:8px; padding:10px 12px; flex-wrap:wrap; }
  .run-input { background:#0b0d11; border:1px solid var(--border); color:var(--text);
               padding:6px 10px; border-radius:6px; font-size:13px; min-width:120px; }
  .run-textarea { flex:3; resize:vertical; line-height:1.5;
                  font-family:inherit; }
  .run-hint { color:var(--dim); font-size:12px; }
  .op-bar { display:flex; gap:8px; align-items:center; margin:8px 0 12px;
            flex-wrap:wrap; }
  .op-msg { font-size:12px; margin-left:6px; }
  .review-panel { border-top:1px solid var(--border); margin-top:12px;
                  padding-top:4px; }
  .pending-card { background:rgba(210,153,34,0.08); border:1px solid var(--yellow);                  border-radius:8px; padding:12px 14px; margin-bottom:12px; }
  .humility-card { background:rgba(210,153,34,0.06); border:1px solid var(--yellow); border-radius:8px;
                   padding:10px 14px; margin-top:12px; margin-bottom:12px; }
  .humility-card .h-title { font-weight:600; color:var(--yellow); margin-bottom:6px; }
  .humility-card .h-line { color:var(--dim); font-size:13px; line-height:1.6; }
  .kanban-toolbar { display:flex; gap:12px; align-items:center; margin-bottom:12px; }
  .kanban-toolbar input { background:var(--panel); border:1px solid var(--border);
                          color:var(--text); padding:6px 10px; border-radius:6px; font-size:13px; }
  .kanban-board { display:flex; gap:12px; align-items:flex-start; overflow-x:auto;
                  padding-bottom:8px; }
  .kanban-col { flex:1 1 0; min-width:220px; background:var(--panel);
                border:1px solid var(--border); border-radius:8px; padding:8px; }
  .kanban-col.drag-over { border-color:var(--blue); }
  .kanban-col-head { display:flex; align-items:center; gap:8px; padding:4px 6px 10px;
                     font-weight:600; }
  .kanban-new-btn { margin-left:auto; background:transparent; border:1px solid var(--border);
                    color:var(--dim); border-radius:6px; cursor:pointer; font-size:12px;
                    padding:2px 8px; }
  .kanban-new-btn:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-card { background:#1a1e26; border:1px solid var(--border); border-radius:8px;
                 padding:10px 12px; margin-bottom:8px; cursor:pointer; }
  .kanban-card:hover { border-color:var(--blue); }
  .kanban-card.archived { opacity:0.55; filter:saturate(0.5); }
  .kanban-card.archived:hover { border-color:var(--yellow); }
  .kanban-card.open { border-color:var(--blue); }
  .kanban-card .kc-title { font-weight:500; margin-bottom:6px; }
  .kanban-card .kc-meta { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .kanban-card .kc-foot { display:flex; gap:6px; align-items:center; margin-top:8px; }
  .kanban-move-btn { background:transparent; border:1px solid var(--border); color:var(--dim);
                     border-radius:4px; cursor:pointer; font-size:11px; padding:1px 6px; }
  .kanban-move-btn:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-detail { border-top:1px solid var(--border); margin-top:8px; padding-top:8px; }
  .kanban-detail pre { max-height:240px; }
  .kanban-detail .kc-tasks { margin:8px 0; }
  .kanban-task-link { cursor:pointer; }
  .kanban-task-link:hover { color:var(--blue); border-color:var(--blue); }
  .kanban-history { font-size:12px; color:var(--dim); margin-top:8px; }
  .kanban-history .kh-item { padding:3px 0; border-bottom:1px dashed var(--border); }
  .kanban-form { background:#0b0d11; border:1px solid var(--border); border-radius:8px;
                 padding:10px; margin-bottom:8px; }
  .kanban-form input, .kanban-form select, .kanban-form textarea {
    width:100%; background:var(--panel); border:1px solid var(--border); color:var(--text);
    padding:5px 8px; border-radius:6px; font-size:12px; margin-bottom:6px; }
  .kanban-form textarea { resize:vertical; font-family:inherit; line-height:1.5; }
</style>
</head>
<body>
<header>
  <h1>🌐 agent_go 观察平台</h1>
  <nav class="nav-tabs">
    <button class="nav-tab" data-view="kanban">🗂 看板</button>
    <button class="nav-tab active" data-view="tasks">📋 任务</button>
    <button class="nav-tab" data-view="overview">📊 总览</button>
    <button class="nav-tab" data-view="cost">💰 成本</button>
    <button class="nav-tab" data-view="models">🤖 模型</button>
    <button class="nav-tab" data-view="config">⚙️ 配置</button>
    <button class="nav-tab" data-view="storage">💾 运维</button>
    <button class="nav-tab" data-view="archive">🗄️ 归档</button>
  </nav>
  <span class="badge" id="connBadge">连接中…</span>
  <div class="status" id="headerStatus"></div>
</header>
<div class="container">
  <div id="filtersBar" class="filters">
    <input type="text" id="searchInput" placeholder="🔍 搜索任务/ID/描述…">
    <span id="statusFilters"></span>
    <button class="filter-btn" id="refreshBtn">🔄 刷新</button>
  </div>
  <div id="mainView">
    <div class="loading">加载中…</div>
  </div>
</div>

<script>
const STATUS_COLORS = {
  // 新规范状态（status.py TASK_STATES）—— 按生命周期阶段着色
  EXECUTING:'st-running',
  DELIVERY_READY:'st-completed', ACCEPTED_DELIVERY:'st-completed',
  VERIFICATION_FAILED:'st-failed', DELIVERY_FAILED:'st-failed',
  BLOCKED:'st-blocked', CANCELLED:'st-cancelled',
  // 兼容：未知/legacy 状态兜底
  unknown:'st-pending'
};
let tasks = [];
let statusFilter = 'all';
let sse = null;

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
function fmtDur(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return sec.toFixed(1)+'s';
  if (sec < 3600) return (sec/60).toFixed(1)+'m';
  return (sec/3600).toFixed(2)+'h';
}
function fmtCost(c) {
  if (c === null || c === undefined) return '—';
  return '$'+Number(c).toFixed(4);
}

// token 鉴权：服务器启用 --token 时，fetch 带 Authorization 头；
// 401 时提示输入并存 sessionStorage（EventSource 走 ?token= query，见 connectSSE）
let authToken = sessionStorage.getItem('agent_go_token') || '';

async function api(path) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = authToken ? {'Authorization': 'Bearer '+authToken} : {};
    const r = await fetch(path, {headers});
    if (r.status === 401 && attempt === 0) {
      const t = prompt('🔐 服务器启用了 token 鉴权，请输入 token：');
      if (t) { authToken = t; sessionStorage.setItem('agent_go_token', t); continue; }
    }
    if (!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }
  throw new Error('HTTP 401');
}

async function postJSON(path, body) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = {'Content-Type': 'application/json'};
    if (authToken) headers['Authorization'] = 'Bearer '+authToken;
    const r = await fetch(path, {method: 'POST', headers, body: JSON.stringify(body || {})});
    if (r.status === 401 && attempt === 0) {
      const t = prompt('🔐 服务器启用了 token 鉴权，请输入 token：');
      if (t) { authToken = t; sessionStorage.setItem('agent_go_token', t); continue; }
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
    return data;
  }
  throw new Error('HTTP 401');
}

function statusIcon(st) {
  return {EXECUTING:'🔄',
          DELIVERY_READY:'🟢', ACCEPTED_DELIVERY:'✅',
          VERIFICATION_FAILED:'🔴', DELIVERY_FAILED:'🔴',
          BLOCKED:'⛔', CANCELLED:'⏹️',
          unknown:'⚪'}[st] || '⚪';
}

// 子任务状态（与任务级状态不同，是执行结果维度）
const SUBTASK_STATUS_COLORS = {
  completed:'st-completed', no_changes:'st-completed', degraded:'st-aborted',
  failed:'st-failed', blocked:'st-blocked', pending:'st-pending'
};
function subtaskStatusIcon(st) {
  return {completed:'🟢', no_changes:'⏭️', degraded:'⚠️',
          failed:'🔴', blocked:'⛔', pending:'⚪'}[st] || '⚪';
}

// ── 任务清单 ────────────────────────────────────────────────
async function loadTasks(endpoint) {
  // endpoint='/api/archive' 时加载历史归档任务，其余默认 '/api/tasks'
  // 两者共享 renderStatusFilters/renderTasks（归档状态已归一化）
  try {
    const data = await api(endpoint || '/api/tasks');
    tasks = data.tasks || [];
    renderStatusFilters();
    renderTasks();
    setConn(true);
  } catch (e) {
    setConn(false);
    document.getElementById('mainView').innerHTML =
      '<div class="err">加载失败: '+esc(e.message)+'</div>';
  }
}

// 状态分组：canonical state → 阶段组（用于聚合筛选）
const STATUS_GROUPS = {
  executing: ['EXECUTING','PAUSED'],
  delivered: ['DELIVERY_READY','ACCEPTED_DELIVERY'],
  failed: ['VERIFICATION_FAILED','DELIVERY_FAILED'],
  blocked: ['BLOCKED'],
  cancelled: ['CANCELLED']
};
const GROUP_LABELS = {
  planning:'📐 规划中', executing:'🔄 执行中', delivered:'🟢 已交付',
  failed:'🔴 失败', blocked:'⛔ 阻断', cancelled:'⏹️ 已取消'
};

function statusGroup(st) {
  for (const [g, states] of Object.entries(STATUS_GROUPS)) {
    if (states.includes(st)) return g;
  }
  return 'executing'; // unknown 兜底：未知状态多为运行中间态，归入执行中
}

function renderStatusFilters() {
  // 按阶段组聚合计数
  const counts = {};
  tasks.forEach(t => {
    const g = statusGroup(t.status);
    counts[g] = (counts[g]||0)+1;
  });
  const order = ['executing','planning','delivered','failed','blocked','cancelled'];
  const html = ['<span class="filter-btn'+(statusFilter==='all'?' active':'')+'" data-s="all">全部 ('+tasks.length+')</span>'];
  // U1：待确认过滤器（有 pending 任务时置最前，高可见）
  const pendingCount = tasks.filter(t => t.pending_confirmation).length;
  if (pendingCount) html.push(
    '<span class="filter-btn'+(statusFilter==='pending-confirm'?' active':'')+'" data-s="pending-confirm" '+
    'style="color:var(--yellow)">🔔 待确认 ('+pendingCount+')</span>');
  order.forEach(g => {
    if (counts[g]) html.push(
      '<span class="filter-btn'+(statusFilter===g?' active':'')+'" data-s="'+g+'">'+
      GROUP_LABELS[g]+' ('+counts[g]+')</span>');
  });
  document.getElementById('statusFilters').innerHTML = html.join('');
  document.querySelectorAll('#statusFilters .filter-btn').forEach(b => {
    b.onclick = () => { statusFilter = b.dataset.s; renderStatusFilters(); renderTasks(); };
  });
  document.getElementById('headerStatus').textContent =
    '共 '+tasks.length+' 个任务（新规范）';
}

function filteredTasks() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  return tasks.filter(t => {
    if (statusFilter === 'pending-confirm') return !!t.pending_confirmation;
    if (statusFilter !== 'all' && statusGroup(t.status) !== statusFilter) return false;
    if (!q) return true;
    return (t.id+' '+t.task+' '+(t.repo||'')).toLowerCase().includes(q);
  });
}

function renderTasks() {
  const list = filteredTasks();
  const runForm =
    '<div class="run-form">'+
    '<input id="runRepo" class="run-input" style="flex:2" placeholder="仓库绝对路径，如 /Users/me/proj">'+
    '<textarea id="runTask" class="run-input run-textarea" rows="3" placeholder="任务描述（自然语言，可多行详细描述需求、验收标准、约束等）"></textarea>'+
    '<select id="runParallel" class="run-input"><option value="1">并发1</option>'+
    '<option value="2">并发2</option><option value="3">并发3</option><option value="4">并发4</option></select>'+
    '<select id="runConfirm" class="run-input">'+
    '<option value="auto">auto（跳过计划确认）</option>'+
    '<option value="web">web（页面确认 Plan）</option></select>'+
    '<button class="btn primary" id="btnRunStart">🚀 启动任务</button>'+
    '<span id="runMsg" style="margin-left:8px;font-size:12px"></span>'+
    '</div>';
  if (!list.length) {
    document.getElementById('mainView').innerHTML = runForm +
      '<div class="loading">暂无匹配任务</div>';
    bindRunForm();
    return;
  }
  const rows = list.map(t => {
    const statusCls = STATUS_COLORS[t.status] || 'st-pending';
    return '<tr class="task-row" data-id="'+esc(t.id)+'">'+
      '<td><span class="'+statusCls+'">'+statusIcon(t.status)+' '+esc(t.status)+'</span>'+
      (t.pending_confirmation ? ' <span title="等待计划确认" style="color:var(--yellow)">🔔</span>' : '')+'</td>'+
      '<td>'+esc(t.id)+'</td>'+
      '<td>'+esc(t.task)+'</td>'+
      '<td>'+t.subtask_count+'</td>'+
      '<td>'+t.completed+'/'+t.failed+(t.blocked?'/⛔'+t.blocked:'')+'</td>'+
      '<td>'+fmtCost(t.cost_usd)+'</td>'+
      '<td>'+fmtDur(t.total_elapsed_sec)+'</td>'+
      '</tr>';
  }).join('');
  document.getElementById('mainView').innerHTML = runForm +
    '<table><thead><tr><th>状态</th><th>任务 ID</th><th>描述</th>'+
    '<th>子任务</th><th>完成/失败</th><th>成本</th><th>耗时</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table>';
  document.querySelectorAll('.task-row').forEach(row => {
    row.addEventListener('click', () => toggleTask(row.dataset.id, row));
  });
  bindRunForm();
}

function bindRunForm() {
  const btnRun = document.getElementById('btnRunStart');
  if (!btnRun) return;
  btnRun.onclick = async () => {
    const repo = document.getElementById('runRepo').value.trim();
    const task = document.getElementById('runTask').value.trim();
    const parallel = parseInt(document.getElementById('runParallel').value, 10) || 1;
    const confirmMode = document.getElementById('runConfirm').value;
    const msg = document.getElementById('runMsg');
    if (!repo || !task) { msg.textContent = '⚠️ 请填写仓库路径和任务描述'; msg.style.color = 'var(--yellow)'; return; }
    btnRun.disabled = true;
    msg.textContent = '启动中（生成 Plan 约需数十秒）…'; msg.style.color = 'var(--dim)';
    try {
      const d = await postJSON('/api/tasks/run', {repo, task, parallel, confirm_mode: confirmMode});
      msg.textContent = '✅ 已启动: '+d.task_id+'（'+d.note+'）';
      msg.style.color = 'var(--green)';
      // U2：web 确认模式 → 自动展开新任务行并滚动定位（用户立即看到确认入口）
      setTimeout(async () => {
        await loadTasks();
        if (confirmMode === 'web') {
          const row = document.querySelector('.task-row[data-id="'+d.task_id+'"]');
          if (row) {
            row.scrollIntoView({block: 'center', behavior: 'smooth'});
            toggleTask(d.task_id, row);
          }
        }
      }, 1200);
    } catch (e) {
      msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
    } finally { btnRun.disabled = false; }
  };
}

// ── 任务详情展开 ────────────────────────────────────────────
async function toggleTask(id, row) {
  const existing = document.querySelector('.task-detail[data-id="'+id+'"]');
  if (existing) { existing.remove(); return; }
  const tr = document.createElement('tr');
  tr.className = 'task-detail open';
  tr.dataset.id = id;
  const td = document.createElement('td');
  td.colSpan = 7;
  td.innerHTML = '<div class="detail-box"><div class="loading">加载任务详情…</div></div>';
  tr.appendChild(td);
  row.after(tr);
  try {
    const data = await api('/api/tasks/'+encodeURIComponent(id));
    td.innerHTML = '<div id="pendingCard"></div>' + taskOpsBar(id, data.status, data.managed) +
      renderTaskDetail(data) +
      '<div class="review-panel" id="reviewPanel"><div class="loading">加载审批台…</div></div>';
    bindDetailEvents(id, tr);
    bindTaskOps(id, td);
    loadReviewPanel(id, td);
    loadPendingCard(id, td);
    loadExtraPanels(id, td);
  } catch (e) {
    td.innerHTML = '<div class="detail-box"><div class="err">'+esc(e.message)+'</div></div>';
  }
}

function taskOpsBar(id, status, managed) {
  const running = (status === 'EXECUTING' || status === 'PLANNING');
  // U5：cancel 边界标识——运行中但非本实例托管（CLI 启动/孤儿）→ 禁用 + 明示
  const unmanagedRunning = running && !managed;
  return '<div class="op-bar">'+
    '<button class="btn" data-op="resume" '+(running?'disabled':'')+'>▶️ 恢复</button>'+
    '<button class="btn" data-op="cancel" '+(running && !unmanagedRunning ?'':'disabled')+
      (unmanagedRunning ? ' title="任务非本 web 实例启动（CLI 或孤儿进程），无法用本页取消"' : '')+'>⏹ 取消</button>'+
    '<button class="btn" data-op="clean" '+(running?'disabled':'')+'>🗑 清理</button>'+
    '<button class="btn" data-op="report">📄 报告</button>'+
    (unmanagedRunning ? '<span class="tag" style="color:var(--yellow)">👁 外部进程，仅可观测（CLI: agent_go resume/cancel）</span>' : '')+
    '<span class="op-msg" id="opMsg"></span>'+
    '</div>';
}

function bindTaskOps(id, td) {
  const msg = td.querySelector('#opMsg');
  const say = (t, color) => { msg.textContent = t; msg.style.color = color || 'var(--dim)'; };
  td.querySelectorAll('[data-op]').forEach(btn => {
    btn.onclick = async () => {
      const op = btn.dataset.op;
      if (op === 'report') {
        window.open('/api/tasks/'+encodeURIComponent(id)+'/report?format=html', '_blank');
        return;
      }
      if (op === 'resume' && !confirm('恢复任务 '+id+'？（从断点续跑剩余子任务）')) return;
      if (op === 'cancel' && !confirm('取消任务 '+id+'？\\n将发送 SIGINT（与 Ctrl+C 同义），pipeline 收尾后停止。')) return;
      if (op === 'clean' && !confirm('清理任务 '+id+'？\\n将删除任务数据目录（worktree/tag 一并清理），不可恢复！')) return;
      btn.disabled = true;
      say(op+' 执行中…');
      try {
        let d;
        if (op === 'clean') d = await delJSON('/api/tasks/'+encodeURIComponent(id), {confirm: true});
        else d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/'+op, {});
        say('✅ '+(d.note || d.status || (d.removed ? '已清理 '+d.removed.length+' 个目录' : '完成')), 'var(--green)');
        setTimeout(loadTasks, 1200);
      } catch (e) {
        say('❌ '+e.message, 'var(--red)');
        btn.disabled = false;
      }
    };
  });
}

async function loadReviewPanel(id, td) {
  const panel = td.querySelector('#reviewPanel');
  let rv = {};
  try { rv = await api('/api/tasks/'+encodeURIComponent(id)+'/review'); } catch (e) {}
  const decision = rv.decision;
  const decBadge = decision ?
    {'approved':'<span style="color:var(--green)">✅ 已通过</span>',
     'rejected':'<span style="color:var(--red)">❌ 已拒绝</span>',
     'changes-requested':'<span style="color:var(--yellow)">📝 需修改</span>'}[decision] || esc(decision)
    : '<span style="color:var(--dim)">未审批</span>';
  panel.innerHTML =
    '<div class="section-title">⚖️ 交付审批台</div>'+
    '<div class="op-bar"><span>当前决策: '+decBadge+'</span><span class="vline"></span>'+
    '<button class="btn" data-rv="review">🔍 聚合审查</button>'+
    '<button class="btn" data-rv="deep">🔬 深层审查</button><span class="vline"></span>'+
    '<button class="btn" data-rv="approve">✅ 通过</button>'+
    '<button class="btn" data-rv="reject">❌ 拒绝</button>'+
    '<button class="btn" data-rv="changes">📝 需修改</button><span class="vline"></span>'+
    '<button class="btn primary" data-rv="merge">🔀 Merge</button>'+
    '<button class="btn" data-rv="pr">🚀 PR</button>'+
    '<span class="op-msg" id="rvMsg"></span></div>';
  const msg = panel.querySelector('#rvMsg');
  const say = (t, color) => { msg.textContent = t; msg.style.color = color || 'var(--dim)'; };
  panel.querySelectorAll('[data-rv]').forEach(btn => {
    btn.onclick = async () => {
      const op = btn.dataset.rv;
      btn.disabled = true;
      say('执行中…');
      try {
        if (op === 'review' || op === 'deep') {
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/review',
                                   op === 'deep' ? {deep: true} : {});
          say('✅ '+(d.status === 'review_started' ? '深层审查已启动（后台运行，完成后刷新查看 review.json）' : '审查完成'), 'var(--green)');
        } else if (op === 'approve' || op === 'reject' || op === 'changes') {
          const decision = op === 'changes' ? 'changes-requested' : op;
          let comment = '';
          if (decision !== 'approve') {
            comment = prompt('审批意见（可选）：') || '';
          }
          if (!confirm('确认决策「'+decision+'」？将写入 review.json 并记录审计。')) { btn.disabled = false; say(''); return; }
          await postJSON('/api/tasks/'+encodeURIComponent(id)+'/review/decision', {decision, comment});
          say('✅ 决策已记录: '+decision, 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        } else if (op === 'merge') {
          const pv = await api('/api/tasks/'+encodeURIComponent(id)+'/merge-preview');
          if (pv.pr_url) { say('⚠️ 已走 PR 交付路径（'+pv.pr_url+'），merge 互斥', 'var(--yellow)'); btn.disabled = false; return; }
          if (pv.explicit_merge_commit) { say('✅ 已合并过: '+pv.explicit_merge_commit.slice(0,12), 'var(--green)'); btn.disabled = false; return; }
          if (pv.mergeable === false) {
            say('❌ 无法 clean merge'+((pv.conflicts||[]).length ? '，冲突: '+pv.conflicts.join(', ') : (pv.error ? ': '+pv.error : '')), 'var(--red)');
            btn.disabled = false; return;
          }
          const text = '确认合并？\\n\\n  delivery 分支: '+pv.delivery_branch+
            '\\n  目标分支: '+pv.target_branch+'\\n  新增 commit: '+(pv.ahead != null ? pv.ahead : '?')+
            '\\n\\n确定后选择是否推送 remote。';
          if (!confirm(text)) { btn.disabled = false; say(''); return; }
          const push = confirm('合并成功。是否推送到 remote（origin）？\\n确定=推送，取消=仅本地合并');
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/merge', {push, remote: 'origin'});
          say('✅ merge 完成'+(push ? '（已推送）' : ''), 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        } else if (op === 'pr') {
          const push = confirm('创建 PR？\\n确定=推送分支并创建真实 PR\\n取消=仅生成 PR.md（offline 预览）');
          const d = await postJSON('/api/tasks/'+encodeURIComponent(id)+'/pr', {push, remote: 'origin'});
          if (d.pr_url) say('✅ PR 已创建: '+d.pr_url, 'var(--green)');
          else say('✅ '+(push ? 'PR 完成' : 'PR.md 已生成（offline）'), 'var(--green)');
          setTimeout(() => loadReviewPanel(id, td), 800);
        }
      } catch (e) {
        say('❌ '+e.message, 'var(--red)');
      } finally {
        btn.disabled = false;
      }
    };
  });
}

async function loadExtraPanels(id, td) {
  // R12 偏差 + R17 worktree 折叠面板（审批台下方）
  const anchor = td.querySelector('#reviewPanel');
  if (!anchor) return;
  const box = document.createElement('div');
  box.innerHTML = '<div id="devPanel"></div><div id="wtPanel"></div>';
  anchor.after(box);
  try {
    const dv = await api('/api/tasks/'+encodeURIComponent(id)+'/deviation');
    if (dv.total > 0) {
      const typeRows = Object.entries(dv.by_type || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
      const causeRows = Object.entries(dv.by_root_cause || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
      const evRows = (dv.events || []).slice(-10).map(e =>
        '<tr><td>'+esc(e.deviation_type||'')+'</td><td>'+esc(e.root_cause_category||'')+'</td>'+
        '<td>'+esc((e.summary||'').slice(0,80))+'</td></tr>').join('');
      box.querySelector('#devPanel').innerHTML =
        '<div class="section-title">📐 偏差记录（'+dv.total+'）</div>'+
        '<div style="display:flex;gap:24px;flex-wrap:wrap">'+
        '<table><thead><tr><th>类型</th><th>数</th></tr></thead><tbody>'+typeRows+'</tbody></table>'+
        '<table><thead><tr><th>根因</th><th>数</th></tr></thead><tbody>'+causeRows+'</tbody></table></div>'+
        (evRows ? '<table style="margin-top:8px"><thead><tr><th>类型</th><th>根因</th><th>摘要</th></tr></thead><tbody>'+evRows+'</tbody></table>' : '');
    }
  } catch (e) {}
  try {
    const wt = await api('/api/tasks/'+encodeURIComponent(id)+'/worktrees');
    if ((wt.worktrees || []).length) {
      const rows = wt.worktrees.map(w =>
        '<tr><td>'+esc(w.subtask_id)+'</td><td>'+esc(w.status)+'</td>'+
        '<td style="font-family:Menlo,monospace;font-size:11px">'+esc(w.branch)+'</td>'+
        '<td>'+(w.preserved ? '📌 保留' : '')+'</td>'+
        '<td style="font-size:11px;color:var(--dim)">'+esc((w.failure_reason||'').slice(0,60))+'</td></tr>').join('');
      box.querySelector('#wtPanel').innerHTML =
        '<div class="section-title">🌳 保留 Worktree（'+wt.worktrees.length+'）</div>'+
        '<table><thead><tr><th>子任务</th><th>状态</th><th>分支</th><th></th><th>失败原因</th></tr></thead>'+
        '<tbody>'+rows+'</tbody></table>';
    }
  } catch (e) {}
}

async function loadPendingCard(id, td) {
  const slot = td.querySelector('#pendingCard');
  if (!slot) return;
  // U3：详情行存活期间每 5s 轮询（Plan 生成 30-60s，pending 出现后自动渲染卡片；
  // 决策提交后 pending 消失/下一级 pending 出现均靠轮询驱动视图更新）
  if (!slot.dataset.polling) {
    slot.dataset.polling = '1';
    const poll = () => {
      if (!document.body.contains(slot)) return;  // 详情已关闭 → 停止
      loadPendingCard(id, td);
    };
    setTimeout(poll, 5000);
  }
  let d;
  try { d = await api('/api/tasks/'+encodeURIComponent(id)+'/pending-confirmation'); }
  catch (e) { return; }
  if (!d.pending) { slot.innerHTML = ''; return; }
  const p = d.pending;
  const age = Math.max(0, Math.round((Date.now() - new Date(p.ts).getTime()) / 60000));
  const left = Math.max(1, Math.round(p.timeout_sec / 60) - age);
  let body = '';
  if (p.stage === 'plan') {
    const plan = p.payload || {};
    const steps = (plan.steps || []).map((s, i) =>
      '<tr><td>'+(i+1)+'</td><td>'+esc(s.title || s.name || '')+'</td>'+
      '<td>'+esc(s.difficulty || '')+'</td><td>'+esc(s.agent_type || '')+'</td></tr>').join('');
    body = '<div style="font-weight:600;margin-bottom:6px">'+esc(plan.title || '执行计划')+'</div>'+
      (plan.summary ? '<div style="color:var(--dim);margin-bottom:8px">'+esc(plan.summary)+'</div>' : '')+
      '<table><thead><tr><th>#</th><th>步骤</th><th>难度</th><th>角色</th></tr></thead><tbody>'+steps+'</tbody></table>';
  } else {
    const subs = (p.payload && p.payload.subtasks) || [];
    body = '<div style="font-weight:600;margin-bottom:6px">子任务拆解（'+subs.length+' 个）</div>'+
      '<table><thead><tr><th>ID</th><th>标题</th><th>难度</th></tr></thead><tbody>'+
      subs.map(s => '<tr><td>'+esc(s.id||'')+'</td><td>'+esc(s.title||'')+'</td><td>'+esc(s.difficulty||'')+'</td></tr>').join('')+
      '</tbody></table>';
  }
  const btns = p.stage === 'plan'
    ? '<button class="btn primary" data-cf="Y">✅ 确认执行</button>'+
      '<button class="btn" data-cf="R">🔄 重新生成</button>'+
      '<button class="btn" data-cf="N">❌ 取消任务</button>'
    : '<button class="btn primary" data-cf="Y">✅ 确认子任务</button>'+
      '<button class="btn" data-cf="N">❌ 取消任务</button>';
  slot.innerHTML =
    '<div class="pending-card">'+
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'+
    '<span style="font-size:16px">🔔</span>'+
    '<span style="font-weight:600">等待确认：'+(p.stage === 'plan' ? '执行计划' : '子任务拆解')+'</span>'+
    '<span style="color:var(--yellow);font-size:12px">约 '+left+' 分钟后超时自动取消</span></div>'+
    body+
    '<div class="op-bar" style="margin-top:10px">'+btns+'<span class="op-msg" id="cfMsg"></span></div>'+
    '</div>';
  slot.querySelectorAll('[data-cf]').forEach(btn => {
    btn.onclick = async () => {
      const decision = btn.dataset.cf;
      const msg = slot.querySelector('#cfMsg');
      if (decision === 'N' && !confirm('取消任务 '+id+'？')) return;
      btn.disabled = true;
      try {
        await postJSON('/api/tasks/'+encodeURIComponent(id)+'/confirm', {stage: p.stage, decision});
        msg.textContent = '✅ 已提交决策: '+decision; msg.style.color = 'var(--green)';
        setTimeout(() => loadPendingCard(id, td), 3000);
      } catch (e) {
        msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
        btn.disabled = false;
      }
    };
  });
}

async function delJSON(path, body) {
  const headers = {'Content-Type': 'application/json'};
  if (authToken) headers['Authorization'] = 'Bearer '+authToken;
  const r = await fetch(path, {method: 'DELETE', headers, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}

function renderTaskDetail(d) {
  const items = (d.subtasks||[]).map((s,i) => {
    const statusCls = SUBTASK_STATUS_COLORS[s.status] || 'st-pending';
    const src = s.agent_type_source || 'default';
    return '<div class="sub-item">'+
      '<div class="sub-head" data-sub="'+esc(s.id)+'">'+
        '<span class="icon '+statusCls+'">'+subtaskStatusIcon(s.status)+'</span>'+
        '<span class="title">['+esc(s.id)+'] '+esc(s.title)+'</span>'+
        '<span class="tag">'+esc(s.difficulty||'medium')+'</span>'+
        '<span class="tag">'+esc(s.agent_type||'developer')+'</span>'+
        '<span class="tag">'+esc(src)+'</span>'+
        (s.retry_count? '<span class="tag" style="color:var(--yellow)">retry ×'+s.retry_count+'</span>':'')+
        '<span class="tag">'+fmtDur(s.duration_sec)+'</span>'+
        (s.verify_ok!==undefined? '<span class="tag">verify:'+ (s.verify_ok?'✅':'❌')+'</span>':'')+
      '</div>'+
      '<div class="sub-body" id="sub-body-'+esc(s.id)+'">'+
        '<div class="loading">点击子任务查看明细</div>'+
      '</div></div>';
  }).join('');
  // 谦逊层盲区卡片（#51：交底报告进操作台）
  const bs = d.blind_spots || {};
  const persp = d.uncovered_perspectives || [];
  const layer = d.layer_attribution || {};
  const blindLines = [];
  if (bs.uncovered_acceptance_ids && bs.uncovered_acceptance_ids.length) blindLines.push('未覆盖验收 ID: ' + bs.uncovered_acceptance_ids.join(', '));
  if (bs.weakly_anchored_subtasks && bs.weakly_anchored_subtasks.length) blindLines.push('弱锚定验证子任务: ' + bs.weakly_anchored_subtasks.join(', '));
  if (bs.unattributed_failures && bs.unattributed_failures.length) blindLines.push('无根因失败: ' + bs.unattributed_failures.join(', '));
  if (bs.baseline_dirty) blindLines.push('任务启动时工作区有未提交改动');
  if (bs.inconclusive_evaluations && bs.inconclusive_evaluations.length) blindLines.push('语义评估不确定: ' + bs.inconclusive_evaluations.join(', '));
  persp.forEach(p => blindLines.push('未覆盖视角 [' + esc(p.perspective||'') + ']: ' + esc(p.reason||'')));
  if (layer.primary) blindLines.push('层间归因: ' + esc(layer.primary));
  const humilityHtml = blindLines.length
    ? '<div class="humility-card"><div class="h-title">⚠️ 已知盲区（系统主动交底）</div>' +
      blindLines.map(l => '<div class="h-line">' + l + '</div>').join('') + '</div>'
    : '';
  return '<div class="kv">'+
    '<dt>任务</dt><dd>'+esc(d.task)+'</dd>'+
    '<dt>仓库</dt><dd>'+esc(d.repo)+'</dd>'+
    '<dt>状态</dt><dd><span class="'+((STATUS_COLORS[d.status])||'')+'">'+statusIcon(d.status)+' '+esc(d.status)+'</span></dd>'+
    '<dt>创建时间</dt><dd>'+esc(d.created_at||'')+'</dd>'+
    '</div>'+humilityHtml+'<div style="margin-top:12px">'+items+'</div>';
}

function bindDetailEvents(taskId, tr) {
  tr.querySelectorAll('.sub-head').forEach(head => {
    head.addEventListener('click', async () => {
      const subId = head.dataset.sub;
      const body = document.getElementById('sub-body-'+subId);
      const isOpen = body.classList.contains('open');
      if (isOpen) { body.classList.remove('open'); return; }
      if (!body.dataset.loaded) {
        body.innerHTML = '<div class="loading">加载子任务明细…</div>';
        try {
          const detail = await api('/api/tasks/'+encodeURIComponent(taskId)+'/'+encodeURIComponent(subId)+'/detail');
          body.innerHTML = renderSubDetail(detail);
          bindSubTabs(body, taskId, subId);
          body.dataset.loaded = '1';
        } catch (e) {
          body.innerHTML = '<div class="err">'+esc(e.message)+'</div>';
        }
      }
      body.classList.add('open');
    });
  });
}

function renderSubDetail(d) {
  const r = d.result || {};
  const stats = r.change_stats || {};
  const statsHtml = Object.keys(stats).length ? '<pre>'+esc(JSON.stringify(stats, null, 2))+'</pre>'
    : '<span class="diff-stats">无改动统计</span>';
  return '<div class="tabs">'+
    '<button class="tab-btn active" data-tab="overview">概览</button>'+
    '<button class="tab-btn" data-tab="verify">验证</button>'+
    '<button class="tab-btn" data-tab="log">日志</button>'+
    '<button class="tab-btn" data-tab="metering">计量</button>'+
    '<button class="tab-btn" data-tab="timeline">时间线</button>'+
    '</div>'+
    '<div class="tab-panel active" data-panel="overview">'+
      '<div class="kv">'+
      '<dt>描述</dt><dd>'+esc(d.description||'')+'</dd>'+
      '<dt>依赖</dt><dd>'+(Array.isArray(d.depends_on)&&d.depends_on.length?d.depends_on.map(esc).join(', '):'—')+'</dd>'+
      '<dt>文件</dt><dd>'+(Array.isArray(d.files_hint)&&d.files_hint.length?d.files_hint.map(esc).join(', '):'—')+'</dd>'+
      '<dt>技能</dt><dd>'+(Array.isArray(d.skills)&&d.skills.length?d.skills.map(esc).join(', '):'—')+'</dd>'+
      '<dt>Agent</dt><dd>'+esc(d.agent_type||'developer')+'（'+esc(r.agent_type_source||'default')+'）</dd>'+
      '<dt>状态</dt><dd>'+esc(r.status||'—')+'</dd>'+
      '<dt>耗时</dt><dd>'+fmtDur(r.duration_sec)+'</dd>'+
      '<dt>重试</dt><dd>'+(r.retry_count??'—')+'</dd>'+
      '<dt>验证</dt><dd>'+(r.verify_ok===true?'✅ 通过':r.verify_ok===false?'❌ 失败':'—')+'</dd>'+
      '<dt>退出码</dt><dd>'+(r.exit_code??'—')+'</dd>'+
      '<dt>沙箱</dt><dd>'+esc(r.sandbox_type||'—')+'</dd>'+
      '<dt>失败原因</dt><dd>'+esc(r.failure_reason||'—')+'</dd>'+
      '<dt>工作树</dt><dd>'+esc(r.worktree||'—')+'</dd>'+
      '<dt>摘要</dt><dd>'+esc(r.summary||'—')+'</dd>'+
      '</div>'+
      '<div class="kv"><dt>改动统计</dt><dd></dd></div>'+
      statsHtml+
    '</div>'+
    '<div class="tab-panel" data-panel="verify">'+
      renderVerify(r.verification_results||[])+
    '</div>'+
    '<div class="tab-panel" data-panel="log"><div class="loading">加载日志…</div></div>'+
    '<div class="tab-panel" data-panel="metering"><div class="loading">加载计量…</div></div>'+
    '<div class="tab-panel" data-panel="timeline"><div class="loading">加载时间线…</div></div>';
}

function renderVerify(vrs) {
  if (!vrs.length) return '<div class="kv"><dt>无验证结果</dt><dd></dd></div>';
  return '<table class="kv-table"><thead><tr><th>命令</th><th>类型</th><th>结果</th><th>耗时</th></tr></thead><tbody>'+
    vrs.map(v => '<tr><td>'+esc(v.command||v.desc||'')+'</td>'+
      '<td>'+esc(v.type||'shell')+'</td>'+
      '<td>'+(v.passed===true?'<span class="st-completed">✅ 通过</span>':v.passed===false?'<span class="st-failed">❌ 失败</span>':'—')+'</td>'+
      '<td>'+fmtDur(v.duration_sec)+'</td></tr>').join('')+
    '</tbody></table>';
}

function bindSubTabs(body, taskId, subId) {
  const panels = { overview:null, verify:null, log:null, metering:null, timeline:null };
  body.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      body.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      body.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const name = btn.dataset.tab;
      body.querySelector('.tab-panel[data-panel="'+name+'"]').classList.add('active');
      const t = name;
      if (t === 'log' && !panels.log) {
        panels.log = api('/api/tasks/'+encodeURIComponent(taskId)+'/'+encodeURIComponent(subId)+'/log')
          .then(d => { body.querySelector('[data-panel="log"]').innerHTML = renderLog(d.lines||[]); });
      } else if (t === 'metering' && !panels.metering) {
        panels.metering = api('/api/tasks/'+encodeURIComponent(taskId)+'/metering')
          .then(d => { body.querySelector('[data-panel="metering"]').innerHTML = renderMetering(d); });
      } else if (t === 'timeline' && !panels.timeline) {
        panels.timeline = api('/api/tasks/'+encodeURIComponent(taskId)+'/replay')
          .then(d => { body.querySelector('[data-panel="timeline"]').innerHTML = renderTimeline(d); });
      }
    });
  });
}

function renderLog(lines) {
  if (!lines.length) return '<div class="kv"><dt>无日志</dt><dd></dd></div>';
  return '<pre>'+lines.map(l => esc(l.text)).join('\\n')+'</pre>';
}

function renderMetering(d) {
  if (!d || !d.summary) return '<div class="kv"><dt>无计量数据</dt><dd></dd></div>';
  const cards = Object.entries(d.summary).map(([role, s]) =>
    '<div class="meter-card"><div class="label">'+esc(role)+'</div>'+
    '<div class="val">$'+s.cost_usd+'</div>'+
    '<div>'+s.count+' 次调用 · '+s.prompt_tokens+'→'+s.completion_tokens+' tokens</div>'+
    '<div>延迟 '+s.latency_ms+'ms</div></div>').join('');
  const rows = (d.rows||[]).map(r => {
    // 模型列：actual_model；若 routed_model 不同则标注路由别名→实际后端
    let modelCell = esc(r.actual_model||r.virtual_model||'');
    const rm = r.routed_model||'';
    if (rm && rm !== (r.actual_model||'')) {
      modelCell += '<br><span class="dim">'+esc(rm)+' →</span>';
    }
    return '<tr><td>'+esc(r.subtask_id||'')+'</td>'+
    '<td>'+esc(r.role)+'</td><td>'+modelCell+'</td>'+
    '<td>'+r.prompt_tokens+'</td><td>'+r.completion_tokens+'</td>'+
    '<td>$'+r.cost_usd+'</td><td>'+r.latency_ms+'ms</td>'+
    '<td>'+esc(r.result||'')+'</td></tr>';
  }).join('');
  return '<div class="meter-summary">'+cards+'</div>'+
    (rows? '<table class="kv-table"><thead><tr><th>子任务</th><th>角色</th><th>模型</th>'+
    '<th>prompt</th><th>completion</th><th>成本</th><th>延迟</th><th>结果</th></tr></thead><tbody>'+rows+'</tbody></table>':'');
}

function renderTimeline(d) {
  const rows = (d.timeline||[]).map(ev => {
    const st = ev.type || '';
    return '<tr><td>'+esc(ev.ts||'')+'</td><td>'+esc(st)+'</td><td>'+esc(ev.label||ev.detail||'')+'</td></tr>';
  }).join('');
  return rows? '<table class="kv-table"><thead><tr><th>时间</th><th>类型</th><th>详情</th></tr></thead><tbody>'+rows+'</tbody></table>'
    : '<div class="kv"><dt>无时间线</dt><dd></dd></div>';
}

// ── 看板（Kanban）──────────────────────────────────────
let kanbanData = null;
let kanbanRepoFilter = '';
let kanbanExpanded = null;      // 展开详情的卡片 id
let kanbanEditing = null;       // 正在编辑的卡片 id
let kanbanNewCardStage = null;  // 正在新建卡片的列
let kanbanShowArchived = false; // 归档视图开关（含已归档卡片，可取消归档）

async function loadKanban() {
  try {
    kanbanData = await api('/api/kanban' + (kanbanShowArchived ? '?archived=1' : ''));
    renderKanban();
    setConn(true);
  } catch (e) {
    setConn(false);
    document.getElementById('mainView').innerHTML =
      '<div class="err">加载失败: '+esc(e.message)+'</div>';
  }
}

function renderKanban() {
  const d = kanbanData || {stages: [], cards: {}, card_types: {}};
  const filter = kanbanRepoFilter.trim().toLowerCase();
  let html = '<div class="kanban-toolbar">'+
    '<input type="text" id="kanbanRepoFilter" placeholder="🔍 按 repo 筛选…" value="'+esc(kanbanRepoFilter)+'">'+
    '<button class="btn '+(kanbanShowArchived?'primary':'')+'" id="kanbanArchToggle" title="显示/隐藏已归档卡片">🗂 '+
    (kanbanShowArchived?'已归档（含）':'已归档')+'</button>'+
    '<span class="dim">共 '+(d.total||0)+' 张卡片'+(filter?'（已筛选）':'')+'</span></div>';
  html += '<div class="kanban-board">';
  d.stages.forEach((st, si) => {
    let cards = d.cards[st.key] || [];
    if (filter) cards = cards.filter(c => (c.repo||'').toLowerCase().includes(filter));
    html += '<div class="kanban-col" data-stage="'+st.key+'">'+
      '<div class="kanban-col-head"><span>'+esc(st.label)+'</span>'+
      '<span class="tag">'+cards.length+'</span>'+
      '<button class="kanban-new-btn" data-stage="'+st.key+'">＋ 新建</button></div>';
    if (kanbanNewCardStage === st.key) html += kanbanFormHtml('new', {});
    cards.forEach(c => { html += kanbanCardHtml(c, si, d.stages.length); });
    html += '</div>';
  });
  html += '</div>';
  document.getElementById('mainView').innerHTML = html;
  bindKanbanEvents();
}

function findKanbanCard(id) {
  const cards = (kanbanData||{}).cards || {};
  for (const key of Object.keys(cards)) {
    const hit = (cards[key]||[]).find(c => c.id === id);
    if (hit) return hit;
  }
  return null;
}

function kanbanCardHtml(c, stageIdx, stageCount) {
  const typeLabel = ((kanbanData||{}).card_types||{})[c.type] || c.type;
  const repoShort = c.repo ? c.repo.split('/').filter(Boolean).pop() : '';
  let html = '<div class="kanban-card'+(c.archived?' archived':'')+(kanbanExpanded===c.id?' open':'')+'"'+(c.archived?'':' draggable="true"')+' data-card="'+esc(c.id)+'">'+
    '<div class="kc-title">'+esc(c.title)+'</div>'+
    '<div class="kc-meta"><span class="tag">'+esc(typeLabel)+'</span>';
  if (c.archived) html += '<span class="tag" style="color:var(--yellow)">🗂 已归档</span>';
  if (repoShort) html += '<span class="tag" title="'+esc(c.repo)+'">📁 '+esc(repoShort)+'</span>';
  if (c.cron) html += '<span class="tag">⏰ '+esc(c.cron)+'</span>';
  if (c.latest_task) html += '<span class="badge '+(STATUS_COLORS[c.latest_task.status]||'st-pending')+'">'+esc(c.latest_task.status)+'</span>';
  html += '</div>';
  // 流转按钮（◀▶ 无拖拽 fallback）
  html += '<div class="kc-foot">';
  if (!c.archived && stageIdx > 0) html += '<button class="kanban-move-btn" data-card="'+esc(c.id)+'" data-dir="-1" title="移到上一阶段">◀</button>';
  if (!c.archived && stageIdx < stageCount-1) html += '<button class="kanban-move-btn" data-card="'+esc(c.id)+'" data-dir="1" title="移到下一阶段">▶</button>';
  html += '<span class="dim" style="margin-left:auto">'+esc((c.updated||'').replace('T',' ').slice(5,16))+'</span></div>';
  if (kanbanExpanded === c.id) html += kanbanDetailHtml(c);
  html += '</div>';
  return html;
}

function kanbanDetailHtml(c) {
  let html = '<div class="kanban-detail">';
  if (kanbanEditing === c.id) {
    html += kanbanFormHtml('edit', c);
  } else if (c.description) {
    // MVP 不做 markdown 渲染，pre 原文展示
    html += '<pre>'+esc(c.description)+'</pre>';
  } else {
    html += '<div class="dim">（无描述）</div>';
  }
  if ((c.task_ids||[]).length) {
    html += '<div class="kc-tasks">'+c.task_ids.map(tid =>
      '<span class="tag kanban-task-link" data-task="'+esc(tid)+'" title="点击查看任务列表">🔗 '+esc(tid)+'</span>').join(' ')+'</div>';
  }
  html += '<div class="op-bar">'+
    '<button class="btn kanban-op" data-op="edit" data-card="'+esc(c.id)+'">✏️ 编辑</button>';
  if (!c.archived && (c.type === 'implementation' || c.type === 'periodic')) {
    // 幂等防护前端侧：最新任务运行中 → 禁派发
    const lt = c.latest_task || {};
    const running = (lt.status === 'EXECUTING' || lt.status === 'PLANNING');
    html += '<button class="btn primary kanban-op" data-op="dispatch" data-card="'+esc(c.id)+'"'+
      (running ? ' disabled title="该卡片已有运行中任务（'+esc(lt.status)+'），不可重复派发"' : '') +
      '>🚀 派发执行</button>';
  }
  if (c.archived)
    html += '<button class="btn kanban-op" data-op="unarchive" data-card="'+esc(c.id)+'">♻️ 恢复（取消归档）</button>';
  else
    html += '<button class="btn kanban-op" data-op="archive" data-card="'+esc(c.id)+'">🗄️ 归档</button>';
  if (!(c.task_ids||[]).length)
    html += '<button class="btn kanban-op" data-op="delete" data-card="'+esc(c.id)+'">🗑️ 删除</button>';
  html += '<span class="op-msg" id="kanbanMsg-'+esc(c.id)+'"></span></div>';
  const hist = (c.history||[]).slice().reverse();
  if (hist.length) {
    html += '<div class="kanban-history">'+hist.map(h =>
      '<div class="kh-item">'+esc((h.ts||'').replace('T',' '))+' · '+esc(h.action)+
      (h.from?' '+esc(h.from)+' → '+esc(h.to||''):'')+
      (h.note?' · '+esc(h.note):'')+'</div>').join('')+'</div>';
  }
  html += '</div>';
  return html;
}

function kanbanFormHtml(mode, c) {
  const isNew = mode === 'new';
  const types = (kanbanData||{}).card_types || {};
  let html = '<div class="kanban-form" data-mode="'+mode+'" data-card="'+esc(c.id||'')+'">';
  html += '<input type="text" class="kf-title" placeholder="标题 *" value="'+esc(c.title||'')+'">';
  if (isNew) {
    html += '<select class="kf-type">'+
      Object.entries(types).map(([k, v]) =>
        '<option value="'+esc(k)+'">'+esc(v)+'</option>').join('')+'</select>';
  }
  html += '<input type="text" class="kf-repo" placeholder="repo 路径（实施/周期类必填）" value="'+esc(c.repo||'')+'">';
  html += '<textarea class="kf-desc" rows="3" placeholder="描述（markdown，讨论沉淀）">'+esc(c.description||'')+'</textarea>';
  if (isNew || c.type === 'periodic')
    html += '<input type="text" class="kf-cron" placeholder="cron 表达式（周期类展示用）" value="'+esc(c.cron||'')+'">';
  html += '<div style="display:flex;gap:8px;align-items:center">'+
    '<button class="btn primary kf-save">'+(isNew?'创建':'保存')+'</button>'+
    '<button class="btn kf-cancel">取消</button>'+
    '<span class="op-msg kf-msg"></span></div></div>';
  return html;
}

function bindKanbanEvents() {
  const main = document.getElementById('mainView');
  // repo 筛选（客户端过滤）
  const fi = document.getElementById('kanbanRepoFilter');
  if (fi) {
    fi.oninput = () => { kanbanRepoFilter = fi.value; renderKanban(); };
    if (kanbanRepoFilter) { fi.focus(); fi.setSelectionRange(fi.value.length, fi.value.length); }
  }
  // 归档视图开关
  const archBtn = document.getElementById('kanbanArchToggle');
  if (archBtn) {
    archBtn.onclick = () => {
      kanbanShowArchived = !kanbanShowArchived;
      kanbanExpanded = null;
      loadKanban();
    };
  }
  // 列头新建按钮（内联表单开关）
  main.querySelectorAll('.kanban-new-btn').forEach(b => {
    b.onclick = () => {
      kanbanNewCardStage = (kanbanNewCardStage === b.dataset.stage) ? null : b.dataset.stage;
      renderKanban();
    };
  });
  // 卡片：点击展开/收起详情 + 拖拽流转
  main.querySelectorAll('.kanban-card').forEach(el => {
    el.addEventListener('click', ev => {
      if (ev.target.closest('button') || ev.target.closest('.kanban-form')) return;
      const id = el.dataset.card;
      kanbanExpanded = (kanbanExpanded === id) ? null : id;
      kanbanEditing = null;
      renderKanban();
    });
    el.addEventListener('dragstart', ev => {
      ev.dataTransfer.setData('text/plain', el.dataset.card);
      ev.dataTransfer.effectAllowed = 'move';
    });
  });
  // 列：拖放目标
  main.querySelectorAll('.kanban-col').forEach(col => {
    col.addEventListener('dragover', ev => { ev.preventDefault(); col.classList.add('drag-over'); });
    col.addEventListener('dragleave', () => col.classList.remove('drag-over'));
    col.addEventListener('drop', async ev => {
      ev.preventDefault();
      col.classList.remove('drag-over');
      const cardId = ev.dataTransfer.getData('text/plain');
      const stage = col.dataset.stage;
      if (!cardId || !stage) return;
      try {
        await postJSON('/api/kanban/cards/'+encodeURIComponent(cardId)+'/move', {stage});
        loadKanban();
      } catch (e) { alert('流转失败: '+e.message); }
    });
  });
  // ◀▶ 流转按钮（无拖拽 fallback）
  main.querySelectorAll('.kanban-move-btn').forEach(b => {
    b.onclick = async ev => {
      ev.stopPropagation();
      const stages = (kanbanData||{}).stages || [];
      const card = findKanbanCard(b.dataset.card);
      if (!card) return;
      const idx = stages.findIndex(s => s.key === card.stage);
      const ni = idx + parseInt(b.dataset.dir, 10);
      if (ni < 0 || ni >= stages.length) return;
      try {
        await postJSON('/api/kanban/cards/'+encodeURIComponent(card.id)+'/move', {stage: stages[ni].key});
        loadKanban();
      } catch (e) { alert('流转失败: '+e.message); }
    };
  });
  // 新建/编辑表单
  main.querySelectorAll('.kanban-form').forEach(f => {
    const msg = f.querySelector('.kf-msg');
    f.querySelector('.kf-cancel').onclick = () => {
      if (f.dataset.mode === 'new') kanbanNewCardStage = null; else kanbanEditing = null;
      renderKanban();
    };
    f.querySelector('.kf-save').onclick = async () => {
      const title = f.querySelector('.kf-title').value.trim();
      const repo = f.querySelector('.kf-repo').value.trim();
      const description = f.querySelector('.kf-desc').value;
      const cronEl = f.querySelector('.kf-cron');
      const typeEl = f.querySelector('.kf-type');
      if (!title) { msg.textContent = '⚠️ 标题必填'; msg.style.color = 'var(--yellow)'; return; }
      const type = typeEl ? typeEl.value : '';
      // 前端预校验（后端仍会再校验一次）
      if (typeEl && (type === 'implementation' || type === 'periodic') && !repo) {
        msg.textContent = '⚠️ 实施/周期类卡片必须填 repo'; msg.style.color = 'var(--yellow)'; return;
      }
      try {
        if (f.dataset.mode === 'new') {
          await postJSON('/api/kanban/cards', {title, type, stage: kanbanNewCardStage,
            repo, description, cron: cronEl ? cronEl.value.trim() : ''});
          kanbanNewCardStage = null;
        } else {
          const body = {title, repo, description};
          if (cronEl) body.cron = cronEl.value.trim();
          await postJSON('/api/kanban/cards/'+encodeURIComponent(f.dataset.card)+'/update', body);
          kanbanEditing = null;
        }
        loadKanban();
      } catch (e) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
    };
  });
  // 详情操作按钮
  main.querySelectorAll('.kanban-op').forEach(b => {
    b.onclick = async ev => {
      ev.stopPropagation();
      const id = b.dataset.card;
      const op = b.dataset.op;
      const msg = document.getElementById('kanbanMsg-'+id);
      if (op === 'edit') { kanbanEditing = id; renderKanban(); return; }
      if (op === 'dispatch') {
        if (!confirm('派发卡片到 agent_go 执行？\\n任务文本 = 卡片标题 + 描述')) return;
        b.disabled = true;
        try {
          const d = await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/dispatch', {parallel: 1});
          if (msg) { msg.textContent = '✅ 已派发: '+d.task_id; msg.style.color = 'var(--green)'; }
          setTimeout(loadKanban, 800);
        } catch (e) {
          if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; }
          b.disabled = false;
        }
        return;
      }
      if (op === 'archive') {
        if (!confirm('归档该卡片？（归档后不在看板展示）')) return;
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/archive', {archived: true});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
      if (op === 'unarchive') {
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/archive', {archived: false});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
      if (op === 'delete') {
        if (!confirm('物理删除该卡片？仅未派发过任务的卡片可删除。')) return;
        try {
          await postJSON('/api/kanban/cards/'+encodeURIComponent(id)+'/delete', {});
          kanbanExpanded = null;
          loadKanban();
        } catch (e) { if (msg) { msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)'; } }
        return;
      }
    };
  });
  // 关联任务 → 跳任务列表
  main.querySelectorAll('.kanban-task-link').forEach(el => {
    el.onclick = ev => { ev.stopPropagation(); switchView('tasks'); };
  });
}

// ── 视图切换 + 新视图渲染（P0-2 / P1 / P2）─────────────────
let currentView = 'tasks';

function switchView(name) {
  currentView = name;
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  // 任务视图和归档视图都显示 filters（归档复用任务渲染）
  document.getElementById('filtersBar').style.display =
    (name === 'tasks' || name === 'archive') ? '' : 'none';
  const main = document.getElementById('mainView');
  main.innerHTML = '<div class="loading">加载中…</div>';
  // 返回 loader 的 Promise，调用方可链式等待渲染完成
  if (name === 'tasks') return loadTasks();
  if (name === 'kanban') return loadKanban();
  if (name === 'archive') return loadTasks('/api/archive');
  if (name === 'overview') return loadOverview();
  if (name === 'cost') return loadCost();
  if (name === 'models') return loadModels();
  if (name === 'config') return loadConfig();
  if (name === 'storage') return loadStorage();
  return Promise.resolve();
}

async function loadOverview() {
  const d = await api('/api/overview');
  const k = d.kpi || {};
  const dpr = d.dollar_per_pass_rate;
  // KPI 卡片
  let html = '<div class="kpi-grid">'+
    kpiCard('任务总数', k.total||0, 'blue')+
    kpiCard('进行中', k.in_progress||0, k.in_progress>0?'yellow':'')+
    kpiCard('已交付', k.delivered||0, 'green')+
    kpiCard('失败', k.failed||0, k.failed>0?'red':'')+
    kpiCard('今日交付', k.today_delivered||0, 'green')+
    kpiCard('今日成本', fmtCost(k.today_cost||0), '')+
    kpiCard('$/pass rate', dpr!=null?('$'+Number(dpr).toFixed(4)):'—',
            dpr!=null&&dpr>0.05?'red':'green')+
    '</div>';
  // 7 天成本趋势（SVG 柱状图）
  html += '<div class="section-title">📈 近 7 天成本趋势</div>';
  html += renderTrendChart(d.cost_trend_7d || []);
  document.getElementById('mainView').innerHTML = html;
}

function kpiCard(label, val, color) {
  const cls = color ? ' '+color : '';
  return '<div class="kpi-card"><div class="label">'+esc(label)+'</div>'+
    '<div class="val'+cls+'">'+esc(val)+'</div></div>';
}

function renderTrendChart(days) {
  if (!days.length) return '<div class="kv"><dt>无数据</dt><dd></dd></div>';
  const maxCost = Math.max(...days.map(d => d.cost), 0.01);
  const w = 60, h = 120, pad = 30;
  const totalW = days.length * (w + 10) + pad * 2;
  const bars = days.map((d, i) => {
    const barH = maxCost > 0 ? (d.cost / maxCost) * h : 0;
    const x = pad + i * (w + 10);
    const y = pad + h - barH;
    return '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+barH+'" fill="var(--blue)" rx="3">'+
      '<title>'+esc(d.date)+': $'+Number(d.cost).toFixed(4)+'</title></rect>'+
      '<text x="'+(x+w/2)+'" y="'+(pad+h+18)+'" text-anchor="middle" fill="var(--dim)" font-size="11">'+esc(d.date.slice(5))+'</text>'+
      '<text x="'+(x+w/2)+'" y="'+(y-5)+'" text-anchor="middle" fill="var(--text)" font-size="11">$'+Number(d.cost).toFixed(2)+'</text>';
  }).join('');
  return '<div class="trend-chart"><svg width="'+totalW+'" height="'+(pad*2+h+30)+'">'+
    bars+'</svg></div>';
}

async function loadCost() {
  const d = await api('/api/cost');
  let html = '<div class="section-title">💵 全局成本总览</div>';
  html += '<div class="kpi-grid">'+
    kpiCard('总成本', fmtCost(d.total_cost||0), 'blue')+
    '</div>';
  // by_model
  html += '<div class="section-title">按模型分解</div>';
  const modelRows = (d.by_model||[]).map(m => {
    // 模型名标注三态，区分「路由别名」「直连真模型」「未解析回退」
    let nameCell = esc(m.name);
    if (m.routed_model && m.routed_model !== m.name) {
      // 路由别名：实际后端 ≠ 路由名（如 deepseek-v4-flash 的路由名是 claude-haiku-4-5）
      nameCell += '<br><span class="dim">路由别名 '+esc(m.routed_model)+' →</span>';
    } else if (!m.routed_model && m.resolved !== false) {
      // 直连：无路由名，actual_model 是真实后端（如真调 Claude API，非别名）
      nameCell += '<br><span class="dim">直连（非路由别名）</span>';
    }
    if (m.resolved === false) {
      nameCell += ' <span title="未解析出真实后端模型，显示的是路由别名">⚠️</span>';
    }
    return '<tr><td>'+nameCell+'</td><td>'+fmtCost(m.cost)+'</td><td>'+m.pct+'%</td>'+
    '<td>'+m.calls+'</td><td>'+m.prompt_tokens+'</td><td>'+m.completion_tokens+'</td></tr>';
  }).join('');
  html += '<table><thead><tr><th>模型</th><th>成本</th><th>占比</th>'+
    '<th>调用数</th><th>prompt tokens</th><th>completion tokens</th></tr></thead><tbody>'+modelRows+'</tbody></table>';
  // by_role
  html += '<div class="section-title">按角色分解</div>';
  const roleRows = (d.by_role||[]).map(r =>
    '<tr><td>'+esc(r.name)+'</td><td>'+fmtCost(r.cost)+'</td><td>'+r.pct+'%</td><td>'+r.calls+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>角色</th><th>成本</th><th>占比</th><th>调用数</th></tr></thead><tbody>'+roleRows+'</tbody></table>';
  // Top N 任务
  html += '<div class="section-title">💸 成本最高的 20 个任务</div>';
  const topRows = (d.top_tasks||[]).map((t,i) =>
    '<tr class="task-row" data-id="'+esc(t.task_id)+'">'+
    '<td>'+(i+1)+'</td><td>'+esc(t.task_id)+'</td><td>'+fmtCost(t.cost)+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>#</th><th>任务</th><th>成本</th></tr></thead><tbody>'+topRows+'</tbody></table>';
  // R13 本地 TCO 面板（D1：显著标注估算）
  try {
    const tco = await api('/api/local-tco');
    if (tco.total_calls > 0) {
      const rows = (tco.by_model || []).map(r =>
        '<tr><td>'+esc(r.model)+'</td><td>'+r.calls+'</td>'+
        '<td>$'+r.unit_cost.toFixed(4)+'</td><td>$'+r.tco_usd.toFixed(4)+'</td>'+
        '<td>'+(r.configured ? '' : '<span style="color:var(--yellow)">未配置</span>')+'</td></tr>').join('');
      html += '<div class="section-title">🔌 本地模型 TCO（估算成本）</div>'+
        '<div class="warn-banner">⚠️ 以下为按 local_model_cost 单价 × 调用次数的<b>估算成本</b>，非真实账单。</div>'+
        '<div class="kpi-grid">'+
        kpiCard('本地调用总数', tco.total_calls, '')+
        kpiCard('估算总成本', '$'+tco.total_tco_usd.toFixed(4), 'yellow')+
        '</div>'+
        '<table><thead><tr><th>模型</th><th>调用数</th><th>单价/次</th><th>估算成本</th><th></th></tr></thead>'+
        '<tbody>'+rows+'</tbody></table>'+
        (tco.note ? '<div style="color:var(--dim);font-size:12px;margin-top:6px">'+esc(tco.note)+'</div>' : '');
    }
  } catch (e) {}
  document.getElementById('mainView').innerHTML = html;
  // 复用任务详情展开：跳转到任务视图并定位到该任务（等待加载完成再填搜索框）
  document.querySelectorAll('#mainView .task-row').forEach(row => {
    row.addEventListener('click', () => {
      switchView('tasks').then(() => {
        document.getElementById('searchInput').value = row.dataset.id;
        renderTasks();
      });
    });
  });
}

async function loadModels() {
  const d = await api('/api/models');
  let html = '<div class="section-title">🏭 生产环境模型成本（实际任务 metering）</div>';
  const prodRows = (d.production||[]).map(m => {
    let nameCell = esc(m.model);
    if (m.routed_model && m.routed_model !== m.model) {
      nameCell += '<br><span class="dim">路由别名 '+esc(m.routed_model)+' →</span>';
    } else if (!m.routed_model && m.resolved !== false) {
      nameCell += '<br><span class="dim">直连（非路由别名）</span>';
    }
    if (m.resolved === false) {
      nameCell += ' <span title="未解析出真实后端">⚠️</span>';
    }
    return '<tr><td>'+nameCell+'</td><td>'+fmtCost(m.cost)+'</td>'+
    '<td>'+m.calls+'</td><td>'+m.task_count+'</td>'+
    '<td>$'+Number(m.avg_cost_per_call||0).toFixed(6)+'</td></tr>';
  }).join('') || '<tr><td colspan="5">无数据</td></tr>';
  html += '<table><thead><tr><th>模型</th><th>总成本</th><th>调用数</th>'+
    '<th>任务数</th><th>avg $/call</th></tr></thead><tbody>'+prodRows+'</tbody></table>';
  // bench 对比
  html += '<div class="section-title">🧪 Bench 模型决策矩阵（实验数据）</div>';
  if (d.bench && d.bench.length) {
    const benchRows = d.bench.map(m =>
      '<tr><td>'+esc(m.model)+'</td><td>'+m.sample_size+'</td>'+
      '<td>'+((m.avg_pass_rate||0)*100).toFixed(1)+'%</td>'+
      '<td>'+fmtCost(m.avg_cost_usd||0)+'</td>'+
      '<td>$'+Number(m.dollar_per_pass||0).toFixed(4)+'</td>'+
      '<td>'+esc(m.recommendation||'—')+'</td></tr>'
    ).join('');
    html += '<table><thead><tr><th>模型</th><th>样本</th><th>通过率</th>'+
      '<th>avg 成本</th><th>$/pass</th><th>建议</th></tr></thead><tbody>'+benchRows+'</tbody></table>';
  } else {
    html += '<div class="kv"><dt>无 bench 数据</dt><dd>（运行 agent_go eval bench 生成）</dd></div>';
  }
  document.getElementById('mainView').innerHTML = html;
}

async function loadConfig() {
  const [d, prof, health] = await Promise.all([
    api('/api/config'), api('/api/profiles'), api('/api/health'),
  ]);
  let html = '<div class="section-title">🎛️ 配置中心</div>';
  // 模式徽标 + 切换按钮
  const modeLabel = {local: '🟢 纯本地模式', cloud: '☁️ 云端模式', custom: '🔧 自定义: '+esc(prof.current)};
  html += '<div style="display:flex;align-items:center;gap:14px;margin-bottom:12px">'+
    '<span class="mode-badge '+esc(prof.mode)+'">'+(modeLabel[prof.mode] || esc(prof.mode))+'</span>'+
    (prof.mode !== 'local' ? '<button class="btn primary" id="btnLocal">⚡ 一键本地</button>' : '')+
    (prof.mode !== 'cloud' ? '<button class="btn" id="btnCloud">☁️ 恢复云端</button>' : '')+
    '</div>';
  // 健康面板
  html += '<div class="health-grid">';
  for (const role of ['plan', 'worker', 'evaluator', 'local_proxy']) {
    const h = health[role] || {};
    let st, cls;
    if (h.skipped) { st = '⏭ '+(h.reason || '跳过'); cls = 'st-skip'; }
    else if (h.ok) { st = '✅ 可达'+(h.latency_ms != null ? ' · '+h.latency_ms+'ms' : ''); cls = 'st-ok'; }
    else { st = '❌ '+(h.error || '不可达'); cls = 'st-bad'; }
    html += '<div class="health-card"><div class="role">'+esc(role)+'</div>'+
      '<div class="'+cls+'">'+esc(st)+'</div>'+
      (h.model ? '<div>模型: '+esc(h.model)+'</div>' : '')+
      (h.url ? '<div class="url">'+esc(h.url)+'</div>' : '')+
      '</div>';
  }
  html += '</div>';
  if (health.mismatch) {
    html += '<div class="warn-banner">⚠️ '+esc(health.suggestion || '本地代理模型与 profile 不一致')+
            ' <button class="btn primary" id="btnLocalFix">重新生成 local profile</button></div>';
  }
  // R9 消费：代理路由策略可视（模型→后端路由偏好/云端模型/智能路由阈值）
  try {
    const pp = await api('/api/proxy-policies');
    if (pp.ok) {
      html += '<div class="section-title">🛣️ 代理路由策略（'+esc(pp.proxy_url)+'）</div>';
      html += '<div class="kpi-grid">'+
        kpiCard('智能路由', pp.route_enabled ? '✅ 启用' : '⏸ 关闭', pp.route_enabled ? 'green' : '')+
        kpiCard('云转阈值', pp.threshold_chars != null ? (pp.threshold_chars/1000)+'K chars' : '-', '')+
        kpiCard('云端模型', esc(pp.cloud_model || '-'), '')+
        kpiCard('云端 Key', pp.cloud_key_set ? '✅ 已配置' : '❌ 未配置', pp.cloud_key_set ? 'green' : 'red')+
        '</div>';
      // 模型路由偏好表
      const prefRows = Object.entries(pp.preferences || {}).map(([m, p]) => {
        const behavior = (p.behavior||'prefer') + (p.route_bias ? '·'+p.route_bias : '');
        return '<tr><td>'+esc(m)+'</td><td>'+esc(behavior)+'</td>'+
          '<td>'+esc(p.cloud_model || '-')+'</td>'+
          '<td>'+(p.threshold_factor ? '×'+p.threshold_factor : '-')+'</td></tr>';
      }).join('');
      if (prefRows) html += '<table><thead><tr><th>模型</th><th>偏好</th><th>云端模型</th><th>阈值系数</th></tr></thead><tbody>'+prefRows+'</tbody></table>';
      // 后端 providers
      const provs = Object.entries(pp.providers || {}).map(([k, v]) =>
        '<tr><td>'+esc(k)+'</td><td>'+esc(v.base_url || '')+'</td>'+
        '<td>'+(v.key_set ? '✅' : '❌')+'</td></tr>').join('');
      if (provs) html += '<table style="margin-top:8px"><thead><tr><th>Provider</th><th>Base URL</th><th>Key</th></tr></thead><tbody>'+provs+'</tbody></table>';
    } else {
      html += '<div class="section-title">🛣️ 代理路由策略</div><div style="color:var(--dim);font-size:12px">代理不可达或未提供 R9 接口（'+esc(pp.error||'')+'）</div>';
    }
  } catch (e) {}
  // profile 列表（非备份）
  const userProfiles = (prof.profiles || []).filter(p => !p.is_backup);
  if (userProfiles.length) {
    html += '<div class="section-title">📁 Profiles</div><table><thead><tr>'+
      '<th>名称</th><th>模式</th><th>状态</th><th>操作</th></tr></thead><tbody>'+
      userProfiles.map(p =>
        '<tr><td>'+esc(p.name)+'</td><td>'+esc(p.mode)+'</td>'+
        '<td>'+(p.active ? '<span style="color:var(--green)">● 生效中</span>' : '')+'</td>'+
        '<td>'+(!p.active ? '<button class="btn" data-activate="'+esc(p.name)+'">激活</button>' : '')+
        '<button class="btn" data-diff="'+esc(p.name)+'">对比</button></td></tr>'
      ).join('')+'</tbody></table><div id="diffView"></div>';
  }
  // R14 白名单字段编辑
  html += '<div class="section-title">✏️ 编辑配置（白名单字段，写入当前生效配置文件）</div>'+
    '<div class="run-form"><select id="editField" class="run-input">'+
    ['worker_models','worker_backends','local_models','local_model_cost','goal','evaluator',
     'plan_api.worker_base_url','planner_api.base_url'].map(f => '<option value="'+f+'">'+f+'</option>').join('')+
    '</select>'+
    '<input id="editValue" class="run-input" style="flex:3" placeholder="JSON 值，如 {&quot;easy&quot;:&quot;claude-haiku-4-5&quot;} 或 [&quot;m1&quot;]">'+
    '<button class="btn" id="btnEditSave">💾 保存</button>'+
    '<span id="editMsg" style="font-size:12px"></span></div>';
  // 只读配置展示（原有）
  html += '<div class="section-title">⚙️ 用户配置（生效值，api_key 已脱敏）</div>';
  html += '<div class="json-view">'+esc(JSON.stringify(d.config, null, 2))+'</div>';
  html += '<div class="kv" style="margin-top:10px"><dt>配置路径</dt><dd>'+esc(d.config_path)+'</dd></div>';
  if (d.role_skill_map) {
    html += '<div class="section-title">🎭 角色-Skill 映射</div>';
    html += '<div class="json-view">'+esc(JSON.stringify(d.role_skill_map, null, 2))+'</div>';
    html += '<div class="kv" style="margin-top:10px"><dt>路径</dt><dd>'+esc(d.role_skill_map_path)+'</dd></div>';
  }
  document.getElementById('mainView').innerHTML = html;
  // 绑定操作
  const bindOp = (id, fn, confirmMsg) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.onclick = async () => {
      if (confirmMsg && !confirm(confirmMsg)) return;
      el.disabled = true;
      try { await fn(); } catch (e) { alert('❌ 操作失败: '+e.message); }
      loadConfig();
    };
  };
  bindOp('btnLocal', () => postJSON('/api/profile/local', {}),
    '切换到纯本地模式？\\n将探测 localhost:4000 代理并生成 local profile（当前配置自动备份）。');
  bindOp('btnLocalFix', () => postJSON('/api/profile/local', {}),
    '重新生成 local profile？（当前配置自动备份）');
  bindOp('btnCloud', () => postJSON('/api/profile/cloud', {}),
    '恢复云端配置？（当前配置自动备份）');
  document.querySelectorAll('[data-activate]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.activate;
      if (!confirm('激活 profile「'+name+'」？（当前配置自动备份）')) return;
      btn.disabled = true;
      try { await postJSON('/api/profile/activate', {name}); }
      catch (e) { alert('❌ 操作失败: '+e.message); }
      loadConfig();
    };
  });
  // R15 diff 对比
  document.querySelectorAll('[data-diff]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.diff;
      const slot = document.getElementById('diffView');
      slot.innerHTML = '<div class="loading">对比中…</div>';
      try {
        const d = await api('/api/config/diff?name='+encodeURIComponent(name));
        if (!d.diff_count) {
          slot.innerHTML = '<div style="color:var(--green);margin:8px 0">✅ 当前配置与「'+esc(name)+'」无差异</div>';
          return;
        }
        const rows = d.diffs.map(x =>
          '<tr><td style="font-family:Menlo,monospace;font-size:12px">'+esc(x.field)+'</td>'+
          '<td class="json-view" style="max-height:120px">'+esc(JSON.stringify(x.current))+'</td>'+
          '<td class="json-view" style="max-height:120px">'+esc(JSON.stringify(x.target))+'</td></tr>').join('');
        slot.innerHTML = '<div class="section-title">当前生效 vs「'+esc(name)+'」（'+d.diff_count+' 处差异）</div>'+
          '<table><thead><tr><th>字段</th><th>当前</th><th>'+esc(name)+'</th></tr></thead><tbody>'+rows+'</tbody></table>';
      } catch (e) {
        slot.innerHTML = '<div class="err">'+esc(e.message)+'</div>';
      }
    };
  });
  // R14 编辑保存
  const btnSave = document.getElementById('btnEditSave');
  if (btnSave) btnSave.onclick = async () => {
    const field = document.getElementById('editField').value;
    const raw = document.getElementById('editValue').value.trim();
    const msg = document.getElementById('editMsg');
    let value;
    try { value = JSON.parse(raw); }
    catch (e) { msg.textContent = '⚠️ 值必须是合法 JSON'; msg.style.color = 'var(--yellow)'; return; }
    if (!confirm('保存字段「'+field+'」到当前生效配置？\\n新任务立即生效。')) return;
    btnSave.disabled = true;
    try {
      const d = await putJSON('/api/config', {field, value});
      msg.textContent = '✅ 已保存到 '+d.saved_to+'（'+d.effective+'）';
      msg.style.color = 'var(--green)';
      setTimeout(loadConfig, 1000);
    } catch (e) {
      msg.textContent = '❌ '+e.message; msg.style.color = 'var(--red)';
      btnSave.disabled = false;
    }
  };
}

async function putJSON(path, body) {
  const headers = {'Content-Type': 'application/json'};
  if (authToken) headers['Authorization'] = 'Bearer '+authToken;
  const r = await fetch(path, {method: 'PUT', headers, body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}

async function loadStorage() {
  const d = await api('/api/storage');
  let html = '<div class="section-title">💾 磁盘占用</div>';
  html += '<div class="kpi-grid">'+
    kpiCard('总占用', d.total_size_mb+' MB', 'blue')+
    kpiCard('任务目录', d.task_count, '')+
    kpiCard('孤儿目录', d.orphan_count, d.orphan_count>0?'yellow':'')+
    '</div>';
  if (d.orphan_count > 0) {
    html += '<div class="warn-banner">⚠️ 检测到 '+d.orphan_count+' 个孤儿目录（无 meta.json，可能是异常中断的残留）。'+
            '可用 <code>agent_go clean --orphans</code> 清理。</div>';
  }
  html += '<div class="section-title">📦 最大任务目录 Top 20</div>';
  const rows = (d.top_tasks||[]).map((t,i) =>
    '<tr><td>'+(i+1)+'</td><td>'+esc(t.name)+'</td>'+
    '<td>'+(t.size/1024/1024).toFixed(2)+' MB</td>'+
    '<td>'+(t.has_meta?'✓':'<span class="st-failed">✗ 孤儿</span>')+'</td></tr>'
  ).join('');
  html += '<table><thead><tr><th>#</th><th>任务</th><th>大小</th><th>meta</th></tr></thead><tbody>'+rows+'</tbody></table>';
  // U6：写操作审计（R16 消费端闭环）
  try {
    const audit = await api('/api/audit');
    if (audit.records.length) {
      const aRows = audit.records.map(r =>
        '<tr><td style="white-space:nowrap">'+esc((r.ts||'').replace('T',' ').slice(0,19))+'</td>'+
        '<td>'+esc(r.op||'')+'</td>'+
        '<td>'+(r.ok ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--red)">✗</span>')+'</td>'+
        '<td style="font-size:11px;color:var(--dim);max-width:340px;word-break:break-all">'+
          esc(JSON.stringify(r.params||{}).slice(0,120))+'</td>'+
        '<td style="font-size:11px">'+esc(r.auth||'-')+'</td></tr>').join('');
      html += '<div class="section-title">📜 操作审计（最近 '+audit.records.length+' / 共 '+audit.total+' 条）</div>'+
        '<table><thead><tr><th>时间</th><th>操作</th><th>结果</th><th>参数摘要</th><th>操作者</th></tr></thead>'+
        '<tbody>'+aRows+'</tbody></table>';
    }
  } catch (e) {}
  document.getElementById('mainView').innerHTML = html;
}

function setConn(ok) {
  const b = document.getElementById('connBadge');
  b.textContent = ok ? '● 已连接' : '○ 连接断开';
  b.style.color = ok ? 'var(--green)' : 'var(--red)';
}

function connectSSE() {
  if (sse) sse.close();
  const qs = '?interval=5' + (authToken ? '&token='+encodeURIComponent(authToken) : '');
  sse = new EventSource('/api/events'+qs);
  sse.addEventListener('message', e => {
    try {
      const m = JSON.parse(e.data);
      if (m.type === 'refresh') {
        // 任务/看板视图自动刷新（其他视图按需手动刷新，避免覆盖用户正在看的页面）
        if (currentView === 'tasks') loadTasks();
        if (currentView === 'kanban') loadKanban();
      }
    } catch(_) {}
  });
  // EventSource 自带自动重连；仅在彻底关闭（CLOSED）时才手动重建，避免连接叠加
  sse.onerror = () => {
    if (sse.readyState === EventSource.CLOSED) setTimeout(connectSSE, 5000);
  };
}

document.getElementById('refreshBtn').onclick = () => switchView(currentView);
document.getElementById('searchInput').addEventListener('input', renderTasks);
document.querySelectorAll('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
});
loadTasks();
connectSSE();
</script>
</body>
</html>
"""


def main(args: Any = None) -> None:  # CLI 入口
    """agent_go web [--host H] [--port P] [--token T] [--viewer-token VT]"""
    import argparse

    # 兼容两种调用：CLI 分发传入已解析 Namespace（args 无 .split 方法），
    # 直接命令行调用传入 argv 列表。
    if isinstance(args, argparse.Namespace):
        serve_web(host=args.host, port=args.port, token=args.token,
                  viewer_token=getattr(args, "viewer_token", None))
        return

    parser = argparse.ArgumentParser(prog="agent_go web",
                                     description="只读 Web 观察平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--token", default=None,
                        help="可选 admin Bearer token 鉴权（全部操作，默认关闭）")
    parser.add_argument("--viewer-token", default=None,
                        help="可选 viewer Bearer token（只读 GET；写操作 403）")
    ns = parser.parse_args(args)
    serve_web(host=ns.host, port=ns.port, token=ns.token, viewer_token=ns.viewer_token)
