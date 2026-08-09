"""LLM 语义评估器单元测试。

覆盖范围:
  1. _parse_eval_response / _build_eval_prompt / _get_worktree_diff 纯函数
  2. evaluate_semantic 主体 — mock call_api + _get_worktree_diff
     （评估通过/不通过、API 失败容错、metering 事件、cost 统计、配置覆盖）
  3. executor 集成 — shell 验证通过 → 触发语义评估 → 评估不通过转修复流程
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.config import DEFAULT_CONFIG
from agent_go.evaluator import (
    _parse_eval_response,
    _build_eval_prompt,
    _get_worktree_diff,
    evaluate_semantic,
)
from agent_go.executor import run_subtask


class TestParseEvalResponse:
    def test_direct_json_pass(self):
        content = '{"passed": true, "reason": "ok", "suggestions": ""}'
        result = _parse_eval_response(content)
        assert result["passed"] is True
        assert result["reason"] == "ok"
        assert result["suggestions"] == ""

    def test_direct_json_fail(self):
        content = '{"passed": false, "reason": "doc missing", "suggestions": "update doc"}'
        result = _parse_eval_response(content)
        assert result["passed"] is False
        assert result["reason"] == "doc missing"
        assert result["suggestions"] == "update doc"

    def test_json_in_code_block(self):
        content = '```json\n{"passed": false, "reason": "style inconsistent", "suggestions": "use pep8"}\n```'
        result = _parse_eval_response(content)
        assert result["passed"] is False
        assert "style inconsistent" in result["reason"]

    def test_unparseable_defaults_to_failed(self):
        result = _parse_eval_response("random text without json")
        assert result["passed"] is False
        assert "无法解析" in result["reason"]


class TestBuildEvalPrompt:
    def test_includes_subtask_info(self):
        subtask = {
            "title": "Fix login",
            "description": "Fix the login bug",
            "agent_prompt": "Find and fix",
        }
        prompt = _build_eval_prompt(subtask, "pytest tests/", "diff here", [])
        assert "Fix login" in prompt
        assert "pytest tests/" in prompt
        assert "diff here" in prompt

    def test_includes_history_when_present(self):
        subtask = {"title": "T", "description": "D", "agent_prompt": "A"}
        history = [{"attempt": 1, "fix_summary": "fix import", "failure_summary": "2 tests fail"}]
        prompt = _build_eval_prompt(subtask, "pytest", "", history)
        assert "历史修复尝试" in prompt
        assert "fix import" in prompt

    def test_no_history_section_when_empty(self):
        subtask = {"title": "T", "description": "D", "agent_prompt": "A"}
        prompt = _build_eval_prompt(subtask, "pytest", "", [])
        assert "历史修复尝试" not in prompt


class TestGetWorktreeDiff:
    def test_returns_string(self, tmp_path):
        # 创建最小 git 仓库
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
        (repo / "a.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
        (repo / "a.txt").write_text("world")

        diff = _get_worktree_diff(repo)
        assert "a.txt" in diff or "world" in diff

    def test_multiple_commits_returns_cumulative_diff(self, tmp_path):
        """修复分多次 commit 时，应返回 root..HEAD 累积 diff（而非仅最近一次 commit 的 diff）。

        这是语义评估误判修复：evaluator 用 `git show HEAD` 只看最近 commit 的增量，
        导致评估器看不到此前已提交的签名/结构修改。累积 diff 覆盖子任务全部改动。
        """
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
        # 初始 commit（base）
        (repo / "cli.py").write_text("def f(x=''):\n    pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
        # commit 1：改签名
        (repo / "cli.py").write_text("def f(x='all'):\n    pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "fix signature"], cwd=str(repo), capture_output=True, check=True)
        # commit 2：改调用处（工作区干净）
        (repo / "cli.py").write_text("def f(x='all'):\n    return x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "fix call"], cwd=str(repo), capture_output=True, check=True)

        diff = _get_worktree_diff(repo)
        # 累积 diff 应包含 commit1 的签名改动（"x='all'"）
        assert "x='all'" in diff, f"累积 diff 应包含第一次 commit 的签名改动: {diff}"
        assert "return x" in diff, f"累积 diff 应包含第二次 commit 的改动: {diff}"


# ═══════════════════════════════════════════════════════════════
# evaluate_semantic 主体测试（mock LLM API + git diff）
# ═══════════════════════════════════════════════════════════════

_EVAL_SUBTASK = {
    "id": "sub-1",
    "title": "实现登录功能",
    "description": "实现用户登录接口",
    "agent_prompt": "请实现登录接口并写测试",
}

_PASS_JSON = '{"passed": true, "reason": "变更完整实现了目标", "suggestions": ""}'
_FAIL_JSON = '{"passed": false, "reason": "缺少文档更新", "suggestions": "补充 README 说明"}'


def _make_eval_config(**overrides):
    """深拷贝 DEFAULT_CONFIG 后按 key 覆盖。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


class TestEvaluateSemantic:
    """evaluate_semantic 主流程 — mock call_api 与 _get_worktree_diff。"""

    def test_passed_result(self, tmp_path, logger):
        """LLM 返回 passed=true 时应透传 reason/suggestions 并统计 cost。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value="diff --git a/f.py"), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON):
            result = evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        assert result["passed"] is True
        assert result["reason"] == "变更完整实现了目标"
        assert result["suggestions"] == ""
        assert result["raw_response"] == _PASS_JSON
        assert result["latency_ms"] >= 0
        # D3 修复后：token 由 prompt/response 长度估算（~3 字符/token），不再硬编码 1000/200。
        # cost_usd 由估算 token × haiku-4-5 单价（0.80/4.0 per Mtok）计算。
        assert result["cost_usd"] > 0

    def test_not_passed_result(self, tmp_path, logger):
        """LLM 返回 passed=false 时应透传失败原因与修复建议。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_FAIL_JSON):
            result = evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        assert result["passed"] is False
        assert result["reason"] == "缺少文档更新"
        assert result["suggestions"] == "补充 README 说明"

    def test_api_failure_fallback_not_passed(self, tmp_path, logger):
        """API 调用抛异常时不假通过：返回 failed，cost 为 0，reason 含错误信息。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", side_effect=RuntimeError("connection refused")):
            result = evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        assert result["passed"] is False
        assert "API 调用失败" in result["reason"]
        assert "connection refused" in result["reason"]
        assert result["cost_usd"] == 0.0
        assert result["raw_response"] == ""

    def test_unparseable_response_defaults_failed(self, tmp_path, logger):
        """LLM 返回非 JSON 内容时不过（避免隐藏评估失败）。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value="我无法评估这个变更"):
            result = evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        assert result["passed"] is False
        assert "无法解析" in result["reason"]

    def test_evaluator_config_overrides_plan_api(self, tmp_path, logger):
        """evaluator 专用配置（provider/model/base_url/api_key）应覆盖 plan_api。"""
        config = _make_eval_config(evaluator={
            "enabled": True,
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "api_key": "sk-eval",
        })
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON) as mock_api:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        eval_config = mock_api.call_args[0][0]
        assert eval_config["plan_api"]["provider"] == "openai"
        assert eval_config["plan_api"]["model"] == "gpt-4o"
        assert eval_config["plan_api"]["base_url"] == "https://api.openai.com/v1/chat/completions"
        assert eval_config["plan_api"]["api_key"] == "sk-eval"

    def test_no_evaluator_override_uses_plan_api(self, tmp_path, logger):
        """evaluator 配置为空时应复用 plan_api 的 provider/model。"""
        config = _make_eval_config(evaluator={})
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON) as mock_api:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        eval_config = mock_api.call_args[0][0]
        assert eval_config["plan_api"]["provider"] == "anthropic"
        assert eval_config["plan_api"]["model"] == "claude-sonnet-4-20250514"

    def test_prompt_contains_diff_and_history(self, tmp_path, logger):
        """评估 prompt 应包含 git diff 与历史修复尝试。"""
        config = _make_eval_config()
        history = [{"attempt": 1, "fix_summary": "fix import", "failure_summary": "2 tests fail"}]
        with patch("agent_go.evaluator._get_worktree_diff", return_value="+new line in f.py"), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON) as mock_api:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", history, config, logger)

        messages = mock_api.call_args[0][1]
        prompt = messages[0]["content"]
        assert "+new line in f.py" in prompt
        assert "历史修复尝试" in prompt
        assert "fix import" in prompt
        assert "实现登录功能" in prompt
        assert "pytest tests/" in prompt

    def test_meter_event_success(self, tmp_path, logger):
        """评估通过时应写入 result=success 的计量事件。"""
        config = _make_eval_config(_task_id="task-9")
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON), \
             patch("agent_go.evaluator.meter_event") as mock_meter:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        mock_meter.assert_called_once()
        event = mock_meter.call_args[0][1]
        assert event["role"] == "evaluator"
        assert event["result"] == "success"
        assert event["task_id"] == "task-9"
        assert event["subtask_id"] == "sub-1"
        assert event["actual_provider"] == "anthropic"
        assert event["actual_model"] == "claude-haiku-4-5-20251001"
        # D3 修复后：token 由 prompt/response 长度估算（~3 字符/token），不再硬编码 1000/200。
        # 断言估算值合理（>0，prompt 远大于 completion）+ cost 与估算一致。
        assert event["prompt_tokens"] > 0
        assert event["completion_tokens"] > 0
        assert event["prompt_tokens"] > event["completion_tokens"]
        # cost_usd 与估算 token + haiku-4-5 单价（0.80/4.0 per Mtok）一致
        expected_cost = round(
            event["prompt_tokens"] * 0.80 / 1_000_000
            + event["completion_tokens"] * 4.0 / 1_000_000, 6)
        assert event["cost_usd"] == pytest.approx(expected_cost, rel=1e-3)

    def test_meter_event_quality_fail(self, tmp_path, logger):
        """评估不通过时应写入 result=quality_fail 的计量事件。"""
        config = _make_eval_config(_task_id="task-9")
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_FAIL_JSON), \
             patch("agent_go.evaluator.meter_event") as mock_meter:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        event = mock_meter.call_args[0][1]
        assert event["result"] == "quality_fail"

    def test_api_failure_no_meter_event(self, tmp_path, logger):
        """API 失败提前返回时不应写计量事件。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", side_effect=RuntimeError("boom")), \
             patch("agent_go.evaluator.meter_event") as mock_meter:
            evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        mock_meter.assert_not_called()

    def test_estimate_cost_exception_tolerated(self, tmp_path, logger):
        """estimate_cost 异常时应容忍，cost 记 0 且不影响评估结果。"""
        config = _make_eval_config()
        with patch("agent_go.evaluator._get_worktree_diff", return_value=""), \
             patch("agent_go.evaluator.call_api", return_value=_PASS_JSON), \
             patch("agent_go.evaluator.estimate_cost", side_effect=Exception("pricing broken")):
            result = evaluate_semantic(_EVAL_SUBTASK, tmp_path, "pytest tests/", [], config, logger)

        assert result["passed"] is True
        assert result["cost_usd"] == 0.0


# ═══════════════════════════════════════════════════════════════
# executor 集成：shell 验证通过 → 语义评估 → 评估不通过转修复
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_repo(tmp_path):
    """创建一个模拟的 git 仓库（含 .git 目录 + 一些文件）。"""
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("print('hello')", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    """模拟 ~/.agent_go/task-xxx 目录。"""
    d = tmp_path / ".agent_go" / "task-eval-test"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def eval_subtask():
    """带验证命令的子任务。"""
    return {
        "id": "sub-1",
        "title": "验证任务",
        "description": "执行并验证",
        "agent_prompt": "do work",
        "verification": "pytest tests/",
        "risks": [],
        "depends_on": [],
        "skills": [],
        "agent_type": "developer",
    }


@pytest.fixture
def eval_enabled_config():
    """开启语义评估的运行时 config。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["evaluator"]["enabled"] = True
    cfg["verification"]["max_retries"] = 2
    return cfg


def make_subprocess_mock(returncode=0, stdout="", stderr=""):
    """创建一个模拟的 subprocess.CompletedProcess。"""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _git_and_pytest_pass(args, **kwargs):
    """git 有变更 + pytest 验证通过的 subprocess side_effect。"""
    cmd_str = " ".join(args) if isinstance(args, list) else str(args)
    if "status" in cmd_str and "--porcelain" in cmd_str:
        return make_subprocess_mock(stdout="M  src/main.py\n")
    if "diff" in cmd_str and "--stat" in cmd_str:
        return make_subprocess_mock(stdout="src/main.py | 2 +-")
    if "numstat" in cmd_str:
        return make_subprocess_mock(stdout="1\t1\tsrc/main.py")
    if "pytest" in cmd_str:
        return make_subprocess_mock(returncode=0, stdout="1 passed")
    return make_subprocess_mock()


_SEMANTIC_FAIL = {
    "passed": False, "reason": "缺少文档更新", "suggestions": "补充 README 说明",
    "cost_usd": 0.001, "latency_ms": 10.0, "raw_response": _FAIL_JSON,
}
_SEMANTIC_PASS = {
    "passed": True, "reason": "变更完整", "suggestions": "",
    "cost_usd": 0.001, "latency_ms": 10.0, "raw_response": _PASS_JSON,
}

_METRICS_CHANGES = {
    "files_changed": 1, "insertions": 1, "deletions": 1,
    "new_files": 0, "modified_files": 1, "actual_files": ["src/main.py"],
}


class TestExecutorSemanticEval:
    """executor._verify_changes 的 Phase 3 语义评估集成（mock 隔离 git/subprocess）。"""

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.evaluator.evaluate_semantic")
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_shell_pass_semantic_fail_triggers_fix(
            self, mock_wt_create, mock_subprocess, mock_headless,
            mock_metrics, mock_eval, mock_load_agent,
            temp_repo, task_dir, logger, eval_subtask, eval_enabled_config):
        """shell 验证通过但语义评估不通过 → 转入修复流程，修复后复评通过。"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = dict(_METRICS_CHANGES)
        mock_subprocess.side_effect = _git_and_pytest_pass
        # 第一次评估不通过 → 修复；第二次评估通过 → 收敛
        mock_eval.side_effect = [dict(_SEMANTIC_FAIL), dict(_SEMANTIC_PASS)]

        result = run_subtask("test-task", eval_subtask, temp_repo, task_dir,
                             logger, headless=True, config=eval_enabled_config)

        # 语义评估执行了 2 次（初评 + 修复后复评）
        assert mock_eval.call_count == 2
        # _run_headless 2 次：初始执行 + 1 次修复
        assert mock_headless.call_count == 2
        fix_prompt = mock_headless.call_args_list[1][0][0]
        assert "LLM 语义评估反馈" in fix_prompt
        assert "缺少文档更新" in fix_prompt
        assert "补充 README 说明" in fix_prompt

        assert result["verify_ok"] is True
        assert result["status"] == "completed"
        assert result["retry_count"] == 1

        # verification_results 中应有两条 semantic 记录（先不通过后通过）
        semantic_entries = [e for e in result["verification_results"] if e.get("type") == "semantic"]
        assert len(semantic_entries) == 2
        assert semantic_entries[0]["passed"] is False
        assert "缺少文档更新" in semantic_entries[0]["reason"]
        assert semantic_entries[0]["cost_usd"] == 0.001
        assert semantic_entries[1]["passed"] is True

        # 验证状态应持久化到 verify_state.json
        assert (task_dir / "sub-1" / "verify_state.json").exists()

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.evaluator.evaluate_semantic")
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_semantic_always_fails_exhausts_retries(
            self, mock_wt_create, mock_subprocess, mock_headless,
            mock_metrics, mock_eval, mock_load_agent,
            temp_repo, task_dir, logger, eval_subtask, eval_enabled_config):
        """语义评估持续不通过 → 耗尽重试次数后标记失败。"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = dict(_METRICS_CHANGES)
        mock_subprocess.side_effect = _git_and_pytest_pass
        mock_eval.return_value = dict(_SEMANTIC_FAIL)
        eval_enabled_config["verification"]["max_retries"] = 1

        result = run_subtask("test-task", eval_subtask, temp_repo, task_dir,
                             logger, headless=True, config=eval_enabled_config)

        # max_retries=1：初评 + 修复后复评共 2 次评估，1 次修复
        assert mock_eval.call_count == 2
        assert mock_headless.call_count == 2
        assert result["verify_ok"] is False
        assert result["status"] == "failed"
        assert result["retry_count"] == 1
        # 语义评估失败应记录具体原因，而非兜底文案
        assert "LLM 语义评估未通过" in result["failure_reason"]
        assert "缺少文档更新" in result["failure_reason"]
        assert "验证未通过（无变更或未知原因）" not in result["failure_reason"]

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.evaluator.evaluate_semantic")
    @patch("agent_go.executor.collect_change_stats")
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_evaluator_disabled_skips_semantic_eval(
            self, mock_wt_create, mock_subprocess, mock_headless,
            mock_metrics, mock_eval, mock_load_agent,
            temp_repo, task_dir, logger, eval_subtask, eval_enabled_config):
        """evaluator.enabled=False 时 shell 验证通过即收敛，不做语义评估。"""
        mock_wt_create.return_value = (True, "")
        mock_headless.return_value = make_subprocess_mock(returncode=0)
        mock_metrics.return_value = dict(_METRICS_CHANGES)
        mock_subprocess.side_effect = _git_and_pytest_pass
        eval_enabled_config["evaluator"]["enabled"] = False

        result = run_subtask("test-task", eval_subtask, temp_repo, task_dir,
                             logger, headless=True, config=eval_enabled_config)

        mock_eval.assert_not_called()
        assert mock_headless.call_count == 1
        assert result["verify_ok"] is True
        assert result["status"] == "completed"
        assert all(e.get("type") != "semantic" for e in result["verification_results"])

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.evaluator.evaluate_semantic")
    @patch("agent_go.executor.collect_change_stats")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_semantic_eval_skipped_when_not_headless(
            self, mock_wt_create, mock_subprocess, mock_metrics,
            mock_eval, mock_load_agent,
            temp_repo, task_dir, logger, eval_subtask, eval_enabled_config):
        """交互模式（headless=False）下不触发语义评估。"""
        mock_wt_create.return_value = (True, "")
        mock_metrics.return_value = dict(_METRICS_CHANGES)
        mock_subprocess.side_effect = _git_and_pytest_pass

        with patch("shutil.which", return_value=None):
            result = run_subtask("test-task", eval_subtask, temp_repo, task_dir,
                                 logger, headless=False, config=eval_enabled_config)

        mock_eval.assert_not_called()
        assert result["verify_ok"] is True
        assert result["status"] == "completed"


# ═══════════════════════════════════════════════════════════════
# 策略注册表
# ═══════════════════════════════════════════════════════════════

class TestStrategyRegistry:
    """evaluator 策略注册、路由、列出。"""

    def test_register_and_list_strategies(self):
        """注册自定义策略后可通过 list_strategies 列出"""
        from agent_go.evaluator import register, list_strategies
        # 清理测试前注册的策略
        class _TestStrategy:
            name = "test_strategy"
            description = "test"
            def __call__(self, *a, **kw):
                return {"passed": True, "confidence": 1.0, "reason": "test"}
        register(_TestStrategy())
        strategies = list_strategies()
        names = [s["name"] for s in strategies]
        assert "test_strategy" in names

    def test_evaluate_routes_to_custom_strategy(self, logger):
        """config.evaluator.strategy 指定自定义策略时，路由到该策略"""
        from agent_go.evaluator import evaluate, register

        called = [False]
        class _RouteStrategy:
            name = "route_test"
            description = ""
            def __call__(self, *a, **kw):
                called[0] = True
                return {"passed": False, "confidence": 0.1, "reason": "routed"}

        register(_RouteStrategy())
        config = {"evaluator": {"strategy": "route_test"}}
        result = evaluate({}, None, "test", [], config, logger)
        assert called[0] is True
        assert result["passed"] is False
        assert result["confidence"] == 0.1


# ═══════════════════════════════════════════════════════════════
# 评估事件持久化
# ═══════════════════════════════════════════════════════════════

class TestAssessmentPersistence:
    """evaluate() 自动写 assessment.jsonl"""

    def test_evaluate_writes_assessment_jsonl(self, tmp_path, logger, monkeypatch):
        """调用 evaluate() 时在指定路径写入 assessment.jsonl"""
        from agent_go.evaluator import evaluate

        # mock LLM 调用（避免真正调 API）
        mock_content = '{"passed": true, "confidence": 0.85, "reason": "ok"}'
        with patch("agent_go.evaluator.call_api", return_value=mock_content), \
             patch("agent_go.evaluator._get_worktree_diff", return_value=""):
            config = {"_task_id": "t1", "plan_api": {"provider": "anthropic"}}
            result = evaluate(
                {"id": "s1"}, tmp_path, "npm run lint", [],
                config, logger,
                assessment_path=str(tmp_path),
                verification_confidence={"level": "heuristic"},
            )

        # assessment.jsonl 已写入
        af = tmp_path / "assessment.jsonl"
        assert af.exists()
        events = af.read_text(encoding="utf-8").strip().split("\n")
        assert len(events) == 1
        import json
        data = json.loads(events[0])
        assert data["task_id"] == "t1"
        assert data["subtask_id"] == "s1"
        assert data["trigger_source"] == "auto"  # verification_confidence 传入 → auto
        assert data["verification_confidence"] == "heuristic"
        assert data["passed"] is True
        assert data["confidence"] == 0.85

    def test_evaluate_manual_trigger_source(self, tmp_path, logger):
        """不传 verification_confidence → trigger_source=manual"""
        from agent_go.evaluator import evaluate
        with patch("agent_go.evaluator.call_api", return_value='{"passed": true, "confidence": 1.0, "reason": "ok"}'), \
             patch("agent_go.evaluator._get_worktree_diff", return_value=""):
            config = {"_task_id": "t1", "plan_api": {"provider": "anthropic"}}
            evaluate({"id": "s1"}, tmp_path, "pytest", [],
                     config, logger, assessment_path=str(tmp_path))
        data = json.loads((tmp_path / "assessment.jsonl").read_text())
        assert data["trigger_source"] == "manual"
