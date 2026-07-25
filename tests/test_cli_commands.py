"""测试 cli.py 中尚未覆盖的命令：
  - cmd_router：角色感知模型路由配置管理（show/enable/disable/set-role）
  - cmd_cache：Plan 缓存管理（list/clean/clear/stats）
  - cmd_agents：列出 Agent 类型
  - cmd_skills：列出 Skill
  - cmd_pr：生成 PR 描述（offline 模式 + gh 失败回退）

这些命令此前仅有签名冒烟测试（test_p0_p1_fixes.py），无行为覆盖。
"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════
# cmd_router
# ═══════════════════════════════════════════════════════════════

class TestCmdRouter:
    """角色感知模型路由配置管理（PRD P1 / S4）。"""

    def _ns(self, **kw):
        defaults = {"router_subcommand": "show", "role": None, "provider": None,
                    "model": None, "base_url": None,
                    "fallback_provider": None, "fallback_model": None,
                    "fallback_base_url": None}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def _stub(self, monkeypatch, config, tmp_path):
        """隔离 cmd_router 的全局依赖。

        - load_config：cli.py 顶部导入，patch agent_go.cli.load_config
        - CONFIG_PATH：cmd_router 函数内 `from .config import CONFIG_PATH`，
          patch agent_go.config.CONFIG_PATH（指向 tmp_path 避免污染真实配置）
        """
        monkeypatch.setattr("agent_go.cli.load_config", lambda: config)
        cfg_path = tmp_path / "config.json"
        import agent_go.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        return cfg_path

    def test_show_default(self, capsys, monkeypatch, tmp_path):
        """无 subcommand → 默认 show"""
        from agent_go.cli import cmd_router
        self._stub(monkeypatch, {}, tmp_path)
        cmd_router(self._ns(router_subcommand="show"))
        out = capsys.readouterr().out
        assert "角色感知路由" in out

    def test_show_with_roles_configured(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        config = {"router": {"enabled": True, "roles": {
            "planner": {"provider": "anthropic", "model": "opus"},
            "worker": {"provider": "openai", "model": "gpt",
                        "fallback": {"provider": "anthropic", "model": "haiku"}},
        }}}
        self._stub(monkeypatch, config, tmp_path)
        cmd_router(self._ns(router_subcommand="show"))
        out = capsys.readouterr().out
        assert "启用" in out
        assert "planner" in out
        assert "worker" in out

    def test_enable_writes_config(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {"enabled": False}}, tmp_path)
        cmd_router(self._ns(router_subcommand="enable"))
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["router"]["enabled"] is True
        assert "已启用" in capsys.readouterr().out

    def test_disable_writes_config(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {"enabled": True}}, tmp_path)
        cmd_router(self._ns(router_subcommand="disable"))
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["router"]["enabled"] is False
        assert "已禁用" in capsys.readouterr().out

    def test_set_role_writes_role_config(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {"enabled": True}}, tmp_path)
        cmd_router(self._ns(
            router_subcommand="set-role", role="worker",
            provider="openai", model="gpt-4o", base_url="http://api/v1",
        ))
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        role = saved["router"]["roles"]["worker"]
        assert role["provider"] == "openai"
        assert role["model"] == "gpt-4o"
        assert "fallback" not in role

    def test_set_role_with_fallback(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {}}, tmp_path)
        cmd_router(self._ns(
            router_subcommand="set-role", role="worker",
            provider="openai", model="gpt-4o", base_url="http://a",
            fallback_provider="anthropic", fallback_model="haiku",
            fallback_base_url="http://b",
        ))
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["router"]["roles"]["worker"]["fallback"]["model"] == "haiku"

    def test_set_role_partial_fallback_warns(self, capsys, monkeypatch, tmp_path):
        """PRD 铁律：部分 fallback 参数 → 警告，不写 fallback"""
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {}}, tmp_path)
        cmd_router(self._ns(
            router_subcommand="set-role", role="worker",
            provider="openai", model="gpt", base_url="http://a",
            fallback_provider="anthropic",  # 缺 model/base_url
        ))
        out = capsys.readouterr().out
        assert "需要同时指定" in out
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "fallback" not in saved["router"]["roles"]["worker"]

    def test_set_role_planner_fallback_warns(self, capsys, monkeypatch, tmp_path):
        """PRD 铁律：Planner 配置 fallback → 政策违规警告 + metering 标记 policy_violation"""
        from agent_go.cli import cmd_router
        cfg_path = self._stub(monkeypatch, {"router": {}}, tmp_path)
        cmd_router(self._ns(
            router_subcommand="set-role", role="planner",
            provider="anthropic", model="opus", base_url="http://a",
            fallback_provider="anthropic", fallback_model="haiku",
            fallback_base_url="http://b",
        ))
        out = capsys.readouterr().out
        # 政策违规提示（铁律执行力加强：从软警告升级为违规标记）
        assert "Planner" in out
        assert "政策违规" in out or "不应配置降级" in out
        assert "policy_violation" in out  # metering 标记（eval 可统计违规配置）

    def test_unknown_subcommand(self, capsys, monkeypatch, tmp_path):
        from agent_go.cli import cmd_router
        self._stub(monkeypatch, {}, tmp_path)
        cmd_router(self._ns(router_subcommand="bogus"))
        assert "未知操作" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════
# cmd_cache
# ═══════════════════════════════════════════════════════════════

class TestCmdCache:
    """Plan 缓存管理（ISSUE-2 修复后的缓存体系）。"""

    def _ns(self, sub):
        return argparse.Namespace(subcommand=sub)

    def test_list_empty(self, capsys, monkeypatch):
        from agent_go.cli import cmd_cache
        monkeypatch.setattr("agent_go.api.list_cache_entries", lambda: [])
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})
        cmd_cache(self._ns("list"))
        assert "暂无缓存" in capsys.readouterr().out

    def test_list_with_entries(self, capsys, monkeypatch):
        from agent_go.cli import cmd_cache
        entries = [
            {"cache_key": "abc123def456", "meta": {"task": "task-A", "created_at": "2026-07-25T10", "hit_count": 3}},
            {"cache_key": "xyz789", "meta": {"task": "task-B-very-long-name-here", "created_at": "2026-07-25T11", "hit_count": 0}},
        ]
        monkeypatch.setattr("agent_go.api.list_cache_entries", lambda: entries)
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})
        cmd_cache(self._ns("list"))
        out = capsys.readouterr().out
        assert "task-A" in out
        assert "abc123def456"[:12] in out  # key 截断到 12 字符

    def test_clean_reports_removed(self, capsys, monkeypatch):
        from agent_go.cli import cmd_cache
        monkeypatch.setattr("agent_go.api.clean_expired_cache", lambda c: 5)
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})
        cmd_cache(self._ns("clean"))
        assert "清理 5 条" in capsys.readouterr().out

    def test_clear_wipes_directory(self, capsys, tmp_path, monkeypatch):
        """clear: 删除并重建 _cache_dir()"""
        from agent_go.cli import cmd_cache
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "entry1.json").write_text("{}", encoding="utf-8")

        import agent_go.api as api_mod
        monkeypatch.setattr(api_mod, "_cache_dir", lambda: cache_dir)
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})

        cmd_cache(self._ns("clear"))
        out = capsys.readouterr().out
        assert "已清除" in out
        assert cache_dir.exists()  # 重建
        assert list(cache_dir.iterdir()) == []  # 内容被清空

    def test_stats_with_entries(self, capsys, tmp_path, monkeypatch):
        from agent_go.cli import cmd_cache
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "a.json").write_text("{}", encoding="utf-8")
        (cache_dir / "b.json").write_text("x" * 2048, encoding="utf-8")

        entries = [{"meta": {"hit_count": 2}}, {"meta": {"hit_count": 5}}]
        monkeypatch.setattr("agent_go.api.list_cache_entries", lambda: entries)
        import agent_go.api as api_mod
        monkeypatch.setattr(api_mod, "_cache_dir", lambda: cache_dir)
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})

        cmd_cache(self._ns("stats"))
        out = capsys.readouterr().out
        assert "缓存条目: 2" in out
        assert "总命中: 7" in out
        assert "KB" in out  # 2048B 文件 → KB 显示

    def test_stats_empty(self, capsys, monkeypatch):
        from agent_go.cli import cmd_cache
        monkeypatch.setattr("agent_go.api.list_cache_entries", lambda: [])
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})
        cmd_cache(self._ns("stats"))
        assert "缓存条目: 0" in capsys.readouterr().out

    def test_unknown_subcommand(self, capsys, monkeypatch):
        from agent_go.cli import cmd_cache
        monkeypatch.setattr("agent_go.config.load_config", lambda: {})
        cmd_cache(self._ns("bogus"))
        assert "未知子命令" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════
# cmd_agents / cmd_skills
# ═══════════════════════════════════════════════════════════════

class TestCmdAgents:
    def test_lists_builtin_agents(self, capsys):
        from agent_go.cli import cmd_agents
        cmd_agents()
        out = capsys.readouterr().out
        assert "Agent 类型" in out
        # 内置至少包含 developer
        assert "developer" in out

    def test_long_description_truncated(self, capsys):
        """描述超过 40 字符时截断 + ..."""
        from agent_go.cli import cmd_agents
        with patch("agent_go.cli.list_agent_types", return_value=[
            {"type": "x", "source": "builtin", "description": "a" * 50},
        ]):
            cmd_agents()
        out = capsys.readouterr().out
        assert "..." in out


class TestCmdSkills:
    def test_no_skills_hint(self, capsys):
        from agent_go.cli import cmd_skills
        with patch("agent_go.cli.list_skills", return_value=[]):
            cmd_skills()
        out = capsys.readouterr().out
        assert "暂无可用 Skill" in out

    def test_lists_skills(self, capsys):
        from agent_go.cli import cmd_skills
        with patch("agent_go.cli.list_skills", return_value=[
            {"name": "security-review", "description": "代码安全审查"},
            {"name": "perf", "description": "p" * 50},  # 截断
        ]):
            cmd_skills()
        out = capsys.readouterr().out
        assert "可用 Skill (2 个)" in out
        assert "security-review" in out
        assert "..." in out


# ═══════════════════════════════════════════════════════════════
# cmd_pr
# ═══════════════════════════════════════════════════════════════

class TestCmdPr:
    """PR 描述生成（PRD Phase 4 交付阶段）。"""

    def _meta(self, **kw):
        meta = {
            "task": "实现登录功能", "results": [
                {"subtask_id": "sub-1", "status": "completed", "summary": "新增 auth.py",
                 "sandbox_type": "headless", "duration_sec": 30.0},
                {"subtask_id": "sub-2", "status": "failed", "summary": "前端失败",
                 "sandbox_type": "headless", "duration_sec": 12.0},
            ],
        }
        meta.update(kw)
        return meta

    def _setup_task(self, tmp_path, meta):
        import agent_go.cli as cli_mod
        import agent_go.config as config_mod
        agent_dir = tmp_path / "agent_go_dir"
        agent_dir.mkdir()
        task_dir = agent_dir / "task-pr"
        task_dir.mkdir()
        (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        monkeypatch_targets = [
            (cli_mod, "AGENT_GO_DIR", agent_dir),
            (config_mod, "AGENT_GO_DIR", agent_dir),
        ]
        return task_dir, monkeypatch_targets

    def test_usage_no_task_id(self, monkeypatch):
        """无 task_id 参数 + 无 argv → Usage + sys.exit(1)"""
        from agent_go.cli import cmd_pr
        monkeypatch.setattr(sys, "argv", ["agent_go", "pr"])
        with pytest.raises(SystemExit) as exc:
            cmd_pr()
        assert exc.value.code == 1

    def test_nonexistent_task_exits(self, monkeypatch, tmp_path):
        from agent_go.cli import cmd_pr
        import agent_go.cli as cli_mod
        monkeypatch.setattr(cli_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(sys, "argv", ["agent_go", "pr", "task-ghost"])
        with pytest.raises(SystemExit):
            cmd_pr()

    def test_offline_writes_pr_md(self, monkeypatch, tmp_path, capsys):
        from agent_go.cli import cmd_pr
        task_dir, targets = self._setup_task(tmp_path, self._meta())
        for mod, attr, val in targets:
            monkeypatch.setattr(mod, attr, val)
        args = argparse.Namespace(task_id="task-pr", offline=True)
        cmd_pr(args)
        out = capsys.readouterr().out
        pr = (task_dir / "PR.md").read_text(encoding="utf-8")
        assert "PR 描述已写入" in out
        assert "## Summary" in pr
        assert "实现登录功能" in pr
        assert "sub-1" in pr
        # 失败子任务用 ❌ 标记
        assert "❌" in pr

    def test_offline_with_issue_prefix(self, monkeypatch, tmp_path):
        """meta 含 issue → PR.md 前缀 Fixes #N"""
        from agent_go.cli import cmd_pr
        task_dir, targets = self._setup_task(tmp_path, self._meta(issue="42"))
        for mod, attr, val in targets:
            monkeypatch.setattr(mod, attr, val)
        cmd_pr(argparse.Namespace(task_id="task-pr", offline=True))
        pr = (task_dir / "PR.md").read_text(encoding="utf-8")
        assert pr.startswith("Fixes #42")

    def test_offline_includes_shared_context(self, monkeypatch, tmp_path):
        """存在 SHARED_CONTEXT.md → 内容进 Verification 段"""
        from agent_go.cli import cmd_pr
        task_dir, targets = self._setup_task(tmp_path, self._meta())
        (task_dir / "SHARED_CONTEXT.md").write_text("验证详情：pytest 全绿", encoding="utf-8")
        for mod, attr, val in targets:
            monkeypatch.setattr(mod, attr, val)
        cmd_pr(argparse.Namespace(task_id="task-pr", offline=True))
        pr = (task_dir / "PR.md").read_text(encoding="utf-8")
        assert "pytest 全绿" in pr

    def test_online_gh_success(self, monkeypatch, tmp_path, capsys):
        """在线模式 + gh 成功 → 打印 PR URL"""
        from agent_go.cli import cmd_pr
        task_dir, targets = self._setup_task(tmp_path, self._meta())
        for mod, attr, val in targets:
            monkeypatch.setattr(mod, attr, val)
        cp = MagicMock(returncode=0, stdout="https://github.com/x/y/pull/1\n")
        monkeypatch.setattr("agent_go.cli.subprocess.run", lambda *a, **k: cp)
        cmd_pr(argparse.Namespace(task_id="task-pr", offline=False))
        assert "github.com" in capsys.readouterr().out

    def test_online_gh_failure_backups_pr(self, monkeypatch, tmp_path, capsys):
        """在线模式 + gh 失败 → 备份到 PR.md"""
        from agent_go.cli import cmd_pr
        task_dir, targets = self._setup_task(tmp_path, self._meta())
        for mod, attr, val in targets:
            monkeypatch.setattr(mod, attr, val)
        cp = MagicMock(returncode=1, stderr="auth error")
        monkeypatch.setattr("agent_go.cli.subprocess.run", lambda *a, **k: cp)
        cmd_pr(argparse.Namespace(task_id="task-pr", offline=False))
        out = capsys.readouterr().out
        assert "失败" in out
        assert (task_dir / "PR.md").exists()
