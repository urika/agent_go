"""测试 eval.py — 覆盖 cmd_eval / _resolve_task_dir / _read_meta / _read_log_events
以及 MODEL_PRICES / PROVIDER_DEFAULT_MODEL 结构验证。

p0_p1_fixes.py 中已有 cmd_eval 签名和 --all 基础测试，
本文件补充：子命令路由、task_id 解析、helper 函数、数据结构验证。
"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_go.eval import (
    cmd_eval,
    _resolve_task_dir,
    _read_meta,
    _read_log_events,
    _scan_task_dirs,
    MODEL_PRICES,
    PROVIDER_DEFAULT_MODEL,
    analyze_quality,
    analyze_performance,
    analyze_cost,
    analyze_reliability,
    analyze_ux,
    aggregate_quality,
    aggregate_performance,
)


# ═══════════════════════════════════════════════════════════════
# MODEL_PRICES / PROVIDER_DEFAULT_MODEL 结构验证
# ═══════════════════════════════════════════════════════════════

class TestModelPrices:
    """MODEL_PRICES 定价表结构"""

    def test_all_known_models_present(self):
        assert "claude-sonnet-4-20250514" in MODEL_PRICES
        assert "deepseek-chat" in MODEL_PRICES
        assert "gpt-4o" in MODEL_PRICES

    def test_each_model_has_prompt_and_completion(self):
        for model, prices in MODEL_PRICES.items():
            assert "prompt" in prices, f"{model} missing prompt price"
            assert "completion" in prices, f"{model} missing completion price"
            assert isinstance(prices["prompt"], (int, float))
            assert isinstance(prices["completion"], (int, float))
            assert prices["prompt"] > 0
            assert prices["completion"] > 0

    def test_prices_are_per_million_tokens(self):
        """价格单位为 $/1M tokens"""
        # Claude 价格基准校验
        assert MODEL_PRICES["claude-sonnet-4-20250514"]["prompt"] == 3.0
        assert MODEL_PRICES["claude-sonnet-4-20250514"]["completion"] == 15.0
        # DeepSeek 应显著低于 Claude
        assert MODEL_PRICES["deepseek-chat"]["prompt"] < 1.0


class TestProviderDefaultModel:
    """PROVIDER_DEFAULT_MODEL 回退映射表"""

    def test_all_known_providers_mapped(self):
        assert "anthropic" in PROVIDER_DEFAULT_MODEL
        assert "openai" in PROVIDER_DEFAULT_MODEL
        assert "deepseek" in PROVIDER_DEFAULT_MODEL

    def test_defaults_point_to_valid_models(self):
        """每个默认模型都存在于 MODEL_PRICES 中"""
        for provider, model in PROVIDER_DEFAULT_MODEL.items():
            assert model in MODEL_PRICES, (
                f"Provider {provider} default model '{model}' not in MODEL_PRICES"
            )


# ═══════════════════════════════════════════════════════════════
# _scan_task_dirs
# ═══════════════════════════════════════════════════════════════

class TestScanTaskDirs:
    """任务目录扫描"""

    def test_finds_task_dirs(self, tmp_path):
        (tmp_path / "task-001").mkdir()
        (tmp_path / "task-002").mkdir()
        (tmp_path / "other-file").write_text("")
        (tmp_path / "not-a-task").mkdir()

        dirs = _scan_task_dirs(tmp_path)
        names = [d.name for d in dirs]
        assert "task-001" in names
        assert "task-002" in names
        assert "other-file" not in names
        assert "not-a-task" not in names

    def test_sorted_reverse(self, tmp_path):
        """按名称降序排列（最新的在前）"""
        (tmp_path / "task-001").mkdir()
        (tmp_path / "task-010").mkdir()
        (tmp_path / "task-002").mkdir()

        dirs = _scan_task_dirs(tmp_path)
        names = [d.name for d in dirs]
        assert names == ["task-010", "task-002", "task-001"]

    def test_empty_dir(self, tmp_path):
        dirs = _scan_task_dirs(tmp_path)
        assert dirs == []


# ═══════════════════════════════════════════════════════════════
# _read_meta
# ═══════════════════════════════════════════════════════════════

class TestReadMeta:
    """元数据文件读取"""

    def test_reads_meta_json(self, tmp_path):
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": "task-001",
            "status": "completed",
        }), encoding="utf-8")

        meta = _read_meta(td)
        assert meta is not None
        assert meta["task_id"] == "task-001"
        assert meta["status"] == "completed"

    def test_missing_file_returns_none(self, tmp_path):
        td = tmp_path / "task-001"
        td.mkdir()  # dir exists but no meta.json
        meta = _read_meta(td)
        assert meta is None

    def test_nonexistent_dir_returns_none(self):
        meta = _read_meta(Path("/nonexistent/path"))
        assert meta is None

    def test_invalid_json_returns_none(self, tmp_path):
        """无效 JSON 返回 None（安全降级，不抛异常）"""
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text("{invalid json", encoding="utf-8")

        meta = _read_meta(td)
        assert meta is None


# ═══════════════════════════════════════════════════════════════
# _read_log_events
# ═══════════════════════════════════════════════════════════════

class TestReadLogEvents:
    """日志事件解析"""

    def _make_log(self, log_path, events):
        """写入 execution.log 并插入结构化事件。"""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        ts = "2026-07-24 00:00:00"
        for ev_type, ev_data in events:
            # 使用 compact JSON（无空格，匹配 real log_event 输出）
            ev_json = json.dumps(
                {"timestamp": ts, "event": ev_type, **ev_data},
                ensure_ascii=False, separators=(",", ":"),
            )
            lines.append(f"{ts} | DEBUG    | agent_go.task | {ev_json}")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_reads_specific_event(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        self._make_log(log_path, [
            ("api_call", {"provider": "anthropic", "prompt_tokens": 100}),
            ("api_error", {"provider": "openai", "status_code": 429}),
            ("api_call", {"provider": "deepseek", "prompt_tokens": 200}),
        ])

        api_calls = _read_log_events(log_path, "api_call")
        assert len(api_calls) == 2
        assert api_calls[0]["provider"] == "anthropic"
        assert api_calls[1]["provider"] == "deepseek"

    def test_reads_errors(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        self._make_log(log_path, [
            ("api_call", {"provider": "test"}),
            ("api_error", {"provider": "test", "status_code": 500}),
        ])

        errors = _read_log_events(log_path, "api_error")
        assert len(errors) == 1
        assert errors[0]["status_code"] == 500

    def test_no_matching_events(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        self._make_log(log_path, [("api_call", {"provider": "test"})])

        errors = _read_log_events(log_path, "api_error")
        assert errors == []

    def test_missing_log_file(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        events = _read_log_events(log_path, "api_call")
        assert events == []

    def test_event_with_spaces_in_json(self, tmp_path):
        """兼容 JSON 中 'event': 'name' 有空格的形式"""
        log_path = tmp_path / "task-001" / "execution.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            '2026-07-24 00:00:00 | DEBUG    | agent_go.task | '
            + json.dumps({"event": "api_call", "provider": "test"}, ensure_ascii=False),
        ]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        events = _read_log_events(log_path, "api_call")
        assert len(events) == 1

    def test_reads_plan_complete(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        self._make_log(log_path, [
            ("plan_complete", {"plan_duration_ms": 5000, "iteration": 1}),
        ])

        events = _read_log_events(log_path, "plan_complete")
        assert len(events) == 1
        assert events[0]["plan_duration_ms"] == 5000

    def test_reads_plan_generate(self, tmp_path):
        log_path = tmp_path / "task-001" / "execution.log"
        self._make_log(log_path, [
            ("plan_generate", {"iteration": 3}),
        ])

        events = _read_log_events(log_path, "plan_generate")
        assert len(events) == 1
        assert events[0]["iteration"] == 3


# ═══════════════════════════════════════════════════════════════
# _resolve_task_dir
# ═══════════════════════════════════════════════════════════════

class TestResolveTaskDir:
    """任务目录解析"""

    def test_resolve_by_task_id(self, tmp_path):
        td = tmp_path / "task-abc"
        td.mkdir()
        result = _resolve_task_dir(tmp_path, "task-abc")
        assert result == td

    def test_resolve_nonexistent_task_id(self, tmp_path):
        result = _resolve_task_dir(tmp_path, "task-nonexistent")
        assert result is None

    def test_resolve_no_task_id_returns_latest(self, tmp_path):
        (tmp_path / "task-003").mkdir()
        (tmp_path / "task-001").mkdir()
        (tmp_path / "task-002").mkdir()

        result = _resolve_task_dir(tmp_path, "")
        # 最新（排序最大）的是 task-003
        assert result is not None
        assert result.name == "task-003"

    def test_resolve_no_tasks(self, tmp_path):
        result = _resolve_task_dir(tmp_path, "")
        assert result is None


# ═══════════════════════════════════════════════════════════════
# cmd_eval — 子命令路由
# ═══════════════════════════════════════════════════════════════

class TestCmdEvalRouting:
    """cmd_eval 子命令路由测试"""

    def test_quality_subcommand(self, tmp_path, monkeypatch, capsys):
        """quality 子命令"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="quality", task_id=None, eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "暂无任务" in out

    def test_perf_subcommand(self, tmp_path, monkeypatch, capsys):
        """perf 子命令"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="perf", task_id=None, eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "暂无任务" in out

    def test_cost_subcommand(self, tmp_path, monkeypatch, capsys):
        """cost 子命令（不依赖历史数据也能输出）"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="cost", task_id=None, eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "成本报告" in out

    def test_reliability_subcommand(self, tmp_path, monkeypatch, capsys):
        """reliability 子命令"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="reliability", task_id=None, eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "可靠性报告" in out

    def test_ux_subcommand(self, tmp_path, monkeypatch, capsys):
        """ux 子命令"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="ux", task_id=None, eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "使用习惯报告" in out

    def test_all_subcommand(self, tmp_path, monkeypatch, capsys):
        """all 子命令输出所有报告"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="all", task_id=None, eval_all=True)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "成本报告" in out
        assert "可靠性报告" in out
        assert "使用习惯报告" in out

    def test_unknown_subcommand(self, tmp_path, monkeypatch, capsys):
        """未知子命令打印提示"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="unknown_cmd", task_id=None,
                                  eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "未知子命令" in out

    def test_quality_with_specific_task(self, tmp_path, monkeypatch, capsys):
        """quality 指定 task-id"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": "task-001",
            "status": "completed",
            "results": [],
        }), encoding="utf-8")

        args = argparse.Namespace(subcommand="quality", task_id="task-001",
                                  eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "无数据" in out  # 空 results → 无数据

    def test_quality_all_mode(self, tmp_path, monkeypatch, capsys):
        """quality --all 聚合模式"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        # 创建有数据的任务目录
        for i in range(2):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(json.dumps({
                "task_id": f"task-00{i}",
                "status": "completed",
                "results": [
                    {
                        "subtask_id": f"sub-{i}",
                        "status": "completed",
                        "verify_ok": True,
                        "retry_count": 0,
                        "change_stats": {
                            "files_changed": 1, "insertions": 10, "deletions": 0,
                            "new_files": 0, "modified_files": 1, "actual_files": [],
                        },
                    }
                ],
            }), encoding="utf-8")

        args = argparse.Namespace(subcommand="quality", task_id="--all",
                                  eval_all=True)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "质量聚合" in out

    def test_perf_all_mode(self, tmp_path, monkeypatch, capsys):
        """perf --all 聚合模式"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        for i in range(2):
            td = tmp_path / f"task-00{i}"
            td.mkdir()
            (td / "meta.json").write_text(json.dumps({
                "task_id": f"task-00{i}",
                "status": "completed",
                "results": [
                    {
                        "subtask_id": f"sub-{i}",
                        "status": "completed",
                        "duration_sec": 50 + i * 10,
                    }
                ],
            }), encoding="utf-8")
            # 需要不同的时间戳：analyze_performance 通过首尾行时间差计算 P1
            (td / "execution.log").write_text(
                "2026-01-01 00:00:00 | INFO | test | start\n"
                "2026-01-01 00:00:10 | INFO | test | end\n",
                encoding="utf-8",
            )

        args = argparse.Namespace(subcommand="perf", task_id="--all",
                                  eval_all=True)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "性能聚合" in out

    def test_quality_with_data_report(self, tmp_path, monkeypatch, capsys):
        """quality 有完整数据时的报告输出"""
        monkeypatch.setattr("agent_go.config.AGENT_GO_DIR", tmp_path)
        td = tmp_path / "task-001"
        td.mkdir()
        (td / "meta.json").write_text(json.dumps({
            "task_id": "task-001",
            "status": "completed",
            "subtasks": [],
            "results": [
                {
                    "subtask_id": "sub-1",
                    "status": "completed",
                    "verify_ok": True,
                    "retry_count": 0,
                    "change_stats": {
                        "files_changed": 2, "insertions": 50, "deletions": 10,
                        "new_files": 1, "modified_files": 1,
                        "actual_files": ["src/main.py"],
                    },
                }
            ],
        }), encoding="utf-8")

        args = argparse.Namespace(subcommand="quality", task_id="task-001",
                                  eval_all=False)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "质量报告" in out
        assert "Q1" in out
        assert "评分" in out


# ═══════════════════════════════════════════════════════════════
# cmd_eval — sys.argv 路径
# ═══════════════════════════════════════════════════════════════

class TestCmdEvalSysArgv:
    """cmd_eval 通过 sys.argv 调用的路径（无 argparse Namespace）"""

    def test_cost_via_sys_argv(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["agent_go", "eval", "cost"])

        cmd_eval(None)
        out = capsys.readouterr().out
        assert "成本报告" in out

    def test_reliability_via_sys_argv(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["agent_go", "eval", "reliability"])

        cmd_eval(None)
        out = capsys.readouterr().out
        assert "可靠性报告" in out

    def test_insufficient_sys_argv(self, tmp_path, monkeypatch, capsys):
        """sys.argv 不足 3 个参数时打印用法"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["agent_go", "eval"])

        cmd_eval(None)
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_all_via_sys_argv(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["agent_go", "eval", "all"])

        cmd_eval(None)
        out = capsys.readouterr().out
        assert "成本报告" in out
