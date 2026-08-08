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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .status import task_status
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .config import AGENT_GO_DIR, CONFIG_PATH, load_config
from .console import _LazyConsole
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


def api_tasks() -> list[dict]:
    """任务清单（轻量，不含 subtasks 明细）。"""
    out = []
    for td in _list_task_dirs():
        meta = _task_meta(td)
        if not meta:  # eval._read_json 不存在/坏文件返回 {}
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
            "status": task_status(meta),
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
        "meta": {
            k: meta.get(k) for k in ("planner_model", "source_batch")
            if meta.get(k)
        },
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
    task_counts = {"total": 0, "running": 0, "completed": 0, "failed": 0,
                   "blocked": 0, "today_completed": 0, "today_cost": 0.0}
    cost_by_day: dict[str, float] = {}  # YYYY-MM-DD -> cost
    dollar_per_pass_rate = None
    completed_with_cost = 0
    total_cost_for_pass = 0.0

    for td in _list_task_dirs():
        meta = _task_meta(td)
        if not meta:
            continue
        task_counts["total"] += 1
        status = task_status(meta)
        if status in task_counts:
            task_counts[status] += 1
        created = meta.get("created", "")
        if created.startswith(today) and status == "completed":
            task_counts["today_completed"] += 1

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
            cost = rec.get("cost_usd", 0) or 0
            model = rec.get("actual_model") or rec.get("virtual_model") or "unknown"
            role = rec.get("role", "unknown")
            m = by_model.setdefault(model, {"cost": 0.0, "calls": 0,
                                            "prompt_tokens": 0, "completion_tokens": 0})
            m["cost"] += cost
            m["calls"] += 1
            m["prompt_tokens"] += rec.get("prompt_tokens", 0) or 0
            m["completion_tokens"] += rec.get("completion_tokens", 0) or 0
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
            model = rec.get("actual_model") or rec.get("virtual_model") or "unknown"
            # 只统计 worker 角色的（代表实际执行模型）
            if rec.get("role") != "worker":
                continue
            m = prod.setdefault(model, {"cost": 0.0, "calls": 0, "tasks": set()})
            m["cost"] += rec.get("cost_usd", 0) or 0
            m["calls"] += 1
            m["tasks"].add(td.name)
    prod_rows = [{
        "model": k,
        "cost": round(v["cost"], 4),
        "calls": v["calls"],
        "task_count": len(v["tasks"]),
        "avg_cost_per_call": round(v["cost"] / v["calls"], 6) if v["calls"] else 0,
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
        entry = {"name": d.name, "size": size, "has_meta": has_meta}
        if has_meta:
            tasks.append(entry)
        else:
            orphans.append(entry)

    tasks.sort(key=lambda x: x["size"], reverse=True)
    return {
        "total_size": total,
        "total_size_mb": round(total / 1024 / 1024, 2),
        "task_count": len(tasks),
        "orphan_count": len(orphans),
        "top_tasks": tasks[:20],  # 最大的 20 个
        "orphans": orphans,
    }


class WebHandler(BaseHTTPRequestHandler):
    """只读观察 API handler。"""

    protocol_version = "HTTP/1.1"
    server_version = "agent_go-web/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静默 access log

    # ── 鉴权 ─────────────────────────────────────────────────

    def _auth_ok(self, query: str = "") -> bool:
        token = getattr(self.server, "token", None)  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-Api-Key", "")
        if auth == f"Bearer {token}" or api_key == token:
            return True
        # EventSource 无法自定义请求头，允许 ?token= query 传递（仅 SSE 等场景）
        for pair in query.split("&"):
            if pair.startswith("token=") and unquote(pair[6:]) == token:
                return True
        return False

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

    def _auth_guard(self, query: str = "") -> bool:
        if not self._auth_ok(query):
            self._reply_json(401, {"error": "unauthorized"})
            return False
        return True

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
            if not self._auth_guard(parsed.query):
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
                data = _extract_subtask_log(parts[2], parts[3])
                self._reply_json(200, {"lines": data})
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
            if len(parts) == 2 and parts[1] == "events":
                self._stream_events(query)
                return
        except Exception as exc:  # pragma: no cover - 防御性
            logger.exception("api error on %s", path)
            self._reply_json(500, {"error": str(exc)})
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
        return "|".join(sigs)


def serve_web(host: str = "127.0.0.1", port: int = 8091,
              token: Optional[str] = None) -> None:
    """启动只读观察 Web 服务（阻塞）。"""
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    httpd.token = token or ""  # type: ignore[attr-defined]
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
</style>
</head>
<body>
<header>
  <h1>🌐 agent_go 观察平台</h1>
  <nav class="nav-tabs">
    <button class="nav-tab active" data-view="tasks">📋 任务</button>
    <button class="nav-tab" data-view="overview">📊 总览</button>
    <button class="nav-tab" data-view="cost">💰 成本</button>
    <button class="nav-tab" data-view="models">🤖 模型</button>
    <button class="nav-tab" data-view="config">⚙️ 配置</button>
    <button class="nav-tab" data-view="storage">💾 运维</button>
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
  completed:'st-completed', failed:'st-failed', aborted:'st-aborted',
  blocked:'st-blocked', cancelled:'st-cancelled', running:'st-running',
  pending:'st-pending'
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

function statusIcon(st) {
  return {completed:'🟢', failed:'🔴', aborted:'🟡', blocked:'⛔',
          running:'🔄', pending:'⚪', cancelled:'⏹️'}[st] || '⚪';
}

// ── 任务清单 ────────────────────────────────────────────────
async function loadTasks() {
  try {
    const data = await api('/api/tasks');
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

function renderStatusFilters() {
  const counts = {};
  tasks.forEach(t => counts[t.status] = (counts[t.status]||0)+1);
  const order = ['running','pending','completed','failed','aborted','blocked','cancelled'];
  const html = ['<span class="filter-btn'+(statusFilter==='all'?' active':'')+'" data-s="all">全部</span>'];
  order.forEach(s => {
    if (counts[s]) html.push(
      '<span class="filter-btn'+(statusFilter===s?' active':'')+'" data-s="'+s+'">'+
      esc(s)+' ('+counts[s]+')</span>');
  });
  document.getElementById('statusFilters').innerHTML = html.join('');
  document.querySelectorAll('#statusFilters .filter-btn').forEach(b => {
    b.onclick = () => { statusFilter = b.dataset.s; renderStatusFilters(); renderTasks(); };
  });
  const n = tasks.length;
  document.getElementById('headerStatus').textContent =
    '共 '+n+' 个任务';
}

function filteredTasks() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  return tasks.filter(t => {
    if (statusFilter !== 'all' && t.status !== statusFilter) return false;
    if (!q) return true;
    return (t.id+' '+t.task+' '+(t.repo||'')).toLowerCase().includes(q);
  });
}

function renderTasks() {
  const list = filteredTasks();
  if (!list.length) {
    document.getElementById('mainView').innerHTML =
      '<div class="loading">暂无匹配任务</div>';
    return;
  }
  const rows = list.map(t => {
    const statusCls = STATUS_COLORS[t.status] || 'st-pending';
    return '<tr class="task-row" data-id="'+esc(t.id)+'">'+
      '<td><span class="'+statusCls+'">'+statusIcon(t.status)+' '+esc(t.status)+'</span></td>'+
      '<td>'+esc(t.id)+'</td>'+
      '<td>'+esc(t.task)+'</td>'+
      '<td>'+t.subtask_count+'</td>'+
      '<td>'+t.completed+'/'+t.failed+(t.blocked?'/⛔'+t.blocked:'')+'</td>'+
      '<td>'+fmtCost(t.cost_usd)+'</td>'+
      '<td>'+fmtDur(t.total_elapsed_sec)+'</td>'+
      '</tr>';
  }).join('');
  document.getElementById('mainView').innerHTML =
    '<table><thead><tr><th>状态</th><th>任务 ID</th><th>描述</th>'+
    '<th>子任务</th><th>完成/失败</th><th>成本</th><th>耗时</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table>';
  document.querySelectorAll('.task-row').forEach(row => {
    row.addEventListener('click', () => toggleTask(row.dataset.id, row));
  });
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
    td.innerHTML = renderTaskDetail(data);
    bindDetailEvents(id, tr);
  } catch (e) {
    td.innerHTML = '<div class="detail-box"><div class="err">'+esc(e.message)+'</div></div>';
  }
}

function renderTaskDetail(d) {
  const items = (d.subtasks||[]).map((s,i) => {
    const statusCls = STATUS_COLORS[s.status] || 'st-pending';
    const src = s.agent_type_source || 'default';
    return '<div class="sub-item">'+
      '<div class="sub-head" data-sub="'+esc(s.id)+'">'+
        '<span class="icon '+statusCls+'">'+statusIcon(s.status)+'</span>'+
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
  return '<div class="kv">'+
    '<dt>任务</dt><dd>'+esc(d.task)+'</dd>'+
    '<dt>仓库</dt><dd>'+esc(d.repo)+'</dd>'+
    '<dt>状态</dt><dd><span class="'+((STATUS_COLORS[d.status])||'')+'">'+statusIcon(d.status)+' '+esc(d.status)+'</span></dd>'+
    '<dt>创建时间</dt><dd>'+esc(d.created_at||'')+'</dd>'+
    '</div><div style="margin-top:12px">'+items+'</div>';
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
  const rows = (d.rows||[]).map(r => '<tr><td>'+esc(r.subtask_id||'')+'</td>'+
    '<td>'+esc(r.role)+'</td><td>'+esc(r.actual_model||r.virtual_model||'')+'</td>'+
    '<td>'+r.prompt_tokens+'</td><td>'+r.completion_tokens+'</td>'+
    '<td>$'+r.cost_usd+'</td><td>'+r.latency_ms+'ms</td>'+
    '<td>'+esc(r.result||'')+'</td></tr>').join('');
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

// ── 视图切换 + 新视图渲染（P0-2 / P1 / P2）─────────────────
let currentView = 'tasks';

function switchView(name) {
  currentView = name;
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === name);
  });
  // 只有任务视图显示 filters
  document.getElementById('filtersBar').style.display = (name === 'tasks') ? '' : 'none';
  const main = document.getElementById('mainView');
  main.innerHTML = '<div class="loading">加载中…</div>';
  // 返回 loader 的 Promise，调用方可链式等待渲染完成
  if (name === 'tasks') return loadTasks();
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
    kpiCard('运行中', k.running||0, k.running>0?'yellow':'')+
    kpiCard('已完成', k.completed||0, 'green')+
    kpiCard('失败', k.failed||0, k.failed>0?'red':'')+
    kpiCard('今日完成', k.today_completed||0, 'green')+
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
  const modelRows = (d.by_model||[]).map(m =>
    '<tr><td>'+esc(m.name)+'</td><td>'+fmtCost(m.cost)+'</td><td>'+m.pct+'%</td>'+
    '<td>'+m.calls+'</td><td>'+m.prompt_tokens+'</td><td>'+m.completion_tokens+'</td></tr>'
  ).join('');
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
  const prodRows = (d.production||[]).map(m =>
    '<tr><td>'+esc(m.model)+'</td><td>'+fmtCost(m.cost)+'</td>'+
    '<td>'+m.calls+'</td><td>'+m.task_count+'</td>'+
    '<td>$'+Number(m.avg_cost_per_call||0).toFixed(6)+'</td></tr>'
  ).join('') || '<tr><td colspan="5">无数据</td></tr>';
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
  const d = await api('/api/config');
  let html = '<div class="section-title">⚙️ 用户配置（config.json，api_key 已脱敏）</div>';
  html += '<div class="json-view">'+esc(JSON.stringify(d.config, null, 2))+'</div>';
  html += '<div class="kv" style="margin-top:10px"><dt>配置路径</dt><dd>'+esc(d.config_path)+'</dd></div>';
  if (d.role_skill_map) {
    html += '<div class="section-title">🎭 角色-Skill 映射</div>';
    html += '<div class="json-view">'+esc(JSON.stringify(d.role_skill_map, null, 2))+'</div>';
    html += '<div class="kv" style="margin-top:10px"><dt>路径</dt><dd>'+esc(d.role_skill_map_path)+'</dd></div>';
  }
  document.getElementById('mainView').innerHTML = html;
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
        // 仅任务视图自动刷新（其他视图按需手动刷新，避免覆盖用户正在看的页面）
        if (currentView === 'tasks') loadTasks();
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
    """agent_go web [--host H] [--port P] [--token T]"""
    import argparse

    # 兼容两种调用：CLI 分发传入已解析 Namespace（args 无 .split 方法），
    # 直接命令行调用传入 argv 列表。
    if isinstance(args, argparse.Namespace):
        serve_web(host=args.host, port=args.port, token=args.token)
        return

    parser = argparse.ArgumentParser(prog="agent_go web",
                                     description="只读 Web 观察平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--token", default=None,
                        help="可选 Bearer token 鉴权（默认关闭）")
    ns = parser.parse_args(args)
    serve_web(host=ns.host, port=ns.port, token=ns.token)
