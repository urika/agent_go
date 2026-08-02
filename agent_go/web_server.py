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
  GET /api/events                     SSE：任务状态变化实时推送
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .config import AGENT_GO_DIR
from .console import _LazyConsole

logger = logging.getLogger(__name__)
console = _LazyConsole()

MAX_LOG_LINE = 2000  # execution.log 单行截断长度（防大响应）

# 任务目录前缀：只有匹配 task-* 的目录才纳入观察（与 cmd_list 一致）
_TASK_PREFIX = "task-"


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return records


def _list_task_dirs() -> list[Path]:
    if not AGENT_GO_DIR.exists():
        return []
    return sorted(
        d for d in AGENT_GO_DIR.iterdir()
        if d.is_dir() and d.name.startswith(_TASK_PREFIX) and (d / "meta.json").exists()
    )


def _task_meta(task_dir: Path) -> Optional[dict]:
    return _read_json(task_dir / "meta.json")


def api_tasks() -> list[dict]:
    """任务清单（轻量，不含 subtasks 明细）。"""
    out = []
    for td in _list_task_dirs():
        meta = _task_meta(td)
        if not meta:
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
        out.append({
            "id": td.name,
            "task": meta.get("task", ""),
            "status": meta.get("status", "unknown"),
            "repo": meta.get("repo", ""),
            "subtask_count": len(meta.get("subtasks", []) or []),
            "completed": sum(1 for r in results if r.get("status") == "completed"),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
            "blocked": sum(1 for r in results if r.get("status") == "blocked"),
            "cost_usd": round(cost, 4),
            "total_elapsed_sec": round(total_elapsed, 1),
            "total_retries": retries,
            "created": meta.get("created_at", ""),
            "mtime": td.joinpath("meta.json").stat().st_mtime,
        })
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def api_task(task_id: str) -> Optional[dict]:
    td = AGENT_GO_DIR / task_id
    if not td.is_dir() or not td.name.startswith(_TASK_PREFIX):
        return None
    meta = _task_meta(td)
    if not meta:
        return None
    subtasks = meta.get("subtasks", []) or []
    results = meta.get("results", []) or []
    items = []
    for i, st in enumerate(subtasks):
        r = results[i] if i < len(results) else {}
        items.append({
            "id": st.get("id", f"sub-{i+1}"),
            "title": st.get("title", ""),
            "difficulty": st.get("difficulty", ""),
            "depends_on": st.get("depends_on", []) or [],
            "skills": st.get("skills", []) or [],
            "agent_type": st.get("agent_type", ""),
            "verification": st.get("verification", []) or [],
            "status": (r or {}).get("status", "pending"),
            "duration_sec": (r or {}).get("duration_sec"),
            "retry_count": (r or {}).get("retry_count"),
            "verify_ok": (r or {}).get("verify_ok"),
            "exit_code": (r or {}).get("exit_code"),
            "summary": (r or {}).get("summary", ""),
            "failure_reason": (r or {}).get("failure_reason", ""),
            "worktree": (r or {}).get("worktree", ""),
            "agent_type_source": (r or {}).get("agent_type_source",
                                               st.get("_agent_type_source", "")),
        })
    return {
        "id": td.name,
        "task": meta.get("task", ""),
        "status": meta.get("status", "unknown"),
        "repo": meta.get("repo", ""),
        "created_at": meta.get("created_at", ""),
        "subtasks": items,
        "meta": {
            k: meta.get(k) for k in ("planner_model", "source_batch")
            if meta.get(k)
        },
    }


def api_subtask_detail(task_id: str, sub_id: str) -> Optional[dict]:
    td = AGENT_GO_DIR / task_id
    if not td.is_dir():
        return None
    meta = _task_meta(td)
    if not meta:
        return None
    subtasks = meta.get("subtasks", []) or []
    results = meta.get("results", []) or []
    idx = next((i for i, st in enumerate(subtasks)
                if st.get("id") == sub_id), None)
    if idx is None:
        return None
    st = subtasks[idx]
    r = results[idx] if idx < len(results) else {}
    return {
        "id": sub_id,
        "title": st.get("title", ""),
        "description": st.get("description", ""),
        "difficulty": st.get("difficulty", ""),
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
    返回 [{line_no, level, message}]，每行截断 MAX_LOG_LINE 字符。
    """
    td = AGENT_GO_DIR / task_id
    log_path = td / "execution.log"
    if not log_path.exists():
        return []
    lines_out = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return []
    # 找子任务相关行：行内含 sub_id（作为 sub-N 出现的部分）
    start_idx = None
    for i, ln in enumerate(all_lines):
        if sub_id in ln:
            start_idx = i
            break
    if start_idx is None:
        return []
    # 段落上边界：从 start_idx 往前回退到上一个含 "[subtask]" 或子任务启动标记的行
    begin = start_idx
    for i in range(start_idx - 1, max(-1, start_idx - 200), -1):
        if "[subtask]" in all_lines[i] or "start subtask" in all_lines[i].lower():
            begin = i
            break
    # 下边界：往后到下一个子任务启动标记或文件尾
    end = len(all_lines)
    for i in range(start_idx + 1, min(len(all_lines), start_idx + 3000)):
        if "[subtask]" in all_lines[i] and sub_id not in all_lines[i]:
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
    td = AGENT_GO_DIR / task_id
    if not td.is_dir():
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
    td = AGENT_GO_DIR / task_id
    if not td.is_dir():
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
    td = AGENT_GO_DIR / task_id
    if not td.is_dir():
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


class WebHandler(BaseHTTPRequestHandler):
    """只读观察 API handler。"""

    protocol_version = "HTTP/1.1"
    server_version = "agent_go-web/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静默 access log

    # ── 鉴权 ─────────────────────────────────────────────────

    def _auth_ok(self) -> bool:
        token = getattr(self.server, "token", None)  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-Api-Key", "")
        return auth == f"Bearer {token}" or api_key == token

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

    def _auth_guard(self) -> bool:
        if not self._auth_ok():
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
            if not self._auth_guard():
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
        interval = max(2, min(30, int(params.get("interval", "5"))))
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
</style>
</head>
<body>
<header>
  <h1>🌐 agent_go 观察平台</h1>
  <span class="badge" id="connBadge">连接中…</span>
  <div class="status" id="headerStatus"></div>
</header>
<div class="container">
  <div class="filters">
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

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error('HTTP '+r.status);
  return r.json();
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
  document.getElementById('searchInput').oninput = renderTasks;
}

// ── 任务详情展开 ────────────────────────────────────────────
async function toggleTask(id, row) {
  const existing = document.querySelector('.task-detail[data-id="'+id+'"]');
  if (existing) {
    existing.classList.remove('open');
    setTimeout(() => existing.remove(), 200);
    return;
  }
  let detail = document.querySelector('.task-detail[data-id="'+id+'"]');
  if (!detail) {
    const tr = document.createElement('tr');
    tr.className = 'task-detail';
    tr.dataset.id = id;
    const td = document.createElement('td');
    td.colSpan = 7;
    td.innerHTML = '<div class="detail-box"><div class="loading">加载任务详情…</div></div>';
    tr.appendChild(td);
    row.after(tr);
    tr.classList.add('open');
    try {
      const data = await api('/api/tasks/'+encodeURIComponent(id));
      td.innerHTML = renderTaskDetail(data);
      bindDetailEvents(id, tr);
    } catch (e) {
      td.innerHTML = '<div class="detail-box"><div class="err">'+esc(e.message)+'</div></div>';
    }
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
      '<dt>依赖</dt><dd>'+(d.depends_on.length?d.depends_on.join(', '):'—')+'</dd>'+
      '<dt>文件</dt><dd>'+(d.files_hint.length?d.files_hint.join(', '):'—')+'</dd>'+
      '<dt>技能</dt><dd>'+(d.skills.length?d.skills.join(', '):'—')+'</dd>'+
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

function setConn(ok) {
  const b = document.getElementById('connBadge');
  b.textContent = ok ? '● 已连接' : '○ 连接断开';
  b.style.color = ok ? 'var(--green)' : 'var(--red)';
}

function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource('/api/events?interval=5');
  sse.addEventListener('message', e => {
    try { const m = JSON.parse(e.data); if (m.type === 'refresh') loadTasks(); } catch(_) {}
  });
  sse.onerror = () => { setTimeout(connectSSE, 5000); };
}

document.getElementById('refreshBtn').onclick = loadTasks;
document.getElementById('searchInput').addEventListener('input', renderTasks);
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
