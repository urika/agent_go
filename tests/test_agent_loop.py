"""测试 agent_loop — AgentLoop 多轮对话循环

通过 mock urllib.request.urlopen 避免真实网络请求；
git commit/tag 流程使用 tmp_path 下的真实 git 仓库验证。
"""

import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.agent_loop import (
    AgentLoop,
    _anthropic_messages,
    _openai_messages,
    _parse_tool_calls,
    _assistant_message,
)
from agent_go.config import DEFAULT_CONFIG
from agent_go.router import ProviderConfig, RoleRoute


class MockResponse:
    """模拟 urllib.request.urlopen 返回值的上下文管理器"""

    def __init__(self, json_data, status=200):
        self._json_data = json_data
        self.status = status

    def __enter__(self):
        self.body = io.BytesIO()
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self._json_data).encode("utf-8")


def make_config(**overrides):
    """DEFAULT_CONFIG 深拷贝后改字段"""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in overrides.items():
        config[key] = value
    return config


def make_route(provider="anthropic"):
    primary = ProviderConfig(
        provider=provider,
        base_url="https://api.test/v1/messages",
        model="test-model",
    )
    return RoleRoute(role="worker", primary=primary)


def anthropic_response(text="", tool_calls=None, usage=None):
    """构造 Anthropic 格式响应"""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls or []:
        content.append({
            "type": "tool_use",
            "id": tc["id"],
            "name": tc["name"],
            "input": tc["input"],
        })
    return {
        "content": content,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def openai_response(text="", tool_calls=None, usage=None):
    """构造 OpenAI 兼容格式响应"""
    message = {"content": text or None}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["input"], ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]
    return {
        "choices": [{"message": message}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def init_git_repo(path: Path):
    """在 path 下初始化一个带首次提交的 git 仓库"""
    (path / "init.txt").write_text("init\n", encoding="utf-8")
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "init"],
    ]:
        subprocess.run(cmd, cwd=str(path), check=True, capture_output=True)


class TestMessageConversion:
    """内部消息格式 → 各 provider 格式"""

    def test_anthropic_messages_tool_role(self):
        """tool 角色转换为 user + tool_result 块"""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
        ]
        result = _anthropic_messages(messages)
        assert result[0] == {"role": "user", "content": "hi"}
        assert result[1]["role"] == "user"
        assert result[1]["content"] == [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "result",
        }]

    def test_openai_messages_tool_role(self):
        """tool 角色保持 tool 并携带 tool_call_id"""
        messages = [{"role": "tool", "tool_call_id": "t1", "content": "result"}]
        result = _openai_messages(messages)
        assert result == [{
            "role": "tool",
            "tool_call_id": "t1",
            "content": "result",
        }]

    def test_messages_passthrough_other_roles(self):
        """user/assistant 角色原样传递"""
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        assert _anthropic_messages(messages) == messages
        assert _openai_messages(messages) == messages


class TestParseToolCalls:
    """API 响应 → tool_calls 提取"""

    def test_anthropic_tool_use(self):
        """只提取 tool_use 块，忽略 text 块"""
        data = {"content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}},
        ]}
        calls = _parse_tool_calls(data, "anthropic")
        assert calls == [{"id": "t1", "name": "Read", "input": {"file_path": "a"}}]

    def test_anthropic_no_tool_calls(self):
        data = {"content": [{"type": "text", "text": "done"}]}
        assert _parse_tool_calls(data, "anthropic") == []

    def test_openai_tool_calls(self):
        data = {"choices": [{"message": {"tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
        }]}}]}
        calls = _parse_tool_calls(data, "openai")
        assert calls == [{"id": "c1", "name": "Bash", "input": {"command": "ls"}}]

    def test_openai_invalid_arguments_json(self):
        """arguments 非法 JSON 时降级为空 dict"""
        data = {"choices": [{"message": {"tool_calls": [{
            "id": "c1",
            "function": {"name": "Bash", "arguments": "{not json"},
        }]}}]}
        calls = _parse_tool_calls(data, "openai")
        assert calls[0]["input"] == {}

    def test_openai_empty_response(self):
        assert _parse_tool_calls({"choices": []}, "openai") == []
        assert _parse_tool_calls({}, "openai") == []


class TestAssistantMessage:
    """assistant 消息构建"""

    def test_anthropic_with_text_and_tool_calls(self):
        tcs = [{"id": "t1", "name": "Read", "input": {"file_path": "a"}}]
        msg = _assistant_message(tcs, "让我读一下", "anthropic")
        assert msg["role"] == "assistant"
        assert msg["content"][0] == {"type": "text", "text": "让我读一下"}
        assert msg["content"][1]["type"] == "tool_use"
        assert msg["content"][1]["id"] == "t1"

    def test_anthropic_tool_calls_only(self):
        """无文本时不生成 text 块"""
        tcs = [{"id": "t1", "name": "Read", "input": {}}]
        msg = _assistant_message(tcs, "", "anthropic")
        assert all(b["type"] == "tool_use" for b in msg["content"])

    def test_openai_with_tool_calls(self):
        tcs = [{"id": "c1", "name": "Write", "input": {"file_path": "a", "content": "x"}}]
        msg = _assistant_message(tcs, "", "openai")
        assert msg["content"] is None
        assert msg["tool_calls"][0]["id"] == "c1"
        args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
        assert args == {"file_path": "a", "content": "x"}

    def test_openai_without_tool_calls(self):
        msg = _assistant_message([], "完成", "openai")
        assert msg == {"role": "assistant", "content": "完成"}
        assert "tool_calls" not in msg


class TestCallApi:
    """AgentLoop._call_api 请求构造与响应解析"""

    @patch("urllib.request.urlopen")
    def test_anthropic_request(self, mock_urlopen, logger):
        """Anthropic 请求头与 payload"""
        mock_urlopen.return_value = MockResponse(anthropic_response(text="ok"))
        loop = AgentLoop(logger)
        text, tool_calls, cost = loop._call_api(
            "anthropic", "https://api.test/v1/messages", "test-model",
            "sk-ant-key", [{"role": "user", "content": "hi"}], [{"name": "Read"}],
            "", "t1", "s1",
        )
        assert text == "ok"
        assert tool_calls == []

        req = mock_urlopen.call_args[0][0]
        assert req.headers["X-api-key"] == "sk-ant-key"
        assert req.headers["Anthropic-version"] == "2023-06-01"
        payload = json.loads(req.data)
        assert payload["model"] == "test-model"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert payload["tools"] == [{"name": "Read"}]

    @patch("urllib.request.urlopen")
    def test_openai_request(self, mock_urlopen, logger):
        """OpenAI 兼容请求头与响应解析"""
        mock_urlopen.return_value = MockResponse(openai_response(
            tool_calls=[{"id": "c1", "name": "Bash", "input": {"command": "ls"}}],
        ))
        loop = AgentLoop(logger)
        text, tool_calls, cost = loop._call_api(
            "openai", "https://api.test/v1/chat/completions", "test-model",
            "sk-key", [{"role": "user", "content": "hi"}], [], "", "t1", "s1",
        )
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "Bash"

        req = mock_urlopen.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer sk-key"

    @patch("urllib.request.urlopen")
    def test_metering_written(self, mock_urlopen, logger, temp_dir):
        """metering_path 非空时写入计量事件"""
        metering = temp_dir / "metering.jsonl"
        mock_urlopen.return_value = MockResponse(anthropic_response(
            usage={"input_tokens": 100, "output_tokens": 50},
        ))
        loop = AgentLoop(logger)
        loop._call_api(
            "anthropic", "https://api.test/v1/messages", "test-model",
            "key", [{"role": "user", "content": "hi"}], [], str(metering), "t1", "s1",
        )
        event = json.loads(metering.read_text(encoding="utf-8").strip())
        assert event["role"] == "worker"
        assert event["actual_provider"] == "anthropic"
        assert event["prompt_tokens"] == 100
        assert event["completion_tokens"] == 50
        assert event["task_id"] == "t1"
        assert event["result"] == "success"

    @patch("urllib.request.urlopen")
    def test_no_metering_when_path_empty(self, mock_urlopen, logger, temp_dir):
        """metering_path 为空时不写文件、不报错"""
        mock_urlopen.return_value = MockResponse(anthropic_response())
        loop = AgentLoop(logger)
        text, _, _ = loop._call_api(
            "anthropic", "https://api.test/v1/messages", "m",
            "key", [{"role": "user", "content": "hi"}], [], "", "t", "s",
        )
        assert list(temp_dir.iterdir()) == []


class TestAgentLoopRun:
    """AgentLoop.run 多轮循环"""

    @patch("urllib.request.urlopen")
    def test_completes_without_tool_calls(self, mock_urlopen, logger, temp_dir):
        """首轮即无工具调用 → 正常结束 exit 0"""
        mock_urlopen.return_value = MockResponse(anthropic_response(text="完成"))
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", make_config())
        assert result.returncode == 0
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_executes_tool_then_completes(self, mock_urlopen, logger, temp_dir):
        """工具调用被真实执行后再结束"""
        mock_urlopen.side_effect = [
            MockResponse(anthropic_response(tool_calls=[{
                "id": "t1", "name": "Write",
                "input": {"file_path": "out.txt", "content": "agent 产出"},
            }])),
            MockResponse(anthropic_response(text="完成")),
        ]
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", make_config())
        assert result.returncode == 0
        assert mock_urlopen.call_count == 2
        # Write 工具真实写入了 worktree
        assert (temp_dir / "out.txt").read_text(encoding="utf-8") == "agent 产出"

    @patch("urllib.request.urlopen")
    def test_tool_result_fed_back(self, mock_urlopen, logger, temp_dir):
        """工具执行结果以 tool 角色消息回传给下一轮 API"""
        mock_urlopen.side_effect = [
            MockResponse(anthropic_response(tool_calls=[{
                "id": "t1", "name": "Read", "input": {"file_path": "missing.txt"},
            }])),
            MockResponse(anthropic_response(text="完成")),
        ]
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", make_config())
        assert result.returncode == 0

        # 第二轮请求中应包含 tool_result（文件不存在的错误）
        second_req = mock_urlopen.call_args_list[1][0][0]
        payload = json.loads(second_req.data)
        tool_results = [
            block for m in payload["messages"] if m["role"] == "user"
            for block in (m["content"] if isinstance(m["content"], list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "t1"
        assert "文件不存在" in tool_results[0]["content"]

    @patch("urllib.request.urlopen")
    def test_max_turns_forces_exit_1(self, mock_urlopen, logger, temp_dir):
        """持续返回工具调用，达到 max_turns 强制结束"""
        config = make_config(agent_loop={"max_turns": 3})
        mock_urlopen.return_value = MockResponse(anthropic_response(tool_calls=[{
            "id": "t1", "name": "Bash", "input": {"command": "ls"},
        }]))
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", config)
        assert result.returncode == 1
        assert mock_urlopen.call_count == 3

    @patch("urllib.request.urlopen")
    def test_message_window_compression(self, mock_urlopen, logger, temp_dir):
        """消息超过 40 条时压缩窗口（保留首条 + 最近 30 条）"""
        records = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger.addHandler(ListHandler())

        # 每轮 1 个工具调用 = +2 条消息，第 20 轮后 41 条触发压缩
        tool_resp = anthropic_response(tool_calls=[{
            "id": "t1", "name": "Bash", "input": {"command": "ls"},
        }])
        config = make_config(agent_loop={"max_turns": 25})
        mock_urlopen.side_effect = (
            [MockResponse(tool_resp) for _ in range(20)]
            + [MockResponse(anthropic_response(text="完成"))]
        )
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", config)
        assert result.returncode == 0
        assert mock_urlopen.call_count == 21
        assert any("消息窗口压缩" in r and "41 → 31" in r for r in records)

    @patch("urllib.request.urlopen")
    def test_openai_provider_loop(self, mock_urlopen, logger, temp_dir):
        """OpenAI 兼容 provider 也能完成完整循环"""
        mock_urlopen.side_effect = [
            MockResponse(openai_response(tool_calls=[{
                "id": "c1", "name": "Write",
                "input": {"file_path": "o.txt", "content": "openai"},
            }])),
            MockResponse(openai_response(text="done")),
        ]
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route("openai"), "key", make_config())
        assert result.returncode == 0
        assert (temp_dir / "o.txt").read_text(encoding="utf-8") == "openai"

        # 第二轮请求包含 tool 角色回传消息
        second_req = mock_urlopen.call_args_list[1][0][0]
        payload = json.loads(second_req.data)
        roles = [m["role"] for m in payload["messages"]]
        assert "tool" in roles

    @patch("urllib.request.urlopen")
    def test_git_commit_and_tag(self, mock_urlopen, logger, temp_dir):
        """tag_name 非空时自动 add/commit/tag"""
        worktree = temp_dir / "repo"
        worktree.mkdir()
        init_git_repo(worktree)

        mock_urlopen.side_effect = [
            MockResponse(anthropic_response(tool_calls=[{
                "id": "t1", "name": "Write",
                "input": {"file_path": "new.txt", "content": "新增文件"},
            }])),
            MockResponse(anthropic_response(text="完成")),
        ]
        loop = AgentLoop(logger)
        result = loop.run(
            "任务", worktree, make_route(), "key", make_config(),
            tag_name="task1/sub1", sub_id="sub1", task_id="task1",
        )
        assert result.returncode == 0

        tags = subprocess.run(
            ["git", "tag", "-l"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        ).stdout
        assert "task1/sub1" in tags

        # 新文件已提交，工作区干净
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        ).stdout
        assert status.strip() == ""

    @patch("urllib.request.urlopen")
    def test_no_git_when_no_changes(self, mock_urlopen, logger, temp_dir):
        """无文件变更时不产生新 commit，但 tag 仍创建"""
        worktree = temp_dir / "repo"
        worktree.mkdir()
        init_git_repo(worktree)
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        mock_urlopen.return_value = MockResponse(anthropic_response(text="完成"))
        loop = AgentLoop(logger)
        result = loop.run(
            "任务", worktree, make_route(), "key", make_config(),
            tag_name="task1/sub1", sub_id="sub1", task_id="task1",
        )
        assert result.returncode == 0

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head == before  # 无新 commit

        tags = subprocess.run(
            ["git", "tag", "-l"], cwd=str(worktree),
            capture_output=True, text=True, check=True,
        ).stdout
        assert "task1/sub1" in tags

    @patch("urllib.request.urlopen")
    def test_run_writes_metering(self, mock_urlopen, logger, temp_dir):
        """run 全程将每次 API 调用写入 metering.jsonl"""
        metering = temp_dir / "metering.jsonl"
        config = make_config(_metering_path=str(metering))
        mock_urlopen.side_effect = [
            MockResponse(anthropic_response(tool_calls=[{
                "id": "t1", "name": "Bash", "input": {"command": "ls"},
            }])),
            MockResponse(anthropic_response(text="完成")),
        ]
        loop = AgentLoop(logger)
        result = loop.run("任务", temp_dir, make_route(), "key", config,
                          task_id="t1", sub_id="s1")
        assert result.returncode == 0

        lines = metering.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3  # 2 API calls + 1 summary event
        for line in lines:
            event = json.loads(line)
            assert event["task_id"] == "t1"
            assert event["subtask_id"] == "s1"
