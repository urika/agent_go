"""Ground-truth tests for src/cli.py cmd_list (fix-missing-default task).

These assertions target the specific behaviors the task requires:
1. Default signature value is "all" (not "").
2. cmd_list(None) is handled defensively (equivalent to default "all").
3. cmd_list("todo") filters output to todo tasks only.

The output-behavior assertions are data-independent: they check the
relationship between calls, not absolute task counts.
"""

import io
import inspect
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cli import cmd_list


def test_default_signature_value_is_all():
    """cmd_list 的 status 默认值应为 "all"（而非 ""）。"""
    sig = inspect.signature(cmd_list)
    assert sig.parameters["status"].default == "all"


def test_none_equivalent_to_default():
    """cmd_list(None) 应被防御处理，行为等同默认 all（显示全部任务）。"""
    buf_none = io.StringIO()
    with redirect_stdout(buf_none):
        cmd_list(None)
    buf_all = io.StringIO()
    with redirect_stdout(buf_all):
        cmd_list()
    assert buf_none.getvalue() == buf_all.getvalue()


def test_todo_filters_output():
    """cmd_list("todo") 应只输出 todo 状态的任务。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_list("todo")
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    assert lines, "todo 过滤应至少输出一个任务"
    assert all("[todo]" in l for l in lines), f"list todo 应只输出 todo 任务: {lines[:3]}"
