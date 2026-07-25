"""复杂产品开发场景的端到端（E2E）验证。

设计原则（PRD 对齐）：
  - 不调真实 LLM/Claude/git，但构造**真实的 pipeline 落盘产物**（metering.jsonl + meta.json +
    execution.log），模拟 pipeline 跑完后的状态，驱动 analyze_cost/gate_cost/analyze_reliability，
    断言「复杂场景下的指标计算与门禁判定」。
  - 跨模块断言：不仅断言 analyze_cost 返回值，还断言 gate_cost / cmd_eval / analyze_reliability
    对同一组产物的解读一致。
  - 可观测性断言：每个场景断言新增的 cost_source_breakdown / unknown_model_events /
    fallback_events 字段，确保计价失真可被发现。

覆盖 6 个场景：
  1. 多模型混合计费（验证 D1 修复：claude-code-executor 不再兜底 deepseek）
  2. 验证循环重试 + blocked 级联（验证 metering 累加 + $/pass 分母）
  3. router 熔断 + 降级（验证 fallback_reason 被读 + fallback_events 统计）
  4. Plan 缓存命中 vs 未命中（验证省钱的指标反映）
  5. 多波次拓扑 + 并发（验证拓扑对成本/可靠性的影响）
  6. gate 端到端（产物驱动，非 fixture 拼凑；覆盖绝对阈值 + 回归对比两种模式）
"""

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.eval import (
    analyze_cost, analyze_reliability,
    load_cost_baseline, save_cost_baseline, cmd_eval,
)


# ═══════════════════════════════════════════════════════════════
# 共享 fixture 工厂：构造真实 pipeline 产物
# ═══════════════════════════════════════════════════════════════

def _write_metering(task_dir: Path, events: list[dict]):
    """写 metering.jsonl（每行一个 JSON 事件，模拟 pipeline 跑完的落盘状态）。"""
    (task_dir / "metering.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
        encoding="utf-8")


def _write_meta(task_dir: Path, *, task_id: str, status: str,
                results: list[dict], issue: str = "", base_branch: str = "main"):
    """写 meta.json（模拟 pipeline 末尾写入的任务元数据）。"""
    (task_dir / "meta.json").write_text(json.dumps({
        "task_id": task_id, "task": f"任务 {task_id}", "status": status,
        "issue": issue, "base_branch": base_branch,
        "subtasks": [{"id": r["subtask_id"]} for r in results],
        "results": results,
        "created": "20260725-100000",
    }, ensure_ascii=False), encoding="utf-8")


def _write_log(task_dir: Path, events: list[dict]):
    """写 execution.log（紧凑 JSON 事件，匹配 eval._read_log_events 的解析格式）。

    eval._read_log_events 用正则 "event"\\s*:\\s*"name" 匹配，需用紧凑分隔符。
    """
    lines = [
        json.dumps({"timestamp": "2026-07-25T10:00:00", "event": ev["event"], **{k: v for k, v in ev.items() if k != "event"}},
                   separators=(",", ":"), ensure_ascii=False)
        for ev in events
    ]
    (task_dir / "execution.log").write_text("\n".join(lines), encoding="utf-8")


def _mk_task(base: Path, name: str) -> Path:
    td = base / name
    td.mkdir(parents=True, exist_ok=True)
    return td


def _result(sub_id: str, status: str, **kw):
    """构造单个 subtask result（meta.json results 数组元素）。"""
    r = {"subtask_id": sub_id, "status": status,
         "sandbox_type": "greywall", "duration_sec": 30.0,
         "retry_count": 0, "verify_ok": status == "completed",
         "summary": f"{sub_id} {status}"}
    r.update(kw)
    return r


# 真实 cost_usd（模拟 Claude 子进程的 total_cost_usd 写入）
def _worker_event(cost_usd: float, *, model="claude-code-executor", provider="claude-code",
                  prompt=2000, completion=500, result="success", fallback_reason="",
                  subtask_id="s1", difficulty="medium"):
    return {"role": "worker", "virtual_model": "agentgo-worker",
            "actual_provider": provider, "actual_model": model,
            "difficulty": difficulty,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "cost_usd": cost_usd, "latency_ms": 3000, "result": result,
            "fallback_reason": fallback_reason, "task_id": "t1", "subtask_id": subtask_id}


def _planner_event(cost_usd: float, *, model="claude-sonnet-4", provider="anthropic",
                   prompt=1500, completion=800):
    return {"role": "planner", "virtual_model": "agentgo-planner",
            "actual_provider": provider, "actual_model": model,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "cost_usd": cost_usd, "latency_ms": 5000, "result": "success",
            "fallback_reason": "", "task_id": "t1"}


def _evaluator_event(cost_usd: float, *, passed=True, model="claude-haiku-4-5-20251001",
                     prompt=500, completion=100, subtask_id="s1"):
    return {"role": "evaluator", "virtual_model": "agentgo-evaluator",
            "actual_provider": "anthropic", "actual_model": model,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "cost_usd": cost_usd, "latency_ms": 2000,
            "result": "success" if passed else "quality_fail",
            "fallback_reason": "", "task_id": "t1", "subtask_id": subtask_id}


# ═══════════════════════════════════════════════════════════════
# 场景 1：多模型混合计费（验证 D1 修复）
# ═══════════════════════════════════════════════════════════════

class TestScenario1MultiModelPricing:
    """多模型混合：planner(sonnet) + worker(claude-code-executor 真实 cost) +
    worker(opus 路由) + evaluator(haiku)。

    验证 D1/D2 修复：claude-code-executor 不再兜底 deepseek 低估成本，
    而是优先用真实 cost_usd。
    """

    def test_claude_code_executor_uses_real_cost(self, tmp_path):
        """claude-code-executor 事件用真实 cost_usd，不被兜底为 deepseek 单价"""
        td = _mk_task(tmp_path, "task-multi")
        events = [
            _planner_event(0.02),  # sonnet planner
            _worker_event(0.08, model="claude-code-executor", subtask_id="s1"),  # 真实 cost
            _worker_event(0.15, model="claude-opus-4-20250514", provider="anthropic",
                          subtask_id="s2"),  # 路由到 opus
            _evaluator_event(0.003, subtask_id="s1"),
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-multi", status="completed",
                    results=[_result("s1", "completed"), _result("s2", "completed")])

        report = analyze_cost(tmp_path)

        # D1 核心：所有真实 cost_usd 都进 metering 通道
        assert report["cost_source_breakdown"]["metering"] == pytest.approx(0.253, rel=1e-3)
        # 无未知模型事件（claude-code-executor 有 cost_usd，opus 有 cost_usd，都不需重算）
        assert report["unknown_model_events"] == 0
        # $/pass rate = 总真实成本 / 2 completed = 0.253/2 = 0.1265
        assert report["dollar_per_pass_rate"] == pytest.approx(0.1265, rel=1e-3)
        # by_role 三角色都有真实成本
        assert report["by_role"]["planner"]["cost_usd"] == pytest.approx(0.02)
        assert report["by_role"]["worker"]["cost_usd"] == pytest.approx(0.23)
        assert report["by_role"]["evaluator"]["cost_usd"] == pytest.approx(0.003)

    def test_missing_cost_usd_with_known_model_rebuilds(self, tmp_path):
        """缺 cost_usd 但模型在价目表 → 按 token 重算（rebuilt 通道）"""
        td = _mk_task(tmp_path, "task-rebuild")
        events = [
            # sonnet 但 cost_usd=0（旧日志或字段缺失）→ 按 token×sonnet 单价重算
            {"role": "planner", "actual_provider": "anthropic",
             "actual_model": "claude-sonnet-4",
             "prompt_tokens": 10000, "completion_tokens": 3000,
             "cost_usd": 0, "result": "success", "fallback_reason": ""},
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-rebuild", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_cost(tmp_path)
        # 重算：10000*3/1M + 3000*15/1M = 0.03 + 0.045 = 0.075
        assert report["cost_source_breakdown"]["rebuilt"] == pytest.approx(0.075, rel=1e-3)
        assert report["cost_source_breakdown"]["metering"] == 0.0
        assert report["estimated_cost_usd"] == pytest.approx(0.075, rel=1e-3)

    def test_unknown_model_without_cost_counted_as_unknown(self, tmp_path):
        """未知模型 + 缺 cost_usd → 计为 unknown_model_events（可观测失真）"""
        td = _mk_task(tmp_path, "task-unknown")
        events = [
            {"role": "worker", "actual_provider": "claude-code",
             "actual_model": "claude-code-executor",  # 不在 MODEL_PRICES
             "prompt_tokens": 5000, "completion_tokens": 2000,
             "cost_usd": 0,  # 且无真实成本 → 无法计价
             "result": "success", "fallback_reason": ""},
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-unknown", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_cost(tmp_path)
        assert report["unknown_model_events"] == 1
        assert report["estimated_cost_usd"] == 0.0  # 无法计价


# ═══════════════════════════════════════════════════════════════
# 场景 2：验证循环重试 + blocked 级联
# ═══════════════════════════════════════════════════════════════

class TestScenario2RetryAndBlocked:
    """sub-1 重试 3 次后 completed（4 条 worker metering）；sub-2 上游失败→blocked。

    验证：重试 token 全计入分子；blocked 不进 $/pass 分母但已花的 plan token 在分子。
    """

    def test_retry_tokens_included_in_cost(self, tmp_path):
        td = _mk_task(tmp_path, "task-retry")
        events = [
            _planner_event(0.02),
            # sub-1 重试 3 次 + 最终成功 = 4 条 worker 记录（模拟验证循环每次都起 Claude）
            _worker_event(0.03, subtask_id="s1", result="failed"),   # attempt 1 失败
            _worker_event(0.03, subtask_id="s1", result="failed"),   # attempt 2 失败
            _worker_event(0.03, subtask_id="s1", result="failed"),   # attempt 3 失败
            _worker_event(0.03, subtask_id="s1", result="success"),  # attempt 4 成功
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-retry", status="completed",
                    results=[_result("s1", "completed", retry_count=3)])

        report = analyze_cost(tmp_path)
        # 总成本：planner 0.02 + 4×worker 0.03 = 0.14
        assert report["estimated_cost_usd"] == pytest.approx(0.14, rel=1e-3)
        # 分母只有 1 个 completed → $/pass = 0.14（重试惩罚体现）
        assert report["dollar_per_pass_rate"] == pytest.approx(0.14, rel=1e-3)
        assert report["completed_subtasks"] == 1

    def test_blocked_excluded_from_denominator(self, tmp_path):
        """blocked 子任务不进 $/pass 分母，但其 plan token 已计入分子"""
        td = _mk_task(tmp_path, "task-blocked")
        events = [
            _planner_event(0.02),  # plan 阶段已花钱（含对 blocked 子任务的规划）
            _worker_event(0.05, subtask_id="s1", result="success"),  # s1 完成
            # s2 因上游失败被 blocked，不起 Claude，无 worker metering
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-blocked", status="completed",
                    results=[_result("s1", "completed"),
                             _result("s2", "blocked")])

        report = analyze_cost(tmp_path)
        # 分母 = 1（只有 s1 completed，s2 blocked 不算）
        assert report["completed_subtasks"] == 1
        # 分子 = 0.02 + 0.05 = 0.07（plan token 含 blocked 的规划成本）
        assert report["estimated_cost_usd"] == pytest.approx(0.07, rel=1e-3)
        assert report["dollar_per_pass_rate"] == pytest.approx(0.07, rel=1e-3)

    def test_retry_and_blocked_combined(self, tmp_path):
        """组合：重试 + blocked 级联（跨模块一致性：cost + reliability）"""
        td = _mk_task(tmp_path, "task-combo")
        events = [
            _planner_event(0.02),
            _worker_event(0.03, subtask_id="s1", result="failed"),
            _worker_event(0.04, subtask_id="s1", result="success"),
            # s2、s3 依赖 s1 的兄弟 s0（failed）→ 级联 blocked
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-combo", status="completed",
                    results=[_result("s1", "completed", retry_count=1),
                             _result("s2", "blocked"),
                             _result("s3", "blocked")])

        cost_report = analyze_cost(tmp_path)
        rel_report = analyze_reliability(tmp_path)

        # cost 一致性
        assert cost_report["completed_subtasks"] == 1
        assert cost_report["dollar_per_pass_rate"] == pytest.approx(0.09, rel=1e-3)
        # reliability 一致性
        assert rel_report["blocked"] == 2
        assert rel_report["blocked_rate"] == pytest.approx(66.7, rel=1e-2)


# ═══════════════════════════════════════════════════════════════
# 场景 3：router 熔断 + 降级
# ═══════════════════════════════════════════════════════════════

class TestScenario3RouterFallback:
    """worker primary 失败 → fallback 到弱模型（result="fallback", fallback_reason 非空）。

    验证 D5：fallback_reason 被读，fallback_events 统计正确，降级不计 errors。
    """

    def test_fallback_events_counted(self, tmp_path):
        td = _mk_task(tmp_path, "task-fallback")
        events = [
            _planner_event(0.02),
            # 正常 worker 调用
            _worker_event(0.05, subtask_id="s1", result="success"),
            # 降级调用（primary 熔断 → fallback 到弱模型）
            _worker_event(0.02, subtask_id="s2", model="deepseek-chat", provider="deepseek",
                          result="fallback", fallback_reason="circuit_open"),
            # 另一种降级标记方式（result=success 但 fallback_reason 非空）
            _worker_event(0.03, subtask_id="s3", result="success",
                          fallback_reason="primary_timeout"),
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-fallback", status="completed",
                    results=[_result("s1", "completed"), _result("s2", "completed"),
                             _result("s3", "completed")])

        report = analyze_cost(tmp_path)
        # D5 核心：fallback_events 计数正确（s2 + s3 = 2）
        assert report["fallback_events"] == 2
        # 降级不计入 errors（result=fallback 不在 failed/quality_fail）
        assert report["errors"] == 0

    def test_quality_fail_counted_as_error(self, tmp_path):
        """result=quality_fail 计入 errors（evaluator 语义评估失败）"""
        td = _mk_task(tmp_path, "task-qfail")
        events = [
            _planner_event(0.02),
            _worker_event(0.05, subtask_id="s1", result="success"),
            _evaluator_event(0.003, passed=False, subtask_id="s1"),  # quality_fail
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-qfail", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_cost(tmp_path)
        assert report["errors"] == 1
        assert report["fallback_events"] == 0  # quality_fail 不是降级


# ═══════════════════════════════════════════════════════════════
# 场景 4：Plan 缓存命中 vs 未命中
# ═══════════════════════════════════════════════════════════════

class TestScenario4PlanCache:
    """task-1 缓存命中（无 planner metering）；task-2 未命中（有 planner metering）。

    验证：缓存命中省 planner 成本，avg_cost_per_task 反映差异。
    """

    def test_cache_hit_costs_less_than_miss(self, tmp_path):
        # task-1：缓存命中，只花 worker 钱
        td1 = _mk_task(tmp_path, "task-cache-hit")
        _write_metering(td1, [_worker_event(0.05, subtask_id="s1")])
        _write_meta(td1, task_id="task-cache-hit", status="completed",
                    results=[_result("s1", "completed")])

        # task-2：缓存未命中，planner + worker 都花钱
        td2 = _mk_task(tmp_path, "task-cache-miss")
        _write_metering(td2, [_planner_event(0.02), _worker_event(0.05, subtask_id="s1")])
        _write_meta(td2, task_id="task-cache-miss", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_cost(tmp_path)
        # 缓存命中任务无 planner cost_usd
        by_role_planner = report["by_role"].get("planner", {}).get("cost_usd", 0)
        assert by_role_planner == pytest.approx(0.02)  # 仅 task-2 贡献
        # 两个任务的总成本 = 0.05 + 0.02 + 0.05 = 0.12
        assert report["estimated_cost_usd"] == pytest.approx(0.12, rel=1e-3)
        assert report["avg_cost_per_task"] == pytest.approx(0.06, rel=1e-3)


# ═══════════════════════════════════════════════════════════════
# 场景 5：多波次拓扑 + 并发
# ═══════════════════════════════════════════════════════════════

class TestScenario5WaveTopology:
    """wave0: 3 个独立 subtask（并发）；wave1: 1 个依赖 wave0 全部。

    验证：拓扑结构反映在 meta.results；并发不丢失 metering 事件；
    reliability 的 subtask_total 正确。
    """

    def test_multi_wave_topology_metering_intact(self, tmp_path):
        td = _mk_task(tmp_path, "task-waves")
        events = [
            _planner_event(0.03),
            # wave0：3 个独立 subtask 并发
            _worker_event(0.04, subtask_id="s1", result="success"),
            _worker_event(0.04, subtask_id="s2", result="success"),
            _worker_event(0.04, subtask_id="s3", result="success"),
            # wave1：依赖 wave0
            _worker_event(0.05, subtask_id="s4", result="success"),
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-waves", status="completed",
                    results=[_result(s, "completed") for s in ["s1", "s2", "s3", "s4"]])

        cost_report = analyze_cost(tmp_path)
        rel_report = analyze_reliability(tmp_path)

        # 4 个 subtask 全完成，无 metering 丢失（并发安全）
        assert cost_report["total_calls"] == 5  # 1 planner + 4 worker
        assert cost_report["completed_subtasks"] == 4
        # 总成本 = 0.03 + 3×0.04 + 0.05 = 0.20
        assert cost_report["estimated_cost_usd"] == pytest.approx(0.20, rel=1e-3)
        assert cost_report["dollar_per_pass_rate"] == pytest.approx(0.05, rel=1e-3)
        # reliability 拓扑一致
        assert rel_report["tasks_total"] == 1
        assert rel_report["blocked"] == 0

    def test_partial_failure_in_wave(self, tmp_path):
        """wave0 中 1 个失败 → wave1 全部 blocked（级联）"""
        td = _mk_task(tmp_path, "task-partial")
        events = [
            _planner_event(0.03),
            _worker_event(0.04, subtask_id="s1", result="success"),
            _worker_event(0.04, subtask_id="s2", result="failed"),  # wave0 失败
            # s3、s4 依赖 s2 → blocked，不起 Claude
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-partial", status="failed",
                    results=[_result("s1", "completed"),
                             _result("s2", "failed"),
                             _result("s3", "blocked"),
                             _result("s4", "blocked")])

        cost_report = analyze_cost(tmp_path)
        rel_report = analyze_reliability(tmp_path)

        # 只有 s1 completed → $/pass = 总成本/1
        assert cost_report["completed_subtasks"] == 1
        # 总成本含失败的 s2 worker 成本（钱已花）
        assert cost_report["estimated_cost_usd"] == pytest.approx(0.11, rel=1e-3)
        assert rel_report["blocked"] == 2
        assert rel_report["success_rate"] == 0  # task status=failed


# ═══════════════════════════════════════════════════════════════
# 场景 6：gate 端到端（产物驱动，两种门禁模式）
# ═══════════════════════════════════════════════════════════════

class TestScenario6GateE2E:
    """用真实多模型产物驱动 gate，覆盖绝对阈值 + 回归对比两种模式 + CI 退出码。"""

    def _setup_multi_model_scenario(self, tmp_path):
        """构造场景 1 的多模型产物（$/pass = 0.125，高于 Q3 目标 0.05）。"""
        td = _mk_task(tmp_path, "task-gate-e2e")
        events = [
            _planner_event(0.02),
            _worker_event(0.08, subtask_id="s1"),
            _worker_event(0.15, model="claude-opus-4-20250514", provider="anthropic",
                          subtask_id="s2"),
        ]
        _write_metering(td, events)
        _write_meta(td, task_id="task-gate-e2e", status="completed",
                    results=[_result("s1", "completed"), _result("s2", "completed")])

    def test_absolute_gate_fails_on_expensive_scenario(self, tmp_path, monkeypatch, capsys):
        """场景 1 多模型 $/pass=0.125 > baseline=0.05 → 绝对门禁失败 + exit 1"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        self._setup_multi_model_scenario(tmp_path)

        args = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                  baseline=0.05, check_regression=False, update_baseline=False)
        with pytest.raises(SystemExit) as exc:
            cmd_eval(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "不通过" in out
        assert "0.125" in out  # 真实计价（非 deepseek 低估）

    def test_regression_gate_first_run_establishes_baseline(self, tmp_path, monkeypatch, capsys):
        """回归门禁首次运行：无基线 → 建立基线 + 通过"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        self._setup_multi_model_scenario(tmp_path)

        args = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                  baseline=None, check_regression=True, update_baseline=False)
        cmd_eval(args)  # 不抛 SystemExit
        out = capsys.readouterr().out
        assert "已建立基线" in out
        # 基线文件已落盘
        assert load_cost_baseline(tmp_path) == pytest.approx(0.125, rel=1e-3)

    def test_regression_gate_detects_regression(self, tmp_path, monkeypatch, capsys):
        """回归门禁：基线 0.05 → 当前 0.125，劣化 >10% → 失败"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        # 先存一个低基线
        save_cost_baseline(tmp_path, 0.05)
        self._setup_multi_model_scenario(tmp_path)

        args = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                  baseline=None, check_regression=True, update_baseline=False)
        with pytest.raises(SystemExit) as exc:
            cmd_eval(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "劣化" in out
        assert "153" in out or "150" in out  # (0.125-0.05)/0.05 ≈ 153%

    def test_update_baseline_resets(self, tmp_path, monkeypatch, capsys):
        """--update-baseline 强制重置基线为当前 rate"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        save_cost_baseline(tmp_path, 0.001)  # 旧基线
        self._setup_multi_model_scenario(tmp_path)

        args = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                  baseline=None, check_regression=False, update_baseline=True)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "基线已更新" in out
        assert load_cost_baseline(tmp_path) == pytest.approx(0.125, rel=1e-3)

    def test_no_data_gate_passes(self, tmp_path, monkeypatch, capsys):
        """空目录 → dollar_per_pass=None → 两种模式都通过（不阻挡新项目）"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)

        # 绝对阈值模式
        args = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                  baseline=0.05, check_regression=False, update_baseline=False)
        cmd_eval(args)
        out1 = capsys.readouterr().out
        assert "通过" in out1
        assert "门禁未生效" in out1

        # 回归模式
        args2 = argparse.Namespace(subcommand="gate", task_id=None, eval_all=False,
                                   baseline=None, check_regression=True, update_baseline=False)
        cmd_eval(args2)
        out2 = capsys.readouterr().out
        assert "门禁未生效" in out2


# ═══════════════════════════════════════════════════════════════
# 场景 7（K5）：中断恢复成功率
# ═══════════════════════════════════════════════════════════════

class TestScenario7ResumeSuccessRate:
    """K5 中断恢复成功率：被中断过的任务，恢复后最终 completed 的比例。

    原始数据已在 execution.log（task_paused/subtask_resume 事件），K5 派生计算新增。
    """

    def test_resumed_task_completed_counts_as_success(self, tmp_path):
        """任务被中断 1 次后恢复并 completed → resume_success_rate=100%"""
        td = _mk_task(tmp_path, "task-resumed-ok")
        _write_log(td, [
            {"event": "task_paused"},
            {"event": "subtask_resume"},
        ])
        _write_meta(td, task_id="task-resumed-ok", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_reliability(tmp_path)
        assert report["interrupted_tasks"] == 1
        assert report["resume_success_rate"] == 100.0

    def test_resumed_task_failed_lowers_rate(self, tmp_path):
        """任务中断后恢复但最终 failed → resume_success_rate=0%"""
        td = _mk_task(tmp_path, "task-resumed-fail")
        _write_log(td, [{"event": "task_paused"}])
        _write_meta(td, task_id="task-resumed-fail", status="failed",
                    results=[_result("s1", "failed")])

        report = analyze_reliability(tmp_path)
        assert report["interrupted_tasks"] == 1
        assert report["resume_success_rate"] == 0.0

    def test_mixed_interrupted_tasks(self, tmp_path):
        """3 个中断任务：2 个最终 completed，1 个 failed → resume_success_rate=66.7%"""
        for name, status in [("task-1", "completed"), ("task-2", "completed"), ("task-3", "failed")]:
            td = _mk_task(tmp_path, name)
            _write_log(td, [{"event": "task_paused"}])
            _write_meta(td, task_id=name, status=status,
                        results=[_result("s1", status)])

        report = analyze_reliability(tmp_path)
        assert report["interrupted_tasks"] == 3
        assert report["resume_success_rate"] == pytest.approx(66.7, rel=1e-2)

    def test_no_interrupted_tasks_returns_none(self, tmp_path):
        """无中断任务 → resume_success_rate=None（PRD K5 数据未采集）"""
        td = _mk_task(tmp_path, "task-normal")
        _write_meta(td, task_id="task-normal", status="completed",
                    results=[_result("s1", "completed")])

        report = analyze_reliability(tmp_path)
        assert report["interrupted_tasks"] == 0
        assert report["resume_success_rate"] is None
