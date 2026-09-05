"""Web 写处置端点（ops）：任务生命周期/审批/合并/清理/配置写入的 HTTP 方法。

拆分自 web_server.py（ISSUE-55）。WebOpsMixin 承载 do_POST / do_PUT /
do_DELETE 及全部 _op_* 实现（run/resume/cancel/clean-old/review/merge/pr/
confirm/notes/blind-spot/insight/config put），路由内派发的看板写端点由
基类 web_kanban.WebKanbanMixin 提供；最终由 web_handler.WebHandler 组合。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import unquote, urlparse

from .profiles import (
    ProfileError,
    activate_cloud,
    activate_local,
    activate_profile,
    read_current_profile,
)
from .task_runner import TaskRunnerError, task_runner
from . import kanban
from .kanban import KanbanError
from .web_data import (
    _RUNNING_STATES,
    _audit,
    _root,
    _task_dir,
    _task_status_of,
    add_note,
    api_insights,
    api_merge_preview,
    put_config_field,
)
from .web_kanban import WebKanbanMixin

logger = logging.getLogger(__name__)


class WebOpsMixin(WebKanbanMixin):
    """写处置端点 mixin（由 web_handler.WebHandler 组合进 HTTP handler）。

    方法体与原 web_server.WebHandler 实现逐行一致；_reply_json/_auth_guard/
    _auth_role 及 BaseHTTPRequestHandler 请求属性由组合层提供
    （TYPE_CHECKING 声明供 mypy 静态检查）。
    """

    if TYPE_CHECKING:
        path: str
        server: Any
        headers: Any
        rfile: Any

        def _reply_json(self, code: int, payload: Any,
                        extra_headers: Optional[dict] = None) -> None: ...
        def _auth_guard(self, query: str = "", required: str = "read") -> bool: ...
        def _auth_role(self, query: str = "") -> str: ...

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
            # POST /api/kanban/import-spec（从 Task Spec 需求文档生成看板卡片）
            if len(parts) == 3 and parts[1] == "kanban" and parts[2] == "decompose":
                self._op_kanban_decompose(body, token)
                return
            if len(parts) == 3 and parts[1] == "kanban" and parts[2] == "import-spec":
                self._op_kanban_import_spec(body, token)
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
                if action == "review":
                    self._op_kanban_review(card_id, body, token)
                    return
                if action == "suggest-degrade":
                    self._op_kanban_suggest_degrade(card_id, token)
                    return
            # POST /api/tasks/<id>/notes {text}（M5.2 协作备注）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "notes":
                self._op_add_note(parts[2], body, token)
                return
            # POST /api/tasks/<id>/blind-spot-attribution {item, attribution, note}
            # （P1.5 盲区归因四按钮写端点）
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "blind-spot-attribution":
                self._op_blind_spot_attrib(parts[2], body, token)
                return
            # POST /api/insight/generate {batch, goal?, plan?}（M6.3 生成洞察报告）
            if len(parts) == 3 and parts[1] == "insight" and parts[2] == "generate":
                self._op_insight_generate(body, token)
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

    def _op_insight_generate(self, body: dict, token: str) -> None:
        """M6.3：生成洞察报告（复用 CLI eval insight，报告自动归档 insights/ 供读取）。"""
        batch = str(body.get("batch") or "").strip()
        if not batch:
            self._reply_json(400, {"error": "batch 不能为空"})
            return
        if not re.match(r"^[A-Za-z0-9._/-]+$", batch):
            self._reply_json(400, {"error": "batch 含非法字符"})
            return
        goal = str(body.get("goal") or "").strip()[:500]
        plan = str(body.get("plan") or "").strip()[:500]
        argv = ["eval", "insight", "--results", batch, "--output", "-"]
        if goal:
            argv += ["--analysis-goal", goal]
        if plan:
            argv += ["--analysis-plan", plan]
        result = _root()._run_cli(argv, timeout=600)
        if result["ok"]:
            # CLI 已把报告归档到 ~/.agent_go/insights/；找最新一份
            reports = api_insights().get("reports", [])
            latest = reports[0]["name"] if reports else ""
            result["report_name"] = latest
            _audit("insight.generate", {"batch": batch, "goal": goal[:80]}, result, True, token)
            self._reply_json(200, result)
        else:
            _audit("insight.generate", {"batch": batch}, result["stderr"][-300:], False, token)
            self._reply_json(422, result)

    def _op_blind_spot_attrib(self, task_id: str, body: dict, token: str) -> None:
        """P1.5 盲区归因注记写端点（Web 四按钮）。

        body: {item: "sig:key" | "", attribution: confirmed/false-hit/false-clear/missed,
               note?: str}——项级注记覆盖自动判定，任务级 missed 记漏报。
        """
        from .metrics import write_attribution
        td = _task_dir(task_id)
        if td is None:
            self._reply_json(404, {"error": "task not found"})
            return
        item = str(body.get("item") or "").strip()
        attribution = str(body.get("attribution") or "").strip()
        note = str(body.get("note") or "").strip()
        ok, msg = write_attribution(td, item, attribution, note)
        _audit("tasks.blind_spot_attribution",
               {"task_id": task_id, "item": item, "attribution": attribution},
               msg, ok, token)
        if not ok:
            self._reply_json(422, {"error": msg})
            return
        self._reply_json(200, {"ok": True, "message": msg})

    def _op_add_note(self, task_id: str, body: dict, token: str) -> None:
        if _task_dir(task_id) is None:
            self._reply_json(404, {"error": "task not found"})
            return
        text = str(body.get("text") or "").strip()
        author = "local"
        if token:
            role = self._auth_role()
            author = f"{role}:{token[:6]}"
        rec = add_note(task_id, author, text)
        _audit("tasks.note", {"task_id": task_id, "author": author}, rec, True, token)
        self._reply_json(200, {"ok": True, "note": rec})

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
            _cfg = _root().load_config()
            # A-1：优先 worker_base_url（统一入口），worker_backends 为 deprecated 兼容
            local_url = (_cfg.get("plan_api") or {}).get("worker_base_url") or ""
            if not (isinstance(local_url, str) and ("localhost" in local_url or "127.0.0.1" in local_url)):
                backends = (_cfg.get("worker_backends") or {})
                local_url = next((v for v in backends.values()
                                  if isinstance(v, str) and ("localhost" in v or "127.0.0.1" in v)),
                                 DEFAULT_LOCAL_URL)
            try:
                _root().probe_local_models(local_url)
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
        victims = [t for t in _root().AGENT_GO_DIR.glob("task-*")
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
        cli_result = _root()._run_cli(["review", "--task", task_id, "--yes"], timeout=300)
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
        result = _root()._run_cli(argv, timeout=120)
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
        result = _root()._run_cli(argv, timeout=180)
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
        result = _root()._run_cli(argv, timeout=300)
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
        # W3.1：design 列卡片计划确认后自动流转 implementation（Y 决策时）
        if decision == "Y":
            try:
                card = kanban.find_card_by_task(task_id)
                if card and card.get("stage") == "design":
                    kanban.move_card(card["id"], "implementation", note=f"计划确认通过（task {task_id}）")
                    logger.info("[kanban] 卡片 %s 计划确认通过 → implementation", card["id"])
            except Exception as _ke:
                logger.warning("[kanban] 确认后流转失败: %s", _ke)
        result = {"task_id": task_id, "stage": stage, "decision": decision, "status": "accepted"}
        _audit("tasks.confirm", {"task_id": task_id, "stage": stage, "decision": decision},
               result, True, token)
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
