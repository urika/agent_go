"""LLM 语义评估器单元测试。"""

import pytest
from pathlib import Path

from agent_go.evaluator import (
    _parse_eval_response,
    _build_eval_prompt,
    _get_worktree_diff,
)


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

    def test_unparseable_defaults_to_passed(self):
        result = _parse_eval_response("random text without json")
        assert result["passed"] is True
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
