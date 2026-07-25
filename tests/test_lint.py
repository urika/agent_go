"""测试 AST 静态检查器 (agent_go.lint)。

覆盖场景：
  1. 对整个 agent_go 代码库运行检查 — 汇报发现但不阻断
  2. 已知 bug 模式（自包含代码片段）— 应被精确捕获
  3. 正常模式 — 不应误报
"""

import ast
from pathlib import Path

import pytest

from agent_go.lint import check_path, _check_suspicious_for_loops


# ═══════════════════════════════════════════════════════════════════════════════
# 代码库级检查（info 级别，不阻断 CI）
# ═══════════════════════════════════════════════════════════════════════════════

class TestLintDiscovery:
    """对整个代码库运行 AST 检查并汇报发现。

    这些测试会打印所有触发警告的位置，供人工 review 判断是否为真 bug。
    不阻断 CI —— 新引入的真 bug 由单元测试 (TestDetectSuspiciousForLoop) 防护。
    """

    def test_discover_suspicious_loops_in_agent_go(self, capsys):
        """扫描 agent_go/*.py 并打印所有可疑 for 循环位置。"""
        reports = check_path("agent_go")
        _print_reports("agent_go", reports, capsys)
        # 当前所有报告是已知误报（合法的 for 使用模式）。
        # 当新引入一个真 bug 时，单元测试层会拦截。

    def test_discover_suspicious_loops_in_tests(self, capsys):
        """扫描 tests/*.py 并打印。"""
        reports = check_path("tests")
        _print_reports("tests", reports, capsys)


def _print_reports(label: str, reports: list[dict], capsys) -> None:
    """打印报告到 stderr，便于在测试输出中查看。"""
    if not reports:
        print(f"\n[LINT] {label}: 未发现可疑 for 循环 ✓")
        return
    print(f"\n[LINT] {label}: 发现 {len(reports)} 个可疑 for 循环（请人工 review）")
    for r in reports:
        print(f"  {r['file']}:{r['line']} [{r['severity']}] {r['message']}")


# ═══════════════════════════════════════════════════════════════════════════════
# 单元测试：精确检测已知 bug 模式
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectSuspiciousForLoop:
    """验证检查器能识别各种缩进错误模式。"""

    # ── 应被捕获的模式 ────────────────────────────────────────────────────

    def test_for_as_completed_tiny_body(self):
        """for-as_completed 体只有 1 条语句 + 后续使用循环变量。"""
        code = """\
def run():
    futures = {}
    for fut in as_completed(futures):
        st = futures[fut]
    try:
        result = fut.result()
    except Exception:
        pass
    with meta_lock:
        worktree_map[st["id"]] = result
    completed_ids.add(st["id"])
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        assert any("st" in r["message"] for r in reports), (
            f"Expected report about 'st', got: {reports}"
        )

    def test_for_loop_with_two_body_stmts(self):
        """body 有 2 条语句也属于可疑范围。"""
        code = """\
def run():
    items = [1, 2]
    for x in items:
        y = x + 1
        z = y * 2
    print(y)
    print(z)
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        # 首条兄弟语句 print(y) 使用 y → 触发报告
        assert len(reports) == 1
        assert "y" in reports[0]["message"]

    def test_for_mixed_with_if(self):
        """for 后在 if 中使用循环变量。"""
        code = """\
for item in items:
    processed = item.strip()
if processed:
    print(processed)
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        assert any("processed" in r["message"] for r in reports)

    # ── 不应误报的模式 ────────────────────────────────────────────────────

    def test_normal_loop_many_body_stmts(self):
        """体大于 2 条语句的 for 不应触发。"""
        code = """\
for i in range(10):
    x = i * 2
    y = x + 1
    print(x, y)
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        assert len(reports) == 0

    def test_loop_without_assignments(self):
        """body 无赋值的 for 不应触发（没有可泄露的变量）。"""
        code = """\
for _ in range(5):
    print("hello")
print("done")
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        assert len(reports) == 0

    def test_many_siblings_no_var_usage(self):
        """后续语句不使用循环变量时不应触发。"""
        code = """\
for name in names:
    first = name.split()[0]
print("finished")
x = 42
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        assert len(reports) == 0

    def test_nested_loop_not_confused(self):
        """嵌套 for 不应互相干扰。"""
        code = """\
for i in range(10):
    x = i * 2
    for j in range(5):
        y = j + x
    print(y)
"""
        tree = ast.parse(code)
        reports = _check_suspicious_for_loops(tree, "<test>")
        # 外层 for (i, x) 有 4 条语句 → 不触发
        # 内层 for (j, y) 有 1 条语句，print(y) 用 y → 触发
        assert len(reports) == 1
