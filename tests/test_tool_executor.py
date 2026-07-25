"""测试 tool_executor — Read/Write/Edit/Bash 工具注册与执行

全部在 tmp_path 下真实执行（无网络、无外部依赖），
timeout 场景通过 mock subprocess.run 模拟。
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.tool_executor import ToolRegistry, TOOL_DEFINITIONS


class TestToolDefinitions:
    """工具 schema 注册"""

    def test_four_tools_registered(self):
        """注册 Read/Write/Edit/Bash 四个工具"""
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert names == ["Read", "Write", "Edit", "Bash"]

    def test_definitions_returned_by_registry(self):
        """ToolRegistry.definitions() 返回完整 schema 列表"""
        defs = ToolRegistry.definitions()
        assert defs is TOOL_DEFINITIONS
        for tool in defs:
            assert tool["description"]
            assert tool["input_schema"]["type"] == "object"
            for field in tool["input_schema"]["required"]:
                assert field in tool["input_schema"]["properties"]

    def test_required_fields(self):
        """每个工具的 required 参数"""
        required = {t["name"]: t["input_schema"]["required"] for t in TOOL_DEFINITIONS}
        assert required["Read"] == ["file_path"]
        assert required["Write"] == ["file_path", "content"]
        assert required["Edit"] == ["file_path", "old_string", "new_string"]
        assert required["Bash"] == ["command"]


class TestReadFile:
    """Read 工具"""

    def test_read_success(self, temp_dir):
        (temp_dir / "hello.txt").write_text("你好，世界", encoding="utf-8")
        result = ToolRegistry.execute("Read", {"file_path": "hello.txt"}, temp_dir)
        assert result["success"] is True
        assert result["output"] == "你好，世界"

    def test_read_missing_file(self, temp_dir):
        result = ToolRegistry.execute("Read", {"file_path": "nope.txt"}, temp_dir)
        assert result["success"] is False
        assert "文件不存在" in result["error"]

    def test_read_directory_fails(self, temp_dir):
        """读取目录返回读取失败"""
        (temp_dir / "subdir").mkdir()
        result = ToolRegistry.execute("Read", {"file_path": "subdir"}, temp_dir)
        assert result["success"] is False
        assert "读取失败" in result["error"]

    def test_read_nested_path(self, temp_dir):
        sub = temp_dir / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "c.txt").write_text("nested", encoding="utf-8")
        result = ToolRegistry.execute("Read", {"file_path": "a/b/c.txt"}, temp_dir)
        assert result["success"] is True
        assert result["output"] == "nested"


class TestWriteFile:
    """Write 工具"""

    def test_write_new_file(self, temp_dir):
        args = {"file_path": "out.txt", "content": "hello"}
        result = ToolRegistry.execute("Write", args, temp_dir)
        assert result["success"] is True
        assert "5 字符" in result["output"]
        assert (temp_dir / "out.txt").read_text(encoding="utf-8") == "hello"

    def test_write_creates_parent_dirs(self, temp_dir):
        args = {"file_path": "x/y/z.txt", "content": "deep"}
        result = ToolRegistry.execute("Write", args, temp_dir)
        assert result["success"] is True
        assert (temp_dir / "x" / "y" / "z.txt").read_text(encoding="utf-8") == "deep"

    def test_write_overwrites_existing(self, temp_dir):
        (temp_dir / "f.txt").write_text("old", encoding="utf-8")
        args = {"file_path": "f.txt", "content": "new"}
        result = ToolRegistry.execute("Write", args, temp_dir)
        assert result["success"] is True
        assert (temp_dir / "f.txt").read_text(encoding="utf-8") == "new"

    def test_write_error_when_parent_is_file(self, temp_dir):
        """父路径是文件时 mkdir 失败，返回错误而非抛异常"""
        (temp_dir / "blocker").write_text("x", encoding="utf-8")
        args = {"file_path": "blocker/sub.txt", "content": "y"}
        result = ToolRegistry.execute("Write", args, temp_dir)
        assert result["success"] is False
        assert "写入失败" in result["error"]


class TestEditFile:
    """Edit 工具"""

    def test_edit_success(self, temp_dir):
        (temp_dir / "f.txt").write_text("foo bar baz", encoding="utf-8")
        args = {"file_path": "f.txt", "old_string": "bar", "new_string": "qux"}
        result = ToolRegistry.execute("Edit", args, temp_dir)
        assert result["success"] is True
        assert (temp_dir / "f.txt").read_text(encoding="utf-8") == "foo qux baz"

    def test_edit_text_not_found(self, temp_dir):
        (temp_dir / "f.txt").write_text("foo bar", encoding="utf-8")
        args = {"file_path": "f.txt", "old_string": "missing", "new_string": "x"}
        result = ToolRegistry.execute("Edit", args, temp_dir)
        assert result["success"] is False
        assert "未在" in result["error"]
        # 文件未被修改
        assert (temp_dir / "f.txt").read_text(encoding="utf-8") == "foo bar"

    def test_edit_multiple_matches_rejected(self, temp_dir):
        """old_string 出现多次时要求更精确的匹配"""
        (temp_dir / "f.txt").write_text("aa bb aa", encoding="utf-8")
        args = {"file_path": "f.txt", "old_string": "aa", "new_string": "x"}
        result = ToolRegistry.execute("Edit", args, temp_dir)
        assert result["success"] is False
        assert "2 处匹配" in result["error"]

    def test_edit_missing_file(self, temp_dir):
        args = {"file_path": "nope.txt", "old_string": "a", "new_string": "b"}
        result = ToolRegistry.execute("Edit", args, temp_dir)
        assert result["success"] is False
        assert "文件不存在" in result["error"]

    def test_edit_multiline_replacement(self, temp_dir):
        (temp_dir / "f.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        args = {"file_path": "f.txt", "old_string": "line1\nline2", "new_string": "LINE"}
        result = ToolRegistry.execute("Edit", args, temp_dir)
        assert result["success"] is True
        assert (temp_dir / "f.txt").read_text(encoding="utf-8") == "LINE\nline3\n"


class TestBash:
    """Bash 工具"""

    def test_bash_allowed_command(self, temp_dir):
        (temp_dir / "hello.txt").write_text("content-xyz", encoding="utf-8")
        result = ToolRegistry.execute("Bash", {"command": "cat hello.txt"}, temp_dir)
        assert result["success"] is True
        assert "content-xyz" in result["output"]

    def test_bash_runs_in_worktree(self, temp_dir):
        """命令在 worktree 目录下执行"""
        (temp_dir / "marker.txt").write_text("x", encoding="utf-8")
        result = ToolRegistry.execute("Bash", {"command": "ls"}, temp_dir)
        assert result["success"] is True
        assert "marker.txt" in result["output"]

    def test_bash_nonzero_exit(self, temp_dir):
        result = ToolRegistry.execute("Bash", {"command": "ls no_such_dir_xyz"}, temp_dir)
        assert result["success"] is False
        # stderr 被合并进 output
        assert result["output"]

    @pytest.mark.parametrize("command", [
        "rm -rf /tmp/x",
        "rm file.txt",
        "mv a b",
        "cp a b",
        "sudo ls",
        "curl http://example.com",
        "wget http://example.com",
        "echo hi > out.txt",
        "echo hi >> out.txt",
        "ls | head",
        "ls; pwd",
        "ls && pwd",
        "ls || pwd",
        "git push origin main",
        "git remote add x url",
        "git config user.name x",
        "chmod 777 f",
    ])
    def test_bash_blocked_patterns(self, temp_dir, command):
        """危险命令被拦截"""
        result = ToolRegistry.execute("Bash", {"command": command}, temp_dir)
        assert result["success"] is False
        assert "禁止的命令" in result["error"]

    @pytest.mark.parametrize("command", [
        "echo warm ",
        'echo "rm is fine"',
        "ls warm_dir",
        "cat curl_notes.txt",
    ])
    def test_bash_harmless_commands_allowed(self, temp_dir, command):
        """包含拦截词子串的无害命令不被误伤（token 精确匹配）"""
        result = ToolRegistry.execute("Bash", {"command": command}, temp_dir)
        assert "禁止的命令" not in result.get("error", "")

    def test_bash_rm_as_bare_token_blocked(self, temp_dir):
        """rm 作为独立 token 出现即拦截（即使只是 echo 的参数，保守收紧）"""
        result = ToolRegistry.execute("Bash", {"command": "echo rm is fine"}, temp_dir)
        assert result["success"] is False
        assert "禁止的命令" in result["error"]

    def test_bash_command_not_found(self, temp_dir):
        result = ToolRegistry.execute(
            "Bash", {"command": "nonexistent_cmd_xyz_123 arg"}, temp_dir
        )
        assert result["success"] is False
        assert "命令未找到" in result["error"]

    def test_bash_timeout(self, temp_dir):
        """超时返回错误（mock subprocess.run 避免真实等待 60s）"""
        with patch(
            "agent_go.tool_executor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep", timeout=60),
        ):
            result = ToolRegistry.execute("Bash", {"command": "sleep 120"}, temp_dir)
        assert result["success"] is False
        assert "超时" in result["error"]

    def test_bash_output_truncated(self, temp_dir):
        """超长输出截断到 8000 字符"""
        big = "x" * 9000
        (temp_dir / "big.txt").write_text(big, encoding="utf-8")
        result = ToolRegistry.execute("Bash", {"command": "cat big.txt"}, temp_dir)
        assert result["success"] is True
        assert len(result["output"]) == 8000


class TestToolRegistryDispatch:
    """ToolRegistry.execute 分发逻辑"""

    def test_unknown_tool(self, temp_dir):
        result = ToolRegistry.execute("NoSuchTool", {}, temp_dir)
        assert result["success"] is False
        assert "未知工具: NoSuchTool" in result["error"]

    @pytest.mark.parametrize("tool_name,arguments,missing", [
        ("Read", {}, "file_path"),
        ("Write", {"file_path": "a.txt"}, "content"),
        ("Edit", {"file_path": "a.txt", "old_string": "x"}, "new_string"),
        ("Bash", {}, "command"),
    ])
    def test_missing_arguments_return_error(self, temp_dir, tool_name, arguments, missing):
        """LLM 产生缺参 tool_call 时返回错误 dict 而非抛 KeyError"""
        result = ToolRegistry.execute(tool_name, arguments, temp_dir)
        assert result["success"] is False
        assert "缺少参数" in result["error"]
        assert missing in result["error"]

    def test_dispatch_all_tools(self, temp_dir):
        """四个工具都能通过 execute 正常分发"""
        assert ToolRegistry.execute(
            "Write", {"file_path": "a.txt", "content": "1"}, temp_dir
        )["success"] is True
        assert ToolRegistry.execute(
            "Read", {"file_path": "a.txt"}, temp_dir
        )["output"] == "1"
        assert ToolRegistry.execute(
            "Edit", {"file_path": "a.txt", "old_string": "1", "new_string": "2"},
            temp_dir,
        )["success"] is True
        assert ToolRegistry.execute(
            "Bash", {"command": "cat a.txt"}, temp_dir
        )["output"] == "2"
