"""P0 全链路 E2E 测试：Plan → Execute → PR。

设计原则：
  不调真实 LLM/Claude/git，所有外部依赖 mock。
  测试 cmd_run 的核心编排流程：
    generate_plan → plan_to_subtasks → _run_pipeline → cmd_pr
  验证跨模块数据流动正确性：
    - plan 正确分解为 subtasks
    - subtask 结果正确写入 meta.json
    - PR 正确包含子任务摘要和质量仪表 (M3)

覆盖 3 个场景：
  1. 全链路成功：所有子任务成功 → meta.completed → PR.md 含质量仪表
  2. 级联阻断：子任务失败 → 下游 blocked → 报告含失败原因
  3. 中断恢复：已完成子任务跳过 → 剩余子任务执行 → PR.md
"""

import argparse
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.cli import cmd_pr, _build_quality_dashboard
from agent_go.pipeline import _run_pipeline
from agent_go.ui import plan_to_subtasks


# ═══════════════════════════════════════════════════════════════
# 共享辅助函数（复用 test_pipeline.py 模式）
# ═══════════════════════════════════════════════════════════════

def _success_result(sub_id):
    return {
        "subtask_id": sub_id, "status": "completed", "exit_code": 0,
        "summary": f"done-{sub_id}", "worktree": "",
        "sandbox_type": "headless", "verify_ok": True, "duration_sec": 1.0,
    }


def _failed_result(sub_id, reason="验证失败"):
    return {
        "subtask_id": sub_id, "status": "failed", "exit_code": 1,
        "summary": f"fail-{sub_id}", "failure_reason": reason,
        "worktree": "", "sandbox_type": "headless", "verify_ok": False,
        "duration_sec": 1.0,
    }


def _setup_env(tmp_path, task_id="task-e2e"):
    """创建 repo + .agent_go/task_id 目录，patch AGENT_GO_DIR。

    Returns:
        (repo_path, agent_dir, task_dir, monkeypatch 补丁列表)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "README.md").write_text("# Test")

    agent_dir = tmp_path / ".agent_go"
    agent_dir.mkdir()
    task_dir = agent_dir / task_id
    task_dir.mkdir(parents=True)
    return repo, agent_dir, task_dir


# 已知 Plan（模拟 LLM 生成结果）
_SAMPLE_PLAN = {
    "overview": "实现用户认证功能",
    "steps": [
        {"id": 1, "title": "后端 JWT 认证", "description": "实现 JWT 签发和验证",
         "files": ["src/auth/jwt.py"], "verification": "pytest tests/test_auth.py",
         "risks": [], "agent_prompt": "请在后端实现 JWT", "difficulty": "medium"},
        {"id": 2, "title": "前端登录页面", "description": "实现登录表单和 token 存储",
         "files": ["src/pages/login.tsx"], "verification": "npm run test:login",
         "risks": [], "agent_prompt": "请实现前端登录页", "difficulty": "medium"},
    ],
    "dependencies": {},
    "estimated_effort": "1 天",
}


# ═══════════════════════════════════════════════════════════════
# 场景 1：全链路成功
# ═══════════════════════════════════════════════════════════════

class TestFullWorkflowSuccess:
    """全部子任务成功完成 → completed → PR.md 含质量仪表"""

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_full_workflow_success(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc, mock_notify,
        tmp_path, monkeypatch, capsys, logger,
    ):
        """全链路成功：Plan → subtasks → pipeline → PR"""
        repo, agent_dir, task_dir = _setup_env(tmp_path)
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", agent_dir)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", agent_dir)

        # 1) Plan → Subtasks（模拟 cmd_run 的编排层）
        subtasks = plan_to_subtasks(_SAMPLE_PLAN, logger)

        # 2) 写 meta.json（同 cmd_run 在 pipeline 前的操作）
        meta = {
            "task_id": "task-e2e", "task": "实现用户认证", "repo": str(repo),
            "status": "running", "subtasks": subtasks, "results": [],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # 3) Pipeline（mock run_subtask，使用函数 side_effect 避免 StopIteration）
        def _run_side_effect(*a, **kw):
            st = a[1]
            return _success_result(st["id"])

        mock_run.side_effect = _run_side_effect
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(subtasks, repo, task_dir, logger, config={},
                      headless=True, parallel=1, issue_ref="", meta=meta)

        # 4) 验证 pipeline 结果
        assert meta["status"] == "completed"
        assert len(meta["results"]) == 2
        assert meta["results"][0]["status"] == "completed"
        assert meta["results"][1]["status"] == "completed"

        # 5) 验证 meta.json 落盘
        saved = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        assert saved["status"] == "completed"

        # 6) cmd_pr 生成 PR
        args = argparse.Namespace(subcommand="pr", task_id="task-e2e",
                                  offline=True, push=False, remote="origin")
        cmd_pr(args)

        # 7) 验证 PR.md
        pr_path = task_dir / "PR.md"
        assert pr_path.exists()
        pr_content = pr_path.read_text()
        assert "Summary" in pr_content, "PR 应含 Summary 段"
        assert "实现用户认证" in pr_content, "PR 应含任务描述"
        assert "sub-1" in pr_content and "sub-2" in pr_content, "PR 应含子任务"
        assert "Quality Dashboard" in pr_content, "PR 应含 M3 质量仪表"
        assert "🟢" in pr_content, "全部通过应显示可合并绿灯"


# ═══════════════════════════════════════════════════════════════
# 场景 2：级联阻断
# ═══════════════════════════════════════════════════════════════

class TestFullWorkflowBlocked:
    """上游失败 → 下游 blocked → 报告含失败原因 + 质量仪表红灯"""

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_workflow_with_blocked(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc, mock_notify,
        tmp_path, monkeypatch, capsys, logger,
    ):
        """一个子任务失败 → 依赖它的下游被级联阻断"""
        repo, agent_dir, task_dir = _setup_env(tmp_path)
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", agent_dir)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", agent_dir)

        # 带依赖的 Plan：sub-2 依赖 sub-1（dependencies 的 key 必须用字符串）
        plan = {
            "overview": "实现用户认证",
            "steps": [
                {"id": 1, "title": "后端 JWT", "description": "JWT",
                 "files": ["auth.py"], "verification": "pytest",
                 "risks": [], "agent_prompt": "work", "difficulty": "medium"},
                {"id": 2, "title": "前端登录页", "description": "login page",
                 "files": ["login.tsx"], "verification": "npm test",
                 "risks": [], "agent_prompt": "work", "difficulty": "medium"},
            ],
            "dependencies": {"2": [1]},
            "estimated_effort": "1d",
        }
        subtasks = plan_to_subtasks(plan, logger)

        meta = {
            "task_id": "task-e2e", "task": "实现用户认证", "repo": str(repo),
            "status": "running", "subtasks": subtasks, "results": [],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # sub-1 被执行并失败；sub-2 因依赖失败被级联阻断（不执行 run_subtask）
        def _run_side_effect(*a, **kw):
            st = a[1]
            if st["id"] == "sub-1":
                return _failed_result("sub-1", reason="pytest exit=1")
            # sub-2 不会被调用（被阻断），此分支仅防御性编码
            return _failed_result(st["id"], reason="上游依赖失败")

        mock_run.side_effect = _run_side_effect
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(subtasks, repo, task_dir, logger, config={},
                      headless=True, parallel=1, issue_ref="", meta=meta)

        # 验证
        assert meta["status"] == "failed"
        results_map = {r["subtask_id"]: r for r in meta["results"]}
        assert results_map["sub-1"]["status"] == "failed"
        assert results_map["sub-2"]["status"] == "blocked"

        # 质量仪表显示红灯
        saved_meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        dashboard = _build_quality_dashboard(saved_meta, task_dir=task_dir)
        assert "🔴" in dashboard or "不建议合并" in dashboard

        # PR 包含失败信息
        args = argparse.Namespace(subcommand="pr", task_id="task-e2e",
                                  offline=True, push=False, remote="origin")
        cmd_pr(args)
        assert (task_dir / "PR.md").exists()
        pr_content = (task_dir / "PR.md").read_text()
        assert "❌" in pr_content, "PR 应显示失败子任务"


# ═══════════════════════════════════════════════════════════════
# 场景 3：中断恢复
# ═══════════════════════════════════════════════════════════════

class TestFullWorkflowResume:
    """中断后恢复：已完成子任务跳过 → 剩余子任务执行 → PR"""

    @patch("agent_go.notify.notify_event")
    @patch("agent_go.pipeline.subprocess.run")
    @patch("agent_go.pipeline._worktree_prune", return_value=(True, ""))
    @patch("agent_go.pipeline._worktree_remove", return_value=(True, ""))
    @patch("agent_go.pipeline._set_gc_auto", return_value=("1", True, ""))
    @patch("agent_go.pipeline.run_subtask")
    def test_resume_skips_completed_then_pr(
        self, mock_run, mock_gc, mock_wt_remove, mock_wt_prune, mock_subproc, mock_notify,
        tmp_path, monkeypatch, capsys, logger,
    ):
        """恢复场景：sub-1 已完成 → 跳过 → 只执行 sub-2 → PR"""
        repo, agent_dir, task_dir = _setup_env(tmp_path)
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", agent_dir)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", agent_dir)

        subtasks = plan_to_subtasks(_SAMPLE_PLAN, logger)

        # 模拟 sub-1 已完成（中断恢复场景）
        completed_ids = {"sub-1"}
        results_map = {"sub-1": _success_result("sub-1")}

        meta = {
            "task_id": "task-e2e", "task": "实现用户认证", "repo": str(repo),
            "status": "running", "subtasks": subtasks,
            "results": [results_map["sub-1"]],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # run_subtask 只被调 1 次 → sub-2
        executed_ids = []
        import sys as _sys

        def _run_side_effect(*a, **kw):
            st = a[1]
            executed_ids.append(st["id"])
            return _success_result(st["id"])

        mock_run.side_effect = _run_side_effect
        mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr=b"")

        _run_pipeline(subtasks, repo, task_dir, logger, config={},
                      headless=True, parallel=1, issue_ref="", meta=meta,
                      completed_ids=completed_ids, results_map=results_map)

        # 验证 pipeline
        assert meta["status"] == "completed"
        assert len(meta["results"]) == 2
        assert executed_ids == ["sub-2"], f"期望只执行 sub-2，实际: {executed_ids}"

        # PR 生成
        args = argparse.Namespace(subcommand="pr", task_id="task-e2e",
                                  offline=True, push=False, remote="origin")
        cmd_pr(args)
        assert (task_dir / "PR.md").exists()
