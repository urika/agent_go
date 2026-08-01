"""Phase 3 Review 阶段测试 — 审查命令输出 + 三态审批写入。

设计原则：
  - 不调真实 LLM/Claude/git，所有外部依赖 mock
  - 构造带 meta.json 的事后审查场景，验证 _show_task_review 和 cmd_review
  - 验证以下维度：
      1. 基本审查输出（按文件分组、子任务详情、质量仪表）
      2. 三态审批（approve / reject / changes-requested → review.json）
      3. 异常路径（任务不存在、meta.json 损坏）

覆盖 5 个场景：
  1. 混合结果审查：completed + failed + blocked 子任务
  2. --approve 写入 review.json + 输出
  3. --reject 写入 review.json + 输出
  4. --changes-requested 含评论写入 review.json
  5. 异常路径：任务不存在、meta 损坏
"""

import argparse
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _make_task_dir(tmp_path, task_id, results, status="completed"):
    """构造带 meta.json 的伪 task 目录。"""
    td = tmp_path / task_id
    td.mkdir(parents=True)
    meta = {
        "task_id": task_id, "task": "实现用户认证", "status": status,
        "subtasks": [
            {"id": r["subtask_id"],
             "title": f"任务{r['subtask_id']}",
             "agent_type": "developer"}
            for r in results
        ],
        "results": results,
        "created": "20260725-100000",
    }
    (td / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return td


def _result(sub_id, status="completed", verify_ok=True, duration=30,
            summary="执行完成", failure_reason="", files=None):
    """构造单个子任务结果。"""
    r = {
        "subtask_id": sub_id, "status": status, "verify_ok": verify_ok,
        "duration_sec": duration, "summary": summary,
        "sandbox_type": "headless",
        "change_stats": {
            "files_changed": len(files) if files else 0,
            "insertions": 20, "deletions": 5,
            "actual_files": files or [],
        },
    }
    if failure_reason:
        r["failure_reason"] = failure_reason
    return r


# ═══════════════════════════════════════════════════════════════
# 场景 1：基本审查输出
# ═══════════════════════════════════════════════════════════════

class TestReviewOutput:
    """审查命令的格式化和内容输出"""

    def test_review_with_mixed_results(self, tmp_path, monkeypatch, capsys):
        """mixed results → 输出包含状态图标和质量仪表"""
        td = _make_task_dir(tmp_path, "task-r1", [
            _result("sub-1", status="completed", files=["auth.py"]),
            _result("sub-2", status="failed", verify_ok=False,
                    failure_reason="pytest 3 个用例失败"),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(argparse.Namespace(
            task_id="task-r1", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=False,
            comment_text="", deep=False,
        ))

        out = capsys.readouterr().out
        # 任务标题
        assert "任务审查: task-r1" in out
        assert "实现用户认证" in out
        # 文件变更摘要
        assert "auth.py" in out
        # 子任务详情含图标
        assert "sub-1" in out
        assert "sub-2" in out
        assert "❌" in out  # failed 子任务
        # 质量仪表
        assert "Quality Dashboard" in out or "通过率" in out

    def test_review_with_blocked_subtask(self, tmp_path, monkeypatch, capsys):
        """blocked 子任务 → 🔗 图标 + 失败原因"""
        td = _make_task_dir(tmp_path, "task-r2", [
            _result("sub-1", status="failed", verify_ok=False,
                    failure_reason="编译失败"),
            _result("sub-2", status="blocked", verify_ok=False,
                    failure_reason="上游依赖失败"),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(argparse.Namespace(
            task_id="task-r2", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=False,
            comment_text="", deep=False,
        ))

        out = capsys.readouterr().out
        assert "🔗" in out  # blocked 图标
        assert "编译失败" in out or "上游依赖失败" in out

    def test_review_success_all_green(self, tmp_path, monkeypatch, capsys):
        """全部完成 → 质量仪表显示🟢"""
        td = _make_task_dir(tmp_path, "task-r3", [
            _result("sub-1", status="completed", files=["a.py"]),
            _result("sub-2", status="completed", files=["b.py"]),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(argparse.Namespace(
            task_id="task-r3", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=False,
            comment_text="", deep=False,
        ))

        out = capsys.readouterr().out
        assert "🟢" in out or "可以合并" in out


# ═══════════════════════════════════════════════════════════════
# 场景 2/3/4：三态审批
# ═══════════════════════════════════════════════════════════════

class TestReviewDecision:
    """四种审查结论的 review.json 写入"""

    def _approve_args(self, task_id="task-appr"):
        return argparse.Namespace(
            task_id=task_id, repo=None, yes=False, pr_ref="",
            approve=True, reject=False, changes_requested=False,
            comment_text="", deep=False,
        )

    def _reject_args(self, task_id="task-rej"):
        return argparse.Namespace(
            task_id=task_id, repo=None, yes=False, pr_ref="",
            approve=False, reject=True, changes_requested=False,
            comment_text="", deep=False,
        )

    def _changes_args(self, task_id="task-chg", comment="需要修改接口文档"):
        return argparse.Namespace(
            task_id=task_id, repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=True,
            comment_text=comment, deep=False,
        )

    def test_approve_writes_review_json(self, tmp_path, monkeypatch, capsys):
        """--approve → review.json decision=approved"""
        td = _make_task_dir(tmp_path, "task-appr", [
            _result("sub-1", files=["a.py"]),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(self._approve_args())

        review_file = td / "review.json"
        assert review_file.exists()
        review = json.loads(review_file.read_text(encoding="utf-8"))
        assert review["decision"] == "approved"
        assert "reviewed_at" in review
        out = capsys.readouterr().out
        assert "✅" in out
        assert "审查通过" in out

    def test_reject_writes_review_json(self, tmp_path, monkeypatch, capsys):
        """--reject → review.json decision=rejected"""
        td = _make_task_dir(tmp_path, "task-rej", [
            _result("sub-1", status="failed", verify_ok=False, failure_reason="测试失败"),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(self._reject_args())

        review = json.loads((td / "review.json").read_text(encoding="utf-8"))
        assert review["decision"] == "rejected"
        assert "reviewed_at" in review
        out = capsys.readouterr().out
        assert "❌" in out
        assert "审查未通过" in out

    def test_changes_requested_with_comment(self, tmp_path, monkeypatch, capsys):
        """--changes-requested —comment-text → review.json 含评论"""
        td = _make_task_dir(tmp_path, "task-chg", [
            _result("sub-1", files=["a.py"]),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(self._changes_args())

        review = json.loads((td / "review.json").read_text(encoding="utf-8"))
        assert review["decision"] == "changes-requested"
        assert "需要修改接口文档" in review.get("summary", "")
        out = capsys.readouterr().out
        assert "📝" in out
        assert "需要修改" in out

    def test_changes_without_comment_default_summary(self, tmp_path, monkeypatch, capsys):
        """--changes-requested 无 --comment-text → 默认 '需要修改后重新审查'"""
        td = _make_task_dir(tmp_path, "task-chg2", [
            _result("sub-1", files=["a.py"]),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(argparse.Namespace(
            task_id="task-chg2", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=True,
            comment_text="", deep=False,
        ))

        review = json.loads((td / "review.json").read_text(encoding="utf-8"))
        assert review["decision"] == "changes-requested"
        assert review["summary"] == "需要修改后重新审查"


# ═══════════════════════════════════════════════════════════════
# 场景 5：异常路径
# ═══════════════════════════════════════════════════════════════

class TestReviewErrors:
    """审查命令的异常处理"""

    def test_task_not_found(self, tmp_path, monkeypatch, capsys):
        """任务不存在 → 错误提示"""
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        cmd_review(argparse.Namespace(
            task_id="task-nonexistent", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=False,
            comment_text="", deep=False,
        ))

        out = capsys.readouterr().out
        assert "不存在" in out or "❌" in out

    def test_corrupt_meta_json(self, tmp_path, monkeypatch, capsys):
        """meta.json 损坏 → 不是 crash，是错误提示"""
        td = tmp_path / "task-bad"
        td.mkdir()
        (td / "meta.json").write_text("{invalid json}", encoding="utf-8")

        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        from agent_go.cli import cmd_review
        # 不应抛出异常
        cmd_review(argparse.Namespace(
            task_id="task-bad", repo=None, yes=False, pr_ref="",
            approve=False, reject=False, changes_requested=False,
            comment_text="", deep=False,
        ))

        out = capsys.readouterr().out
        assert "无法读取" in out or "❌" in out


# ═══════════════════════════════════════════════════════════════
# 场景 6：Deep Review（深层审查）
# ═══════════════════════════════════════════════════════════════

class TestDeepReview:
    """cmd_review --deep：独立模型分析每个子任务的 diff。"""

    def _setup_deep_review(self, tmp_path, task_id="task-dr", results=None):
        """构造含 worktree 路径的 task 目录。"""
        if results is None:
            results = [
                _result("sub-1", files=["auth.py"]),
                _result("sub-2", files=["login.tsx"]),
            ]
        td = tmp_path / task_id
        td.mkdir(parents=True)

        # 为每个子任务创建 worktree 目录
        for r in results:
            wt = td / r["subtask_id"] / "work"
            wt.mkdir(parents=True)
            (wt / ".git").mkdir()
            r["worktree"] = str(wt)

        # 写 meta.json（含 worktree 路径）
        meta = {
            "task_id": task_id, "task": "实现用户认证", "status": "completed",
            "subtasks": [
                {"id": r["subtask_id"],
                 "title": f"任务{r['subtask_id']}",
                 "agent_type": "developer"}
                for r in results
            ],
            "results": results,
            "created": "20260725-100000",
        }
        (td / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return td

    def test_deep_review_calls_independent_model(self, tmp_path, monkeypatch, capsys):
        """--deep → 调用独立模型审查 diff"""
        td = self._setup_deep_review(tmp_path)
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        with patch("agent_go.cli.subprocess.run") as mock_subprocess, \
             patch("agent_go.router.resolve_provider") as mock_resolve, \
             patch("agent_go.router.call_with_role") as mock_call, \
             patch("agent_go.cli.config", {
                 "router": {"enabled": True},
                 "plan_api": {"provider": "anthropic", "api_key": "sk-test"},
             }, create=True):
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="diff --git a/auth.py b/auth.py\n+def login(): pass\n",
                stderr="",
            )
            # 模拟 router 返回一个有效的路由
            from collections import namedtuple
            _fake_route = namedtuple("_Route", ["role", "primary", "fallback"])
            _fake_pc = namedtuple("_ProviderConfig", ["provider", "base_url", "model", "api_key"])
            mock_resolve.return_value = _fake_route(
                role="reviewer",
                primary=_fake_pc(provider="anthropic", base_url="", model="claude-sonnet-4", api_key="sk-test"),
                fallback=None,
            )
            mock_call.return_value = ("审查通过，代码质量良好", {"cost_usd": 0.002})

            from agent_go.cli import cmd_review
            cmd_review(argparse.Namespace(
                task_id="task-dr", repo=None, yes=False, pr_ref="",
                approve=False, reject=False, changes_requested=False,
                comment_text="", deep=True,
            ))

        # 验证独立模型被调用
        assert mock_call.called, "call_with_role 应被调用"
        out = capsys.readouterr().out
        assert "深层审查" in out
        assert "审查通过" in out

    def test_deep_review_worktree_missing_skips(self, tmp_path, monkeypatch, capsys):
        """worktree 目录不存在 → 跳过该子任务（不 crash）"""
        td = _make_task_dir(tmp_path, "task-dr2", [
            _result("sub-1", files=["auth.py"]),
        ])
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        with patch("agent_go.cli.subprocess.run") as mock_subprocess, \
             patch("agent_go.router.resolve_provider") as mock_resolve, \
             patch("agent_go.cli.config", {
                 "router": {"enabled": True},
                 "plan_api": {"provider": "anthropic"},
             }, create=True):
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            from agent_go.cli import cmd_review
            cmd_review(argparse.Namespace(
                task_id="task-dr2", repo=None, yes=False, pr_ref="",
                approve=False, reject=False, changes_requested=False,
                comment_text="", deep=True,
            ))

        # sub-1 worktree 不存在 → 不调用 resolve_provider
        mock_resolve.assert_not_called()
        out = capsys.readouterr().out
        assert "深层审查" in out or "任务审查" in out

    def test_deep_review_empty_diff_skips(self, tmp_path, monkeypatch, capsys):
        """git diff 为空 → 跳过该子任务"""
        td = self._setup_deep_review(tmp_path, task_id="task-dr3")
        monkeypatch.setattr("agent_go.cli.AGENT_GO_DIR", tmp_path)
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "AGENT_GO_DIR", tmp_path)

        with patch("agent_go.cli.subprocess.run") as mock_subprocess, \
             patch("agent_go.router.resolve_provider") as mock_resolve, \
             patch("agent_go.cli.config", {
                 "router": {"enabled": True},
                 "plan_api": {"provider": "anthropic"},
             }, create=True):
            # git diff 返回空
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            from agent_go.cli import cmd_review
            cmd_review(argparse.Namespace(
                task_id="task-dr3", repo=None, yes=False, pr_ref="",
                approve=False, reject=False, changes_requested=False,
                comment_text="", deep=True,
            ))

        # diff 为空 → 跳过，不调用独立模型
        mock_resolve.assert_not_called()
