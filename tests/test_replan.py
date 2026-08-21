"""C3 局部重规划（PRD F-VERIFY-6）测试。

覆盖契约：触发/不触发、最多一次、预算继承、人工拒绝、auto_apply 执行、
replan_triggered/replan_succeeded 可审计记录。
"""

from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

from agent_go.replan import (REPLAN_TRIGGERS, _heuristic_decomposition,
                             _parse_llm_steps, build_decomposition,
                             confirm_replan, render_replan_guidance,
                             should_trigger)


@pytest.fixture
def temp_repo(tmp_path):
    """创建一个模拟的 git 仓库（含 .git 目录 + 一些文件）。"""
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Test Project", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("print('hello')", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    """模拟 ~/.agent_go/task-xxx 目录。"""
    d = tmp_path / ".agent_go" / "task-replan-test"
    d.mkdir(parents=True)
    return d

_SUBTASK_TPL = {
    "id": "sub-1", "title": "基础任务", "description": "执行操作",
    "verification": "pytest tests/",
    "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
    "agent_prompt": "work",
}


class TestReplanPure:
    """replan.py 纯函数测试。"""

    def test_should_trigger_whitelist(self):
        for r in REPLAN_TRIGGERS:
            assert should_trigger(r)
        assert not should_trigger("over_budget_l2")
        assert not should_trigger("hard_timeout")
        assert not should_trigger("")

    def test_heuristic_decomposition_many_files(self):
        steps = _heuristic_decomposition(
            {"title": "改 API", "files_hint": "a.py, b.py, c.py"}, 4)
        assert 2 <= len(steps) <= 4
        assert steps[0]["title"] == "定位根因"
        assert steps[-1]["title"] == "验证收敛"
        # 多文件时按文件逐个修
        assert any("a.py" in s["title"] for s in steps)

    def test_heuristic_decomposition_no_files(self):
        steps = _heuristic_decomposition({"title": "改 API", "files_hint": ""}, 4)
        assert len(steps) == 3  # 定位 → 最小修复 → 验证
        assert any("最小修复" in s["title"] for s in steps)

    def test_parse_llm_steps_valid(self):
        text = '前言 [{"title": "定位", "detail": "读代码"}, {"title": "修复", "detail": "改 a.py"}] 后记'
        steps = _parse_llm_steps(text, 4)
        assert len(steps) == 2
        assert steps[0]["step"] == 1 and steps[1]["title"] == "修复"

    def test_parse_llm_steps_garbage(self):
        assert _parse_llm_steps("不是 JSON", 4) == []
        assert _parse_llm_steps('[{"no_title": 1}]', 4) == []
        assert _parse_llm_steps("[broken", 4) == []

    def test_build_decomposition_fallback_without_api(self, logger):
        """无 planner/plan_api 配置 → 确定性启发式兜底（零 LLM）。"""
        steps = build_decomposition(
            {"title": "改 API", "files_hint": "a.py"}, {"reason": "verify_revert"},
            config={}, logger=logger)
        assert len(steps) >= 2

    def test_build_decomposition_llm_success(self, logger):
        """LLM 返回合法 JSON → 使用 LLM 拆分。"""
        cfg = {"plan_api": {"model": "m", "provider": "anthropic",
                            "base_url": "http://x", "api_key": "k"}}
        with patch("agent_go.api.call_api",
                   return_value='[{"title": "S1", "detail": "d1"}, '
                                '{"title": "S2", "detail": "d2"}]'):
            steps = build_decomposition({"title": "t"}, {"reason": "r"},
                                        config=cfg, logger=logger)
        assert [s["title"] for s in steps] == ["S1", "S2"]

    def test_build_decomposition_llm_failure_fallback(self, logger):
        """LLM 异常 → fail-open 降级启发式，不阻断。"""
        cfg = {"plan_api": {"model": "m", "provider": "anthropic",
                            "base_url": "http://x", "api_key": "k"}}
        with patch("agent_go.api.call_api", side_effect=RuntimeError("boom")):
            steps = build_decomposition({"title": "t", "files_hint": "a.py"},
                                        {"reason": "r"}, config=cfg, logger=logger)
        assert len(steps) >= 2

    def test_render_guidance(self):
        steps = [{"step": 1, "title": "定位根因", "detail": "读代码"},
                 {"step": 2, "title": "最小修复", "detail": "改一处"}]
        text = render_replan_guidance(steps, "verify_revert")
        assert "局部重规划" in text and "定位根因" in text
        assert "循环振荡" in text  # trigger label

    def test_confirm_replan(self):
        assert confirm_replan("verify_revert", [{"step": 1, "title": "t", "detail": "d"}],
                              input_fn=lambda _p: "P")
        assert not confirm_replan("verify_revert", [{"step": 1, "title": "t", "detail": "d"}],
                                  input_fn=lambda _p: "N")
        # EOF（管道关闭）→ 拒绝
        def _eof(_p):
            raise EOFError
        assert not confirm_replan("verify_revert", [{"step": 1, "title": "t", "detail": "d"}],
                                  input_fn=_eof)


def _git_with_base():
    """subprocess.run side_effect：git 成功、pytest 始终失败、diff 恒定（触发 revert）。"""
    def _run(cmd, **kw):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
        if "diff" in cmd_str and "--stat" in cmd_str:
            return MagicMock(returncode=0, stdout="1 file changed, 10 insertions(+)", stderr="")
        if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pytest" in cmd_str:
            return MagicMock(returncode=1, stdout="", stderr="FAIL")
        return MagicMock(returncode=0, stdout="", stderr="")
    return _run


def _git_always_pass():
    """shell 验证始终通过（让循环只卡在语义评估）。"""
    def _run(cmd, **kw):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
        if "diff" in cmd_str and "--stat" in cmd_str:
            return MagicMock(returncode=0, stdout="1 file changed", stderr="")
        if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
            return MagicMock(returncode=0, stdout="", stderr="")
        if "rev-list" in cmd_str or "hash-object" in cmd_str:
            return MagicMock(returncode=0, stdout="abc123\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return _run


class TestReplanTrigger:
    """executor 集成：触发条件与 F-VERIFY-6 契约。"""

    def _run_verify(self, temp_repo, task_dir, logger, config, headless=True):
        from agent_go.executor import _verify_changes
        return _verify_changes(
            "task-1", "sub-1", dict(_SUBTASK_TPL), temp_repo, headless=headless,
            task_md="# Task", env={}, tag_name="task-1/sub-1",
            active_pids=set(), active_pids_lock=Lock(), logger=logger,
            task_dir=task_dir, config=config)

    def test_revert_trigger_suggested_only_by_default(self, temp_repo, task_dir, logger):
        """触发（verify_revert）+ auto_apply 默认 False → 只记录建议不执行。"""
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 5}, "_base_commit": "abc123"})

        replan = result["replan"]
        assert replan is not None, "verify_revert 应触发局部重规划"
        assert replan["replan_triggered"] is True
        assert replan["replan_reason"] == "verify_revert"
        assert replan["replan_status"] == "suggested"
        assert replan["replan_executed"] is False
        assert replan["replan_succeeded"] is None  # 未执行不判定
        assert replan["replan_budget_inherited"] is True
        assert len(replan["replan_steps"]) >= 2
        # 未执行 → _run_headless 调用次数与无 replan 时一致（≤2 次 fix）
        assert mock_fix.call_count <= 2

    def test_replan_disabled_no_trigger(self, temp_repo, task_dir, logger):
        """replan.enabled=False → 不触发，行为与此前完全一致。"""
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 5, "replan": {"enabled": False}},
                 "_base_commit": "abc123"})

        assert result["replan"] is None
        types = [v.get("type") for v in result["verification_results"]]
        assert "revert" in types  # 原 revert 终止逻辑不受影响

    def test_revert_trigger_auto_apply_executes_once(self, temp_repo, task_dir, logger):
        """auto_apply=True → 执行一次拆分修复；验证仍失败 → replan_succeeded=False；
        再次触发不重复执行（最多一次）。"""
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 5,
                                  "replan": {"enabled": True, "auto_apply": True}},
                 "_base_commit": "abc123"})

        replan = result["replan"]
        assert replan["replan_executed"] is True
        assert replan["replan_status"] == "executed"
        assert replan["replan_succeeded"] is False  # pytest 始终失败
        # 最多一次：带 -replan 标签的调用恰好一次
        replan_calls = [c for c in mock_fix.call_args_list
                        if "-replan" in str(c)]
        assert len(replan_calls) == 1, f"拆分修复应只执行一次: {len(replan_calls)}"
        # 拆分指引注入了修复 prompt
        prompt_arg = str(replan_calls[0].args[0]) if replan_calls[0].args else str(replan_calls[0])
        assert "局部重规划" in prompt_arg

    def test_replan_success_path(self, temp_repo, task_dir, logger):
        """失败模式重复触发 + auto_apply → 拆分修复后验证通过 → replan_succeeded=True。"""
        with patch("subprocess.run", side_effect=_git_always_pass()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.evaluator.evaluate_semantic") as mock_eval:
            mock_fix.return_value = MagicMock(returncode=0)
            # 两次同一缺陷（相同 reason → 相似度 1.0 ≥ 阈值 → failure_pattern_repeat），
            # replan 后第三次通过
            mock_eval.side_effect = [
                {"passed": False, "reason": "缺少 None 保护导致 AttributeError（main.py 第 8 行）",
                 "cost_usd": 0.001, "latency_ms": 100},
                {"passed": False, "reason": "缺少 None 保护导致 AttributeError（main.py 第 8 行）",
                 "cost_usd": 0.001, "latency_ms": 100},
                {"passed": True, "reason": "已修复", "cost_usd": 0.001, "latency_ms": 100},
            ]
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"evaluator": {"enabled": True},
                 "verification": {"max_retries": 3,
                                  "replan": {"enabled": True, "auto_apply": True}}})

        assert result["verify_ok"] is True
        replan = result["replan"]
        assert replan is not None
        assert replan["replan_reason"] == "failure_pattern_repeat"
        assert replan["replan_executed"] is True
        assert replan["replan_succeeded"] is True

    def test_replan_budget_inherited(self, temp_repo, task_dir, logger):
        """预算继承：父任务 L2 上限已耗尽 → 不执行拆分修复（budget_exhausted）。"""
        # L2 内联检查在前两轮不熔断（0 < 0.25），第三轮 revert 触发时
        # replan 预算预检读到 1.0 ≥ 0.25 → budget_exhausted
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.executor._metering_available", return_value=True), \
             patch("agent_go.executor._meter_cost_for_sub",
                   side_effect=[0.0, 0.0, 1.0]):
            mock_fix.return_value = MagicMock(returncode=0)
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 5,
                                  "replan": {"enabled": True, "auto_apply": True}},
                 "cost_control": {"enabled": True,
                                  "per_subtask_budget_usd": {"medium": 0.1},
                                  "subtask_multiplier": 2.5},
                 "_base_commit": "abc123",
                 "_metering_path": "/tmp/metering.jsonl"})

        replan = result["replan"]
        assert replan["replan_triggered"] is True
        assert replan["replan_executed"] is False
        assert replan["replan_status"] == "budget_exhausted"
        # 未执行 → 无 -replan 调用
        assert not any("-replan" in str(c) for c in mock_fix.call_args_list)

    def test_replan_human_reject(self, temp_repo, task_dir, logger):
        """人工拒绝路径：交互模式确认选「不执行」→ 只记录建议。"""
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix, \
             patch("agent_go.executor.sys.stdin") as mock_stdin, \
             patch("agent_go.executor.safe_input", return_value="R"), \
             patch("agent_go.replan.confirm_replan", return_value=False) as mock_confirm:
            mock_stdin.isatty.return_value = True
            mock_fix.return_value = MagicMock(returncode=0)
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 5},
                 "_base_commit": "abc123"},
                headless=False)

        assert mock_confirm.called, "交互模式应弹人工确认"
        replan = result["replan"]
        assert replan["replan_triggered"] is True
        assert replan["replan_executed"] is False
        assert replan["replan_status"] == "suggested"
        assert not any("-replan" in str(c) for c in mock_fix.call_args_list)

    def test_no_trigger_normal_failure(self, temp_repo, task_dir, logger):
        """普通失败（无无进展信号、跑满重试）→ 不触发 replan。"""
        with patch("subprocess.run", side_effect=_git_with_base()), \
             patch("agent_go.executor._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            # 无 _base_commit → revert 检测不启用；max_retries=1 跑满即终止
            result = self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 1}})

        assert result["verify_ok"] is False
        assert result["replan"] is None
