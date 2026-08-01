"""agent_go MCP Server — HTTP/SSE transport (P3).

基于 Streamable HTTP 模式（MCP 2025+ 标准传输）：
- POST /mcp      处理 JSON-RPC 请求，返回 JSON 响应（wait=true 同步长请求）
- GET  /mcp      SSE 事件流端点，服务器向客户端推送 notifications（progress 等）
- GET  /health   健康检查

设计要点：
- 复用 mcp_server.MCPServer.handle_message（stdio 与 HTTP 共用消息处理）
- notification_sink 注入：_notify 推送到所有已连接的 SSE 客户端
- 纯 stdlib（http.server），无外部依赖
- 默认绑定 127.0.0.1（仅本地）；AGENT_GO_MCP_HTTP_TOKEN 可启用 Bearer token 鉴权

Usage:
    agent_go mcp --http --host 127.0.0.1 --port 8090
    python3 -m agent_go.mcp_server --http --port 8090
"""

import json, logging, queue, select, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .mcp_server import MCPServer, JSONRPC_VERSION

logger = logging.getLogger("agent_go.mcp.http")

SSE_HEARTBEAT_SEC = 30   # 无消息时发送 ping 心跳
SSE_IDLE_TIMEOUT_SEC = 900  # SSE 连接最长空闲时间（15 分钟）


class _SSEClient:
    """一个 SSE 长连接客户端。服务器通过 q 向其推送消息。"""

    def __init__(self, handler: "MCPHTTPHandler"):
        self.handler = handler
        self.q: "queue.Queue[Optional[dict]]" = queue.Queue()
        self.closed = threading.Event()

    def push(self, msg: dict) -> None:
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            self.close()

    def close(self) -> None:
        self.closed.set()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<_SSEClient closed={self.closed.is_set()}>"


class MCPHTTPHandler(BaseHTTPRequestHandler):
    """HTTP/SSE transport handler。

    server 属性（由 MCPHTTPServer 注入）:
        mcp: MCPServer 实例
        token: 可选 Bearer token（空则无需鉴权）
        sse_clients: 所有活跃 SSE 客户端集合（list + lock）
        shutdown_event: 服务关闭信号（SSE 循环退出用）
    """

    protocol_version = "HTTP/1.1"
    server_version = "agent_go-mcp/1.0"

    # ── 通用 ──────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默 access log
        pass

    def _auth_ok(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        api_key = self.headers.get("X-Api-Key", "")
        return auth == f"Bearer {token}" or api_key == token

    def _reply_json(self, code: int, payload: dict, headers: Optional[dict] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ── GET ───────────────────────────────────────────────────

    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._reply_json(200, {"status": "ok", "server": "agent_go-mcp"})
            return
        if self.path != "/mcp":
            self._reply_json(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._reply_json(401, {"error": "unauthorized"})
            return
        self._stream_sse()

    def _stream_sse(self) -> None:
        """建立 SSE 长连接：服务器通过它推送 notifications。"""
        client = _SSEClient(self)
        with self.server.sse_lock:  # type: ignore[attr-defined]
            self.server.sse_clients.add(client)  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # 禁用代理缓冲
        self.end_headers()

        last_msg_ts = time.time()
        try:
            while not client.closed.is_set() and not self.server.shutdown_event.is_set():  # type: ignore[attr-defined]
                # 探测客户端断开（EventSource 不会主动发数据；可读即 EOF/异常）
                r, _, _ = select.select([self.connection], [], [], 0)
                if r:
                    try:
                        if self.connection.recv(1, socket.MSG_PEEK) == b"":
                            break
                    except OSError:
                        break
                try:
                    msg = client.q.get(timeout=1.0)
                except queue.Empty:
                    # 心跳：保活连接，也帮助中间层识别断连
                    if time.time() - last_msg_ts >= SSE_HEARTBEAT_SEC:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_msg_ts = time.time()
                    continue
                if msg is None:
                    break
                data = json.dumps(msg, ensure_ascii=False)
                self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_msg_ts = time.time()
                if time.time() - last_msg_ts > SSE_IDLE_TIMEOUT_SEC:
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            client.close()
            with self.server.sse_lock:  # type: ignore[attr-defined]
                self.server.sse_clients.discard(client)  # type: ignore[attr-defined]

    # ── POST ──────────────────────────────────────────────────

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self._reply_json(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._reply_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._reply_json(400, {"error": "empty body"})
            return
        try:
            msg = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply_json(400, {"error": "invalid JSON"})
            return
        if not isinstance(msg, dict) or msg.get("jsonrpc") != JSONRPC_VERSION:
            self._reply_json(400, {"error": "invalid JSON-RPC message"})
            return

        try:
            # wait_sync=True：wait=true 的 tools/call 同步阻塞（HTTP 长请求）
            resp = self.server.mcp.handle_message(msg, wait_sync=True)  # type: ignore[attr-defined]
        except Exception as e:  # 兜底：任何异常都返回可解析的错误
            logger.exception("handle_message 异常")
            resp = {"jsonrpc": JSONRPC_VERSION, "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"Internal error: {e}"}}

        if resp is None:
            # notification，无响应
            self._reply_json(202, {})
            return
        self._reply_json(200, resp)

    # ── 其他 ──────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        """CORS 预检（本地工具宽松处理：允许任意 Origin 的 GET/POST）。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key, Mcp-Session-Id")
        self.send_header("Content-Length", "0")
        self.end_headers()


class MCPHTTPServer(ThreadingHTTPServer):
    """MCP HTTP server：持有 MCPServer 实例 + SSE 客户端注册表。

    通过 notification_sink 将 MCPServer 的 notifications 广播到所有 SSE 客户端。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], mcp: MCPServer,
                 token: str = "", allowed_origins: Optional[list] = None):
        self.mcp = mcp
        self.token = token
        self.allowed_origins = allowed_origins
        self.sse_clients: set[_SSEClient] = set()
        self.sse_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.sessions: dict[str, float] = {}  # session_id -> last_activity_ts

        # 注入 notification sink：_notify → 广播到所有 SSE 客户端
        mcp._notification_sink = self._broadcast_notification

        super().__init__(server_address, MCPHTTPHandler)

    def _broadcast_notification(self, msg: dict) -> None:
        """把 MCPServer 的 notification 推送到所有活跃 SSE 客户端。"""
        with self.sse_lock:
            clients = list(self.sse_clients)
        for c in clients:
            c.push(msg)

    def shutdown_soon(self) -> None:
        """关闭服务：通知 SSE 循环退出。"""
        self.shutdown_event.set()
        with self.sse_lock:
            for c in list(self.sse_clients):
                c.close()


def serve_http(host: str = "127.0.0.1", port: int = 8090,
               token: str = "", allowed_origins: Optional[list] = None) -> None:
    """启动 MCP HTTP/SSE server（阻塞运行，Ctrl+C 退出）。"""
    token = token or __import__("os").environ.get("AGENT_GO_MCP_HTTP_TOKEN", "")
    mcp = MCPServer()
    httpd = MCPHTTPServer((host, port), mcp, token=token, allowed_origins=allowed_origins)
    addr = httpd.server_address
    logger.info("MCP HTTP server started: http://%s:%d/mcp (POST JSON-RPC, GET SSE)", addr[0], addr[1])
    logger.info("Endpoints: POST /mcp | GET /mcp (SSE) | GET /health")
    if token:
        logger.info("Auth: Bearer token 已启用 (AGENT_GO_MCP_HTTP_TOKEN)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down HTTP server...")
    finally:
        httpd.shutdown_soon()
        httpd.server_close()
