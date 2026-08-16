"""MCP 消费层（S9-A）— 让 agent_go 子任务调用外部 MCP server 工具。

设计原则（解耦 §S9）：
1. 零外部依赖 — 纯 stdlib 实现 JSON-RPC 2.0 over stdio（与 mcp_server.py 一致）
2. 故障隔离 — 每个 server 独立 try/except，启动/调用失败降级 warning 不阻断 pipeline
3. 命名空间 — 外部工具暴露为 mcp__{server}__{tool}，避免与原生工具（Read/Write/Edit/Bash）重名
4. 结果兼容 — 同时解析 MCP 标准（content blocks）和 agent_go 自有（裸 dict）两种结果格式

协议：NDJSON over stdio，protocolVersion "2024-11-05"。
生命周期：pipeline 启动时 start_all()，三个退出点 stop_all()（复用 mcp_server.py 的 terminate→kill 模式）。

参考：docs/design/office-capability-extension.md §2
"""

import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

__all__ = ["MCPServerConnection", "MCPClientPool"]

logger = logging.getLogger("agent_go.mcp.client")

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
_TOOL_PREFIX = "mcp__"

# 超时（秒）—— 与 mcp_server.py 的 _read_agentgo_start(30s) / bench 的 grace 对齐
_INIT_TIMEOUT = 10      # initialize 握手
_LIST_TIMEOUT = 10      # tools/list
_CALL_TIMEOUT = 60      # tools/call（Office 操作可能较慢）
_STOP_GRACE = 3         # terminate→kill 宽限


class MCPServerConnection:
    """单个外部 MCP server 的连接生命周期管理。

    一个连接 = 一个 subprocess（server 进程）+ 一条 stdio JSON-RPC 通道。
    线程安全：多个 dispatch 可并发 call_tool，靠 _pending Event 匹配响应。
    """

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self._tools: list[dict] = []          # 缓存 list_tools 结果（已加前缀）
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}     # id → result/error dict
        self._reader: Optional[threading.Thread] = None
        self._stderr_drainer: Optional[threading.Thread] = None
        self._closed = False

    # ── 启动 + 握手 ──────────────────────────────────────────────

    def start(self) -> None:
        """Popen server 进程 + JSON-RPC initialize 握手。失败抛异常（由调用方降级）。"""
        command = self.spec.get("command", "")
        args = self.spec.get("args", [])
        if not command:
            raise ValueError(f"MCP server {self.name}: 缺少 command 字段")

        env = os.environ.copy()
        env.update(self.spec.get("env", {}))

        # 复用 mcp_server.py:407-415 的 spawn 模式：PIPE 双向 + line-buffered + daemon stderr drain
        self.proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )

        # daemon 线程排空 stderr，防止 pipe buffer 死锁（stderr 内容丢弃，仅 debug 记录尾部）
        def _drain_stderr() -> None:
            try:
                for line in self.proc.stderr:  # type: ignore[union-attr]
                    logger.debug("[mcp:%s] stderr: %s", self.name, line.rstrip()[:200])
            except Exception:
                pass
        self._stderr_drainer = threading.Thread(target=_drain_stderr, daemon=True)
        self._stderr_drainer.start()

        # daemon 线程读 stdout，按 id 路由响应（复用 mcp_server.py:1390-1414 deferred 模式）
        def _read_loop() -> None:
            try:
                for line in self.proc.stdout:  # type: ignore[union-attr]
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("[mcp:%s] 非 JSON 行: %s", self.name, line[:200])
                        continue
                    mid = msg.get("id")
                    if mid is None:
                        continue  # notification（如 progress），暂不处理
                    with self._lock:
                        event = self._pending.get(mid)
                        if event is not None:
                            self._results[mid] = msg
                            event.set()
            except Exception as e:
                logger.debug("[mcp:%s] reader 退出: %s", self.name, e)
        self._reader = threading.Thread(target=_read_loop, daemon=True)
        self._reader.start()

        # initialize 握手
        resp = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent_go", "version": "1.0.0"},
        }, timeout=_INIT_TIMEOUT)
        if "error" in resp:
            raise RuntimeError(f"MCP server {self.name} initialize 失败: {resp['error']}")
        # 发 initialized 通知（协议要求）
        self._notify("notifications/initialized", {})

    def list_tools(self) -> list[dict]:
        """tools/list → 过滤 tool_filter → 加 mcp__{name}__ 前缀。结果缓存。"""
        if self._tools:
            return self._tools
        resp = self._request("tools/list", {}, timeout=_LIST_TIMEOUT)
        if "error" in resp:
            raise RuntimeError(f"MCP server {self.name} tools/list 失败: {resp['error']}")
        raw_tools = resp.get("result", {}).get("tools", [])
        tool_filter = self.spec.get("tool_filter")
        if tool_filter:
            allowed = set(tool_filter)
            raw_tools = [t for t in raw_tools if t.get("name") in allowed]
        # 加命名空间前缀，避免跨 server / 与原生工具重名
        prefix = f"{_TOOL_PREFIX}{self.name}__"
        self._tools = []
        for t in raw_tools:
            prefixed = dict(t)
            prefixed["name"] = f"{prefix}{t.get('name', '')}"
            self._tools.append(prefixed)
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """tools/call → 兼容解析结果。返回 {success, output/error} 格式（与 ToolRegistry 一致）。"""
        try:
            resp = self._request("tools/call", {"name": tool_name, "arguments": arguments}, timeout=_CALL_TIMEOUT)
        except Exception as e:
            return {"success": False, "error": f"MCP 调用异常 ({self.name}/{tool_name}): {e}"}
        if "error" in resp:
            err = resp["error"]
            return {"success": False, "error": f"MCP server 返回错误 ({self.name}/{tool_name}): {err}"}
        return {"success": True, "output": _parse_tool_result(resp.get("result", {}))}

    # ── 停止 ────────────────────────────────────────────────────

    def stop(self) -> None:
        """terminate → wait(grace) → kill（复用 mcp_server.py:970-981 模式）。"""
        if self._closed:
            return
        self._closed = True
        if self.proc is None:
            return
        if self.proc.poll() is None:  # 仍在运行
            try:
                self.proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                self.proc.wait(timeout=_STOP_GRACE)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    self.proc.wait(timeout=1)
                except Exception:
                    pass
        # 关闭 pipe，避免 ResourceWarning
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    # ── 内部：JSON-RPC 请求/响应 ────────────────────────────────

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        """发请求 + 等响应（按 id 匹配 Event）。返回完整 JSON-RPC 响应 dict。"""
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError(f"MCP server {self.name} 未启动或 stdin 已关闭")
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            event = threading.Event()
            self._pending[mid] = event
            self._results[mid] = None
        msg = {"jsonrpc": JSONRPC_VERSION, "id": mid, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(mid, None)
                self._results.pop(mid, None)
            raise TimeoutError(f"MCP server {self.name} {method} 超时 ({timeout}s)")
        with self._lock:
            self._pending.pop(mid, None)
            return self._results.pop(mid, {"error": {"code": -1, "message": "响应丢失"}})

    def _notify(self, method: str, params: dict) -> None:
        """发通知（无 id，不等响应）。"""
        if self.proc is None or self.proc.stdin is None:
            return
        msg = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params:
            msg["params"] = params
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass


def _parse_tool_result(result: Any) -> str:
    """兼容解析工具结果：MCP 标准（content blocks）或裸 dict。

    - MCP 标准: {"content": [{"type": "text", "text": "..."}], "isError": bool}
    - agent_go 自有: 裸 dict（如 {"task_id": ..., "status": ...}）
    """
    if isinstance(result, dict):
        # MCP 标准：content 数组
        content = result.get("content")
        if isinstance(content, list) and content:
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if texts:
                return "\n".join(texts)
        # 裸 dict：序列化为 JSON（保持结构化信息）
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


class MCPClientPool:
    """多 MCP server 连接池（pipeline 级生命周期）。

    用法：
        pool = MCPClientPool(config.get("mcp_servers", {}))
        pool.start_all()           # pipeline 启动时
        pool.tool_definitions()    # 合并进 AgentLoop tools
        pool.dispatch(name, args)  # 路由工具调用
        pool.stop_all()            # pipeline 退出时（finally / 3 个退出点）
    """

    def __init__(self, server_configs: dict):
        self._raw_configs = server_configs or {}
        self._servers: dict[str, MCPServerConnection] = {}
        self._lock = threading.Lock()

    def start_all(self) -> None:
        """并发启动所有 enabled server，per-server 失败降级（不抛异常）。"""
        enabled = []
        for name, spec in self._raw_configs.items():
            if not isinstance(spec, dict):
                logger.warning("MCP server %s 配置非 dict，跳过", name)
                continue
            if spec.get("enabled", True) is False:
                logger.debug("MCP server %s 已禁用 (enabled=false)", name)
                continue
            enabled.append((name, spec))
        if not enabled:
            return

        # 并发启动（复用 pipeline.py 的 ThreadPoolExecutor + per-future 异常捕获模式）
        def _connect(name_spec: tuple[str, dict]) -> Optional[str]:
            name, spec = name_spec
            try:
                conn = MCPServerConnection(name, spec)
                conn.start()
                conn.list_tools()  # 预取工具列表
                with self._lock:
                    self._servers[name] = conn
                return name
            except Exception as e:
                logger.warning("MCP server %s 连接失败，跳过（不中断核心）: %s", name, e)
                return None

        with ThreadPoolExecutor(max_workers=min(len(enabled), 4)) as ex:
            futures = {ex.submit(_connect, ns): ns[0] for ns in enabled}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logger.warning("MCP server %s 启动异常: %s", futures[fut], e)

        connected = list(self._servers.keys())
        if connected:
            logger.info("[mcp] 已连接 %d/%d 个 MCP server", len(connected), len(enabled))

    def tool_definitions(self) -> list[dict]:
        """合并所有已连接 server 的工具定义（已加 mcp__ 前缀）。"""
        tools = []
        with self._lock:
            servers = list(self._servers.values())
        for conn in servers:
            try:
                tools.extend(conn.list_tools())
            except Exception as e:
                logger.debug("MCP server %s 工具列表获取失败: %s", conn.name, e)
        return tools

    def dispatch(self, full_name: str, arguments: dict) -> dict:
        """按 mcp__{server}__{tool} 路由到对应 server。返回 {success, output/error}。"""
        # 解析命名空间：mcp__excel__read_sheet → server=excel, tool=read_sheet
        if not full_name.startswith(_TOOL_PREFIX):
            return {"success": False, "error": f"非 MCP 工具（缺少 {_TOOL_PREFIX} 前缀）: {full_name}"}
        rest = full_name[len(_TOOL_PREFIX):]
        if "__" not in rest:
            return {"success": False, "error": f"MCP 工具名格式错误（缺少 server/tool 分隔）: {full_name}"}
        server_name, tool_name = rest.split("__", 1)
        with self._lock:
            conn = self._servers.get(server_name)
        if conn is None:
            return {"success": False, "error": f"MCP server 未连接: {server_name}"}
        return conn.call_tool(tool_name, arguments or {})

    def mcp_config_for_claude(self) -> dict:
        """生成 claude CLI --mcp-config 透传用的 JSON（claude 原生 MCP 消费格式）。

        格式：{"mcpServers": {"server_key": {"command": ..., "args": ..., "env": ...}}}
        只含 enabled 且原始配置有效的 server（不含 tool_filter/scope 等 agent_go 专有字段）。
        """
        servers = {}
        for name, spec in self._raw_configs.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("enabled", True) is False:
                continue
            command = spec.get("command")
            if not command:
                continue
            entry = {"command": command, "args": spec.get("args", [])}
            if spec.get("env"):
                entry["env"] = spec["env"]
            servers[name] = entry
        return {"mcpServers": servers}

    def stop_all(self) -> None:
        """并发停止所有 server，per-server 异常不阻断回收。"""
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        if not servers:
            return

        def _stop_one(conn: MCPServerConnection) -> None:
            try:
                conn.stop()
            except Exception as e:
                logger.debug("MCP server %s 停止异常: %s", conn.name, e)

        with ThreadPoolExecutor(max_workers=min(len(servers), 4)) as ex:
            list(ex.map(_stop_one, servers))
        logger.debug("[mcp] 所有 MCP server 已停止")
