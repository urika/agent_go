"""MCP 消费层（S9-A）单元测试。

测试策略：
- @patch("subprocess.Popen") mock server 进程
- 用 _BlockingStdout（基于 queue.Queue）模拟阻塞式 stdout：reader 线程 for-line-in 时
  在 _request 写 stdin 触发后才 pop 对应响应，还原真实 pipe 的「写→读」时序
- 验证协议握手、工具发现、调用路由、故障降级、进程回收
"""

import json
import queue
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_go.mcp_client import (
    MCPServerConnection,
    MCPClientPool,
    _parse_tool_result,
    _TOOL_PREFIX,
)


# ═══════════════════════════════════════════════════════════════
# 辅助：阻塞式 stdout（模拟真实 pipe 的读写时序）
# ═══════════════════════════════════════════════════════════════

class _BlockingStdout:
    """模拟可阻塞迭代的 stdout。

    reader 线程 `for line in proc.stdout` 时：
    - pop_one() 阻塞直到 push 了一条响应或设置 EOF
    - 每次 stdin.write 被调用（即 _request 发出请求）→ 触发 pop 一条预设响应
    还原真实 pipe「client 写请求 → server 返响应」的时序，避免 mock 迭代器
    在 Event 注册前就消费完数据的问题。
    """

    def __init__(self, responses: list[str]):
        # responses: 每个元素是一行 JSON（不含 \\n），按请求顺序对应
        self._q: queue.Queue[str] = queue.Queue()
        for r in responses:
            self._q.put(r if r.endswith("\n") else r + "\n")
        self._closed = False

    def push(self, line: str) -> None:
        self._q.put(line if line.endswith("\n") else line + "\n")

    def close(self) -> None:
        self._closed = True
        self._q.put("")  # 哨兵：空行表示 EOF

    def __iter__(self):
        while True:
            try:
                line = self._q.get(timeout=5)
            except queue.Empty:
                break
            if line == "":
                break  # EOF
            yield line


def _mock_proc_with_responses(responses: list[str], returncode: int = 0) -> MagicMock:
    """构造 mock 进程，还原真实 pipe「写请求→读响应」的因果时序。

    responses: 按请求顺序的响应列表（每个是一行 JSON，不含 \\n）。
    stdin.write 的 side_effect 每次被调用时，push 下一条响应到 stdout 队列——
    这样 reader 线程在 _request 注册 Event 后才会读到对应响应（因果链正确）。
    """
    stdout = _BlockingStdout([])
    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = returncode if returncode else None
    proc.stdout = stdout

    # 核心因果链：stdin.write（_request 发请求）→ push 下一条响应到 stdout
    # 仅对带 id 的请求（initialize/list/call）push 响应；notification（无 id）跳过
    _resp_iter = iter(responses)

    def _on_write(data):
        # notification 无 id，不消耗响应（模拟 server 对 notify 不回复）
        if isinstance(data, str) and '"id"' not in data:
            return
        try:
            resp = next(_resp_iter)
            stdout.push(resp)
        except StopIteration:
            pass  # 没有更多预设响应（如多余的请求）

    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = _on_write
    proc.stdin.flush = MagicMock()

    proc.stderr = MagicMock()
    proc.stderr.__iter__ = lambda self: iter([])
    proc.wait.return_value = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc._test_stdout = stdout
    return proc


def _init_response(req_id: int) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "1.0.0"},
        }
    })


def _tools_list_response(req_id: int, tools: list[dict]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})


def _tool_call_response(req_id: int, result: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error_response(req_id: int, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


# ═══════════════════════════════════════════════════════════════
# _parse_tool_result 结果兼容性
# ═══════════════════════════════════════════════════════════════

class TestParseToolResult:
    def test_mcp_standard_content_blocks(self):
        """MCP 标准：content 数组取 text"""
        result = {"content": [{"type": "text", "text": "hello world"}], "isError": False}
        assert _parse_tool_result(result) == "hello world"

    def test_mcp_standard_multiple_blocks(self):
        """多个 content block 合并"""
        result = {"content": [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]}
        assert _parse_tool_result(result) == "line1\nline2"

    def test_agent_go_raw_dict(self):
        """agent_go 自有：裸 dict 序列化为 JSON"""
        result = {"task_id": "task-x", "status": "completed"}
        parsed = _parse_tool_result(result)
        assert json.loads(parsed) == result

    def test_plain_string(self):
        assert _parse_tool_result("just text") == "just text"

    def test_empty_content_list(self):
        """空 content 数组退化为 JSON 序列化"""
        result = {"content": [], "extra": "data"}
        parsed = _parse_tool_result(result)
        assert json.loads(parsed)["extra"] == "data"


# ═══════════════════════════════════════════════════════════════
# MCPServerConnection 单 server 生命周期
# ═══════════════════════════════════════════════════════════════

class TestMCPServerConnection:
    @patch("subprocess.Popen")
    def test_start_initialize_handshake(self, mock_popen):
        """start() 完成 initialize 握手"""
        proc = _mock_proc_with_responses([_init_response(1)])
        mock_popen.return_value = proc

        conn = MCPServerConnection("test", {"command": "uvx", "args": ["server"]})
        conn.start()

        # 验证 stdin 写入了 initialize 请求
        written = proc.stdin.write.call_args_list
        assert any("initialize" in str(w) for w in written)
        conn.stop()

    @patch("subprocess.Popen")
    def test_start_missing_command_raises(self, mock_popen):
        """缺少 command 字段抛 ValueError"""
        conn = MCPServerConnection("test", {"args": ["x"]})
        with pytest.raises(ValueError, match="缺少 command"):
            conn.start()

    @patch("subprocess.Popen")
    def test_list_tools_with_namespace_prefix(self, mock_popen):
        """list_tools 加 mcp__{name}__ 前缀"""
        raw_tools = [
            {"name": "read_sheet", "description": "Read", "inputSchema": {"type": "object"}},
            {"name": "write_sheet", "description": "Write", "inputSchema": {"type": "object"}},
        ]
        proc = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, raw_tools)])
        mock_popen.return_value = proc

        conn = MCPServerConnection("excel", {"command": "uvx", "args": ["x"]})
        conn.start()
        tools = conn.list_tools()

        assert len(tools) == 2
        assert tools[0]["name"] == f"{_TOOL_PREFIX}excel__read_sheet"
        assert tools[1]["name"] == f"{_TOOL_PREFIX}excel__write_sheet"
        conn.stop()

    @patch("subprocess.Popen")
    def test_list_tools_filter(self, mock_popen):
        """tool_filter 白名单过滤"""
        raw_tools = [
            {"name": "read_sheet", "inputSchema": {}},
            {"name": "write_sheet", "inputSchema": {}},
            {"name": "delete_sheet", "inputSchema": {}},
        ]
        proc = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, raw_tools)])
        mock_popen.return_value = proc

        conn = MCPServerConnection("excel", {
            "command": "uvx", "args": ["x"],
            "tool_filter": ["read_sheet", "write_sheet"],
        })
        conn.start()
        tools = conn.list_tools()

        names = [t["name"] for t in tools]
        assert f"{_TOOL_PREFIX}excel__read_sheet" in names
        assert f"{_TOOL_PREFIX}excel__write_sheet" in names
        assert f"{_TOOL_PREFIX}excel__delete_sheet" not in names
        conn.stop()

    @patch("subprocess.Popen")
    def test_list_tools_cached(self, mock_popen):
        """list_tools 结果缓存（第二次不重新请求）"""
        proc = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, [{"name": "t"}])])
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        first = conn.list_tools()
        second = conn.list_tools()
        assert first is second  # 同一缓存对象
        conn.stop()

    @patch("subprocess.Popen")
    def test_call_tool_success_content_blocks(self, mock_popen):
        """call_tool 成功（MCP 标准 content blocks）"""
        proc = _mock_proc_with_responses([
            _init_response(1),
            _tool_call_response(2, {"content": [{"type": "text", "text": "A1:B2 数据"}]}),
        ])
        mock_popen.return_value = proc

        conn = MCPServerConnection("excel", {"command": "x", "args": []})
        conn.start()
        result = conn.call_tool("read_sheet", {"file": "a.xlsx"})

        assert result["success"] is True
        assert result["output"] == "A1:B2 数据"
        conn.stop()

    @patch("subprocess.Popen")
    def test_call_tool_success_raw_dict(self, mock_popen):
        """call_tool 成功（agent_go 裸 dict 格式）"""
        raw = {"rows": 10, "status": "ok"}
        proc = _mock_proc_with_responses([
            _init_response(1),
            _tool_call_response(2, raw),
        ])
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        result = conn.call_tool("do_thing", {})

        assert result["success"] is True
        assert json.loads(result["output"]) == raw
        conn.stop()

    @patch("subprocess.Popen")
    def test_call_tool_server_error(self, mock_popen):
        """call_tool server 返回 JSON-RPC error"""
        proc = _mock_proc_with_responses([
            _init_response(1),
            _error_response(2, -32000, "参数错误"),
        ])
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        result = conn.call_tool("bad_tool", {})

        assert result["success"] is False
        assert "参数错误" in result["error"]
        conn.stop()

    @patch("subprocess.Popen")
    def test_call_tool_timeout(self, mock_popen):
        """call_tool 超时返回错误（不抛异常）"""
        # 进程不返回任何 tools/call 响应 → 超时
        proc = _mock_proc_with_responses([_init_response(1)])
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        # 临时调小超时加速测试
        import agent_go.mcp_client as mod
        orig = mod._CALL_TIMEOUT
        mod._CALL_TIMEOUT = 0.3
        try:
            result = conn.call_tool("slow_tool", {})
        finally:
            mod._CALL_TIMEOUT = orig

        assert result["success"] is False
        assert "超时" in result["error"] or "异常" in result["error"]
        conn.stop()

    @patch("subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        """stop() 调用 terminate"""
        proc = _mock_proc_with_responses([_init_response(1)])
        proc.poll.return_value = None  # 仍在运行
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        conn.stop()

        proc.terminate.assert_called()
        # stdin/stdout/stderr 关闭
        proc.stdin.close.assert_called()

    @patch("subprocess.Popen")
    def test_stop_kills_if_terminate_times_out(self, mock_popen):
        """stop() terminate 超时后 kill"""
        proc = _mock_proc_with_responses([_init_response(1)])
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=3)
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        conn.stop()

        proc.terminate.assert_called()
        proc.kill.assert_called()

    @patch("subprocess.Popen")
    def test_stop_idempotent(self, mock_popen):
        """stop() 可重复调用"""
        proc = _mock_proc_with_responses([_init_response(1)])
        mock_popen.return_value = proc

        conn = MCPServerConnection("s", {"command": "x", "args": []})
        conn.start()
        conn.stop()
        conn.stop()  # 不抛异常
        assert proc.terminate.call_count == 1


# ═══════════════════════════════════════════════════════════════
# MCPClientPool 多 server 连接池
# ═══════════════════════════════════════════════════════════════

class TestMCPClientPool:
    def test_start_all_empty_config(self):
        """空配置不启动任何 server"""
        pool = MCPClientPool({})
        pool.start_all()
        assert pool.tool_definitions() == []

    def test_start_all_skips_disabled(self):
        """enabled=false 的 server 跳过"""
        pool = MCPClientPool({
            "off": {"command": "x", "enabled": False},
        })
        pool.start_all()
        assert pool.tool_definitions() == []

    def test_start_all_skips_non_dict_spec(self):
        """非 dict 配置跳过"""
        pool = MCPClientPool({"bad": "not a dict"})
        pool.start_all()
        assert len(pool._servers) == 0

    @patch("subprocess.Popen")
    def test_start_all_partial_failure_degradation(self, mock_popen):
        """1 个 server 失败不影响其他（降级）"""
        # 第一个 Popen 抛异常（模拟启动失败），第二个成功
        good_proc = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, [{"name": "t"}])])
        mock_popen.side_effect = [OSError("command not found"), good_proc]

        pool = MCPClientPool({
            "broken": {"command": "nonexistent"},
            "good": {"command": "uvx", "args": ["ok"]},
        })
        pool.start_all()

        # 只有 good 连接成功
        assert "good" in pool._servers
        assert "broken" not in pool._servers
        tools = pool.tool_definitions()
        assert len(tools) == 1
        assert tools[0]["name"] == f"{_TOOL_PREFIX}good__t"
        pool.stop_all()

    @patch("subprocess.Popen")
    def test_dispatch_routes_correctly(self, mock_popen):
        """dispatch 按 mcp__{server}__{tool} 路由"""
        # start_all 预取 list_tools（id=2），dispatch call_tool（id=3）
        proc = _mock_proc_with_responses([
            _init_response(1),
            _tools_list_response(2, [{"name": "read_sheet"}]),
            _tool_call_response(3, {"content": [{"type": "text", "text": "result"}]}),
        ])
        mock_popen.return_value = proc

        pool = MCPClientPool({"excel": {"command": "x", "args": []}})
        pool.start_all()
        result = pool.dispatch(f"{_TOOL_PREFIX}excel__read_sheet", {"file": "a.xlsx"})

        assert result["success"] is True
        assert result["output"] == "result"
        pool.stop_all()

    def test_dispatch_unknown_server(self):
        """dispatch 到未连接的 server 返回错误"""
        pool = MCPClientPool({})
        pool.start_all()
        result = pool.dispatch(f"{_TOOL_PREFIX}unknown__tool", {})
        assert result["success"] is False
        assert "未连接" in result["error"]

    def test_dispatch_bad_prefix(self):
        """dispatch 无 mcp__ 前缀返回错误"""
        pool = MCPClientPool({})
        result = pool.dispatch("read_sheet", {})
        assert result["success"] is False

    def test_dispatch_malformed_name(self):
        """dispatch 名称格式错误（无 server/tool 分隔）"""
        pool = MCPClientPool({})
        result = pool.dispatch(f"{_TOOL_PREFIX}noseparator", {})
        assert result["success"] is False

    @patch("subprocess.Popen")
    def test_stop_all_stops_all_servers(self, mock_popen):
        """stop_all 回收所有 server 进程"""
        # 每个 server 需要 init + list（start_all 预取）
        proc1 = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, [{"name": "t"}])])
        proc2 = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, [{"name": "t"}])])
        mock_popen.side_effect = [proc1, proc2]

        pool = MCPClientPool({
            "s1": {"command": "x", "args": []},
            "s2": {"command": "y", "args": []},
        })
        pool.start_all()
        assert len(pool._servers) == 2
        pool.stop_all()

        proc1.terminate.assert_called()
        proc2.terminate.assert_called()
        assert len(pool._servers) == 0

    def test_stop_all_empty_no_error(self):
        """stop_all 空池不报错"""
        pool = MCPClientPool({})
        pool.stop_all()  # 不抛异常

    @patch("subprocess.Popen")
    def test_stop_all_idempotent(self, mock_popen):
        """stop_all 可重复调用"""
        proc = _mock_proc_with_responses([_init_response(1), _tools_list_response(2, [{"name": "t"}])])
        mock_popen.return_value = proc

        pool = MCPClientPool({"s": {"command": "x", "args": []}})
        pool.start_all()
        pool.stop_all()
        pool.stop_all()  # 不抛异常

    # ── mcp_config_for_claude（claude CLI 透传）──

    def test_mcp_config_for_claude_format(self):
        """生成 claude --mcp-config 格式的 JSON"""
        pool = MCPClientPool({
            "excel": {"command": "uvx", "args": ["excel-mcp-server", "stdio"], "env": {"FOO": "bar"}},
            "ppt": {"command": "uvx", "args": ["ppt-server"]},
            "off": {"command": "x", "enabled": False},
            "bad": "not dict",
        })
        cfg = pool.mcp_config_for_claude()

        assert "mcpServers" in cfg
        servers = cfg["mcpServers"]
        assert "excel" in servers
        assert servers["excel"]["command"] == "uvx"
        assert servers["excel"]["args"] == ["excel-mcp-server", "stdio"]
        assert servers["excel"]["env"] == {"FOO": "bar"}
        assert "ppt" in servers
        # disabled 和非法配置不出现
        assert "off" not in servers
        assert "bad" not in servers

    def test_mcp_config_for_claude_empty(self):
        """空配置返回空 mcpServers"""
        pool = MCPClientPool({})
        cfg = pool.mcp_config_for_claude()
        assert cfg == {"mcpServers": {}}
