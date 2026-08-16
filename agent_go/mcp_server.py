"""agent_go MCP Server — JSON-RPC 2.0 over stdio transport.

Exposes run_task / resume_task / inspect_task / review_task as MCP tools.
Thin shell: spawns agent_go subprocesses with --json, parses output, forwards events.
Pure stdlib — no MCP SDK or external dependencies required.

Usage:
    python3 -m agent_go.mcp_server

Environment:
    AGENT_GO_MCP_ALLOWED_REPOS   Glob pattern for allowed repo paths (default: cwd)
    AGENT_GO_MCP_MAX_CONCURRENT  Max concurrent task subprocesses (default: 3)
    AGENT_GO_API_KEY             API key passed through to agent_go subprocesses
"""

import sys
import json
import os
import subprocess
import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional
from .status import task_status, set_task_status

logger = logging.getLogger("agent_go.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"


class MCPError(Exception):
    """MCP tool error with agent-recoverable guidance.

    Attributes:
        code: 机器可读错误码（如 AGENT_GO_TASK_NOT_FOUND）
        message: 人类可读错误描述
        retryable: 是否可安全重试
        fix: 可选的可执行修复指引 {"description", "tool"/"resource", "params"}
        context: 可选的补充上下文（如失败子任务列表）
    """

    def __init__(self, code: str, message: str, retryable: bool = False,
                 fix: Optional[dict] = None, context: Optional[dict] = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fix = fix
        self.context = context

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.fix:
            d["fix"] = self.fix
        if self.context:
            d["context"] = self.context
        return d


# 预定义错误类型模板 — Agent 收到错误后可依据 fix 字段自主恢复
ERROR_TEMPLATES = {
    "AGENT_GO_TASK_NOT_FOUND": {
        "message": "任务不存在",
        "fix": {
            "description": "获取有效任务列表，确认 task_id",
            "tool": "list_tasks",
            "params": {"status": "all", "limit": 20},
        },
    },
    "AGENT_GO_REPO_INVALID": {
        "message": "仓库不在 allowlist",
        "fix": {
            "description": "检查仓库路径是否在 AGENT_GO_MCP_ALLOWED_REPOS 环境变量配置的 allowlist 内",
            "check_env": "AGENT_GO_MCP_ALLOWED_REPOS",
        },
    },
    "AGENT_GO_CAPACITY": {
        "message": "并发任务已达上限",
        "retryable": True,
        "fix": {
            "description": "等待当前任务完成，或调大 AGENT_GO_MCP_MAX_CONCURRENT 后重试",
            "suggested_wait_sec": 30,
        },
    },
    "AGENT_GO_TASK_RUNNING": {
        "message": "任务正在运行",
        "fix": {
            "description": "任务已在运行，使用 inspect_task 轮询进度，或 cancel_task 取消",
            "tool": "inspect_task",
            "params": {"task_id": "{task_id}"},
        },
    },
    "AGENT_GO_TIMEOUT": {
        "message": "操作超时",
        "retryable": True,
        "fix": {
            "description": "任务仍在后台运行，稍后使用 inspect_task 轮询或 resume_task 续跑",
            "tool": "inspect_task",
            "params": {"task_id": "{task_id}"},
        },
    },
    "AGENT_GO_CLI_ERROR": {
        "message": "agent_go CLI 执行失败",
        "retryable": True,
        "fix": {
            "description": "使用 inspect_task 查看任务状态和日志，诊断后重试",
            "tool": "inspect_task",
            "params": {"task_id": "{task_id}", "include_log_tail": True},
        },
    },
}

def _error_template(code: str, task_id: str = "") -> dict:
    """按模板生成带 task_id 填充的 fix 指引。"""
    tpl = ERROR_TEMPLATES.get(code, {})
    fix = tpl.get("fix")
    if fix and "{task_id}" in str(fix):
        fix = json.loads(json.dumps(fix).replace("{task_id}", task_id or "unknown"))
    return {"message": tpl.get("message", ""), "retryable": tpl.get("retryable", False), "fix": fix}


# ── Tool schemas (MCP tool annotations + JSON Schema inputSchema) ──

TOOLS = [
    {
        "name": "run_task",
        "description": "对指定仓库执行结构化工程任务：LLM 生成 Plan → 拆解子任务 → git worktree 隔离并发执行 → 验证重试 → 报告。默认异步返回 task_id；wait=true 时阻塞至完成并流式推送进度。",
        "annotations": {"title": "Run structured engineering task", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["repo", "task"],
            "properties": {
                "repo": {"type": "string", "description": "目标仓库绝对路径。必须在 server 配置的 repo allowlist 内"},
                "task": {"type": "string", "description": "自然语言任务描述"},
                "docs": {"type": "array", "items": {"type": "string"}},
                "skills": {"type": "array", "items": {"type": "string"}},
                "agent_type": {"type": "string", "enum": ["developer", "architect", "reviewer", "tester"]},
                "parallel": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "remote": {"type": "string"},
                "preserve_worktrees": {"type": "boolean"},
                "wait": {"type": "boolean", "default": False},
                "timeout_sec": {"type": "integer", "default": 3600, "minimum": 60, "maximum": 21600},
            }
        }
    },
    {
        "name": "resume_task",
        "description": "恢复 paused/interrupted 状态的任务，从断点续跑剩余子任务。语义与 wait 同 run_task。",
        "annotations": {"title": "Resume paused task", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "parallel": {"type": "integer", "minimum": 1, "maximum": 8},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "wait": {"type": "boolean", "default": False},
                "timeout_sec": {"type": "integer", "default": 3600},
            }
        }
    },
    {
        "name": "inspect_task",
        "description": "查询任务执行状态（进度/各子任务/成本）与保留 worktree 现场。只读，运行中可任意轮询。",
        "annotations": {"title": "Inspect task status", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "include_log_tail": {"type": "boolean", "default": False},
                "log_lines": {"type": "integer", "default": 30, "minimum": 1, "maximum": 200},
            }
        }
    },
    {
        "name": "review_task",
        "description": "对已完成任务审查：analyze 返回 per-file diff 摘要；approve/reject/changes_requested 记录决策。",
        "annotations": {"title": "Review task results", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "action"],
            "properties": {
                "task_id": {"type": "string"},
                "action": {"type": "string", "enum": ["analyze", "approve", "reject", "changes_requested"]},
                "deep": {"type": "boolean", "default": False},
                "comment": {"type": "string"},
            }
        }
    },
    {
        "name": "governance_task",
        "description": "查询任务治理报告（M1.4 SDD 闭环）：traceability_matrix（requirement→subtask→verification→delivery）、architecture_compliance、追踪完整性评估。只读。",
        "annotations": {"title": "Task governance report", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
            }
        }
    },
    {
        "name": "list_tasks",
        "description": "列出任务概要（ID/状态/进度/成本/描述）。按状态过滤 + 分页。只读。",
        "annotations": {"title": "List tasks", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["running", "completed", "failed", "all"],
                           "default": "all", "description": "按状态过滤"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            }
        }
    },
    {
        "name": "cancel_task",
        "description": "取消正在运行的任务：终止子进程，将 meta.json 标记为 cancelled（保留已完成结果与 metering）。不可逆。confirm=true 时先通过 sampling 向 Host 请求确认",
        "annotations": {"title": "Cancel running task", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "confirm": {"type": "boolean", "default": False,
                            "description": "取消前通过 sampling/createMessage 请求 Host 确认（stdio transport 可用）"},
            }
        }
    },
]

AGENT_GO_DIR = Path.home() / ".agent_go"


# ── Resources (只读上下文，按需加载，避免 tool 返回全量数据) ──

RESOURCES = [
    {
        "uri": "agent_go://tasks/list",
        "name": "Task List",
        "description": "所有任务列表（ID、状态、描述、时间）。比 list_tasks tool 更精简",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/summary",
        "name": "Task Summary",
        "description": "任务概要：状态、进度、耗时、成本。比 inspect_task tool 更精简",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/plan",
        "name": "Latest Plan",
        "description": "最新版本的执行计划（plans/ 目录中版本号最大的快照）",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/metering",
        "name": "Metering Data",
        "description": "Token 用量与成本明细（按 role 聚合）",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/log/recent",
        "name": "Recent Log",
        "description": "最近 50 行执行日志，用于错误诊断",
        "mimeType": "text/plain",
    },
    {
        "uri": "agent_go://tasks/{task_id}/review",
        "name": "Review Status",
        "description": "审查决策状态（approved/rejected/changes_requested）与历史",
        "mimeType": "application/json",
    },
]


# ── Prompts (标准操作规程模板，Agent 无需自己编写流程) ──

PROMPTS = [
    {
        "name": "diagnose_failure",
        "description": "系统诊断任务失败的 prompt 模板：引导 Agent 获取日志 → 分析原因 → 决定修复策略",
        "arguments": [
            {"name": "task_id", "description": "失败任务 ID", "required": True},
        ],
    },
    {
        "name": "review_and_decide",
        "description": "审查任务结果并做出批准/拒绝/修改决策的 prompt 模板",
        "arguments": [
            {"name": "task_id", "description": "任务 ID", "required": True},
        ],
    },
    {
        "name": "resume_or_restart",
        "description": "决定 resume 还是重新 run 的决策 prompt 模板",
        "arguments": [
            {"name": "task_id", "description": "任务 ID", "required": True},
        ],
    },
]


# ── Activity tracker (per-subtask 活动追踪，带时间戳，线程安全) ──

class ActivityTracker:
    """追踪每个 subtask 的最新活动，支持并行任务互不覆盖。

    - update(): 记录 (sub_id → activity, timestamp)
    - get_current(): 最近更新的活动（用于 progress notification）
    - get_all(): 全部 subtask 的活动快照（用于 inspect_task）
    """

    def __init__(self):
        self._activities: dict[str, dict[str, tuple[str, float, int]]] = {}  # task_id -> {sub_id: (activity, ts, seq)}
        self._lock = threading.Lock()
        self._seq = 0  # 单调递增序号，保证同一时间戳下后更新的胜出

    def update(self, task_id: str, sub_id: str, activity: str) -> None:
        with self._lock:
            self._seq += 1
            self._activities.setdefault(task_id, {})[sub_id] = (activity, time.time(), self._seq)

    def update_current(self, task_id: str, activity: str) -> None:
        """记录非 subtask 级活动（如 pipeline_start/complete）。"""
        self.update(task_id, "__pipeline__", activity)

    def get_all(self, task_id: str) -> dict[str, str]:
        with self._lock:
            store = self._activities.get(task_id, {})
            return {sid: act for sid, (act, _ts, _seq) in store.items()}

    def get_current(self, task_id: str) -> str:
        """最近更新的活动（跨 subtask，用于 progress 的 current_activity）。"""
        with self._lock:
            store = self._activities.get(task_id, {})
            if not store:
                return ""
            latest = max(store.items(), key=lambda kv: kv[1][2])  # 按 seq 取最新
            return latest[1][0]

    def snapshot(self, task_id: str) -> dict:
        """与 _activity_store 兼容的快照格式。"""
        return {"current_activity": self.get_current(task_id),
                "activity_per_subtask": self.get_all(task_id)}


# ── Core server ──────────────────────────────────────────────────

class MCPServer:
    def __init__(self, notification_sink: Optional[Any] = None):
        """MCP JSON-RPC 核心。

        Args:
            notification_sink: 可选回调（msg: dict -> None）。设置后 _notify 推送
                到该回调（HTTP/SSE transport 用），未设置时写 stdout（stdio transport）。
        """
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._max_concurrent = int(os.environ.get("AGENT_GO_MCP_MAX_CONCURRENT", "3"))
        self._allowed_repos = self._parse_allowed_repos()
        self._deferred: dict[Any, threading.Event] = {}  # msg_id -> completion event
        self._deferred_result: dict[Any, Any] = {}        # msg_id -> result
        self._activity_store: dict[str, dict] = {}  # task_id -> {current_activity, activity_per_subtask}
        self._tracker = ActivityTracker()  # 并行活动追踪（时间戳 + 线程安全）
        self._notification_sink = notification_sink
        self._sampling_seq = 0  # R-5: sampling 请求 id 递增计数

    def _parse_allowed_repos(self) -> list[str]:
        raw = os.environ.get("AGENT_GO_MCP_ALLOWED_REPOS", "")
        if not raw:
            return [os.getcwd() + "/*"]
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _check_repo_allowed(self, repo_path: str) -> bool:
        resolved = str(Path(repo_path).resolve())
        for pat in self._allowed_repos:
            base = pat.rstrip("*")
            if resolved == base.rstrip("/") or resolved.startswith(base):
                return True
        return False

    # ── JSON-RPC helpers ───────────────────────────────────────

    def _send(self, msg: dict) -> None:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _result_payload(self, msg_id: Any, result: Any) -> dict:
        return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}

    def _error_payload(self, msg_id: Any, code: int, message: str, data: Any = None) -> dict:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": err}

    def _result(self, msg_id: Any, result: Any) -> None:
        self._send(self._result_payload(msg_id, result))

    def _error(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        self._send(self._error_payload(msg_id, code, message, data))

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            msg["params"] = params
        if self._notification_sink is not None:
            # HTTP/SSE transport：推送到所有已连接的 SSE 客户端
            try:
                self._notification_sink(msg)
            except Exception:
                logger.debug("notification sink 推送失败", exc_info=True)
        else:
            self._send(msg)

    # ── Subprocess management ──────────────────────────────────

    def _argv(self, *extra: str) -> list[str]:
        # --json 是顶层 parser 参数，必须放在子命令之前（argparse 不接受子命令后的顶层 flag）
        return [sys.executable, "-m", "agent_go", "--json"] + list(extra) + ["--yes"]

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env)
        def _drain_stderr():
            for _ in proc.stderr:
                pass
        threading.Thread(target=_drain_stderr, daemon=True).start()
        return proc

    def _read_agentgo_start(self, proc: subprocess.Popen, timeout: float = 30) -> str:
        """Read first JSON Lines event containing task_id."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                tid = ev.get("task_id") or (ev.get("data") or {}).get("task_id")
                if tid:
                    return tid
            except json.JSONDecodeError:
                continue
        # Fallback: find latest task dir by mtime
        dirs = sorted(AGENT_GO_DIR.glob("task-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        return dirs[0].name if dirs else "unknown"

    def _start_activity_monitor(self, proc: subprocess.Popen, task_id: str) -> None:
        """后台解析子进程 stdout 事件流，更新 activity tracker。

        供 wait=false（异步）路径使用：即使无人 wait，inspect_task 仍能拿到
        每个 subtask 的实时活动（P1-2 并行活动追踪）。
        """
        def _monitor() -> None:
            for raw in iter(proc.stdout.readline, ""):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("level") != "event":
                    continue
                data = ev.get("data", {})
                payload = data.get("data", data)
                event = ev.get("event", "")
                if event == "subtask_activity":
                    sub_id = payload.get("sub_id", "")
                    activity = payload.get("activity", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, activity)
                elif event == "subtask_start":
                    sub_id = payload.get("sub_id", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, f"Executing {sub_id}: {payload.get('title', '')}")
                elif event == "subtask_complete":
                    sub_id = payload.get("sub_id", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, f"Completed {sub_id}: {payload.get('status', '')}")
                elif event == "pipeline_complete":
                    self._tracker.update_current(task_id, f"Pipeline {payload.get('status', 'completed')}")
                with self._lock:
                    self._activity_store[task_id] = self._tracker.snapshot(task_id)

        threading.Thread(target=_monitor, daemon=True).start()

    def _wait_with_events(self, proc: subprocess.Popen, task_id: str,
                          timeout: float, token: Any = None) -> dict:
        """Read --json event stream from agent_go subprocess for real-time progress.

        Background thread parses JSON Lines events from stdout and forwards
        lifecycle events (subtask_start/complete, pipeline_complete) as MCP
        progress notifications. Main thread polls meta.json as authoritative
        result source. Degraded fallback: pure polling if no lifecycle events
        detected within 2 seconds.
        """
        task_dir = AGENT_GO_DIR / task_id
        meta_path = task_dir / "meta.json"

        from threading import Lock as _Lock
        state: dict = {
            "has_lifecycle": False,
            "total": 0,
            "completed": 0,
            "current_activity": "",
            "activity_per_subtask": {},
        }
        state_lock = _Lock()

        def _reader() -> None:
            for raw in iter(proc.stdout.readline, ""):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("level") != "event":
                    continue
                data = ev.get("data", {})
                payload = data.get("data", data)
                event = ev.get("event", "")
                # 同步到全局 tracker（与 _start_activity_monitor 共用）
                if event == "subtask_activity":
                    sub_id = payload.get("sub_id", "")
                    activity = payload.get("activity", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, activity)
                elif event == "subtask_start":
                    sub_id = payload.get("sub_id", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, f"Executing {sub_id}: {payload.get('title', '')}")
                elif event == "subtask_complete":
                    sub_id = payload.get("sub_id", "")
                    if sub_id:
                        self._tracker.update(task_id, sub_id, f"Completed {sub_id}: {payload.get('status', '')}")
                elif event == "pipeline_complete":
                    self._tracker.update_current(task_id, f"Pipeline {payload.get('status', 'completed')}")
                with state_lock:
                    state["has_lifecycle"] = True
                    if event == "pipeline_start":
                        state["total"] = payload.get("total_subtasks", 0)
                    elif event == "subtask_start":
                        sub_id = payload.get("sub_id", "")
                        state["current_activity"] = (
                            f"Executing {sub_id}: {payload.get('title', '')}")
                    elif event == "subtask_complete":
                        state["completed"] += 1
                        sub_id = payload.get("sub_id", "")
                        state["current_activity"] = (
                            f"Completed {sub_id}: {payload.get('status', '')}")
                    elif event == "subtask_activity":
                        sub_id = payload.get("sub_id", "")
                        activity = payload.get("activity", "")
                        state["current_activity"] = activity
                        state["activity_per_subtask"][sub_id] = activity
                    elif event == "pipeline_complete":
                        state["current_activity"] = (
                            f"Pipeline {payload.get('status', 'completed')}")
                # 同步全局 activity store（tracker 快照，含时间戳与并行 subtask）
                with self._lock:
                    self._activity_store[task_id] = self._tracker.snapshot(task_id)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        last_meta_ok = 0
        deadline = time.time() + timeout

        while time.time() < deadline:
            ret = proc.poll()
            if ret is not None:
                time.sleep(0.3)
                break

            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    status = task_status(meta)
                    results = meta.get("results", [])
                    n_done = sum(1 for r in results
                                 if r.get("status") in ("completed", "no_changes"))

                    if status in ("ACCEPTED_DELIVERY", "DELIVERY_READY", "VERIFICATION_FAILED", "DELIVERY_FAILED", "BLOCKED", "CANCELLED"):
                        time.sleep(0.3)
                        break

                    with state_lock:
                        _total = state["total"] or len(meta.get("subtasks", [results]))
                        _progress = state["completed"] if state["has_lifecycle"] else n_done
                        _activity = state["current_activity"]

                    if (_progress > last_meta_ok or _activity) and token is not None:
                        last_meta_ok = _progress
                        msg = _activity or f"{_progress}/{_total} 完成"
                        self._notify("notifications/progress", {
                            "progressToken": token,
                            "progress": _progress,
                            "total": max(_total, _progress),
                            "current_activity": _activity,
                            "message": msg,
                        })
                except (json.JSONDecodeError, OSError):
                    pass

            time.sleep(0.5)

        try:
            meta = json.loads(
                meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            meta = {}

        # Final progress notification before returning
        if token is not None:
            with state_lock:
                _final_progress = state["completed"]
                _final_total = max(state["total"], _final_progress, 1)
                _final_activity = state["current_activity"]
            self._notify("notifications/progress", {
                "progressToken": token,
                "progress": _final_progress,
                "total": _final_total,
                "current_activity": _final_activity,
                "message": _final_activity or "Pipeline finished",
            })

        return self._build_completed(
            task_id, meta, timed_out=(time.time() >= deadline))

    def _build_completed(self, task_id: str, meta: dict, timed_out: bool = False) -> dict:
        results = meta.get("results", [])
        cost = self._aggregate_cost(AGENT_GO_DIR / task_id)
        status = (
            "EXECUTING" if meta.get("status_schema_version") else "running"
        ) if timed_out else task_status(meta)
        rv = {
            "task_id": task_id,
            "status": status,
            "duration_sec": sum(r.get("duration_sec", 0) for r in results),
            "cost_usd": cost,
            "results": [{
                "id": r.get("subtask_id", ""),
                "title": r.get("title", ""),
                "status": r.get("status", "unknown"),
                "duration_sec": r.get("duration_sec", 0),
                "changes": self._extract_changes(r),
                "verify_ok": r.get("verify_ok", False),
                "retry_count": r.get("retry_count", 0),
            } for r in results],
            "preserved_worktrees": self._find_preserved(task_id, results),
        }
        if timed_out:
            rv["timeout_hint"] = "任务仍在后台运行，可稍后 inspect_task 轮询或 resume_task 续跑"
        return rv

    def _extract_changes(self, r: dict) -> dict:
        cs = r.get("change_stats")
        if cs:
            return {"files": cs.get("files_changed", 0),
                    "insertions": cs.get("insertions", 0),
                    "deletions": cs.get("deletions", 0)}
        return {"files": 0, "insertions": 0, "deletions": 0}

    def _find_preserved(self, task_id: str, results: list) -> list:
        preserved = []
        for r in results:
            if r.get("status") in ("failed", "blocked"):
                sub_id = r.get("subtask_id", "")
                wt = AGENT_GO_DIR / task_id / sub_id / "work"
                preserved.append({
                    "id": sub_id,
                    "status": r.get("status"),
                    "path": str(wt),
                    "branch": f"agent_go/{task_id}/{sub_id}",
                    "failure_reason": r.get("failure_reason", ""),
                })
        return preserved

    def _aggregate_cost(self, task_dir: Path) -> float:
        mp = task_dir / "metering.jsonl"
        if not mp.exists():
            return 0.0
        total = 0.0
        for line in mp.read_text(encoding="utf-8").strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    total += json.loads(line).get("cost_usd", 0)
                except json.JSONDecodeError:
                    pass
        return round(total, 4)

    # ── Tool handlers ──────────────────────────────────────────

    def _ensure_task_dir(self, task_id: str) -> Path:
        td = AGENT_GO_DIR / task_id
        if not td.exists():
            tpl = _error_template("AGENT_GO_TASK_NOT_FOUND", task_id)
            raise MCPError("AGENT_GO_TASK_NOT_FOUND", f"任务不存在: {task_id}",
                           retryable=False, fix=tpl["fix"])
        return td

    def _dispatch_tool(self, name: str, args: dict, token: Any, msg_id: Any) -> None:
        """Dispatch a tool call and send response (stdio transport)."""
        self._send(self._handle_tool_call(name, args, token, msg_id))

    def _handle_tool_call(self, name: str, args: dict, token: Any, msg_id: Any) -> dict:
        """处理工具调用，返回 JSON-RPC 响应 dict（HTTP transport 复用）。

        wait=true 的 run_task/resume_task 会同步阻塞至任务完成
        （HTTP 长请求天然支持；stdio 由 handle_message 视 wait_async 决定是否起线程）。
        """
        try:
            if name == "run_task":
                r = self._tool_run_task(args, token)
            elif name == "resume_task":
                r = self._tool_resume_task(args, token)
            elif name == "inspect_task":
                r = self._tool_inspect(args)
            elif name == "review_task":
                r = self._tool_review(args)
            elif name == "governance_task":
                r = self._tool_governance(args)
            elif name == "list_tasks":
                r = self._tool_list_tasks(args)
            elif name == "cancel_task":
                r = self._tool_cancel_task(args)
            else:
                return self._error_payload(msg_id, -32602, f"Unknown tool: {name}")
            return self._result_payload(msg_id, r)
        except MCPError as e:
            # 错误响应携带 fix 指引，Agent 可据此自主恢复
            return self._error_payload(msg_id, -32000, e.message, {"error": e.to_dict()})
        except Exception as e:
            logger.exception("Unhandled error")
            return self._error_payload(msg_id, -32603, f"Internal error: {e}")

    def handle_message(self, msg: dict, wait_sync: bool = False) -> Optional[dict]:
        """处理单个 JSON-RPC 消息（stdio 与 HTTP transport 共用）。

        Args:
            msg: 解析后的 JSON-RPC 消息 dict
            wait_sync: True 时 wait=true 的 tools/call 同步阻塞执行（HTTP 用）；
                       False 时在后台线程执行（stdio 用，主循环继续读 stdin）

        Returns:
            响应 dict；notification（无响应）返回 None
        """
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "initialize":
            return self._result_payload(mid, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "agent_go-mcp", "version": "1.0.0"}
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._result_payload(mid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            meta = params.get("_meta", {})
            token = meta.get("progressToken")
            if args.get("wait", False) and not wait_sync:
                t = threading.Thread(target=self._dispatch_tool,
                                     args=(name, args, token, mid), daemon=True)
                t.start()
                return None
            return self._handle_tool_call(name, args, token, mid)
        if method == "resources/list":
            return self._result_payload(mid, self._handle_resources_list())
        if method == "resources/read":
            uri = params.get("uri", "")
            try:
                return self._result_payload(mid, self._handle_resources_read(uri))
            except MCPError as e:
                return self._error_payload(mid, -32002, e.message, {"error": e.to_dict()})
        if method == "prompts/list":
            return self._result_payload(mid, self._handle_prompts_list())
        if method == "prompts/get":
            name = params.get("name", "")
            try:
                return self._result_payload(mid, self._handle_prompts_get(name, params.get("arguments", {})))
            except MCPError as e:
                return self._error_payload(mid, -32002, e.message, {"error": e.to_dict()})
        if method == "notifications/cancelled":
            logger.info(f"Cancellation requested: {params}")
            return None
        return self._error_payload(mid, -32601, f"Method not found: {method}")

    def _tool_run_task(self, args: dict, token: Any) -> dict:
        repo = args["repo"]
        if not self._check_repo_allowed(repo):
            tpl = _error_template("AGENT_GO_REPO_INVALID")
            raise MCPError("AGENT_GO_REPO_INVALID", f"仓库不在 allowlist: {repo}",
                           retryable=False, fix=tpl["fix"])

        cmd = self._argv("run", repo, args["task"])
        if args.get("parallel", 1) > 1:
            cmd += ["--parallel", str(args["parallel"])]
        if args.get("max_retries") is not None:
            cmd += ["--max-retries", str(args["max_retries"])]
        if args.get("remote"):
            cmd += ["--remote", args["remote"]]
        if args.get("agent_type"):
            cmd += ["--agent-type", args["agent_type"]]
        if args.get("skills"):
            cmd += ["--skill", ",".join(args["skills"])]
        if args.get("docs"):
            cmd += ["--docs", ",".join(args["docs"])]
        if args.get("preserve_worktrees"):
            cmd.append("--preserve-worktrees")

        with self._lock:
            if len(self._running) >= self._max_concurrent:
                tpl = _error_template("AGENT_GO_CAPACITY")
                raise MCPError("AGENT_GO_CAPACITY", f"并发任务已达上限 ({self._max_concurrent})",
                               retryable=True, fix=tpl["fix"])
            proc = self._spawn(cmd)

        task_id = self._read_agentgo_start(proc)

        with self._lock:
            self._running[task_id] = proc

        if args.get("wait", False):
            result = self._wait_with_events(proc, task_id, args.get("timeout_sec", 3600), token)
            with self._lock:
                self._running.pop(task_id, None)
            return result

        # 异步路径：后台监控活动，inspect_task 可查询实时进度（P1-2）
        self._start_activity_monitor(proc, task_id)

        return {
            "task_id": task_id, "status": "running",
            "task_dir": str(AGENT_GO_DIR / task_id), "pid": proc.pid,
            "poll_hint": {"tool": "inspect_task", "params": {"task_id": task_id}, "suggested_interval_sec": 30}
        }

    def _tool_resume_task(self, args: dict, token: Any) -> dict:
        task_id = args["task_id"]
        td = self._ensure_task_dir(task_id)

        meta_path = td / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status = task_status(meta)
            if status == "completed":
                return self._build_completed(task_id, meta)
            if status == "running":
                tpl = _error_template("AGENT_GO_TASK_RUNNING", task_id)
                raise MCPError("AGENT_GO_TASK_RUNNING", f"任务正在运行: {task_id}",
                               retryable=False, fix=tpl["fix"])

        cmd = self._argv("resume", task_id)
        if args.get("parallel") and args["parallel"] > 1:
            cmd += ["--parallel", str(args["parallel"])]
        if args.get("max_retries") is not None:
            cmd += ["--max-retries", str(args["max_retries"])]
        if args.get("remote"):
            cmd += ["--remote", args["remote"]]

        proc = self._spawn(cmd)
        with self._lock:
            self._running[task_id] = proc

        if args.get("wait", False):
            result = self._wait_with_events(proc, task_id, args.get("timeout_sec", 3600), token)
            with self._lock:
                self._running.pop(task_id, None)
            return result

        # 异步路径：后台监控活动（P1-2）
        self._start_activity_monitor(proc, task_id)

        return {
            "task_id": task_id, "status": "running", "pid": proc.pid,
            "poll_hint": {"tool": "inspect_task", "params": {"task_id": task_id}, "suggested_interval_sec": 30}
        }

    def _tool_list_tasks(self, args: dict) -> dict:
        """列出任务概要（P0-2）。支持状态过滤 + 分页。"""
        status_filter = args.get("status", "all")
        limit = min(max(args.get("limit", 20), 1), 50)
        offset = max(args.get("offset", 0), 0)

        tasks = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        entries = []
        for t in tasks:
            meta_path = t / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            status = task_status(meta)
            if status_filter != "all" and status != status_filter:
                continue
            results = meta.get("results", [])
            total = len(meta.get("subtasks", [results]))
            n_done = sum(1 for r in results if r.get("status") in ("completed", "no_changes"))
            entries.append({
                "task_id": meta.get("task_id", t.name),
                "status": status,
                "task": meta.get("task", "")[:80],
                "repo": meta.get("repo", ""),
                "progress": {"completed": n_done, "total": total},
                "created": meta.get("created", ""),
                "cost_usd": self._aggregate_cost(t),
            })

        total_matched = len(entries)
        page = entries[offset:offset + limit]
        return {
            "total": total_matched,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total_matched,
            "tasks": page,
        }

    def _tool_cancel_task(self, args: dict) -> dict:
        """取消正在运行的任务（P0-3）：终止子进程 + meta.json 标记 cancelled。

        confirm=true 时先通过 sampling 向 Host 请求确认（R-5）；
        sampling 不可用/超时时 fail-open 直接执行（保持既有行为）。
        """
        task_id = args["task_id"]
        td = self._ensure_task_dir(task_id)
        meta_path = td / "meta.json"

        # R-5: 破坏性操作确认（可选）
        if args.get("confirm", False):
            n_done = 0
            if meta_path.exists():
                try:
                    meta0 = json.loads(meta_path.read_text(encoding="utf-8"))
                    n_done = sum(1 for r in meta0.get("results", [])
                                 if r.get("status") in ("completed", "no_changes"))
                except (json.JSONDecodeError, OSError):
                    pass
            question = (
                f"⚠️ 请求确认取消任务 {task_id}（已完成 {n_done} 个子任务）。\n"
                f"取消后：子进程将被终止，已完成结果与 metering 保留，"
                f"可通过 resume_task 续跑。确认取消？[Y/N]"
            )
            confirmed = self.sampling_confirm(question, timeout=20.0)
            if not confirmed:
                return {"task_id": task_id, "status": "running", "cancelled": False,
                        "message": "Host 未确认取消，任务保持运行"}

        proc = None
        with self._lock:
            proc = self._running.get(task_id)

        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        status = task_status(meta)
        if status not in ("running", "paused"):
            return {"task_id": task_id, "status": status,
                    "cancelled": False,
                    "message": f"任务状态为 {status}，无需取消"}

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        # 标记状态（保留已完成结果与 metering，便于后续审计/恢复）
        if meta.get("status_schema_version"):
            set_task_status(meta, "CANCELLED")
        else:
            meta["status"] = "cancelled"
        meta["failure_class"] = "user_cancelled"
        meta["cancelled_at"] = datetime.now().isoformat()
        if meta_path.exists():
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        with self._lock:
            self._running.pop(task_id, None)

        return {
            "task_id": task_id,
            "status": "cancelled",
            "cancelled": True,
            "cancelled_at": meta.get("cancelled_at", datetime.now().isoformat()),
            "completed_subtasks": sum(1 for r in meta.get("results", [])
                                      if r.get("status") in ("completed", "no_changes")),
            "note": "已完成子任务结果与 metering 已保留；如需续跑请用 resume_task",
        }

    def _tool_inspect(self, args: dict) -> dict:
        task_id = args["task_id"]
        td = self._ensure_task_dir(task_id)
        meta_path = td / "meta.json"
        if not meta_path.exists():
            raise MCPError("AGENT_GO_TASK_NOT_FOUND", f"meta.json 不存在: {task_id}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        results = meta.get("results", [])
        total = len(meta.get("subtasks", [results]))
        n_done = sum(1 for r in results if r.get("status") in ("completed", "no_changes"))
        n_fail = sum(1 for r in results if r.get("status") in ("failed", "blocked"))
        n_running = sum(1 for r in results if r.get("status") in ("running", "pending"))
        pending = total - len(results)

        rv = {
            "task_id": task_id, "status": task_status(meta),
            "task": meta.get("task", ""), "repo": meta.get("repo", ""),
            "elapsed_sec": sum(r.get("duration_sec", 0) for r in results),
            "progress": {"completed": n_done, "failed": n_fail, "blocked": n_fail,
                         "running": n_running, "pending": max(0, pending), "total": total or len(results)},
            "cost_usd": self._aggregate_cost(td),
            "subtasks": self._build_subtask_list(results, task_id),
            "preserved_worktrees": self._find_preserved(task_id, results),
        }
        # Add current_activity from activity store (if available)
        act_state = self._activity_store.get(task_id, {})
        if act_state.get("current_activity"):
            rv["current_activity"] = act_state["current_activity"]
        if args.get("include_log_tail"):
            lp = td / "execution.log"
            if lp.exists():
                lines = lp.read_text(encoding="utf-8").strip().split("\n")
                rv["log_tail"] = lines[-args.get("log_lines", 30):]
        return rv

    def _tool_governance(self, args: dict) -> dict:
        """M1.4: 返回任务治理报告（traceability + architecture compliance）。只读。"""
        from .governance import build_traceability_matrix

        task_id = args["task_id"]
        td = self._ensure_task_dir(task_id)
        meta_path = td / "meta.json"
        if not meta_path.exists():
            raise MCPError("AGENT_GO_TASK_NOT_FOUND", f"meta.json 不存在: {task_id}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as _ge:
            raise MCPError("AGENT_GO_META_CORRUPT", f"meta.json 无法解析: {_ge}")
        report = build_traceability_matrix(meta)
        return {
            "task_id": task_id,
            "status": task_status(meta),
            "traceability_matrix": report["traceability"],
            "architecture_compliance": report["architecture_compliance"],
            "assessment": report["assessment"],
        }

    def _tool_review(self, args: dict) -> dict:
        task_id = args["task_id"]
        td = self._ensure_task_dir(task_id)
        action = args["action"]

        if action == "analyze":
            cmd = self._argv("review", "--task", task_id)
            if args.get("deep"):
                cmd.append("--deep")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                tpl = _error_template("AGENT_GO_TIMEOUT", task_id)
                raise MCPError("AGENT_GO_TIMEOUT", "review 命令超时", retryable=True, fix=tpl["fix"])
            if proc.returncode != 0:
                tpl = _error_template("AGENT_GO_CLI_ERROR", task_id)
                raise MCPError("AGENT_GO_CLI_ERROR",
                               f"review 失败: {proc.stderr.strip() or '未知错误'}",
                               retryable=True, fix=tpl["fix"])
            return self._parse_jsonl_last(proc.stdout) or {"raw_output": proc.stdout.strip()[:2000]}

        decision = {"task_id": task_id, "decision": action,
                    "comment": args.get("comment", ""), "recorded_at": datetime.now().isoformat()}
        dp = td / "review_decision.json"
        dp.write_text(json.dumps(decision, indent=2, ensure_ascii=False), encoding="utf-8")
        hp = td / "review_history.jsonl"
        with hp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision, ensure_ascii=False) + "\n")
        return {"task_id": task_id, "decision": action, "recorded_at": decision["recorded_at"],
                "decision_path": str(dp)}

    def _build_subtask_list(self, results: list, task_id: str) -> list:
        """Build subtask list, enriching running subtasks with current_activity."""
        act_state = self._activity_store.get(task_id, {})
        activity_per_subtask = act_state.get("activity_per_subtask", {})
        subtasks = []
        for r in results:
            sid = r.get("subtask_id", "") or r.get("id", "")
            entry = {"id": sid, "title": r.get("title", ""),
                     "status": r.get("status", "unknown"), "duration_sec": r.get("duration_sec", 0),
                     "verify_ok": r.get("verify_ok", False), "retry_count": r.get("retry_count", 0),
                     "changes": self._extract_changes(r)}
            if entry["status"] in ("running", "pending"):
                act = activity_per_subtask.get(sid)
                if act:
                    entry["current_activity"] = act
            subtasks.append(entry)
        return subtasks

    def _parse_jsonl_last(self, text: str) -> Optional[dict]:
        last = None
        for line in text.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    pass
        return last

    # ── Resources / Prompts 原语（P1-1 / P0-4）─────────────────

    def _handle_resources_list(self) -> dict:
        return {"resources": RESOURCES}

    def _handle_resources_read(self, uri: str) -> dict:
        """按需读取任务上下文。失败时返回带 error 的空结果而非抛错（fail-open）。"""
        parsed = self._parse_resource_uri(uri)
        if parsed is None:
            raise MCPError("AGENT_GO_RESOURCE_INVALID", f"无效的资源 URI: {uri}",
                           fix={"description": "使用 resources/list 查看可用资源"})
        resource, task_id = parsed

        try:
            if resource == "list":
                # 精简版任务列表（不带 cost 计算，避免 IO 开销）
                tasks = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)[:50]
                entries = []
                for t in tasks:
                    mp = t / "meta.json"
                    if not mp.exists():
                        continue
                    try:
                        m = json.loads(mp.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue
                    entries.append({
                        "task_id": m.get("task_id", t.name),
                        "status": m.get("status", "unknown"),
                        "task": m.get("task", "")[:60],
                        "created": m.get("created", ""),
                    })
                return {"uri": uri, "mimeType": "application/json",
                        "contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps({"tasks": entries}, ensure_ascii=False)}]}

            if resource == "summary":
                td = self._ensure_task_dir(task_id)
                mp = td / "meta.json"
                if not mp.exists():
                    raise MCPError("AGENT_GO_TASK_NOT_FOUND", f"meta.json 不存在: {task_id}",
                                   fix={"tool": "list_tasks", "params": {"status": "all"}})
                meta = json.loads(mp.read_text(encoding="utf-8"))
                results = meta.get("results", [])
                total = len(meta.get("subtasks", [results]))
                n_done = sum(1 for r in results if r.get("status") in ("completed", "no_changes"))
                n_fail = sum(1 for r in results if r.get("status") in ("failed", "blocked"))
                summary = {
                    "task_id": task_id, "status": task_status(meta),
                    "task": meta.get("task", ""), "repo": meta.get("repo", ""),
                    "progress": {"completed": n_done, "failed": n_fail, "total": total},
                    "duration_sec": sum(r.get("duration_sec", 0) for r in results),
                    "cost_usd": self._aggregate_cost(td),
                    "activity": self._tracker.snapshot(task_id),
                }
                return {"uri": uri, "mimeType": "application/json",
                        "contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps(summary, ensure_ascii=False)}]}

            if resource == "plan":
                td = self._ensure_task_dir(task_id)
                plans_dir = td / "plans"
                versions = sorted([f.stem for f in plans_dir.glob("v*.json")],
                                  key=lambda x: int(x[1:])) if plans_dir.exists() else []
                if not versions:
                    # 回退：读取 PLAN.md（若有）
                    plan_md = td / "PLAN.md"
                    if plan_md.exists():
                        return {"uri": uri, "mimeType": "text/markdown",
                                "contents": [{"uri": uri, "mimeType": "text/markdown",
                                              "text": plan_md.read_text(encoding="utf-8")[:20000]}]}
                    raise MCPError("AGENT_GO_TASK_NO_PLAN", f"任务无 Plan 快照: {task_id}",
                                   fix={"description": "任务可能在 Plan 阶段被中断"})
                plan = json.loads((plans_dir / f"{versions[-1]}.json").read_text(encoding="utf-8"))
                return {"uri": uri, "mimeType": "application/json",
                        "contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps(plan, ensure_ascii=False)[:20000]}]}

            if resource == "metering":
                td = self._ensure_task_dir(task_id)
                mp = td / "metering.jsonl"
                if not mp.exists():
                    return {"uri": uri, "mimeType": "application/json",
                            "contents": [{"uri": uri, "mimeType": "application/json", "text": "{}"}]}
                total = 0.0
                tokens = 0
                calls = 0
                for line in mp.read_text(encoding="utf-8").strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        total += rec.get("cost_usd", 0)
                        tokens += rec.get("total_tokens", 0) or 0
                        calls += 1
                    except json.JSONDecodeError:
                        pass
                data = {"calls": calls, "total_tokens": tokens, "cost_usd": round(total, 4)}
                return {"uri": uri, "mimeType": "application/json",
                        "contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps(data, ensure_ascii=False)}]}

            if resource == "log/recent":
                td = self._ensure_task_dir(task_id)
                lp = td / "execution.log"
                if not lp.exists():
                    return {"uri": uri, "mimeType": "text/plain",
                            "contents": [{"uri": uri, "mimeType": "text/plain", "text": ""}]}
                lines = lp.read_text(encoding="utf-8").strip().split("\n")
                tail = "\n".join(lines[-50:])
                return {"uri": uri, "mimeType": "text/plain",
                        "contents": [{"uri": uri, "mimeType": "text/plain", "text": tail}]}

            if resource == "review":
                td = self._ensure_task_dir(task_id)
                data = {}
                rp = td / "review.json"
                if rp.exists():
                    try:
                        data["decision"] = json.loads(rp.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        pass
                hp = td / "review_history.jsonl"
                if hp.exists():
                    history = []
                    for line in hp.read_text(encoding="utf-8").strip().split("\n"):
                        if line.strip():
                            try:
                                history.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                    data["history"] = history
                return {"uri": uri, "mimeType": "application/json",
                        "contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps(data, ensure_ascii=False)}]}

        except MCPError:
            raise
        except Exception as e:
            logger.exception("resources/read failed for %s", uri)
            raise MCPError("AGENT_GO_RESOURCE_READ_FAILED", f"读取资源失败: {e}")

        raise MCPError("AGENT_GO_RESOURCE_INVALID", f"未知资源: {resource}")

    def _parse_resource_uri(self, uri: str) -> Optional[tuple[str, Optional[str]]]:
        """解析 agent_go://tasks/... URI → (resource, task_id)。"""
        if not uri.startswith("agent_go://tasks"):
            return None
        path = uri[len("agent_go://tasks"):].strip("/")
        if not path:
            return None
        if path == "list":
            return ("list", None)
        parts = path.split("/")
        task_id = parts[0]
        resource = "/".join(parts[1:]) if len(parts) > 1 else "summary"
        if resource not in ("summary", "plan", "metering", "log/recent", "review"):
            return None
        return (resource, task_id)

    def _handle_prompts_list(self) -> dict:
        return {"prompts": PROMPTS}

    def _handle_prompts_get(self, name: str, arguments: dict) -> dict:
        """返回标准操作规程 prompt。Agent 按模板执行，减少推理不确定性。"""
        args = arguments or {}
        if name == "diagnose_failure":
            task_id = args.get("task_id", "{task_id}")
            return {
                "description": "诊断任务失败",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"任务 {task_id} 执行失败，请按以下步骤系统诊断：\n\n"
                        f"1. 读取日志: resources/read agent_go://tasks/{task_id}/log/recent\n"
                        f"2. 查询状态: inspect_task({{task_id: {task_id}}})\n"
                        f"3. 读取 Plan: resources/read agent_go://tasks/{task_id}/plan\n"
                        f"4. 区分失败类型：\n"
                        f"   - 代码问题 → 修复后 resume_task\n"
                        f"   - 验证环境问题 → 修复环境后 resume_task\n"
                        f"   - Plan 本身不合理 → 重新 run_task\n"
                        f"5. 输出结论：失败原因 + 推荐下一步操作"
                    ),
                }],
            }
        if name == "review_and_decide":
            task_id = args.get("task_id", "{task_id}")
            return {
                "description": "审查任务结果并决策",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"审查任务 {task_id} 的执行结果并决策：\n\n"
                        f"1. 审查状态: review_task({{task_id: {task_id}, action: analyze}})\n"
                        f"2. 读取审查历史: resources/read agent_go://tasks/{task_id}/review\n"
                        f"3. 决策：\n"
                        f"   - 全部变更正确 → review_task(action: approve)\n"
                        f"   - 存在需修复问题 → review_task(action: changes_requested, comment: <具体意见>)\n"
                        f"   - 严重错误 → review_task(action: reject)\n"
                        f"4. 输出：决策 + 理由"
                    ),
                }],
            }
        if name == "resume_or_restart":
            task_id = args.get("task_id", "{task_id}")
            return {
                "description": "决定 resume 还是重新 run",
                "messages": [{
                    "role": "user",
                    "content": (
                        f"任务 {task_id} 处于可恢复状态，请决策：\n\n"
                        f"1. 读取摘要: resources/read agent_go://tasks/{task_id}/summary\n"
                        f"2. 读取日志: resources/read agent_go://tasks/{task_id}/log/recent\n"
                        f"3. 决策：\n"
                        f"   - 已完成子任务有效 → resume_task({{task_id: {task_id}}})\n"
                        f"   - Plan 或上下文已过时 → 重新 run_task（可引用原任务描述）\n"
                        f"4. 输出：决策 + 理由"
                    ),
                }],
            }
        raise MCPError("AGENT_GO_PROMPT_NOT_FOUND", f"未知 prompt: {name}",
                       fix={"description": "使用 prompts/list 查看可用 prompt"})

    # ── Main loop ──────────────────────────────────────────────

    def run(self) -> None:
        logger.info("MCP server started")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._send(self._error_payload(None, -32700, "Parse error"))
                continue

            # R-5 Sampling：先检查是否为 server 发起的 sampling/createMessage 响应
            mid = msg.get("id")
            with self._lock:
                is_deferred = mid in self._deferred
            if is_deferred:
                with self._lock:
                    event = self._deferred.pop(mid)
                    self._deferred_result[mid] = msg
                event.set()
                continue

            resp = self.handle_message(msg, wait_sync=False)
            if resp is not None:
                self._send(resp)

        # Cleanup on stdin EOF
        for task_id, proc in list(self._running.items()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    # ── R-5 Sampling 原语 ──────────────────────────────────────

    def request_sampling(self, prompt: str, max_tokens: int = 100,
                         timeout: float = 30.0,
                         system_prompt: str = "") -> Optional[dict]:
        """发起 sampling/createMessage 请求，等待客户端（Host）响应。

        用途：server 在关键决策点（如破坏性操作确认）反向询问 LLM/用户。
        MCP 规范要求 Host 将 sampling 请求展示给用户审核后回复。

        返回:
            客户端响应 dict（{"role": ..., "content": [...]} 或完整 response）
            或 None —— 超时 / 客户端不可达（HTTP transport 无双向通道）/ 无响应

        Fail-open 设计：sampling 不可用时返回 None，调用方自行决定降级行为。
        """
        # HTTP transport 无客户端→server 的双向通道，sampling 不可用
        if self._notification_sink is not None:
            logger.info("HTTP transport 不支持 sampling（无双向通道），跳过")
            return None

        with self._lock:
            self._sampling_seq += 1
            req_id = f"sampling-{self._sampling_seq}"

        params: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "maxTokens": max_tokens,
        }
        if system_prompt:
            params["systemPrompt"] = system_prompt

        event = threading.Event()
        with self._lock:
            self._deferred[req_id] = event
            self._deferred_result[req_id] = None

        self._send({"jsonrpc": JSONRPC_VERSION, "id": req_id,
                    "method": "sampling/createMessage", "params": params})

        ok = event.wait(timeout)
        with self._lock:
            result = self._deferred_result.pop(req_id, None)
            self._deferred.pop(req_id, None)
        if not ok:
            logger.warning("sampling/createMessage 超时（%.0fs），返回 None", timeout)
            return None
        if result is None:
            return None
        # 提取客户端回复内容（兼容 {"result": {...}} 与 {"content": [...]} 两种形状）
        resp = result.get("result", result)
        if isinstance(resp, dict) and "content" in resp:
            return resp
        if isinstance(resp, dict) and resp.get("error"):
            logger.warning("sampling 请求被客户端拒绝: %s", resp["error"])
            return None
        return resp

    def sampling_confirm(self, question: str, timeout: float = 30.0) -> bool:
        """简化确认包装：向 Host 提问，期待 [Y]/[N] 类回答。

        Returns:
            True=确认 / False=拒绝 / 不可用时默认 True（fail-open，
            调用方负责传递「未能确认」的语义——见 cancel_task 的 confirm 参数）。
        """
        resp = self.request_sampling(question, max_tokens=20, timeout=timeout)
        if resp is None:
            return True  # fail-open：无法确认时按通过处理
        # 提取文本回复
        content = resp.get("content", [])
        text = ""
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    text += str(c.get("text", ""))
        else:
            text = str(content)
        text = text.strip().upper()
        if not text:
            return True
        return text.startswith(("Y", "YES", "是", "确认"))


def main(args=None):
    """CLI 入口：agent_go mcp（stdio）或 agent_go mcp --http（HTTP/SSE）。

    也支持 python3 -m agent_go.mcp_server [--http] [--host H] [--port P]。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")

    import argparse as _argparse
    parser = _argparse.ArgumentParser(prog="agent_go mcp")
    parser.add_argument("--http", action="store_true",
                        help="以 HTTP/SSE transport 运行（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP 绑定地址（默认 127.0.0.1，仅本地）")
    parser.add_argument("--port", type=int, default=8090,
                        help="HTTP 监听端口（默认 8090）")
    if args is not None and hasattr(args, "http"):
        http_mode = bool(args.http)
        host, port = getattr(args, "host", "127.0.0.1"), int(getattr(args, "port", 8090))
    else:
        _args = parser.parse_args(args or None)
        http_mode, host, port = _args.http, _args.host, _args.port

    if http_mode:
        from .mcp_http import serve_http
        serve_http(host=host, port=port)
    else:
        server = MCPServer()
        server.run()


if __name__ == "__main__":
    main()
