"""测试 AST 静态检查器 (agent_go.lint)。

覆盖场景：
  1. 对整个 agent_go 代码库运行检查 — 严格断言零警告（CI 防护）
  2. 已知 bug 模式（自包含代码片段）— 应被精确捕获
  3. 正常模式 — 不应误报
"""

import ast
import textwrap
from pathlib import Path

import pytest

from agent_go.lint import check_path, _check_suspicious_for_loops


# ═══════════════════════════════════════════════════════════════════════════════
# 代码库级检查 — 严格断言（误报已通过 break 排除 + 阈值过滤清零）
# ═══════════════════════════════════════════════════════════════════════════════

class TestLintOnCodebase:
    """对整个代码库运行 AST 检查，确保零警告。

    若引入新的真 bug，此测试会失败 —— 这是 CI 防护的核心。
    """

    def test_no_suspicious_for_loops_in_agent_go(self):
        """所有 agent_go/*.py 不应触发 suspicious-for-loop 警告。"""
        reports = check_path("agent_go")
        if reports:
            lines = "\n".join(
                f"  {r['file']}:{r['line']} [{r['severity']}] {r['message']}"
                for r in reports
            )
            pytest.fail(f"Suspicious for-loop patterns found:\n{lines}")

    def test_no_suspicious_for_loops_in_tests(self):
        """tests/*.py 也不应有警告。"""
        reports = check_path("tests")
        if reports:
            lines = "\n".join(
                f"  {r['file']}:{r['line']} [{r['severity']}] {r['message']}"
                for r in reports
            )
            pytest.fail(f"Suspicious for-loop patterns found in tests:\n{lines}")


# ═══════════════════════════════════════════════════════════════════════════════
# 单元测试：精确检测已知 bug 模式
# ═══════════════════════════════════════════════════════════════════════════════

def _check(code: str) -> list[dict]:
    """Helper: dedent + parse + check a code snippet."""
    tree = ast.parse(textwrap.dedent(code))
    return _check_suspicious_for_loops(tree, "<test>")


class TestDetectSuspiciousForLoop:
    """验证检查器能识别各种缩进错误模式。"""

    # ── 应被捕获的模式 ────────────────────────────────────────────────────

    def test_original_bug_pattern(self):
        """精确复现本次内存泄露 bug：try/with 在 ThreadPoolExecutor 块外。"""
        code = """\
            def run(wave, parallel):
                if parallel > 1:
                    with ThreadPoolExecutor(max_workers=parallel) as executor:
                        futures = {}
                        for st in wave:
                            fut = executor.submit(run_subtask, st)
                            futures[fut] = st
                        for fut in as_completed(futures):
                            st = futures[fut]
                    try:
                        result = fut.result()
                    except Exception:
                        result = {}
                    with meta_lock:
                        worktree_map[st["id"]] = result
                    completed_ids.add(st["id"])
                    if result.get("status") == "failed":
                        failed_ids.add(st["id"])
        """
        reports = _check(code)
        assert len(reports) == 1
        assert "st" in reports[0]["message"]

    def test_direct_sibling_usage(self):
        """for 后多个兄弟语句使用循环变量（无嵌套作用域）。"""
        code = """\
            def run():
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
        reports = _check(code)
        assert len(reports) == 1
        assert "st" in reports[0]["message"]

    # ── 不应误报的模式 ────────────────────────────────────────────────────

    def test_normal_loop_many_body_stmts(self):
        """体大于 2 条语句的 for 不应触发。"""
        code = """\
            for i in range(10):
                x = i * 2
                y = x + 1
                print(x, y)
            print(x, y)
        """
        assert len(_check(code)) == 0

    def test_loop_without_assignments(self):
        """body 无赋值的 for 不应触发。"""
        code = """\
            for _ in range(5):
                print("hello")
            print("done")
        """
        assert len(_check(code)) == 0

    def test_search_and_break_pattern(self):
        """搜索循环（含 break）后使用结果是合法模式。"""
        code = """\
            worktree_path = None
            for r in results:
                if r.get("status") == "completed":
                    worktree_path = r["worktree"]
                    break
            if not worktree_path:
                return None
            diff = read_diff(worktree_path)
            process(diff, worktree_path)
        """
        assert len(_check(code)) == 0

    def test_single_sibling_usage(self):
        """变量只在 1 个兄弟语句使用 → 累积模式，不报。"""
        code = """\
            total = 0
            for x in items:
                total += x
            print(total)
        """
        assert len(_check(code)) == 0

    def test_two_sibling_usage_below_threshold(self):
        """变量在 2 个兄弟语句使用 → 仍低于阈值 3，不报。"""
        code = """\
            for s in confirmed:
                r = results_map.get(s["id"])
                if r:
                    print(r)
            print("---")
            print("done")
        """
        assert len(_check(code)) == 0

    def test_nested_loop_correct_inner_usage(self):
        """嵌套 for 内层正确使用循环变量。"""
        code = """\
            for i in range(10):
                x = i * 2
                for j in range(5):
                    y = j + x
                print(y)
        """
        # 内层 for 体只有 1 条语句,但 print(y) 只用 1 次 → 不报
        assert len(_check(code)) == 0
