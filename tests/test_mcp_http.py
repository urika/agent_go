"""Tests for agent_go MCP HTTP/SSE transport (mcp_http.py)."""

import http.client, json, os, socket, threading, time
from pathlib import Path
from unittest.mock import patch

import pytest

sys_path = str(Path(__file__).resolve().parent.parent)
import sys
sys.path.insert(0, sys_path)

import agent_go.mcp_server as mcp_mod
import agent_go.mcp_http as http_mod
from agent_go.mcp_server import MCPServer
from agent_go.mcp_http import MCPHTTPServer, MCPHTTPHandler, serve_http, _SSEClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def http_server(tmp_path):
    """启动真实 HTTP server（随机端口），返回 (server, port)。"""
    with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
        mcp = MCPServer()
        port = _free_port()
        srv = MCPHTTPServer(("127.0.0.1", port), mcp)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv, port
        srv.shutdown_soon()
        srv.shutdown()
        srv.server_close()


def _post(port, payload, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if headers:
        h.update(headers)
    conn.request("POST", "/mcp", body=body, headers=h)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


class TestProtocolOverHTTP:
    def test_health(self, http_server):
        srv, port = http_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 200
        assert data["status"] == "ok"

    def test_initialize(self, http_server):
        srv, port = http_server
        status, data = _post(port, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}
        })
        assert status == 200
        assert data["result"]["serverInfo"]["name"] == "agent_go-mcp"
        assert "resources" in data["result"]["capabilities"]
        assert "prompts" in data["result"]["capabilities"]

    def test_tools_list(self, http_server):
        srv, port = http_server
        status, data = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in data["result"]["tools"]]
        assert "list_tasks" in names
        assert "cancel_task" in names

    def test_resources_and_prompts(self, http_server):
        srv, port = http_server
        _, r = _post(port, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        assert len(r["result"]["resources"]) == 6
        _, p = _post(port, {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
        assert len(p["result"]["prompts"]) == 3

    def test_inspect_error_with_fix(self, http_server):
        srv, port = http_server
        status, data = _post(port, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "inspect_task", "arguments": {"task_id": "task-none"}}
        })
        assert status == 200
        err = data["error"]["data"]["error"]
        assert err["code"] == "AGENT_GO_TASK_NOT_FOUND"
        assert err["fix"]["tool"] == "list_tasks"

    def test_notification_no_response(self, http_server):
        srv, port = http_server
        status, data = _post(port, {
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })
        assert status == 202

    def test_invalid_json(self, http_server):
        srv, port = http_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/mcp", body=b"not-json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 400

    def test_unknown_method(self, http_server):
        srv, port = http_server
        status, data = _post(port, {"jsonrpc": "2.0", "id": 9, "method": "bogus"})
        assert data["error"]["code"] == -32601

    def test_404_other_paths(self, http_server):
        srv, port = http_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/other")
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 404


class TestAuth:
    @pytest.fixture
    def secured_server(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            mcp = MCPServer()
            port = _free_port()
            srv = MCPHTTPServer(("127.0.0.1", port), mcp, token="secret-token")
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            yield srv, port
            srv.shutdown()
            srv.server_close()

    def test_401_without_token(self, secured_server):
        srv, port = secured_server
        status, _ = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 401

    def test_ok_with_bearer(self, secured_server):
        srv, port = secured_server
        status, data = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Authorization": "Bearer secret-token"})
        assert status == 200
        assert len(data["result"]["tools"]) == 7

    def test_ok_with_x_api_key(self, secured_server):
        srv, port = secured_server
        status, data = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"X-Api-Key": "secret-token"})
        assert status == 200

    def test_get_sse_requires_auth(self, secured_server):
        srv, port = secured_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 401


class TestSSE:
    def test_sse_receives_notifications(self, http_server):
        """GET /mcp 建立 SSE 连接后，MCPServer 的 _notify 应推送到该连接。"""
        srv, port = http_server

        # 建立 SSE 连接（在独立线程中阻塞读取）
        sse_data = []
        sse_ready = threading.Event()

        def _reader():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.getheader("Content-Type", "").startswith("text/event-stream")
            sse_ready.set()
            # SSE 无 Content-Length：read1 只读当前可用缓冲，不阻塞到 EOF
            buf = resp.read1(4096)
            sse_data.append(buf.decode("utf-8", errors="replace"))
            conn.close()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        sse_ready.wait(timeout=5)
        time.sleep(0.2)  # 等服务器注册 client

        # 触发 notification（通过 sse_clients 广播）
        assert len(srv.sse_clients) >= 1
        srv.mcp._notify("notifications/progress", {"progress": 1, "total": 3})

        # 等待推送被读取（客户端读到后关闭）
        t.join(timeout=5)
        assert sse_data, "SSE 客户端未收到任何数据"
        assert "notifications/progress" in sse_data[0]
        assert "1" in sse_data[0]

    def test_broadcast_to_all_clients(self, http_server):
        """多个 SSE 客户端都应收到广播。"""
        srv, port = http_server
        received = []
        ready = threading.Event()

        def _reader(idx):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/mcp")
            resp = conn.getresponse()
            ready.set()
            data = resp.read1(2048).decode("utf-8", errors="replace")
            received.append((idx, data))
            conn.close()

        threads = [threading.Thread(target=_reader, args=(i,), daemon=True) for i in range(2)]
        for t in threads:
            t.start()
        ready.wait(timeout=5)
        time.sleep(0.3)

        srv.mcp._notify("notifications/progress", {"progress": 2, "total": 5})
        for t in threads:
            t.join(timeout=5)

        assert len(received) == 2
        assert all("notifications/progress" in d for _, d in received)

    def test_sse_client_cleanup_on_disconnect(self, http_server):
        """客户端断开后应从 sse_clients 移除。"""
        srv, port = http_server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        time.sleep(0.2)
        assert len(srv.sse_clients) == 1
        # 注意：getresponse() 后 socket 所有权移交 resp，必须 resp.close() 才真正断开
        resp.close()
        time.sleep(2.0)  # 等 SSE 循环探测到 EOF（1s 轮询周期）
        assert len(srv.sse_clients) == 0


class TestHandleMessageSync:
    """handle_message wait_sync=True 的同步行为（HTTP 路径）。"""

    def test_wait_sync_returns_response(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            s = MCPServer()
            resp = s.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    wait_sync=True)
            assert resp["id"] == 1
            assert "tools" in resp["result"]

    def test_wait_sync_notification_returns_none(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            s = MCPServer()
            resp = s.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    wait_sync=True)
            assert resp is None

    def test_parse_error_payload(self, tmp_path):
        with patch.object(mcp_mod, "AGENT_GO_DIR", tmp_path):
            s = MCPServer()
            # run() 对 parse error 的处理：发送 error payload
            from io import StringIO
            import sys as _sys
            buf = StringIO()
            old = _sys.stdout
            _sys.stdout = buf
            try:
                s._send(s._error_payload(None, -32700, "Parse error"))
            finally:
                _sys.stdout = old
            assert json.loads(buf.getvalue())["error"]["code"] == -32700


class TestSSEClientUnit:
    def test_push_and_close(self):
        c = _SSEClient.__new__(_SSEClient)
        import queue as _q
        c.q = _q.Queue()
        c.closed = threading.Event()
        c.push({"a": 1})
        assert c.q.get() == {"a": 1}
        assert not c.closed.is_set()

    def test_close_sets_event(self):
        c = _SSEClient.__new__(_SSEClient)
        import queue as _q
        c.q = _q.Queue()
        c.closed = threading.Event()
        c.close()
        assert c.closed.is_set()
