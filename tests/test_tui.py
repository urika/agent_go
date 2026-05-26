import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestTaskStatusData:

    def test_returns_none_for_missing_meta(self, tmp_path):
        from agent_go.tui import _get_task_status
        assert _get_task_status(tmp_path / "nonexistent") is None

    def test_basic_completed_task(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {
            "task_id": "task-test", "task": "test task",
            "created": "20260527-120000", "status": "completed",
            "subtasks": [{"id": "sub-1"}, {"id": "sub-2"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed", "duration_sec": 45.0,
                 "agent_type_source": "llm", "verify_ok": True, "retry_count": 0},
                {"subtask_id": "sub-2", "status": "no_changes", "duration_sec": 12.0,
                 "agent_type_source": "rule", "verify_ok": True, "retry_count": 0},
            ],
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text(
            "2026-05-27 12:00:00 | INFO | test | some log\n")

        result = _get_task_status(tmp_path)
        assert result is not None
        assert result["id"] == tmp_path.name
        assert result["status"] == "completed"
        assert result["progress"] == "2/2"
        assert result["task"] == "test task"
        assert len(result["results"]) == 2

    def test_running_task_shows_elapsed(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {
            "task_id": "task-test", "task": "test",
            "created": "20260527-120000", "status": "running",
            "subtasks": [], "results": [],
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("recent log\n")

        result = _get_task_status(tmp_path)
        assert result is not None
        assert "m" in result["elapsed"] or "s" in result["elapsed"]

    def test_no_log_file_handled(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {
            "task_id": "task-test", "task": "test",
            "created": "20260527-120000", "status": "completed",
            "subtasks": [], "results": [],
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        result = _get_task_status(tmp_path)
        assert result is not None

    def test_results_with_new_metrics_fields(self, tmp_path):
        from agent_go.tui import _get_task_status
        meta = {
            "task_id": "task-v05", "task": "new metrics test",
            "created": "20260527-120000", "status": "completed",
            "subtasks": [{"id": "sub-1"}],
            "results": [{
                "subtask_id": "sub-1", "status": "completed",
                "duration_sec": 95.3, "retry_count": 1, "verify_ok": True,
                "agent_type_source": "llm",
                "timing": {"worktree_create_ms": 320, "merge_upstream_ms": 150,
                           "claude_execute_ms": 93000, "verification_ms": 1500, "git_commit_ms": 200},
                "change_stats": {"files_changed": 3, "insertions": 45, "deletions": 2,
                                 "new_files": 1, "modified_files": 2,
                                 "actual_files": ["cli.py", "version.py"]},
                "merge_results": [{"upstream": "sub-0", "status": "success"}],
                "verification_results": [{"command": "pytest", "exit_code": 0,
                                          "duration_ms": 1500, "attempt": 1}],
            }],
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta))
        (tmp_path / "execution.log").write_text("2026-05-27 12:00:00 | INFO | x | log\n")

        result = _get_task_status(tmp_path)
        assert result is not None
        r = result["results"][0]
        assert r["retry_count"] == 1
        assert r["timing"]["claude_execute_ms"] == 93000
        assert r["change_stats"]["files_changed"] == 3
        assert r["merge_results"][0]["status"] == "success"
        assert r["verification_results"][0]["exit_code"] == 0


class TestTailLines:

    def test_empty_for_missing_file(self, tmp_path):
        from agent_go.tui import _get_tail_lines
        assert _get_tail_lines(tmp_path / "nonexistent.log") == []

    def test_returns_last_n_lines(self, tmp_path):
        from agent_go.tui import _get_tail_lines
        log = tmp_path / "test.log"
        lines = [f"2026-05-27 12:00:{i:02d} | INFO | test | log line {i}" for i in range(20)]
        log.write_text("\n".join(lines))
        result = _get_tail_lines(log, 5)
        assert len(result) <= 5
        assert "log line 19" in result[-1]

    def test_filters_non_pipe_lines(self, tmp_path):
        from agent_go.tui import _get_tail_lines
        log = tmp_path / "test.log"
        log.write_text("plain text line\n2026-05-27 12:00:00 | INFO | x | valid\n")
        result = _get_tail_lines(log, 10)
        assert len(result) == 1
        assert "valid" in result[0]


class TestCLIRouting:

    def test_tui_mode_called_by_default(self):
        with patch("sys.argv", ["agent_go", "status"]):
            with patch("agent_go.cli.cmd_status_tui") as mock_tui:
                from agent_go.cli import cmd_status
                cmd_status()
                assert mock_tui.called

    def test_no_tui_routes_to_text(self):
        with patch("sys.argv", ["agent_go", "status", "--no-tui"]):
            with patch("agent_go.cli.AGENT_GO_DIR") as mock_dir:
                mock_dir.glob.return_value = []
                from agent_go.cli import cmd_status
                cmd_status()


class TestTuiModule:

    def test_all_functions_importable(self):
        from agent_go.tui import _get_task_status, _get_tail_lines, cmd_status_tui
        from agent_go.tui import ICONS, STATUS_COLORS
        assert callable(_get_task_status)
        assert callable(_get_tail_lines)
        assert callable(cmd_status_tui)

    def test_icons_cover_all_statuses(self):
        from agent_go.tui import ICONS
        for s in ["completed", "no_changes", "degraded", "running", "failed", "paused", "aborted"]:
            assert s in ICONS, f"missing icon for {s}"

    def test_colors_cover_all_statuses(self):
        from agent_go.tui import STATUS_COLORS
        for s in ["completed", "no_changes", "degraded", "running", "failed", "paused", "aborted"]:
            assert s in STATUS_COLORS, f"missing color for {s}"
