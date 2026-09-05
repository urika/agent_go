"""C4 KnowledgeStore（A/B 实验臂）测试。

覆盖：开关、三类数据源提取、可淘汰（suppressed_ids/dormant）、fail-open、
repair prompt 注入与臂标记。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

from agent_go.knowledge import (_match_deviations, _match_verify_states,
                                _pattern_similar, build_repair_knowledge,
                                resolve_repair_knowledge)


class TestPatternSimilar:
    def test_equal_and_substring(self):
        assert _pattern_similar("pytest tests/", "pytest tests/")
        assert _pattern_similar("pytest tests/", "pytest tests/ 失败")
        assert _pattern_similar("verify_revert", "verify_revert")

    def test_token_overlap(self):
        assert _pattern_similar("npm test 失败 exit 1", "npm test exit 1")
        assert not _pattern_similar("npm test 失败", "pytest 断言错误")

    def test_empty(self):
        assert not _pattern_similar("", "x")
        assert not _pattern_similar("x", "")


def _write_problems(path: Path, problems: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in problems) + "\n",
                    encoding="utf-8")


def _problem(pid: str, pattern: str, **kw) -> dict:
    base = {
        "id": pid, "failure_pattern": pattern, "failure_class": "verify_fail",
        "task_id": "task-old", "subtask_id": "sub-9", "summary": "历史失败摘要",
        "evidence": "", "root_cause_category": "env",
        "occurrence_count": 3,
        "first_seen_at": datetime.now().isoformat(),
        "last_seen_at": datetime.now().isoformat(),
        "status": "opened", "root_cause": "", "resolved_by": "",
        "github_issue": "", "stale_after_days": 30, "resolution_summary": "",
        "schema_version": 1,
    }
    base.update(kw)
    return base


class TestKnowledgeSources:
    def test_disabled_returns_empty(self, tmp_path, logger):
        result = build_repair_knowledge(
            {"id": "sub-1"}, "pytest tests/", tmp_path,
            {"knowledge": {"enabled": False}}, logger)
        assert result == {"text": "", "sources": []}

    def test_problem_match_resolved_first(self, tmp_path, logger):
        """resolved（有 resolution_summary）优先于 opened；解法入注入文本。"""
        problems_path = tmp_path / "problems.jsonl"
        _write_problems(problems_path, [
            _problem("p-opened", "pytest tests/ 断言失败", occurrence_count=5),
            _problem("p-resolved", "pytest tests/ 失败",
                     status="resolved",
                     resolution_summary="worktree 未继承 venv；TASK.md 注明 source venv 后修复"),
        ])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger)
        assert result["sources"][0] == "p-resolved", "resolved 应排最前"
        assert "resolution" not in result["text"]  # 字段名不泄露，内容才注入
        assert "source venv" in result["text"], "解法摘要应入注入文本"
        assert "复发" in result["text"]

    def test_suppressed_ids_eliminate(self, tmp_path, logger):
        """可淘汰：suppressed_ids 屏蔽错误知识。"""
        problems_path = tmp_path / "problems.jsonl"
        _write_problems(problems_path, [
            _problem("p-bad", "pytest tests/ 失败", status="resolved",
                     resolution_summary="错误解法"),
        ])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True, "suppressed_ids": ["p-bad"]}}, logger)
        assert result == {"text": "", "sources": []}

    def test_dormant_excluded(self, tmp_path, logger):
        """可淘汰：opened 且超半衰期的 dormant Problem 自动排除。"""
        old = (datetime.now() - timedelta(days=90)).isoformat()
        problems_path = tmp_path / "problems.jsonl"
        _write_problems(problems_path, [
            _problem("p-stale", "pytest tests/ 失败",
                     first_seen_at=old, last_seen_at=old, stale_after_days=30),
        ])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger)
        assert result == {"text": "", "sources": []}

    def test_no_match_returns_empty(self, tmp_path, logger):
        problems_path = tmp_path / "problems.jsonl"
        _write_problems(problems_path, [_problem("p-1", "docker 构建缓存失效")])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger)
        assert result == {"text": "", "sources": []}

    def test_corrupt_problems_fail_open(self, tmp_path, logger):
        """fail-open：problems.jsonl 损坏 → 空结果，不抛异常。"""
        (tmp_path / "problems.jsonl").write_text("{broken json\n", encoding="utf-8")
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger)
        assert result == {"text": "", "sources": []}

    def test_deviation_match(self, tmp_path):
        """当前任务 deviation.jsonl：同任务前序子任务失败模式。"""
        task_dir = tmp_path / "task-x"
        task_dir.mkdir()
        (task_dir / "deviation.jsonl").write_text(
            json.dumps({"task_id": "task-x", "subtask_id": "sub-0",
                        "deviation_type": "acceptance_gap",
                        "root_cause_category": "env",
                        "summary": "缺 PYTHONPATH 导致导入失败",
                        "failure_pattern": "pytest tests/ 失败"},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        items = _match_deviations(task_dir, "pytest tests/", 3)
        assert len(items) == 1
        assert items[0]["source_id"] == "deviation:sub-0"
        assert "PYTHONPATH" in items[0]["line"]

    def test_verify_state_match(self, tmp_path):
        """verify_state：只取 reflexion_triggered=True 的其他子任务记录。"""
        task_dir = tmp_path / "task-x"
        (task_dir / "sub-0").mkdir(parents=True)
        (task_dir / "sub-1").mkdir(parents=True)
        (task_dir / "sub-2").mkdir(parents=True)
        (task_dir / "sub-0" / "verify_state.json").write_text(json.dumps({
            "schema_version": 1, "reflexion_triggered": True,
            "failure_analysis": "导入路径缺失",
            "effective_strategy": "先 export PYTHONPATH 再跑 pytest"}), encoding="utf-8")
        # 当前子任务自身 → 排除
        (task_dir / "sub-1" / "verify_state.json").write_text(json.dumps({
            "schema_version": 1, "reflexion_triggered": True,
            "failure_analysis": "自身根因", "effective_strategy": "自身策略"}), encoding="utf-8")
        # 非 Reflexion 来源 → 排除
        (task_dir / "sub-2" / "verify_state.json").write_text(json.dumps({
            "schema_version": 1, "reflexion_triggered": False,
            "failure_analysis": "x", "effective_strategy": "y"}), encoding="utf-8")
        items = _match_verify_states(task_dir, "sub-1", 3)
        assert len(items) == 1
        assert items[0]["source_id"] == "verify_state:sub-0"
        assert "PYTHONPATH" in items[0]["line"]

    def test_max_items_bounded(self, tmp_path, logger):
        """有界：注入条目 ≤ max_items。"""
        problems_path = tmp_path / "problems.jsonl"
        _write_problems(problems_path, [
            _problem(f"p-{i}", "pytest tests/ 失败") for i in range(6)
        ])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            result = build_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True, "max_items": 2}}, logger)
        assert len(result["sources"]) <= 2


_SUBTASK_TPL = {
    "id": "sub-1", "title": "基础任务", "description": "执行操作",
    "verification": "pytest tests/",
    "risks": [], "depends_on": [], "skills": [], "agent_type": "developer",
    "agent_prompt": "work",
}


def _git_fail_then_pass():
    """git 成功、pytest 首次失败后一直失败（驱动一次 fix 重试）。"""
    def _run(cmd, **kw):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "status" in cmd_str and "--porcelain" in cmd_str:
            return MagicMock(returncode=0, stdout=" M main.py\n", stderr="")
        if "diff" in cmd_str and "--stat" in cmd_str:
            return MagicMock(returncode=0, stdout="1 file changed", stderr="")
        if any(g in cmd_str for g in ["git add", "git commit", "git tag"]):
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pytest" in cmd_str:
            return MagicMock(returncode=1, stdout="", stderr="FAIL")
        return MagicMock(returncode=0, stdout="", stderr="")
    return _run


@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("print('hello')", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    d = tmp_path / ".agent_go" / "task-knowledge-test"
    d.mkdir(parents=True)
    return d


class TestKnowledgeInjection:
    """executor 集成：knowledge.enabled 开关决定 repair prompt 是否注入历史经验。"""

    def _run_verify(self, temp_repo, task_dir, logger, config):
        from agent_go.executor import _verify_changes
        return _verify_changes(
            "task-1", "sub-1", dict(_SUBTASK_TPL), temp_repo, headless=True,
            task_md="# Task", env={}, tag_name="task-1/sub-1",
            active_pids=set(), active_pids_lock=Lock(), logger=logger,
            task_dir=task_dir, config=config)

    def test_enabled_injects_into_repair_prompt(self, temp_repo, task_dir, logger, tmp_path):
        problems_path = tmp_path / "global_store"
        _write_problems(problems_path / "problems.jsonl", [
            _problem("p-xyz", "pytest tests/ 失败", status="resolved",
                     resolution_summary="先 source venv 再跑 pytest"),
        ])
        with patch("subprocess.run", side_effect=_git_fail_then_pass()), \
             patch("agent_go.backends.claude_backend._run_headless") as mock_fix, \
             patch("agent_go.config.AGENT_GO_DIR", problems_path):
            mock_fix.return_value = MagicMock(returncode=0)
            self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 1},
                 "knowledge": {"enabled": True}})

        assert mock_fix.call_count >= 1
        prompt = str(mock_fix.call_args_list[0].args[0])
        assert "历史经验" in prompt, "启用后 repair prompt 应注入历史经验"
        assert "p-xyz" in prompt, "注入内容应带来源 id（可审计可淘汰）"
        assert "source venv" in prompt

    def test_disabled_no_injection(self, temp_repo, task_dir, logger):
        """对照臂：默认关闭 → repair prompt 无历史经验。"""
        with patch("subprocess.run", side_effect=_git_fail_then_pass()), \
             patch("agent_go.backends.claude_backend._run_headless") as mock_fix:
            mock_fix.return_value = MagicMock(returncode=0)
            self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 1}})

        assert mock_fix.call_count >= 1
        prompt = str(mock_fix.call_args_list[0].args[0])
        assert "历史经验" not in prompt


class TestResolveSnapshot:
    """KV-cache 稳定快照（C4 前置修订）：knowledge.snapshot 策略。"""

    def test_snapshot_reused_without_rebuild(self, tmp_path, logger):
        """快照存在且开关开 → 原样复用，不重新匹配（fresh=False）。"""
        frozen = {"text": "### 历史经验\n- x", "sources": ["p-1"]}
        with patch("agent_go.knowledge.build_repair_knowledge",
                   side_effect=AssertionError("不应重新构建")):
            ctx, fresh, new_snap = resolve_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path,
                {"knowledge": {"enabled": True}}, logger, snapshot=frozen)
        assert ctx is frozen and fresh is False and new_snap is frozen

    def test_snapshot_disabled_rebuilds(self, tmp_path, logger):
        """snapshot=false → 逐轮重建（对照/调试路径）。"""
        frozen = {"text": "### 历史经验\n- x", "sources": ["p-1"]}
        with patch("agent_go.knowledge.build_repair_knowledge",
                   return_value={"text": "new", "sources": ["p-2"]}) as mock_build:
            ctx, fresh, new_snap = resolve_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path,
                {"knowledge": {"enabled": True, "snapshot": False}},
                logger, snapshot=frozen)
        assert mock_build.call_count == 1
        assert ctx == {"text": "new", "sources": ["p-2"]}
        assert fresh is True and new_snap is frozen, "开关关时不更新快照"

    def test_empty_build_not_frozen(self, tmp_path, logger):
        """首次构建无命中（空文本）→ 不冻结，后续轮次仍可命中新数据。"""
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            ctx, fresh, new_snap = resolve_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger, snapshot=None)
        assert ctx == {"text": "", "sources": []}
        assert fresh is True and new_snap is None

    def test_nonempty_build_frozen(self, tmp_path, logger):
        """首次构建有命中 → 冻结为快照。"""
        _write_problems(tmp_path / "problems.jsonl", [
            _problem("p-1", "pytest tests/ 失败", status="resolved",
                     resolution_summary="先 source venv"),
        ])
        with patch("agent_go.config.AGENT_GO_DIR", tmp_path):
            ctx, fresh, new_snap = resolve_repair_knowledge(
                {"id": "sub-1"}, "pytest tests/", tmp_path / "task",
                {"knowledge": {"enabled": True}}, logger, snapshot=None)
        assert fresh is True and new_snap == ctx and ctx["text"]


class TestSnapshotInjection:
    """executor 集成：快照下两轮修复的知识块逐字节一致且位于稳定前缀。"""

    def _run_verify(self, temp_repo, task_dir, logger, config):
        from agent_go.executor import _verify_changes
        return _verify_changes(
            "task-1", "sub-1", dict(_SUBTASK_TPL), temp_repo, headless=True,
            task_md="# Task", env={}, tag_name="task-1/sub-1",
            active_pids=set(), active_pids_lock=Lock(), logger=logger,
            task_dir=task_dir, config=config)

    def test_snapshot_stable_prefix_across_retries(
            self, temp_repo, task_dir, logger, tmp_path):
        problems_path = tmp_path / "global_store"
        _write_problems(problems_path / "problems.jsonl", [
            _problem("p-xyz", "pytest tests/ 失败", status="resolved",
                     resolution_summary="先 source venv 再跑 pytest"),
        ])
        with patch("subprocess.run", side_effect=_git_fail_then_pass()), \
             patch("agent_go.backends.claude_backend._run_headless") as mock_fix, \
             patch("agent_go.config.AGENT_GO_DIR", problems_path):
            mock_fix.return_value = MagicMock(returncode=0)
            self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 2},
                 "knowledge": {"enabled": True}})

        assert mock_fix.call_count == 2, "max_retries=2 且验证持续失败应有 2 次修复"
        prompts = [str(c.args[0]) for c in mock_fix.call_args_list]
        prefixes = [p.split("## ⚠️ 验证失败")[0] for p in prompts]
        assert prefixes[0] == prefixes[1], \
            "快照下 TASK.md+知识块稳定前缀应逐字节一致（KV-cache 可复用）"
        for p in prompts:
            assert "历史经验" in p
            assert p.index("历史经验") < p.index("## ⚠️ 验证失败"), \
                "知识块应置于逐轮变化的失败头之前"

    def test_snapshot_disabled_prompts_diverge_allowed(
            self, temp_repo, task_dir, logger, tmp_path):
        """snapshot=false 对照路径：每轮都重新构建（不冻结）。"""
        problems_path = tmp_path / "global_store"
        _write_problems(problems_path / "problems.jsonl", [
            _problem("p-xyz", "pytest tests/ 失败", status="resolved",
                     resolution_summary="先 source venv 再跑 pytest"),
        ])
        with patch("subprocess.run", side_effect=_git_fail_then_pass()), \
             patch("agent_go.backends.claude_backend._run_headless") as mock_fix, \
             patch("agent_go.config.AGENT_GO_DIR", problems_path), \
             patch("agent_go.knowledge.build_repair_knowledge",
                   wraps=build_repair_knowledge) as spy_build:
            mock_fix.return_value = MagicMock(returncode=0)
            self._run_verify(
                temp_repo, task_dir, logger,
                {"verification": {"max_retries": 2},
                 "knowledge": {"enabled": True, "snapshot": False}})

        assert mock_fix.call_count == 2
        assert spy_build.call_count == 2, "snapshot=false 时每轮都应重新匹配"
