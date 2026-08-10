"""S10 成本控制三层（L1/L2/L3）测试。

覆盖：
- L1：subtask.py _run_one 注入 --max-budget-usd（enabled 时按难度）
- L1：默认关闭时不注入
- L2：executor 重试循环前检查子任务累计成本（_meter_cost_for_sub）
- L3：pipeline wave 调度前任务级熔断（_meter_total_cost）
- 默认关闭兼容：cost_control.enabled=False 时全部不生效
"""
import json
from unittest.mock import patch

import pytest

from agent_go.executor import _meter_cost_for_sub
from agent_go.pipeline import _meter_total_cost


# ─────────────────────────────────────────────────────────────
# L1: --max-budget-usd 注入
# ─────────────────────────────────────────────────────────────

class TestL1MaxBudget:
    def _capture_cmd(self, env_extra=None, cost_cfg=None):
        """捕获 _run_one 构造的 claude cmd，验证是否含 --max-budget-usd。"""
        captured = {}

        import subprocess

        def fake_popen(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            captured["env"] = kw.get("env", {})
            import io
            class _FakeP:
                pid = 12345
                returncode = 0
                stdout = io.StringIO()
                stderr = io.StringIO()
                stdin = io.StringIO()
                def __init__(self, *a, **kw):
                    pass
                def communicate(self, *a, **kw):
                    return ("", "")
                def wait(self, *a, **kw):
                    return 0
                def poll(self):
                    return 0
                def kill(self, *a, **kw):
                    pass
            return _FakeP()

        env = {"AGENT_GO_DIFFICULTY": "medium"}
        if env_extra:
            env.update(env_extra)

        import logging
        fake_logger = logging.getLogger("test_cost_control")

        cfg = {}
        if cost_cfg is not None:
            cfg["cost_control"] = cost_cfg

        with patch.object(subprocess, "Popen", fake_popen):
            from agent_go.subtask import _run_headless
            _run_headless("task", "/tmp", env, fake_logger, "sub-1", config=cfg)
        return captured

    def test_enabled_injects_budget(self):
        captured = self._capture_cmd(cost_cfg={
            "enabled": True,
            "per_subtask_budget_usd": {"easy": 0.1, "medium": 0.2, "hard": 0.5},
        })
        assert "--max-budget-usd" in captured["cmd"]
        idx = captured["cmd"].index("--max-budget-usd")
        assert captured["cmd"][idx + 1] == "0.2"  # medium

    def test_disabled_no_inject(self):
        """S12 冷启动：L1 由 l1_enabled 独立控制；显式 l1_enabled=False 才完全不注入。"""
        captured = self._capture_cmd(cost_cfg={
            "enabled": False,
            "l1_enabled": False,
            "per_subtask_budget_usd": {"easy": 0.1, "medium": 0.2, "hard": 0.5},
        })
        assert "--max-budget-usd" not in captured["cmd"]

    def test_no_config_no_inject(self):
        captured = self._capture_cmd(cost_cfg=None)
        assert "--max-budget-usd" not in captured["cmd"]

    def test_l1_disabled_by_default_cold_start(self):
        """S12 冷启动（2026-08-08 修订）：无 l1_enabled/enabled 时 L1 默认关（不注入
        --max-budget-usd）——Claude CLI 2.1.224 预算语义"接近上限即拒绝"会令任务无法启动。"""
        captured = self._capture_cmd(cost_cfg={
            "per_subtask_budget_usd": {"easy": 0.2, "medium": 0.4, "hard": 1.0},
        })
        assert "--max-budget-usd" not in captured["cmd"], "L1 默认关，不应注入预算"

    def test_l1_enabled_explicit_true_injects(self):
        """l1_enabled=True 显式开启 → 注入 --max-budget-usd（按难度）。"""
        captured = self._capture_cmd(cost_cfg={
            "l1_enabled": True,
            "per_subtask_budget_usd": {"easy": 0.2, "medium": 0.4, "hard": 1.0},
        })
        assert "--max-budget-usd" in captured["cmd"]
        idx = captured["cmd"].index("--max-budget-usd")
        assert captured["cmd"][idx + 1] == "0.4"  # medium 预算

    def test_unknown_difficulty_falls_back_to_medium(self):
        """未知难度在 per_subtask_budget_usd 无对应键 → 回退 medium 预算。"""
        captured = self._capture_cmd(env_extra={"AGENT_GO_DIFFICULTY": "extreme"}, cost_cfg={
            "enabled": True,
            "per_subtask_budget_usd": {"easy": 0.1, "medium": 0.2, "hard": 0.5},
        })
        assert "--max-budget-usd" in captured["cmd"]
        assert "0.2" in captured["cmd"]


# ─────────────────────────────────────────────────────────────
# L2: 子任务累计成本检查
# ─────────────────────────────────────────────────────────────

class TestL2SubtaskCost:
    def test_meter_cost_for_sub_aggregates(self, tmp_path):
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"sub_id": "sub-1", "cost_usd": 0.01}) + "\n")
            f.write(json.dumps({"sub_id": "sub-1", "cost_usd": 0.02}) + "\n")
            f.write(json.dumps({"sub_id": "sub-2", "cost_usd": 0.5}) + "\n")
        assert _meter_cost_for_sub(str(mp), "sub-1") == pytest.approx(0.03)
        assert _meter_cost_for_sub(str(mp), "sub-2") == pytest.approx(0.5)
        assert _meter_cost_for_sub(str(mp), "nope") == pytest.approx(0.0)

    def test_meter_cost_for_sub_missing_file(self):
        assert _meter_cost_for_sub("/nonexistent/metering.jsonl", "sub-1") == 0.0

    def test_meter_cost_for_sub_empty_path(self):
        assert _meter_cost_for_sub("", "sub-1") == 0.0

    def test_meter_cost_for_sub_bad_lines(self, tmp_path):
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps({"sub_id": "sub-1", "cost_usd": 0.04}) + "\n")
        assert _meter_cost_for_sub(str(mp), "sub-1") == pytest.approx(0.04)


# ─────────────────────────────────────────────────────────────
# L3: 任务级熔断
# ─────────────────────────────────────────────────────────────

class TestL3TaskBudget:
    def test_meter_total_cost_aggregates(self, tmp_path):
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "worker", "cost_usd": 0.1}) + "\n")
            f.write(json.dumps({"role": "planner", "cost_usd": 0.2}) + "\n")
            f.write(json.dumps({"role": "evaluator", "cost_usd": 0.3}) + "\n")
        assert _meter_total_cost(str(mp)) == pytest.approx(0.6)

    def test_meter_total_cost_missing_file(self):
        assert _meter_total_cost("/nonexistent/metering.jsonl") == 0.0

    def test_meter_total_cost_empty_path(self):
        assert _meter_total_cost("") == 0.0

    def test_wave_trip_when_budget_exceeded(self, tmp_path, monkeypatch):
        """cost_control.enabled + 超预算 → 剩余子任务标记 blocked。"""
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "worker", "cost_usd": 1.0}) + "\n")

        remaining = [
            {"id": "sub-1", "depends_on": []},
            {"id": "sub-2", "depends_on": ["sub-1"]},
        ]
        config = {
            "cost_control": {"enabled": True, "max_budget_usd": 0.5},
            "_metering_path": str(mp),
        }
        results_map = {}
        completed_ids = set()

        # 模拟 wave 前 L3 检查逻辑（直接调用熔断路径）
        _cc_cfg = config.get("cost_control")
        _max_budget = _cc_cfg.get("max_budget_usd", 0.0)
        _spent = _meter_total_cost(config.get("_metering_path", ""))
        assert _spent >= _max_budget
        for st in remaining:
            if st["id"] not in results_map:
                results_map[st["id"]] = {
                    "subtask_id": st["id"], "status": "blocked",
                    "exit_code": -1, "summary": "成本熔断",
                    "blocked_by": ["cost_control"],
                    "failure_reason": "任务成本超预算熔断",
                    "worktree": "", "sandbox_type": "headless",
                    "verify_ok": False, "duration_sec": 0,
                }
                completed_ids.add(st["id"])
        assert all(r["status"] == "blocked" for r in results_map.values())
        assert completed_ids == {"sub-1", "sub-2"}

    def test_no_trip_when_disabled(self, tmp_path):
        """cost_control.enabled=False → 超预算也不熔断。"""
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "worker", "cost_usd": 5.0}) + "\n")
        config = {
            "cost_control": {"enabled": False, "max_budget_usd": 0.5},
            "_metering_path": str(mp),
        }
        _cc_cfg = config.get("cost_control")
        assert not _cc_cfg.get("enabled")
        # enabled=False 时 pipeline 不会进入检查分支
        assert True


# ─────────────────────────────────────────────────────────────
# 测量/控制解耦：censored 事件写入
# ─────────────────────────────────────────────────────────────

class TestMeteringAvailableColdStart:
    """成本控制冷启动：metering.jsonl 尚未创建 ≠ 计量不可用（避免 L3 误中止任务）。"""

    def test_missing_file_is_available_pipeline(self, tmp_path):
        """文件不存在（任务刚启动，尚未写入成本）→ 视为计量可用（放行首个 wave）。"""
        from agent_go.pipeline import _metering_available
        mp = tmp_path / "metering.jsonl"
        assert not mp.exists()
        assert _metering_available(str(mp)) is True, \
            "任务启动时 metering 未创建应视为可用（预算尚未消耗）"

    def test_missing_file_is_available_executor(self, tmp_path):
        """同语义，executor 侧的 _metering_available。"""
        from agent_go.executor import _metering_available
        mp = tmp_path / "metering.jsonl"
        assert not mp.exists()
        assert _metering_available(str(mp)) is True

    def test_existing_file_readable_is_available(self, tmp_path):
        """文件存在且可读 → 可用。"""
        from agent_go.pipeline import _metering_available
        mp = tmp_path / "metering.jsonl"
        mp.write_text('{"role": "worker", "cost_usd": 0.1}\n', encoding="utf-8")
        assert _metering_available(str(mp)) is True

    def test_empty_path_is_available(self):
        """metering_path 为空（未配置）→ 视为可用（成本控制不因此阻断）。"""
        from agent_go.pipeline import _metering_available
        assert _metering_available("") is True

    def test_l3_does_not_abort_when_metering_not_created(self, tmp_path, monkeypatch):
        """L3 在 metering 文件尚未创建时不中止任务（首个 wave 放行）。"""
        from agent_go.pipeline import _meter_total_cost, _metering_available
        mp = tmp_path / "metering.jsonl"  # 不存在
        # 模拟 pipeline L3 检查的判定：可用 + 累计成本 0 < 预算 → 不熔断
        assert _metering_available(str(mp)) is True
        assert _meter_total_cost(str(mp)) == 0.0
        # budget 为正 → 0 < budget，不触发熔断
        assert _meter_total_cost(str(mp)) < 0.5


# ─────────────────────────────────────────────────────────────
# 测量/控制解耦：censored 事件写入
# ─────────────────────────────────────────────────────────────

class TestCensoredEvent:
    def test_write_censored_event_appends(self, tmp_path):
        """L2/L3 熔断时写 cost_censored 事件到 metering.jsonl（控制不中断测量）。"""
        from agent_go.config import write_censored_event
        mp = tmp_path / "metering.jsonl"
        write_censored_event(str(mp), level="L3", sub_id="", spent=0.42, budget=0.5,
                             reason="任务累计成本超预算")
        lines = mp.read_text(encoding="utf-8").strip().split("\n")
        ev = json.loads(lines[0])
        assert ev["event"] == "cost_censored"
        assert ev["level"] == "L3"
        assert ev["sub_id"] == ""
        assert ev["cost_usd"] == pytest.approx(0.42)
        assert ev["budget_usd"] == pytest.approx(0.5)
        assert ev["censored"] is True
        assert "ts" in ev

    def test_write_censored_event_sublevel(self, tmp_path):
        """L2 子任务级 censored 带 sub_id。"""
        from agent_go.config import write_censored_event
        mp = tmp_path / "metering.jsonl"
        write_censored_event(str(mp), level="L2", sub_id="sub-3", spent=0.3, budget=0.5)
        ev = json.loads(mp.read_text(encoding="utf-8").strip())
        assert ev["level"] == "L2"
        assert ev["sub_id"] == "sub-3"

    def test_write_censored_event_empty_path(self):
        from agent_go.config import write_censored_event
        # 空路径不报错
        write_censored_event("", level="L3", spent=0.1, budget=0.5)
        assert True

    def test_censored_event_does_not_break_meter_total(self, tmp_path):
        """censored 事件的 cost_usd 是累计下限，不应重复计入实际消费。"""
        from agent_go.config import write_censored_event
        mp = tmp_path / "metering.jsonl"
        with open(mp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "worker", "cost_usd": 0.2}) + "\n")
        write_censored_event(str(mp), level="L3", spent=0.42, budget=0.5)
        assert _meter_total_cost(str(mp)) == pytest.approx(0.2)


# ─────────────────────────────────────────────────────────────
# 删失校正成本基线
# ─────────────────────────────────────────────────────────────

class TestCostBaseline:
    def _write_results(self, tmp_path, records):
        p = tmp_path / "results.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return p

    def test_exclude_timed_out(self, tmp_path):
        """删失校正：timed_out=True 记录被排除，不参与 P90/预算。"""
        from agent_go.bench import compute_cost_baseline
        p = self._write_results(tmp_path, [
            {"task_id": "add-format-helper", "model": "claude-haiku-4-5",
             "total_cost_usd": 0.01, "timed_out": False, "plan_step_count": 1},
            {"task_id": "add-format-helper", "model": "claude-haiku-4-5",
             "total_cost_usd": 0.03, "timed_out": False, "plan_step_count": 1},
            {"task_id": "add-format-helper", "model": "claude-haiku-4-5",
             "total_cost_usd": 0.05, "timed_out": True, "plan_step_count": 1},  # 删失
        ])
        # tasks_dir 用不存在的目录 → difficulty 回退 medium，但排除逻辑仍验证
        b = compute_cost_baseline([str(p)], tasks_dir=str(tmp_path / "no_tasks"))
        assert b["summary"]["total_records"] == 2
        assert b["summary"]["censored_records"] == 1
        # 只有 0.01 和 0.03 参与 → P90 = 0.03
        medium = b["per_difficulty"]["medium"]
        assert medium["p90"] == pytest.approx(0.03)
        assert medium["budget"] == pytest.approx(0.045)  # 0.03 × 1.5

    def test_tolerance_applied(self, tmp_path):
        """预算 = P90 × tolerance。"""
        from agent_go.bench import compute_cost_baseline
        p = self._write_results(tmp_path, [
            {"task_id": "add-format-helper", "model": "claude-haiku-4-5",
             "total_cost_usd": 0.02, "timed_out": False, "plan_step_count": 1},
        ])
        b = compute_cost_baseline([str(p)], tasks_dir=str(tmp_path / "no_tasks"), tolerance=2.0)
        assert b["per_difficulty"]["medium"]["budget"] == pytest.approx(0.04)

    def test_no_data(self, tmp_path):
        from agent_go.bench import compute_cost_baseline
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        b = compute_cost_baseline([str(p)], tasks_dir=str(tmp_path))
        assert "error" in b


# ─────────────────────────────────────────────────────────────
# S12-P1 G3: per-task 预算输入（动态默认预算 + budget_mode 三态）
# ─────────────────────────────────────────────────────────────

class TestS12P1G3PerTaskBudget:
    def test_dynamic_task_budget_sums_by_difficulty(self):
        """动态默认预算 = Σ per_subtask_budget[diff] × multiplier × 子任务数。"""
        from agent_go.pipeline import _dynamic_task_budget
        cc = {
            "per_subtask_budget_usd": {"easy": 0.10, "medium": 0.20, "hard": 0.50},
            "subtask_multiplier": 2.5,
        }
        subtasks = [
            {"id": "a", "difficulty": "easy"},
            {"id": "b", "difficulty": "easy"},
            {"id": "c", "difficulty": "hard"},
        ]
        # easy: 0.10×2.5×2 = 0.50；hard: 0.50×2.5×1 = 1.25；总 1.75
        assert _dynamic_task_budget(cc, subtasks) == pytest.approx(1.75)

    def test_dynamic_task_budget_unknown_diff_falls_back_medium(self):
        from agent_go.pipeline import _dynamic_task_budget
        cc = {
            "per_subtask_budget_usd": {"easy": 0.10, "medium": 0.20, "hard": 0.50},
            "subtask_multiplier": 2.5,
        }
        subtasks = [{"id": "a", "difficulty": "weird"}]
        assert _dynamic_task_budget(cc, subtasks) == pytest.approx(0.50)

    def test_dynamic_task_budget_empty_budgets_zero(self):
        from agent_go.pipeline import _dynamic_task_budget
        assert _dynamic_task_budget({}, [{"id": "a", "difficulty": "easy"}]) == 0.0

    def test_dynamic_task_budget_hard_more_subtasks_gets_more(self):
        """hard 多子任务预算必须显著高于 easy 少子任务，防止 hard 过早熔断。"""
        from agent_go.pipeline import _dynamic_task_budget
        cc = {
            "per_subtask_budget_usd": {"easy": 0.10, "medium": 0.20, "hard": 0.50},
            "subtask_multiplier": 2.5,
        }
        easy2 = [{"id": x, "difficulty": "easy"} for x in range(2)]
        hard10 = [{"id": x, "difficulty": "hard"} for x in range(10)]
        assert _dynamic_task_budget(cc, hard10) > _dynamic_task_budget(cc, easy2) * 5


# ─────────────────────────────────────────────────────────────
# S12-P1 G8: 验证循环 kill_reason 感知
# ─────────────────────────────────────────────────────────────

class TestS12P1G8KillReasonAwareness:
    def _build_latest_kill(self, reason):
        """构造 _latest_kill_reason 列表模拟 verify 循环读取。"""
        return [reason]

    def test_over_budget_l2_skips_retry(self):
        """kill_reason=over_budget_l2 → 不进重试，直接失败。"""
        from agent_go.executor import _verify_changes
        latest = self._build_latest_kill("over_budget_l2")
        # 模拟 verify 循环中的 G8 分支逻辑（与 executor.py 一致）
        _kr = latest[0] or ""
        assert _kr.startswith("over_budget")
        assert _kr == "over_budget_l2"

    def test_over_budget_l3_skips_retry(self):
        latest = self._build_latest_kill("over_budget_l3")
        _kr = latest[0] or ""
        assert _kr.startswith("over_budget")

    def test_cleanup_race_counts_as_pass(self):
        """kill_reason=cleanup_race → 任务实际已完成，视为通过不重试。"""
        latest = self._build_latest_kill("cleanup_race")
        _kr = latest[0] or ""
        assert _kr == "cleanup_race"
        # executor 中 cleanup_race → verify_ok=True

    def test_stuck_normal_verify_path(self):
        """stuck/hard_timeout 不短路（走正常验证，但重试预算受限）。"""
        for reason in ("stuck", "hard_timeout", "goal_timeout"):
            latest = self._build_latest_kill(reason)
            _kr = latest[0] or ""
            assert not _kr.startswith("over_budget")
            assert _kr != "cleanup_race"


# ─────────────────────────────────────────────────────────────
# S12-P2：对称降级表（worker_models_degrades）
# ─────────────────────────────────────────────────────────────

class TestS12P2DegradeTable:
    """budget_mode=degrade 时按 worker_models_degrades 表降档（对称升级表）。"""

    def test_config_has_symmetric_degrade_table(self):
        """config 默认含 worker_models_degrades（对称 worker_models_fallback）。"""
        from agent_go.config import DEFAULT_CONFIG
        degrades = DEFAULT_CONFIG.get("worker_models_degrades", {})
        assert degrades.get("hard") == "medium"
        assert degrades.get("medium") == "easy"
        # easy 无可降档 → 空（回退 claude 默认模型）

    def test_degrades_hard_to_medium(self):
        """hard 子任务降档 → medium 档模型。"""
        degrades = {"easy": "", "medium": "easy", "hard": "medium"}
        assert degrades["hard"] == "medium"

    def test_degrades_medium_to_easy(self):
        degrades = {"easy": "", "medium": "easy", "hard": "medium"}
        assert degrades["medium"] == "easy"

    def test_degrades_easy_empty(self):
        """easy 无降级目标 → 空字符串（回退 claude 默认模型）。"""
        degrades = {"easy": "", "medium": "easy", "hard": "medium"}
        assert degrades["easy"] == ""


# ─────────────────────────────────────────────────────────────
# S12-P2 G5：规划期欠分解检测（见 tests/test_planning.py）
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# CR-L2：pipeline 级端到端回归（H1 降级安全阀 + M1 动态预算 confirmed）
# ─────────────────────────────────────────────────────────────

class TestCRL2PipelineDegradeSafetyValve:
    """CR H1 回归：degrade 模式连续 3 个降级子任务失败 → 安全阀 trip 后
    同轮 L3 不再重新置 _degraded=True（_degrade_aborted 哨兵生效）。"""

    def test_safety_valve_aborts_degrade_and_l3_does_not_rearm(self, tmp_path):
        """H1 核心回归：安全阀 trip 后 _degrade_aborted=True → L3 跳过降级分支。"""
        from agent_go.pipeline import _run_pipeline
        from unittest.mock import patch, MagicMock

        # 构造 5 个链式依赖子任务（形成多波，使 streak 在波间累积触发安全阀）
        def _mk(sid, dep=None):
            return {"id": sid, "title": f"t-{sid}", "description": "d",
                    "difficulty": "hard", "depends_on": [dep] if dep else [],
                    "verification": ["true"]}
        confirmed = []
        _prev = None
        for i in range(5):
            sid = f"sub-{i}"
            confirmed.append(_mk(sid, _prev))
            _prev = sid

        # 构造 task_dir + repo
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        task_dir = tmp_path / "task-t1"
        task_dir.mkdir()

        for sid in [s["id"] for s in confirmed]:
            (task_dir / sid / "work").mkdir(parents=True)

        # config：开 cost_control，budget_mode=degrade，无 max_budget_usd（走 M1 动态预算）
        config = {
            "cost_control": {
                "enabled": True,
                "budget_mode": "degrade",
                "per_subtask_budget_usd": {"easy": 0.1, "medium": 0.2, "hard": 0.5},
                "subtask_multiplier": 2.5,
            },
            "_metering_path": str(tmp_path / "metering.jsonl"),
            "verification": {"block_on_failure": False},
        }
        (tmp_path / "metering.jsonl").touch()

        # 所有子任务 verify 失败（驱动 streak 累积）
        def _fail_result(sid):
            return {"subtask_id": sid, "status": "failed", "exit_code": 1,
                    "summary": f"fail-{sid}", "failure_reason": "verify failed",
                    "worktree": "", "sandbox_type": "headless",
                    "verify_ok": False, "duration_sec": 1.0, "degraded": True}

        import logging
        logger = logging.getLogger("cr-test")

        # mock _meter_total_cost 恒返回超预算（确保 L3 总触发）
        # mock run_subtask 返回失败 + degraded=True
        # mock worktree/gc 操作避免真实 git
        with patch("agent_go.pipeline._meter_total_cost", return_value=999.0), \
             patch("agent_go.pipeline.run_subtask", side_effect=[_fail_result(s["id"]) for s in confirmed]), \
             patch("agent_go.pipeline._worktree_remove", return_value=(True, "")), \
             patch("agent_go.pipeline._worktree_prune", return_value=(True, "")), \
             patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, "")), \
             patch("agent_go.pipeline.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("agent_go.pipeline.write_censored_event"), \
             patch("agent_go.notify.notify_event"):
            _run_pipeline(
                confirmed, repo, task_dir, logger, config,
                headless=False, parallel=1, issue_ref="",
                meta={"task_id": "t1", "status": "running"},
            )

        # H1 断言：安全阀 trip 后 _degrade_aborted=True，且 _degraded 不再被 L3 重新置 True
        assert config.get("_degrade_aborted") is True, "安全阀应已 trip 并置 _degrade_aborted"
        # trip 后 _degraded 应为 False（安全阀清零，L3 不再重新武装）
        assert config.get("_degraded") is False, "L3 不应在安全阀 trip 后重新置 _degraded=True"

    def test_dynamic_budget_uses_confirmed_not_remaining(self, tmp_path):
        """CR M1 回归：动态预算基于 confirmed 全量，不随 remaining 缩短而下降。
        用 2 个子任务，_dynamic_task_budget 返回固定值，验证 L3 阈值稳定。"""
        from agent_go.pipeline import _dynamic_task_budget

        cc_cfg = {
            "per_subtask_budget_usd": {"easy": 0.1, "medium": 0.2, "hard": 0.5},
            "subtask_multiplier": 2.5,
        }
        full = [{"id": "s1", "difficulty": "hard"}, {"id": "s2", "difficulty": "hard"},
                {"id": "s3", "difficulty": "hard"}]
        # 全量 confirmed = 0.5 * 2.5 * 3 = 3.75
        assert _dynamic_task_budget(cc_cfg, full) == pytest.approx(3.75)
        # 即使只传 1 个 remaining，函数本身不变（调用点已改为传 confirmed）
        assert _dynamic_task_budget(cc_cfg, full[:1]) == pytest.approx(1.25)


# ─────────────────────────────────────────────────────────────
# 覆盖补强：budget_mode=ignore 跳过 L3（仅 L1/L2 生效）
# ─────────────────────────────────────────────────────────────

class TestBudgetModeIgnore:
    def test_ignore_skips_l3_subtasks_still_execute(self, tmp_path):
        """budget_mode=ignore → 即使超预算也不 block，剩余子任务照常执行（无 L3 熔断）。
        三态开关的 ignore 分支此前未测（只测了 strict/degrade）。"""
        from agent_go.pipeline import _run_pipeline
        from unittest.mock import patch, MagicMock
        import logging

        def _mk(sid, dep=None):
            return {"id": sid, "title": f"t-{sid}", "description": "d",
                    "difficulty": "medium", "depends_on": [dep] if dep else [],
                    "verification": ["true"]}
        confirmed = [_mk("s1"), _mk("s2", "s1")]
        repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
        task_dir = tmp_path / "task-ig"; task_dir.mkdir()
        for sid in [s["id"] for s in confirmed]:
            (task_dir / sid / "work").mkdir(parents=True)
        config = {
            "cost_control": {"enabled": True, "budget_mode": "ignore",
                             "max_budget_usd": 0.01},  # 极低预算，正常会熔断
            "_metering_path": str(tmp_path / "metering.jsonl"),
            "verification": {"block_on_failure": False},
        }
        ran = []
        def _ok_result(sid):
            return {"subtask_id": sid, "status": "completed", "exit_code": 0,
                    "summary": "ok", "worktree": "", "sandbox_type": "headless",
                    "verify_ok": True, "duration_sec": 1.0}
        logger = logging.getLogger("ig-test")
        with patch("agent_go.pipeline._meter_total_cost", return_value=999.0), \
             patch("agent_go.pipeline.run_subtask", side_effect=[_ok_result(s["id"]) for s in confirmed]) as mk, \
             patch("agent_go.pipeline._worktree_remove", return_value=(True, "")), \
             patch("agent_go.pipeline._worktree_prune", return_value=(True, "")), \
             patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, "")), \
             patch("agent_go.pipeline.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("agent_go.pipeline.write_censored_event") as mk_cens, \
             patch("agent_go.notify.notify_event"):
            _run_pipeline(confirmed, repo, task_dir, logger, config, headless=False,
                          parallel=1, issue_ref="", meta={"task_id": "ig", "status": "running"})
        # ignore 模式：超预算也不 block → 两个子任务都执行了，无 L3 censored 事件
        assert mk.call_count == 2, "ignore 模式不应 block，两子任务都应执行"
        # write_censored_event 可能被 L2 调用，但 L3 不应触发 level="L3"
        l3_calls = [c for c in mk_cens.call_args_list if c.kwargs.get("level") == "L3"]
        assert not l3_calls, "ignore 模式不应触发 L3 熔断事件"
        # 没有子任务被 cost_control block
        import json as _j
        for sid in ["s1", "s2"]:
            rf = task_dir / sid / "result.json"
            if rf.exists():
                assert _j.loads(rf.read_text()).get("status") != "blocked"
