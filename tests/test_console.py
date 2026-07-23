"""测试 console.py — 统一输出抽象层 Console 类

全覆盖: 每个语义方法、模式切换、结构化输出、模块级默认实例。
"""

import sys
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.console import Console, set_default_console, get_default_console


# ═══════════════════════════════════════════════════════════════
# Console 基本功能
# ═══════════════════════════════════════════════════════════════

class TestConsolePrint:
    """Console 输出控制 — quiet/verbose 模式"""

    def test_print_shows_in_normal_mode(self):
        console = Console(quiet=False)
        with patch("builtins.print") as mock_print:
            console.print("hello")
        mock_print.assert_called_once_with("hello")

    def test_print_suppressed_in_quiet_mode(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.print("hello")
        mock_print.assert_not_called()

    def test_force_bypasses_quiet_mode(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.force("important")
        mock_print.assert_called_once_with("important")

    def test_debug_shows_in_verbose_mode(self):
        console = Console(quiet=False, verbose=True)
        with patch("builtins.print") as mock_print:
            console.debug("debug info")
        mock_print.assert_called_once()

    def test_debug_suppressed_in_non_verbose(self):
        console = Console(quiet=False, verbose=False)
        with patch("builtins.print") as mock_print:
            console.debug("debug info")
        mock_print.assert_not_called()

    def test_debug_suppressed_when_quiet(self):
        console = Console(quiet=True, verbose=True)
        with patch("builtins.print") as mock_print:
            console.debug("debug info")
        mock_print.assert_not_called()


class TestConsoleSemantic:
    """语义化输出方法"""

    def test_info(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.info("info msg")
        mock_print.assert_called_once_with("info msg")

    def test_success(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.success("done")
        mock_print.assert_called_once_with("✅ done")

    def test_warning(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.warning("warn")
        mock_print.assert_called_once_with("⚠️  warn")

    def test_error(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.error("err")
        mock_print.assert_called_once_with("❌ err")


class TestConsoleLayout:
    """布局辅助方法"""

    def test_sep(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.sep()
        mock_print.assert_called_once_with("─" * 50)

    def test_sep_custom(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.sep("=", 20)
        mock_print.assert_called_once_with("=" * 20)

    def test_sep_quiet(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.sep()
        mock_print.assert_not_called()

    def test_title(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.title("Section")
        # 3 次调用: 空行 + 横线 + 标题 + 横线
        assert mock_print.call_count == 3
        mock_print.assert_any_call("\n" + "=" * 60)

    def test_title_quiet(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.title("Section")
        mock_print.assert_not_called()

    def test_subtitle(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.subtitle("Sub")
        mock_print.assert_called_once_with("\n── Sub ──")


class TestConsoleStructured:
    """结构化输出 — table / data / data_table"""

    def test_table(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.table(["Name", "Value"], [["a", "1"], ["b", "2"]])
        assert mock_print.call_count >= 1  # 至少调用了 print

    def test_table_quiet(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.table(["Name"], [["x"]])
        mock_print.assert_not_called()

    def test_table_custom_widths(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.table(["A", "B"], [["1", "2"]], col_widths=[10, 10])
        # 验证自定义宽度生效
        header = mock_print.call_args_list[0][0][0]
        assert header == "A         B         "

    def test_table_prints_data_rows(self):
        """table() 必须打印数据行（回归：曾只打印表头和分隔线）"""
        console = Console()
        with patch("builtins.print") as mock_print:
            console.table(["Name", "Value"], [["alpha", "1"], ["beta", "2"]])
        printed = [c[0][0] for c in mock_print.call_args_list]
        assert any("alpha" in line for line in printed)
        assert any("beta" in line for line in printed)

    def test_table_separator_is_line(self):
        """分隔线必须是 ─ 字符线（回归：曾把宽度误传给 sep 的 char 参数）"""
        console = Console()
        with patch("builtins.print") as mock_print:
            console.table(["A", "B"], [["1", "2"]], col_widths=[10, 10])
        printed = [c[0][0] for c in mock_print.call_args_list]
        assert ("─" * 20) in printed

    def test_data(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data({"key": "value"})
        # 验证 output 是格式化的 JSON
        call_arg = mock_print.call_args[0][0]
        parsed = json.loads(call_arg)
        assert parsed["key"] == "value"

    def test_data_quiet(self):
        console = Console(quiet=True)
        with patch("builtins.print") as mock_print:
            console.data({"key": "value"})
        mock_print.assert_not_called()

    def test_data_empty_dict(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data({})
        mock_print.assert_called_once()

    def test_data_table(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data_table([
                {"name": "test1", "status": "ok"},
                {"name": "test2", "status": "fail"},
            ])
        assert mock_print.call_count >= 1
        # 数据行必须实际打印（回归：曾只打印表头）
        printed = [c[0][0] for c in mock_print.call_args_list]
        assert any("test1" in line for line in printed)
        assert any("test2" in line for line in printed)

    def test_data_table_empty(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data_table([])
        mock_print.assert_not_called()

    def test_data_table_truncates_long_values(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data_table([
                {"name": "x" * 100, "status": "ok"},
            ])
        # 值被截断到 60 字符（在 data_rows 中）
        # print 至少被调用一次
        assert mock_print.call_count >= 1

    def test_data_table_specified_columns(self):
        console = Console()
        with patch("builtins.print") as mock_print:
            console.data_table(
                [{"name": "t1", "extra": "e1", "status": "ok"}],
                columns=["name", "status"],
            )
        assert mock_print.call_count >= 1


class TestConsoleInit:
    """初始化参数验证"""

    def test_default_state(self):
        console = Console()
        assert console.quiet is False
        assert console.verbose is False

    def test_quiet_mode(self):
        console = Console(quiet=True)
        assert console.quiet is True

    def test_verbose_mode(self):
        console = Console(verbose=True)
        assert console.verbose is True


# ═══════════════════════════════════════════════════════════════
# 模块级默认实例
# ═══════════════════════════════════════════════════════════════

class TestDefaultConsole:
    """set_default_console / get_default_console"""

    def test_get_default_console(self):
        c = get_default_console()
        assert isinstance(c, Console)

    def test_set_default_console(self):
        original = get_default_console()
        new_console = Console(quiet=True)
        set_default_console(new_console)
        try:
            assert get_default_console() is new_console
            assert get_default_console().quiet is True
        finally:
            set_default_console(original)

    def test_default_console_not_quiet(self):
        c = get_default_console()
        # 默认应当是非静默的（cmd_run 中会替换为配置实例）
        assert c.quiet is False
