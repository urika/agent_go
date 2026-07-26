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

import sys, json, os, subprocess, time, threading, fnmatch, logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("agent_go.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
JSONRPC_VERSION = "2.0"


class MCPError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable


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
]

AGENT_GO_DIR = Path.home() / ".agent_go"


# ── Core server ──────────────────────────────────────────────────

class MCPServer:
    def __init__(self):
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._max_concurrent = int(os.environ.get("AGENT_GO_MCP_MAX_CONCURRENT", "3"))
        self._allowed_repos = self._parse_allowed_repos()
        self._deferred: dict[Any, threading.Event] = {}  # msg_id -> completion event
        self._deferred_result: dict[Any, Any] = {}        # msg_id -> result
        self._activity_store: dict[str, dict] = {}  # task_id -> {current_activity, activity_per_subtask}

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

    def _result(self, msg_id: Any, result: Any) -> None:
        self._send({"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str, data: Any = None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": err})

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    # ── Subprocess management ──────────────────────────────────

    def _argv(self, *extra: str) -> list[str]:
        return [sys.executable, "-m", "agent_go"] + list(extra) + ["--yes", "--json"]

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env)

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
                        # Update server-wide activity store
                        self._activity_store[task_id] = {
                            "current_activity": activity,
                            "activity_per_subtask": dict(state["activity_per_subtask"]),
                        }
                    elif event == "pipeline_complete":
                        state["current_activity"] = (
                            f"Pipeline {payload.get('status', 'completed')}")

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
                    status = meta.get("status", "running")
                    results = meta.get("results", [])
                    n_done = sum(1 for r in results
                                 if r.get("status") in ("completed", "no_changes"))

                    if status in ("completed", "failed", "paused", "stale_aborted"):
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
        total = len(meta.get("subtasks", [results]))
        n_done = sum(1 for r in results if r.get("status") in ("completed", "no_changes"))
        n_fail = sum(1 for r in results if r.get("status") in ("failed", "blocked"))
        cost = self._aggregate_cost(AGENT_GO_DIR / task_id)
        status = "running" if timed_out else meta.get("status", "unknown")
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
            raise MCPError("AGENT_GO_TASK_NOT_FOUND", f"任务不存在: {task_id}")
        return td

    def _dispatch_tool(self, name: str, args: dict, token: Any, msg_id: Any) -> None:
        """Dispatch a tool call and send response."""
        try:
            if name == "run_task":
                r = self._tool_run_task(args, token)
            elif name == "resume_task":
                r = self._tool_resume_task(args, token)
            elif name == "inspect_task":
                r = self._tool_inspect(args)
            elif name == "review_task":
                r = self._tool_review(args)
            else:
                self._error(msg_id, -32602, f"Unknown tool: {name}")
                return
            self._result(msg_id, r)
        except MCPError as e:
            self._error(msg_id, -32000, e.message, {
                "error": {"code": e.code, "message": e.message, "retryable": e.retryable}
            })
        except Exception as e:
            logger.exception("Unhandled error")
            self._error(msg_id, -32603, f"Internal error: {e}")

    def _tool_run_task(self, args: dict, token: Any) -> dict:
        repo = args["repo"]
        if not self._check_repo_allowed(repo):
            raise MCPError("AGENT_GO_REPO_INVALID", f"仓库不在 allowlist: {repo}")

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
                raise MCPError("AGENT_GO_CAPACITY", f"并发任务已达上限 ({self._max_concurrent})", retryable=True)
            proc = self._spawn(cmd)

        task_id = self._read_agentgo_start(proc)

        with self._lock:
            self._running[task_id] = proc

        if args.get("wait", False):
            result = self._wait_with_events(proc, task_id, args.get("timeout_sec", 3600), token)
            with self._lock:
                self._running.pop(task_id, None)
            return result

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
            status = meta.get("status", "")
            if status == "completed":
                return self._build_completed(task_id, meta)
            if status == "running":
                raise MCPError("AGENT_GO_TASK_RUNNING", f"任务正在运行: {task_id}")

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

        return {
            "task_id": task_id, "status": "running", "pid": proc.pid,
            "poll_hint": {"tool": "inspect_task", "params": {"task_id": task_id}, "suggested_interval_sec": 30}
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
            "task_id": task_id, "status": meta.get("status", "unknown"),
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

    def _tool_review(self, args: dict) -> dict:
        task_id = args["task_id"]
        self._ensure_task_dir(task_id)
        action = args["action"]

        if action == "analyze":
            cmd = self._argv("review", "--task", task_id)
            if args.get("deep"):
                cmd.append("--deep")
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                raise MCPError("AGENT_GO_TIMEOUT", "review 命令超时", retryable=True)
            if proc.returncode != 0:
                raise MCPError("AGENT_GO_CLI_ERROR", f"review 失败: {proc.stderr.strip() or '未知错误'}", retryable=True)
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
                self._error(None, -32700, "Parse error")
                continue

            mid = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {})

            if method == "initialize":
                self._result(mid, {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent_go-mcp", "version": "1.0.0"}
                })
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                self._result(mid, {"tools": TOOLS})
            elif method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments", {})
                meta = params.get("_meta", {})
                token = meta.get("progressToken")
                if args.get("wait", False):
                    t = threading.Thread(target=self._dispatch_tool,
                                         args=(name, args, token, mid), daemon=True)
                    t.start()
                else:
                    self._dispatch_tool(name, args, token, mid)
            elif method == "notifications/cancelled":
                logger.info(f"Cancellation requested: {params}")
            else:
                self._error(mid, -32601, f"Method not found: {method}")

        # Cleanup on stdin EOF
        for task_id, proc in list(self._running.items()):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    server = MCPServer()
    server.run()


if __name__ == "__main__":
    main()
