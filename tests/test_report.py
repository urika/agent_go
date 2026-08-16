"""report 命令测试（P1：任务共享——报告导出）。"""
import json
from pathlib import Path

import pytest


@pytest.fixture
def task_dir(tmp_path: Path, monkeypatch) -> Path:
    """构造含 metering/review/delivery 的任务目录。"""
    import agent_go.cli as cli
    adir = tmp_path / "agent_go"
    adir.mkdir()
    monkeypatch.setattr(cli, "AGENT_GO_DIR", adir)
    td = adir / "task-20260816-120000-000-aaaa"
    td.mkdir()
    (td / "meta.json").write_text(json.dumps({
        "task_id": "task-20260816-120000-000-aaaa", "task": "实现登录功能",
        "status": "DELIVERY_READY", "repo": "/repo/x", "created": "20260816-120000",
        "delivery_branch": "agent_go/task-x/delivery",
        "explicit_merge_commit": "abc123def456",
        "subtasks": [{"id": "sub-1", "title": "实现", "agent_type": "developer"}],
        "results": [{"subtask_id": "sub-1", "status": "completed", "verify_ok": True,
                     "duration_sec": 10.5, "summary": "新增 login.py"}],
    }), encoding="utf-8")
    (td / "metering.jsonl").write_text("\n".join([
        json.dumps({"role": "planner", "actual_model": "glm-5.3", "cost_usd": 0.001}),
        json.dumps({"role": "worker", "actual_model": "deepseek-v4-pro",
                    "route_actual_model": "deepseek-v4-pro", "cost_usd": 0.004}),
    ]), encoding="utf-8")
    (td / "review.json").write_text(json.dumps({"decision": "approved"}), encoding="utf-8")
    return td


class TestReport:
    def _run(self, task_dir, fmt="md"):
        import agent_go.cli as cli
        import argparse
        out = task_dir.parent / f"out.{fmt}"
        args = argparse.Namespace(task_id=task_dir.name, format=fmt, output=str(out))
        cli.cmd_report(args)
        return out.read_text(encoding="utf-8")

    def test_md_content(self, task_dir):
        md = self._run(task_dir, "md")
        assert "任务报告: 实现登录功能" in md
        assert "DELIVERY_READY" in md
        assert "总成本**: $0.0050" in md          # 0.001 + 0.004
        assert "总耗时**: 10.5s" in md
        assert "glm-5.3" in md and "deepseek-v4-pro" in md
        assert "approved" in md                  # review 决策
        assert "delivery" in md                  # 交付分支
        assert "sub-1" in md and "completed" in md
        assert "成本明细" in md

    def test_html_structure(self, task_dir):
        html = self._run(task_dir, "html")
        assert "<!DOCTYPE html>" in html
        assert "<h1>" in html and "<h2>" in html
        assert html.count("<table>") == 2        # 子任务表 + 成本表
        assert "任务报告" in html

    def test_default_output_path(self, task_dir, monkeypatch):
        """无 --output 时写到 <task_id>.md。"""
        import agent_go.cli as cli
        import argparse
        args = argparse.Namespace(task_id=task_dir.name, format="md", output="")
        cli.cmd_report(args)
        assert (task_dir.parent / f"{task_dir.name}.md").exists()

    def test_missing_task(self, task_dir, monkeypatch):
        import agent_go.cli as cli
        import argparse
        import sys
        args = argparse.Namespace(task_id="task-20990101-000000", format="md", output="-")
        with pytest.raises(SystemExit):
            cli.cmd_report(args)

    def test_stdout_output(self, task_dir, capsys):
        """--output - 打印到 stdout。"""
        import agent_go.cli as cli
        import argparse
        args = argparse.Namespace(task_id=task_dir.name, format="md", output="-")
        cli.cmd_report(args)
        out = capsys.readouterr().out
        assert "任务报告" in out
