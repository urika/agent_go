"""Web 操作台传输层：HTTP 路由、鉴权（admin/viewer 多角色）、SSE 实时推送。

拆分自 web_server.py（ISSUE-55）。WebHandler 组合 web_ops.WebOpsMixin
（写处置，含 web_kanban.WebKanbanMixin 看板写端点）与
BaseHTTPRequestHandler：自身保留鉴权守卫、响应工具、GET 观测路由与
SSE 事件流；数据组装在 web_data.py，前端模板在 web_frontend.py。
"""
from __future__ import annotations

import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from . import kanban
from .web_data import (
    _extract_subtask_log,
    _list_task_dirs,
    _task_dir,
    _task_status_of,
    api_assessment,
    api_audit,
    api_baseline,
    api_bench_batches,
    api_bench_results,
    api_config,
    api_config_diff,
    api_cost,
    api_cross_judge,
    api_decisions,
    api_deviation,
    api_health,
    api_insight_report,
    api_insights,
    api_local_tco,
    api_merge_preview,
    api_metering,
    api_models,
    api_notes,
    api_overview,
    api_plan,
    api_profiles,
    api_proxy_policies,
    api_replay,
    api_storage,
    api_subtask_detail,
    api_task,
    api_task_report,
    api_task_review,
    api_tasks,
    api_worktrees,
)
from .web_frontend import _SPA_HTML
from .web_kanban import api_kanban
from .web_ops import WebOpsMixin

logger = logging.getLogger(__name__)


class WebHandler(WebOpsMixin, BaseHTTPRequestHandler):
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
                # 惰性状态回流：打开看板即修正卡片状态（覆盖 CLI resume/孤儿进程/web 重启
                # 等所有完成路径，不依赖 task_runner on_exit 托管句柄）
                try:
                    moved = kanban.reconcile_cards(_task_status_of)
                    if moved:
                        logger.info("[kanban] 惰性回流修正 %d 张卡片状态", len(moved))
                except Exception as _re:
                    logger.warning("[kanban] 惰性回流失败（不影响读取）: %s", _re)
                self._reply_json(200, api_kanban(include_archived=include_archived))
                return
            if len(parts) == 3 and parts[1] == "kanban" and parts[2] == "classification-stats":
                self._reply_json(200, kanban.classification_stats())
                return
            if len(parts) == 3 and parts[1] == "kanban" and parts[2] == "cost-quality":
                self._reply_json(200, kanban.cost_quality_analysis())
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
            # ── M6.3 洞察与决策 ──
            if len(parts) == 2 and parts[1] == "decisions":
                self._reply_json(200, api_decisions())
                return
            if len(parts) == 2 and parts[1] == "insights":
                self._reply_json(200, api_insights())
                return
            if len(parts) == 3 and parts[1] == "insights":
                data = api_insight_report(parts[2])
                if data is None:
                    self._reply_json(404, {"error": "report not found"})
                else:
                    self._reply_json(200, data)
                return
            if len(parts) == 2 and parts[1] == "bench-batches":
                self._reply_json(200, api_bench_batches())
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
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "notes":
                data = api_notes(parts[2])
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
