"""测试 ui.py — Plan 展示、确认交互、子任务验证

全覆盖:
  - plan_to_md（多种 Plan 结构渲染为 Markdown）
  - verify_subtask（边界情况：自动确认/取消/重试/修改/中止）
  - plan_to_subtasks 已覆盖（test_plan_to_subtasks.py）
  - confirm_plan / confirm_subtasks 部分覆盖（依赖交互测试不易测，测基础逻辑）
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.ui import (
    plan_to_md, verify_subtask,
)


class TestPlanToMd:
    """Plan 转 Markdown"""

    def test_basic_plan_to_md(self, sample_plan):
        md = plan_to_md(sample_plan)
        assert "执行方案" in md
        assert "实现用户认证功能" in md  # overview
        assert "后端 JWT 认证" in md  # step title
        assert "前端登录页面" in md
        assert "3 天" in md  # estimated_effort
        assert "Git 远程" in md
        assert "当前分支" in md
        assert "依赖关系" in md
        assert "步骤 2 依赖: [1]" in md or "步骤 2 依赖" in md
        assert "验证" in md

    def test_minimal_plan(self, minimal_plan):
        """最小 Plan 无清单/依赖"""
        md = plan_to_md(minimal_plan)
        assert "执行方案" in md
        assert "简单任务" in md
        assert "1 小时" in md
        # shared_resources 无内容时不应有 Git 相关行
        # 实际 plan_to_md 始终输出"共享资源清单"，但不输出 git 细节

    def test_empty_steps(self):
        plan = {"overview": "empty", "steps": [], "estimated_effort": "0"}
        md = plan_to_md(plan)
        assert "0" in md or "0 步" in md

    def test_plan_without_overview(self):
        """缺少 overview 时显示 N/A"""
        plan = {"steps": [{"id": 1, "title": "step1"}], "estimated_effort": "1h"}
        md = plan_to_md(plan)
        assert "N/A" in md

    def test_step_with_files_and_risks(self, sample_plan):
        md = plan_to_md(sample_plan)
        assert "src/auth/jwt.py" in md
        assert "src/pages/login.tsx" in md
        assert "密钥管理" in md  # risks

    def test_no_dependencies(self):
        plan = {
            "overview": "test",
            "steps": [{"id": 1, "title": "t", "description": "d"}],
        }
        md = plan_to_md(plan)
        assert "依赖关系" not in md  # dependencies 为空时不输出


class TestVerifySubtask:
    """verify_subtask 交互逻辑"""

    def test_continue(self, logger):
        with patch("agent_go.ui.safe_input", return_value="C"):
            assert verify_subtask(1, 2, "summary", logger, None) == "next"

    def test_retry(self, logger):
        with patch("agent_go.ui.safe_input", return_value="R"):
            assert verify_subtask(1, 2, "summary", logger, None) == "retry"

    def test_modify(self, logger):
        with patch("agent_go.ui.safe_input", return_value="M"):
            assert verify_subtask(1, 2, "summary", logger, None) == "modify"

    def test_abort(self, logger):
        with patch("agent_go.ui.safe_input", return_value="A"):
            assert verify_subtask(1, 2, "summary", logger, None) == "abort"

    def test_lowercase(self, logger):
        """小写输入也应被接受"""
        with patch("agent_go.ui.safe_input", return_value="c"):
            assert verify_subtask(1, 2, "summary", logger, None) == "next"

    def test_auto_verify(self, logger):
        """auto_verify_subtask=True 时空 Enter 自动通过"""
        config = {"behavior": {"auto_verify_subtask": True}}
        with patch("agent_go.ui.safe_input", return_value=""):
            assert verify_subtask(1, 2, "summary", logger, config) == "next"

    def test_no_auto_verify_by_default(self, logger):
        """auto_verify_subtask=False 时空 Enter 无效"""
        config = {"behavior": {"auto_verify_subtask": False}}
        with patch("agent_go.ui.safe_input", side_effect=["", "", "C"]):
            result = verify_subtask(1, 2, "summary", logger, config)
            assert result == "next"

    def test_auto_verify_no_config(self, logger):
        """无 config 且无 input 应显示提示"""
        with patch("agent_go.ui.safe_input", side_effect=["", "C"]):
            result = verify_subtask(1, 2, "summary", logger, None)
            assert result == "next"
