"""测试 ui.py — Plan 展示、确认交互、子任务验证

全覆盖:
  - plan_to_md（多种 Plan 结构渲染为 Markdown）
  - verify_subtask（边界情况：自动确认/取消/重试/修改/中止）
  - _estimate_duration（串行/并行/环依赖的时间估算）
  - confirm_plan（Y/S/D/E/R/N 各交互分支，mock safe_input 喂序列）
  - confirm_subtasks（Y/N/E/A/D 各交互分支）
  - plan_to_subtasks 已覆盖（test_plan_to_subtasks.py）
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.config import DEFAULT_CONFIG
from agent_go.ui import (
    plan_to_md, verify_subtask, confirm_plan, confirm_subtasks,
    _estimate_duration,
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
        assert "depends_on: 1" in md or "依赖关系" in md
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


def _make_config(**behavior_overrides):
    """DEFAULT_CONFIG 深拷贝后覆盖 behavior 字段。"""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["behavior"].update(behavior_overrides)
    return config


def _make_subtasks():
    """两个典型子任务。"""
    return [
        {"id": "sub-1", "title": "任务一", "description": "描述一",
         "files_hint": "a.py", "agent_prompt": "提示一"},
        {"id": "sub-2", "title": "任务二", "description": "描述二",
         "files_hint": "b.py", "agent_prompt": "提示二"},
    ]


class TestEstimateDuration:
    """_estimate_duration 时间估算逻辑"""

    def test_empty_steps_returns_na(self):
        assert _estimate_duration({"steps": []}) == "N/A"
        assert _estimate_duration({}) == "N/A"

    def test_serial_single_step(self):
        """串行单步: 240s = 4 分钟 → 约 3-4 分钟（0.8/1.2 区间）"""
        plan = {"steps": [{"id": 1, "title": "t"}]}
        assert _estimate_duration(plan, parallel=1) == "约 3-4 分钟"

    def test_serial_multiple_steps(self):
        """串行 3 步: 720s = 12 分钟 → 约 9-14 分钟"""
        plan = {"steps": [{"id": i, "title": "t"} for i in (1, 2, 3)]}
        assert _estimate_duration(plan, parallel=1) == "约 9-14 分钟"

    def test_parallel_with_dependency(self, sample_plan):
        """并行时按拓扑层数: 2 步有依赖 → 2 层 = 480s = 8 分钟"""
        assert _estimate_duration(sample_plan, parallel=3) == "约 6-9 分钟"

    def test_parallel_independent_steps(self):
        """并行无依赖: 2 步同层 → 1 层 = 240s"""
        plan = {"steps": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]}
        assert _estimate_duration(plan, parallel=2) == "约 3-4 分钟"

    def test_parallel_chain_of_three(self):
        """1→2→3 链式依赖 → 3 层 = 720s，与串行相同"""
        plan = {
            "steps": [{"id": i, "title": "t"} for i in (1, 2, 3)],
            "dependencies": {"2": [1], "3": [2]},
        }
        assert _estimate_duration(plan, parallel=4) == "约 9-14 分钟"

    def test_parallel_circular_dependency(self):
        """环依赖无法拓扑排序 → 剩余步骤按串行计入 waves"""
        plan = {
            "steps": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}],
            "dependencies": {"1": [2], "2": [1]},
        }
        assert _estimate_duration(plan, parallel=2) == "约 6-9 分钟"

    def test_unknown_dependency_ids_ignored(self):
        """依赖引用不存在的步骤 id 时被忽略"""
        plan = {
            "steps": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}],
            "dependencies": {"1": [99], "99": [1]},
        }
        assert _estimate_duration(plan, parallel=2) == "约 3-4 分钟"

    def test_mixed_int_and_str_dep_ids(self):
        """依赖里 int/str 混合的 id 统一按 str 匹配"""
        plan = {
            "steps": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}],
            "dependencies": {"2": ["1"]},
        }
        assert _estimate_duration(plan, parallel=2) == "约 6-9 分钟"

    def test_unified_range_no_seconds_branch(self):
        """回归：秒级分支已删除，单步最短 240s 也走 0.8/1.2 分钟区间"""
        plan = {"steps": [{"id": 1, "title": "t"}]}
        result = _estimate_duration(plan, parallel=1)
        assert result == "约 3-4 分钟"
        assert "秒" not in result


class TestConfirmPlan:
    """confirm_plan 交互分支（mock safe_input 喂输入序列）"""

    def test_y_returns_plan(self, sample_plan, tmp_path, logger):
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["Y"]):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is sample_plan
        assert docs == []

    def test_n_exits(self, sample_plan, tmp_path, logger):
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["N"]):
            with pytest.raises(SystemExit) as exc:
                confirm_plan(sample_plan, config, tmp_path, logger)
        assert exc.value.code == 0

    def test_r_returns_none(self, sample_plan, tmp_path, logger):
        """R 请求重新生成：返回 (None, doc_paths)，重试上限由 cli.py 的
        max_plan_iterations 控制，confirm_plan 本身不做限制"""
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["R"]):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is None
        assert docs == []

    def test_auto_confirm_enter(self, sample_plan, tmp_path, logger):
        """auto_confirm_plan=True 时空 Enter 直接确认"""
        config = _make_config(auto_confirm_plan=True)
        with patch("agent_go.ui.safe_input", side_effect=[""]):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is sample_plan

    def test_auto_confirm_any_key_enters_interactive(self, sample_plan, tmp_path, logger):
        """auto_confirm 模式下输入任意键进入交互，再选 Y"""
        config = _make_config(auto_confirm_plan=True)
        with patch("agent_go.ui.safe_input", side_effect=["x", "Y"]):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is sample_plan

    def test_interactive_env_overrides_auto_confirm(self, sample_plan, tmp_path, logger, monkeypatch):
        """AGENT_GO_INTERACTIVE=1 强制交互，跳过默认同意快捷提示"""
        monkeypatch.setenv("AGENT_GO_INTERACTIVE", "1")
        config = _make_config(auto_confirm_plan=True)
        # 若未强制交互，第一个输入 "" 会被当作快捷确认
        with patch("agent_go.ui.safe_input", side_effect=["Y"]):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is sample_plan

    def test_auto_confirm_skipped_on_later_iterations(self, sample_plan, tmp_path, logger):
        """iteration>1 时不再有快捷确认提示，空输入仍按 auto_confirm 确认"""
        config = _make_config(auto_confirm_plan=True)
        with patch("agent_go.ui.safe_input", side_effect=[""]):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger, iteration=2)
        assert plan is sample_plan

    def test_edit_step(self, sample_plan, tmp_path, logger):
        """E 编辑步骤：可改标题/描述/文件/Agent Prompt"""
        config = _make_config()
        inputs = ["E", "1", "新标题", "新描述", "x.py, y.py", "新提示", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        step = plan["steps"][0]
        assert step["title"] == "新标题"
        assert step["description"] == "新描述"
        assert step["files"] == ["x.py", "y.py"]
        assert step["agent_prompt"] == "新提示"
        # 第二个步骤不受影响
        assert plan["steps"][1]["title"] == "前端登录页面"

    def test_edit_step_empty_fields_keep_original(self, sample_plan, tmp_path, logger):
        """E 编辑时全部回车保留原值"""
        config = _make_config()
        original = dict(sample_plan["steps"][0])
        with patch("agent_go.ui.safe_input", side_effect=["E", "1", "", "", "", "", "Y"]):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan["steps"][0] == original

    def test_edit_step_invalid_index(self, sample_plan, tmp_path, logger):
        """E 越界/非数字索引不修改任何步骤"""
        config = _make_config()
        original_titles = [s["title"] for s in sample_plan["steps"]]
        with patch("agent_go.ui.safe_input", side_effect=["E", "99", "E", "abc", "Y"]):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert [s["title"] for s in plan["steps"]] == original_titles

    def test_supplement_regenerates(self, sample_plan, tmp_path, logger):
        """S 补充输入：两个连续空行结束，调用 generate_plan 重新生成"""
        config = _make_config()
        new_plan = {"overview": "新方案", "steps": [{"id": 1, "title": "t", "description": "d"}]}
        with patch("agent_go.ui.safe_input", side_effect=["S", "加一条需求", "", "", "Y"]), \
             patch("agent_go.ui.generate_plan", return_value=new_plan) as mock_gen:
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger, task="原始任务")
        assert mock_gen.call_count == 1
        # supplement 作为第 5 个位置参数传入
        assert mock_gen.call_args[0][4] == "加一条需求"
        assert plan is new_plan
        assert plan["_original_task"] == "原始任务"

    def test_supplement_empty_skips_regeneration(self, sample_plan, tmp_path, logger):
        """S 补充为空时不重新生成"""
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["S", "", "", "Y"]), \
             patch("agent_go.ui.generate_plan") as mock_gen:
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        mock_gen.assert_not_called()
        assert plan is sample_plan

    def test_supplement_api_failure_then_success(self, sample_plan, tmp_path, logger):
        """S 重新生成失败一次后重试成功"""
        config = _make_config()
        new_plan = {"overview": "新方案", "steps": [{"id": 1, "title": "t", "description": "d"}]}
        inputs = ["S", "需求1", "", "", "S", "需求2", "", "", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.generate_plan", side_effect=[Exception("API 挂了"), new_plan]):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is new_plan

    def test_supplement_repeated_failure_fallback(self, sample_plan, tmp_path, logger):
        """S 连续失败 2 次触发降级提示，选 F 返回 __FALLBACK__"""
        config = _make_config()
        inputs = ["S", "x", "", "", "S", "x", "", "", "F"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.generate_plan", side_effect=Exception("API 挂了")):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan == "__FALLBACK__"
        assert docs is None

    def test_fallback_prompt_retry_resets_counter(self, sample_plan, tmp_path, logger):
        """降级提示选 R 重置失败计数，回到主菜单"""
        config = _make_config()
        inputs = ["S", "x", "", "", "S", "x", "", "", "R", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.generate_plan", side_effect=Exception("API 挂了")) as mock_gen:
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert mock_gen.call_count == 2
        assert plan is sample_plan

    def test_fallback_prompt_invalid_then_valid(self, sample_plan, tmp_path, logger):
        """降级提示接受无效输入后循环，直到有效选项"""
        config = _make_config()
        inputs = ["S", "x", "", "", "S", "x", "", "", "X", "F"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.generate_plan", side_effect=Exception("API 挂了")):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan == "__FALLBACK__"

    def test_fallback_prompt_n_exits(self, sample_plan, tmp_path, logger):
        """降级提示选 N 取消任务"""
        config = _make_config()
        inputs = ["S", "x", "", "", "S", "x", "", "", "N"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.generate_plan", side_effect=Exception("API 挂了")):
            with pytest.raises(SystemExit) as exc:
                confirm_plan(sample_plan, config, tmp_path, logger)
        assert exc.value.code == 0

    def test_mount_docs_regenerates(self, sample_plan, tmp_path, logger):
        """D 挂载参考文档：读取内容后调用 generate_plan 重新生成"""
        config = _make_config()
        new_plan = {"overview": "带文档方案", "steps": [{"id": 1, "title": "t", "description": "d"}]}
        with patch("agent_go.ui.safe_input", side_effect=["D", "docs/a.md, docs/b.md", "Y"]), \
             patch("agent_go.ui.read_reference_docs", return_value="文档内容") as mock_read, \
             patch("agent_go.ui.generate_plan", return_value=new_plan) as mock_gen:
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert mock_read.call_args[0][0] == ["docs/a.md", "docs/b.md"]
        # docs_content 作为第 6 个位置参数传入
        assert mock_gen.call_args[0][5] == "文档内容"
        assert plan is new_plan
        assert docs == ["docs/a.md", "docs/b.md"]

    def test_mount_docs_dedup_paths(self, sample_plan, tmp_path, logger):
        """D 多次挂载时路径去重"""
        config = _make_config()
        new_plan = {"overview": "新方案", "steps": [{"id": 1, "title": "t", "description": "d"}]}
        inputs = ["D", "a.md", "D", "a.md, b.md", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs), \
             patch("agent_go.ui.read_reference_docs", return_value="内容"), \
             patch("agent_go.ui.generate_plan", return_value=new_plan):
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        assert docs == ["a.md", "b.md"]

    def test_mount_docs_empty_input_continues(self, sample_plan, tmp_path, logger):
        """D 空输入直接返回主菜单"""
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["D", "", "Y"]), \
             patch("agent_go.ui.generate_plan") as mock_gen:
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        mock_gen.assert_not_called()
        assert plan is sample_plan
        assert docs == []

    def test_mount_docs_no_valid_content_continues(self, sample_plan, tmp_path, logger):
        """D 读不到有效文档时不重新生成"""
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=["D", "missing.md", "Y"]), \
             patch("agent_go.ui.read_reference_docs", return_value=""), \
             patch("agent_go.ui.generate_plan") as mock_gen:
            plan, docs = confirm_plan(sample_plan, config, tmp_path, logger)
        mock_gen.assert_not_called()
        assert plan is sample_plan
        # 路径仍被记录（供下次重新生成使用）
        assert docs == ["missing.md"]

    def test_empty_input_six_times_exits(self, sample_plan, tmp_path, logger):
        """连续 6 次空输入判定为非交互模式，退出码 EX_USAGE(2)"""
        config = _make_config()
        with patch("agent_go.ui.safe_input", side_effect=[""] * 6):
            with pytest.raises(SystemExit) as exc:
                confirm_plan(sample_plan, config, tmp_path, logger)
        assert exc.value.code == 2

    def test_invalid_choice_resets_empty_count(self, sample_plan, tmp_path, logger):
        """无效选项重置空输入计数，不触发退出"""
        config = _make_config()
        inputs = ["", "", "", "", "", "Z", "", "", "", "", "", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs):
            plan, _ = confirm_plan(sample_plan, config, tmp_path, logger)
        assert plan is sample_plan


class TestConfirmSubtasks:
    """confirm_subtasks 交互分支"""

    def test_y_returns_subtasks(self, logger):
        subtasks = _make_subtasks()
        with patch("agent_go.ui.safe_input", side_effect=["Y"]):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert result is subtasks

    def test_n_exits(self, logger):
        with patch("agent_go.ui.safe_input", side_effect=["N"]):
            with pytest.raises(SystemExit) as exc:
                confirm_subtasks(_make_subtasks(), _make_config(), logger)
        assert exc.value.code == 0

    def test_edit_subtask(self, logger):
        """E 编辑：非空字段覆盖，空字段保留"""
        subtasks = _make_subtasks()
        inputs = ["E", "1", "新标题", "", "", "新提示", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert result[0]["title"] == "新标题"
        assert result[0]["description"] == "描述一"  # 空输入保留
        assert result[0]["files_hint"] == "a.py"
        assert result[0]["agent_prompt"] == "新提示"
        assert result[1]["title"] == "任务二"

    def test_edit_subtask_invalid_index(self, logger):
        subtasks = _make_subtasks()
        with patch("agent_go.ui.safe_input", side_effect=["E", "99", "E", "abc", "Y"]):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert [s["title"] for s in result] == ["任务一", "任务二"]

    def test_add_subtask(self, logger):
        """A 添加新子任务，id 按序号生成"""
        subtasks = _make_subtasks()
        inputs = ["A", "新任务", "新描述", "c.py", "新提示", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert len(result) == 3
        assert result[2] == {
            "id": "sub-3", "title": "新任务", "description": "新描述",
            "files_hint": "c.py", "agent_prompt": "新提示",
        }

    def test_delete_subtask_renumbers_ids(self, logger):
        """D 删除后剩余子任务 id 重排序"""
        subtasks = _make_subtasks()
        with patch("agent_go.ui.safe_input", side_effect=["D", "1", "Y"]):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert len(result) == 1
        assert result[0]["id"] == "sub-1"
        assert result[0]["title"] == "任务二"

    def test_delete_subtask_invalid_index(self, logger):
        subtasks = _make_subtasks()
        with patch("agent_go.ui.safe_input", side_effect=["D", "0", "D", "x", "Y"]):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert len(result) == 2

    def test_auto_confirm_enter(self, logger):
        """auto_confirm_subtasks=True 时空 Enter 直接执行"""
        subtasks = _make_subtasks()
        config = _make_config(auto_confirm_subtasks=True)
        with patch("agent_go.ui.safe_input", side_effect=[""]):
            result = confirm_subtasks(subtasks, config, logger)
        assert result is subtasks

    def test_auto_confirm_any_key_enters_interactive(self, logger):
        subtasks = _make_subtasks()
        config = _make_config(auto_confirm_subtasks=True)
        with patch("agent_go.ui.safe_input", side_effect=["x", "Y"]):
            result = confirm_subtasks(subtasks, config, logger)
        assert result is subtasks

    def test_interactive_env_overrides_auto_confirm(self, logger, monkeypatch):
        """AGENT_GO_INTERACTIVE=1 强制交互"""
        monkeypatch.setenv("AGENT_GO_INTERACTIVE", "1")
        subtasks = _make_subtasks()
        config = _make_config(auto_confirm_subtasks=True)
        with patch("agent_go.ui.safe_input", side_effect=["Y"]):
            result = confirm_subtasks(subtasks, config, logger)
        assert result is subtasks

    def test_empty_input_six_times_exits(self, logger):
        """连续 6 次空输入判定为非交互模式，退出码 EX_USAGE(2)"""
        with patch("agent_go.ui.safe_input", side_effect=[""] * 6):
            with pytest.raises(SystemExit) as exc:
                confirm_subtasks(_make_subtasks(), _make_config(), logger)
        assert exc.value.code == 2

    def test_invalid_choice_resets_empty_count(self, logger):
        subtasks = _make_subtasks()
        inputs = ["", "", "", "", "", "Z", "", "", "", "", "", "Y"]
        with patch("agent_go.ui.safe_input", side_effect=inputs):
            result = confirm_subtasks(subtasks, _make_config(), logger)
        assert result is subtasks


class TestMinDifficulty:
    """任务级难度下限（min_difficulty）：优先按输入标注，无输入自行判定。"""

    def _plan_with_diffs(self, diffs):
        return {"steps": [{"id": i+1, "title": f"s{i+1}", "difficulty": d,
                            "agent_type": "developer"} for i, d in enumerate(diffs)],
                "shared_resources": {}, "dependencies": {}}

    def test_no_min_keeps_llm_labels(self):
        import logging
        from agent_go.ui import plan_to_subtasks
        plan = self._plan_with_diffs(["easy", "medium"])
        subs = plan_to_subtasks(plan, logging.getLogger("t"))
        assert [s["difficulty"] for s in subs] == ["easy", "medium"]

    def test_hard_floor_promotes_all(self):
        """min_difficulty=hard：所有低于 hard 的子任务提升到 hard。"""
        import logging
        from agent_go.ui import plan_to_subtasks
        plan = self._plan_with_diffs(["easy", "medium", "hard"])
        subs = plan_to_subtasks(plan, logging.getLogger("t"), min_difficulty="hard")
        assert [s["difficulty"] for s in subs] == ["hard", "hard", "hard"]

    def test_medium_floor_promotes_easy_only(self):
        """min_difficulty=medium：仅 easy 提升，hard 保持。"""
        import logging
        from agent_go.ui import plan_to_subtasks
        plan = self._plan_with_diffs(["easy", "medium", "hard"])
        subs = plan_to_subtasks(plan, logging.getLogger("t"), min_difficulty="medium")
        assert [s["difficulty"] for s in subs] == ["medium", "medium", "hard"]

    def test_invalid_min_ignored(self):
        import logging
        from agent_go.ui import plan_to_subtasks
        plan = self._plan_with_diffs(["easy"])
        subs = plan_to_subtasks(plan, logging.getLogger("t"), min_difficulty="extreme")
        assert subs[0]["difficulty"] == "easy"
