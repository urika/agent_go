"""M3 PR 质量仪表补缺 / M4 时间预估 / S4 复杂度双通道 测试"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.eval import estimate_task_duration
from agent_go.cli import _build_quality_dashboard
from agent_go.ui import plan_to_subtasks
from agent_go.executor import run_subtask
from agent_go.subtask import _run_headless


# ═══════════════════════════════════════════════════════════════
# M4 时间预估
# ═══════════════════════════════════════════════════════════════

def _mk_task_dir(base: Path, name: str, durations: list[float]):
    td = base / name
    td.mkdir(parents=True)
    meta = {
        "task_id": name, "status": "completed",
        "results": [{"subtask_id": f"s{i}", "status": "completed", "duration_sec": d}
                    for i, d in enumerate(durations)],
    }
    (td / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TestEstimateDuration:
    def test_waves_from_dependencies(self, tmp_path):
        chain = [
            {"id": "sub-1", "depends_on": []},
            {"id": "sub-2", "depends_on": ["sub-1"]},
            {"id": "sub-3", "depends_on": ["sub-2"]},
        ]
        est = estimate_task_duration(chain, 1, tmp_path)
        assert est["waves"] == 3

        indep = [{"id": f"sub-{i}", "depends_on": []} for i in range(3)]
        assert estimate_task_duration(indep, 1, tmp_path)["waves"] == 1

    def test_dependency_cycle_safe(self, tmp_path):
        cyc = [
            {"id": "sub-1", "depends_on": ["sub-2"]},
            {"id": "sub-2", "depends_on": ["sub-1"]},
        ]
        est = estimate_task_duration(cyc, 1, tmp_path)  # 不死循环
        assert est["waves"] >= 1

    def test_median_from_history(self, tmp_path):
        for i in range(6):
            _mk_task_dir(tmp_path, f"task-{i:03d}", [100.0, 300.0])
        subtasks = [{"id": "sub-1", "depends_on": []}]
        est = estimate_task_duration(subtasks, 1, tmp_path)
        assert est["sample_size"] == 12
        assert est["median_subtask_sec"] == 300.0  # 排序后中间值
        assert est["estimated_sec"] == 300
        assert est["confidence"] == "medium"

    def test_no_history_fallback(self, tmp_path):
        est = estimate_task_duration([{"id": "sub-1", "depends_on": []}], 1, tmp_path)
        assert est["confidence"] == "none"
        assert est["median_subtask_sec"] == 240

    def test_parallel_reduces_estimate(self, tmp_path):
        _mk_task_dir(tmp_path, "task-001", [200.0, 200.0, 200.0, 200.0, 200.0])
        indep = [{"id": f"sub-{i}", "depends_on": []} for i in range(4)]
        serial = estimate_task_duration(indep, 1, tmp_path)
        parallel = estimate_task_duration(indep, 4, tmp_path)
        assert serial["estimated_sec"] == 800      # 4 × 200
        assert parallel["estimated_sec"] == 200    # max(1 波次 × 200, 800/4)


# ═══════════════════════════════════════════════════════════════
# M3 PR 质量仪表（补缺：blocked 图标 + M5 置信度警告）
# ═══════════════════════════════════════════════════════════════

class TestQualityDashboard:
    def test_blocked_icon_shown(self):
        meta = {
            "subtasks": [{"id": "sub-1"}, {"id": "sub-2"}],
            "results": [
                {"subtask_id": "sub-1", "status": "failed", "verify_ok": False,
                 "duration_sec": 10, "summary": "x", "failure_reason": "pytest exit=1"},
                {"subtask_id": "sub-2", "status": "blocked", "verify_ok": False,
                 "duration_sec": 0, "summary": "上游失败"},
            ],
        }
        out = _build_quality_dashboard(meta)
        assert "🔗 blocked" in out
        assert "不建议合并" in out

    def test_m5_weak_verification_warning(self):
        meta = {
            "subtasks": [{"id": "sub-1"}],
            "results": [
                {"subtask_id": "sub-1", "status": "completed", "verify_ok": True,
                 "duration_sec": 10, "summary": "ok",
                 "verification_confidence": {"level": "low", "warning": "仅启发式检查"}},
            ],
        }
        out = _build_quality_dashboard(meta)
        assert "启发式检查" in out
        assert "sub-1" in out


# ═══════════════════════════════════════════════════════════════
# S4 复杂度双通道
# ═══════════════════════════════════════════════════════════════

class TestDifficultyPropagation:
    def test_plan_to_subtasks_keeps_difficulty(self, logger):
        plan = {
            "steps": [
                {"id": 1, "title": "简单任务", "difficulty": "easy"},
                {"id": 2, "title": "复杂任务", "difficulty": "hard"},
                {"id": 3, "title": "非法值", "difficulty": "extreme"},
                {"id": 4, "title": "未标注"},
            ],
            "dependencies": {},
        }
        subtasks = plan_to_subtasks(plan, logger)
        assert [st["difficulty"] for st in subtasks] == ["easy", "hard", "medium", "medium"]


@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "source_repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Test", encoding="utf-8")
    return repo


@pytest.fixture
def task_dir(tmp_path):
    d = tmp_path / ".agent_go" / "task-s4"
    d.mkdir(parents=True)
    return d


def _mock_cp(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestModelRouting:
    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_hard_routes_to_configured_model(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """difficulty=hard + worker_models.hard 配置 → env 注入模型"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.return_value = _mock_cp()

        subtask = {
            "id": "sub-1", "title": "难任务", "description": "d",
            "agent_prompt": "work", "verification": "",
            "risks": [], "depends_on": [], "skills": [],
            "agent_type": "developer", "difficulty": "hard",
        }
        run_subtask("test-task", subtask, temp_repo, task_dir, logger,
                    headless=True,
                    config={"worker_models": {"hard": "claude-opus-4-20250514"}})

        env = mock_headless.call_args_list[0][0][2]
        assert env["AGENT_GO_CLAUDE_MODEL"] == "claude-opus-4-20250514"
        assert env["AGENT_GO_DIFFICULTY"] == "hard"

    @patch("agent_go.executor.load_agent_type", return_value=None)
    @patch("agent_go.executor._run_headless")
    @patch("subprocess.run")
    @patch("agent_go.executor._worktree_create")
    def test_no_config_no_model_env(
        self, mock_wt, mock_subprocess, mock_headless, mock_agent,
        temp_repo, task_dir, logger,
    ):
        """worker_models 未配置（空值）→ 不设置模型 env，走 CLI 默认"""
        mock_wt.return_value = (True, "")
        mock_headless.return_value = _mock_cp(returncode=0)
        mock_subprocess.return_value = _mock_cp()

        subtask = {
            "id": "sub-1", "title": "易任务", "description": "d",
            "agent_prompt": "work", "verification": "",
            "risks": [], "depends_on": [], "skills": [],
            "agent_type": "developer", "difficulty": "easy",
        }
        run_subtask("test-task", subtask, temp_repo, task_dir, logger,
                    headless=True, config={"worker_models": {}})

        env = mock_headless.call_args_list[0][0][2]
        assert "AGENT_GO_CLAUDE_MODEL" not in env
        assert env["AGENT_GO_DIFFICULTY"] == "easy"

    @patch("subprocess.Popen")
    def test_headless_passes_model_and_meters_it(self, mock_popen, logger, tmp_path):
        """_run_headless 把路由模型传给 claude --model，计量记录真实模型与 difficulty"""
        metering = tmp_path / "metering.jsonl"
        result_event = json.dumps({
            "type": "result", "subtype": "success", "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        mock_proc = MagicMock()
        mock_proc.pid = 12360
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = [result_event + "\n", "", ""]
        mock_proc.stderr.readline.side_effect = ["", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        env = {
            "AGENT_GO_CLAUDE_MODEL": "claude-opus-4-20250514",
            "AGENT_GO_DIFFICULTY": "hard",
            "AGENT_GO_METERING_PATH": str(metering),
            "AGENT_GO_TASK_ID": "task-s4",
        }
        _run_headless("task", Path("/tmp/work"), env, logger, "sub-1")

        cmd = mock_popen.call_args[0][0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-20250514"

        ev = json.loads(metering.read_text(encoding="utf-8").strip())
        assert ev["actual_model"] == "claude-opus-4-20250514"
        assert ev["difficulty"] == "hard"
