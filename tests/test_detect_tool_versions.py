"""测试 _detect_tool_versions — 检测 claude / greywall 版本"""

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.utils import _detect_tool_versions


class TestDetectToolVersions:
    """_detect_tool_versions 版本检测测试"""

    def test_both_found(self, logger):
        """claude 和 greywall 都已安装"""
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = args[0]
                if cmd == "claude":
                    m.returncode = 0
                    m.stdout = "Claude Code v2.1.0\n"
                elif cmd == "greywall":
                    m.returncode = 0
                    m.stdout = "greywall 0.9.5\n"
                else:
                    m.returncode = 1
                return m
            mock_run.side_effect = side_effect

            versions = _detect_tool_versions(logger)
            assert "claude" in versions
            assert "greywall" in versions
            assert versions["claude"] == "Claude Code v2.1.0"
            assert versions["greywall"] == "greywall 0.9.5"

    def test_only_claude(self, logger):
        """仅 claude 已安装"""
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = args[0]
                if cmd == "claude":
                    m.returncode = 0
                    m.stdout = "Claude Code v1.5.0\nextra line\n"
                else:
                    raise FileNotFoundError("greywall not found")
                return m
            mock_run.side_effect = side_effect

            versions = _detect_tool_versions(logger)
            assert "claude" in versions
            assert "greywall" not in versions

    def test_none_installed(self, logger):
        """两者都未安装"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("not found")

            versions = _detect_tool_versions(logger)
            assert versions == {}

    def test_failure_exit_code(self, logger):
        """--version 返回非 0 退出码"""
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                m.returncode = 1
                m.stdout = ""
                return m
            mock_run.side_effect = side_effect

            versions = _detect_tool_versions(logger)
            assert versions == {}

    def test_timeout(self, logger):
        """subprocess 超时"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 10)

            versions = _detect_tool_versions(logger)
            assert "claude" not in versions or versions == {}

    def test_generic_exception(self, logger):
        """其他异常不中断检测"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("spawn failed")

            versions = _detect_tool_versions(logger)
            assert versions == {}

    def test_returns_dict(self, logger):
        """总是返回 dict"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError

            versions = _detect_tool_versions(logger)
            assert isinstance(versions, dict)

    def test_multi_line_output_truncation(self, logger):
        """多行输出仅取首行，且截断到 100 字符"""
        with patch("subprocess.run") as mock_run:
            m = MagicMock()
            m.returncode = 0
            m.stdout = "A" * 200 + "\nsecond line\nthird line"
            mock_run.return_value = m

            versions = _detect_tool_versions(logger)
            assert len(versions["claude"]) <= 100
            assert "\n" not in versions["claude"]
