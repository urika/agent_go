"""测试 metrics.py — 数据采集模块

全覆盖: collect_timing, collect_change_stats, collect_merge_result, extract_usage
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.metrics import (
    collect_timing,
    collect_change_stats,
    collect_merge_result,
    extract_usage,
)


class TestCollectTiming:
    """阶段耗时采集"""

    def test_all_fields_present(self):
        result = collect_timing(
            worktree_create_ms=123.4,
            merge_upstream_ms=45.6,
            claude_execute_ms=30000.0,
            verification_ms=1500.0,
            git_commit_ms=200.0,
        )
        assert result["worktree_create_ms"] == 123
        assert result["merge_upstream_ms"] == 46
        assert result["claude_execute_ms"] == 30000
        assert result["verification_ms"] == 1500
        assert result["git_commit_ms"] == 200

    def test_zero_values(self):
        result = collect_timing(0, 0, 0, 0, 0)
        assert all(v == 0 for v in result.values())

    def test_rounding(self):
        result = collect_timing(1.499, 2.4, 3.501, 4.999, 5.0)
        assert result["worktree_create_ms"] == 1
        assert result["merge_upstream_ms"] == 2
        assert result["claude_execute_ms"] == 4

    def test_returns_dict(self):
        result = collect_timing(1, 2, 3, 4, 5)
        assert isinstance(result, dict)
        assert len(result) == 5


class TestCollectChangeStats:
    """git 变更统计采集"""

    def test_with_changes(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = "5\t3\tsrc/main.py\n2\t0\tsrc/utils.py\n"
                elif "porcelain" in cmd:
                    m.stdout = "M  src/main.py\nA  src/new.py\n"
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        # files_changed = 2 (src/main.py + src/utils.py from numstat)
        # "A  src/new.py" in porcelain does NOT start with "??", so it's not counted as new
        assert result["files_changed"] == 2
        assert result["insertions"] == 7
        assert result["deletions"] == 3
        assert result["new_files"] == 0
        assert result["modified_files"] == 2

    def test_no_changes(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = ""
                elif "porcelain" in cmd:
                    m.stdout = ""
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        assert result["files_changed"] == 0
        assert result["insertions"] == 0
        assert result["deletions"] == 0
        assert result["new_files"] == 0

    def test_new_files_only(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = ""
                elif "porcelain" in cmd:
                    m.stdout = "?? new_file.py\n?? another.py\n"
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))

        assert result["files_changed"] == 2
        assert result["new_files"] == 2
        assert result["insertions"] == 0

    def test_negative_numstat_handled(self):
        with patch("subprocess.run") as mock_run:
            def side_effect(args, **kwargs):
                m = MagicMock()
                cmd = " ".join(args) if isinstance(args, list) else str(args)
                if "numstat" in cmd:
                    m.stdout = "-\t-\tsrc/binary.bin\n"
                elif "porcelain" in cmd:
                    m.stdout = ""
                else:
                    m.stdout = ""
                m.returncode = 0
                return m
            mock_run.side_effect = side_effect

            result = collect_change_stats(Path("/fake/repo"))
            assert result["insertions"] == 0
            assert result["deletions"] == 0
            assert result["files_changed"] == 1

    def test_subprocess_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            # FileNotFoundError 会传播出来，没有 try/except 包裹
            with pytest.raises(FileNotFoundError):
                collect_change_stats(Path("/fake/repo"))


class TestCollectMergeResult:
    """产物传递结果采集"""

    def test_success(self):
        result = collect_merge_result("sub-1", True)
        assert result["upstream"] == "sub-1"
        assert result["status"] == "success"

    def test_failure_no_conflict_files(self):
        result = collect_merge_result("sub-2", False)
        assert result["status"] == "conflict"
        assert "conflict_files" not in result

    def test_failure_with_conflict_files(self):
        result = collect_merge_result("sub-2", False, ["main.py", "utils.py"])
        assert result["status"] == "conflict"
        assert result["conflict_files"] == ["main.py", "utils.py"]

    def test_empty_conflict_list(self):
        result = collect_merge_result("sub-3", False, [])
        assert result["status"] == "conflict"
        # 空列表是 falsy，不会被添加到结果中
        assert "conflict_files" not in result


class TestExtractUsage:
    """API 用量提取"""

    def test_openai_response(self):
        api_resp = {
            "usage": {"input_tokens": 200, "output_tokens": 400}
        }
        result = extract_usage(api_resp, "openai", "gpt-4o")
        assert result["prompt_tokens"] == 200
        assert result["completion_tokens"] == 400
        assert result["model"] == "gpt-4o"
        assert result["provider"] == "openai"

    def test_anthropic_response(self):
        api_resp = {
            "usage": {"input_tokens": 150, "output_tokens": 300}
        }
        result = extract_usage(api_resp, "anthropic", "claude-sonnet-4")
        assert result["prompt_tokens"] == 150
        assert result["completion_tokens"] == 300

    def test_no_usage(self):
        api_resp = {}
        result = extract_usage(api_resp, "anthropic", "claude-sonnet-4")
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    def test_partial_usage(self):
        api_resp = {"usage": {"input_tokens": 100}}
        result = extract_usage(api_resp, "anthropic", "test")
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 0
