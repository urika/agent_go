"""Web 观察平台数据层：17+ 观测 GET API 的纯数据组装函数。

拆分自 web_server.py（ISSUE-55）。本模块只读 meta.json / metering.jsonl /
execution.log / replay 时间线等任务数据，不触碰 HTTP 传输与鉴权；
HTTP 路由在 web_handler.py，写处置端点在 web_ops.py / web_kanban.py。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .status import task_status, normalize_task_status
from .metrics import _load_attributions
from .profiles import ProfileError, health_check, list_profiles
from .task_runner import task_runner
# 复用 eval 的 JSONL/JSON 读取（与 bench/cross_judge 同源，避免实现漂移）
from .eval import _read_jsonl, _read_json

logger = logging.getLogger(__name__)


def _root() -> Any:
    """返回组合层 agent_go.web_server 模块对象（延迟导入，避免循环依赖）。

    测试与调用方的 monkeypatch 统一打在 web_server 命名空间（AGENT_GO_DIR /
    CONFIG_PATH / load_config / probe_local_models / _run_cli /
    _bench_results_path / _resolve_workspace_file）。数据层对这些可 patch 的
    叶子符号必须经组合层做运行时解析，保证拆分后补丁语义与拆分前完全一致。
    """
    from . import web_server
    return web_server


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
    td = _root().AGENT_GO_DIR / task_id
    return td if td.is_dir() else None


def _list_task_dirs() -> list[Path]:
    if not _root().AGENT_GO_DIR.exists():
        return []
    return sorted(
        d for d in _root().AGENT_GO_DIR.iterdir()
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
        # 盲区人工归因注记（P1.5 四按钮状态回显）
        "blind_spot_attributions": _load_attributions(td),        # M4 goal 回溯：合规度正交维度（纯透传，与 status 正交）
        "goal_adherence": meta.get("goal_adherence") or {},
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
    bench_path = _root()._bench_results_path()
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
    path = _root()._resolve_workspace_file("cross_judge_scores.jsonl")
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
    path = _root()._bench_results_path()
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
    bench_path = _root()._resolve_workspace_file("eval_suite/baseline.jsonl")
    bench_records = _read_jsonl(bench_path)

    # 2. cost 门禁基线
    cost_baseline_path = _root().CONFIG_PATH.parent / "cost_baseline.json"
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
        with (_root().AGENT_GO_DIR / "web_audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("audit write failed: %s", e)


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
    for mj in _root().AGENT_GO_DIR.glob("task-*/metering.jsonl"):
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
    current = _root().load_config()
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
    # M6.2：配置修改决策落 log
    try:
        from .decision_log import record_decision
        record_decision(
            change=f"config 字段修改: {field}",
            confirmer="web",
            source="config put",
        )
    except Exception:
        pass
    return {"field": field, "saved_to": str(target), "effective": "新任务生效"}


def api_task_report(task_id: str, fmt: str) -> Optional[dict]:
    """任务报告（M5.2.1）：复用 CLI `agent_go report --output -`（单一实现，行为一致）。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    result = _root()._run_cli(["report", task_id, "--format", fmt, "--output", "-"], timeout=60)
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
    path = _root().AGENT_GO_DIR / "web_audit.jsonl"
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


def api_decisions(limit: int = 100) -> dict:
    """决策历史（M6.3）：decision_log.jsonl 倒序读取。"""
    from .decision_log import list_decisions, decision_count
    return {"records": list_decisions(limit), "total": decision_count()}


_INSIGHT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _insights_dir() -> Path:
    d = _root().AGENT_GO_DIR / "insights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def api_insights() -> dict:
    """洞察报告列表（M6.3）：~/.agent_go/insights/*.md。"""
    d = _insights_dir()
    items = []
    for p in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        items.append({
            "name": p.stem,
            "mtime": mtime,
            "size": p.stat().st_size,
        })
    return {"reports": items}


def api_insight_report(name: str) -> Optional[dict]:
    """读取单个洞察报告内容。"""
    if not _INSIGHT_NAME_RE.match(name):
        return None
    p = _insights_dir() / f"{name}.md"
    if not p.exists() or not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return None
    return {"name": name, "content": content}


def api_bench_batches() -> dict:
    """可选的 insight 生成目标批次（eval_suite/baselines 下含 manifest.json 的目录）。

    返回对象列表（前端下拉展示）：name/records/created_at。
    """
    base = Path.cwd() / "eval_suite" / "baselines"
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for p in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_dir() and (p / "manifest.json").exists():
                try:
                    manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    manifest = {}
                items.append({
                    "name": p.name,
                    "records": manifest.get("record_count", 0),
                    "created_at": manifest.get("created_at", ""),
                })
    return {"batches": items, "count": len(items)}



def api_notes(task_id: str) -> Optional[dict]:
    """任务备注列表（M5.2 协作：多用户沟通）。追加式 notes.jsonl。"""
    td = _task_dir(task_id)
    if td is None:
        return None
    path = td / "notes.jsonl"
    notes = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        notes.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
    return {"task_id": task_id, "notes": notes}


def add_note(task_id: str, author: str, text: str) -> dict:
    """追加任务备注。author 取 web token 角色标识（admin/viewer），无 token 为 local。"""
    td = _task_dir(task_id)
    if td is None:
        raise ProfileError("task not found")
    text = (text or "").strip()
    if not text:
        raise ProfileError("备注内容不能为空")
    if len(text) > 2000:
        raise ProfileError("备注过长（≤2000 字符）")
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "author": author,
        "text": text,
    }
    try:
        with (td / "notes.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        raise ProfileError(f"备注写入失败: {e}") from e
    return rec


def api_config() -> dict:
    """只读展示用户配置（config.json + role_skill_map.json + 熔断器状态）。"""
    config = _root().load_config()
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
    rsmap_path = _root().CONFIG_PATH.parent / "role_skill_map.json"
    role_skill_map = _read_json(rsmap_path) if rsmap_path.exists() else None

    return {
        "config": config,
        "config_path": str(_root().CONFIG_PATH),
        "role_skill_map": role_skill_map,
        "role_skill_map_path": str(rsmap_path),
    }


def api_storage() -> dict:
    """磁盘占用分析：task 目录大小排行 + 孤儿目录（无 meta.json）检测。

    数据源：遍历 AGENT_GO_DIR。用于运维（748+ 任务累积监控）。
    """
    if not _root().AGENT_GO_DIR.exists():
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
    for d in _root().AGENT_GO_DIR.iterdir():
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
    total_mb = round(total / 1024 / 1024, 2)
    # M5.3 磁盘告警：超过阈值提示清理（规模化资源管理）
    alert = ""
    if total_mb > 5000:
        alert = (f"磁盘占用 {total_mb:.0f}MB 超过 5GB，建议 "
                 "`agent_go clean --older-than 7` 清理历史任务")
    elif orphans:
        alert = f"检测到 {len(orphans)} 个孤儿目录（无 meta.json），建议 `agent_go clean --orphans` 清理"
    return {
        "total_size": total,
        "total_size_mb": total_mb,
        "task_count": len(tasks),
        "orphan_count": len(orphans),
        "top_tasks": tasks[:20],  # 最大的 20 个
        "orphans": orphans,
        "alert": alert,
    }
