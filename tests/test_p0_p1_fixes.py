"""P0/P1 审查发现项的回归测试。

覆盖：
- D0: 包级导入冒烟（executor.py 曾因缺 Path 导入导致全包崩溃）
- D1: cmd_eval 接受 argparse Namespace（原签名不匹配导致 TypeError）
- A1: headless 模式强制 agent allowed_tools（原硬编码 bypassPermissions 无工具约束）
- A2: 验证沙箱剔除 AGENT_GO_API_KEY（原 AGENT_GO_ 前缀豁免导致密钥泄漏）
- D2: 成本统计按 model 定价（原按 provider 索引永远 miss，全部按 deepseek 兜底）
- D3: cmd_clean 按 repo→task_ids 逐个清理 tags（原只清理最后一个任务）
- D4: greywall 不得双重包装（原 greywall -- greywall -- claude）
"""

import argparse
import io
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_go
from agent_go import cli as cli_mod
from agent_go.agents import AgentType
from agent_go.eval import analyze_cost, cmd_eval
from agent_go.executor import _build_sandbox_env, _run_claude
from agent_go.subtask import _run_headless


@pytest.fixture
def null_logger():
    log = logging.getLogger("test_p0p1")
    log.handlers = [logging.NullHandler()]
    return log


# ═══════════════════════════════════════════════════════════════
# D0: 导入冒烟
# ═══════════════════════════════════════════════════════════════

class TestImportSmoke:
    def test_package_importable(self):
        """包必须可导入（D0 回归：executor.py Path 缺失曾致全包崩溃）"""
        assert agent_go.__version__

    def test_all_command_modules_importable(self):
        """所有命令模块可独立导入"""
        import agent_go.api  # noqa: F401
        import agent_go.cli  # noqa: F401
        import agent_go.eval  # noqa: F401
        import agent_go.executor  # noqa: F401
        import agent_go.pipeline  # noqa: F401
        import agent_go.subtask  # noqa: F401
        import agent_go.ui  # noqa: F401
        import agent_go.workflow_gen  # noqa: F401

    def test_parser_covers_all_commands(self):
        """CLI parser 必须能解析全部子命令的最小参数集"""
        parser = cli_mod._build_parser()
        cases = [
            (["run", "/tmp/x", "task"], "run"),
            (["resume", "task-1"], "resume"),
            (["list"], "list"),
            (["show", "task-1"], "show"),
            (["status"], "status"),
            (["clean"], "clean"),
            (["config"], "config"),
            (["skills"], "skills"),
            (["agents"], "agents"),
            (["pr", "task-1"], "pr"),
            (["ci"], "ci"),
            (["review", "/tmp/x"], "review"),
            (["cache", "list"], "cache"),
            (["eval", "quality"], "eval"),
        ]
        for argv, expected in cases:
            assert parser.parse_args(argv).command == expected, argv


# ═══════════════════════════════════════════════════════════════
# D1: cmd_eval 签名
# ═══════════════════════════════════════════════════════════════

class TestCmdEvalSignature:
    def test_dispatch_targets_accept_args(self):
        """main() 以 cmd_xxx(args) 形式调用的函数必须接受一个可选位置参数"""
        import inspect
        for fn in (cli_mod.cmd_run, cli_mod.cmd_resume, cli_mod.cmd_show,
                   cli_mod.cmd_pr, cli_mod.cmd_status, cli_mod.cmd_review,
                   cli_mod.cmd_cache, cli_mod.cmd_ci, cli_mod.cmd_eval):
            params = list(inspect.signature(fn).parameters.values())
            assert params, f"{fn.__name__} 必须能接受 args"
            assert params[0].default is None, f"{fn.__name__} 首参数应默认为 None"

    def test_cmd_eval_with_namespace(self, tmp_path, monkeypatch, capsys):
        """cmd_eval(args) 不再 TypeError；空目录输出'暂无任务'"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="quality", task_id=None, eval_all=False)
        cmd_eval(args)
        assert "暂无任务" in capsys.readouterr().out

    def test_cmd_eval_all_flag(self, tmp_path, monkeypatch, capsys):
        """--all 聚合路径可执行"""
        import agent_go.config as config_mod
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        args = argparse.Namespace(subcommand="all", task_id=None, eval_all=True)
        cmd_eval(args)
        out = capsys.readouterr().out
        assert "成本报告" in out  # cost/reliability/ux 不依赖历史数据也会输出


# ═══════════════════════════════════════════════════════════════
# A1: headless 强制 allowed_tools
# ═══════════════════════════════════════════════════════════════

class TestHeadlessAllowedTools:
    def test_run_headless_appends_allowed_tools(self, monkeypatch, tmp_path, null_logger):
        """allowed_tools 非空时应转为 --allowedTools 参数"""
        captured = {}

        class FakeProc:
            pid = 12345
            returncode = 0
            stdout = io.StringIO("")
            stderr = io.StringIO("")

            def poll(self):
                return 0

            def wait(self):
                return 0

            def kill(self):
                pass

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr("agent_go.subtask.subprocess.Popen", fake_popen)
        _run_headless("task", tmp_path, {}, null_logger, "sub-1",
                      allowed_tools=["Read", "Grep", "Glob"])

        cmd = captured["cmd"]
        assert "--allowedTools" in cmd
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep,Glob"

    def test_run_headless_no_restriction_when_empty(self, monkeypatch, tmp_path, null_logger):
        """allowed_tools 为空/None 时不追加 --allowedTools（developer 默认不限制）"""
        captured = {}

        class FakeProc:
            pid = 12345
            returncode = 0
            stdout = io.StringIO("")
            stderr = io.StringIO("")

            def poll(self):
                return 0

            def wait(self):
                return 0

            def kill(self):
                pass

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr("agent_go.subtask.subprocess.Popen", fake_popen)
        _run_headless("task", tmp_path, {}, null_logger, "sub-1", allowed_tools=[])
        assert "--allowedTools" not in captured["cmd"]

    def test_run_claude_headless_passes_agent_tools(self, tmp_path, null_logger):
        """_run_claude 应将 agent 的 allowed_tools 传给 _run_headless"""
        agent = AgentType(type_name="architect",
                          claude_config={"allowed_tools": ["Read", "Grep", "Glob"]})
        with patch("agent_go.executor._run_headless") as mock_h:
            from subprocess import CompletedProcess
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            _run_claude("task", tmp_path, {}, True, agent, "sub-1", set(), None, null_logger)
        assert mock_h.call_args.kwargs.get("allowed_tools") == ["Read", "Grep", "Glob"]

    def test_run_claude_headless_no_agent_unrestricted(self, tmp_path, null_logger):
        """无 agent 时 headless 不限制工具"""
        with patch("agent_go.executor._run_headless") as mock_h:
            from subprocess import CompletedProcess
            mock_h.return_value = CompletedProcess([], 0, stdout="", stderr="")
            _run_claude("task", tmp_path, {}, True, None, "sub-1", set(), None, null_logger)
        assert mock_h.call_args.kwargs.get("allowed_tools") == []


# ═══════════════════════════════════════════════════════════════
# A2: 验证沙箱剔除 AGENT_GO_API_KEY
# ═══════════════════════════════════════════════════════════════

class TestSandboxEnvApiKey:
    def test_agent_go_api_key_removed(self):
        """AGENT_GO_API_KEY 不得进入验证环境，其他 AGENT_GO_* 保留"""
        with patch.dict(os.environ, {
            "AGENT_GO_API_KEY": "sk-ant-secret",
            "AGENT_GO_TASK_ID": "task-1",
        }):
            env = _build_sandbox_env()
            assert "AGENT_GO_API_KEY" not in env
            assert env.get("AGENT_GO_TASK_ID") == "task-1"

    def test_os_environ_not_mutated(self):
        """剔除只作用于返回的副本"""
        with patch.dict(os.environ, {"AGENT_GO_API_KEY": "sk-ant-secret"}):
            _build_sandbox_env()
            assert os.environ.get("AGENT_GO_API_KEY") == "sk-ant-secret"


# ═══════════════════════════════════════════════════════════════
# D2: 成本统计按 model 定价
# ═══════════════════════════════════════════════════════════════

def _write_task_with_api_log(base: Path, task_name: str, events: list[dict]) -> Path:
    td = base / task_name
    td.mkdir(parents=True)
    (td / "meta.json").write_text(json.dumps({"task_id": task_name, "results": []}),
                                  encoding="utf-8")
    lines = [
        f'2026-01-01 00:00:00 | DEBUG    | agent_go.{task_name} | '
        + json.dumps(ev, separators=(",", ":"))
        for ev in events
    ]
    (td / "execution.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return td


class TestCostPricing:
    def test_anthropic_priced_by_model_not_fallback(self, tmp_path):
        """anthropic 调用必须按 claude 价格计费，不得按 deepseek 兜底"""
        _write_task_with_api_log(tmp_path, "task-20260101", [{
            "event": "api_call", "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "prompt_tokens": 1_000_000, "completion_tokens": 0,
        }])
        result = analyze_cost(tmp_path)
        # 1M prompt @ $3.0/M = $3.0；若按 deepseek 兜底只有 $0.27
        assert result["estimated_cost_usd"] == 3.0
        assert result["by_model"]["claude-sonnet-4-20250514"] == 3.0

    def test_missing_model_falls_back_to_provider_default(self, tmp_path):
        """旧日志缺 model 字段时按 provider 默认模型定价"""
        _write_task_with_api_log(tmp_path, "task-20260102", [{
            "event": "api_call", "provider": "anthropic",
            "prompt_tokens": 1_000_000, "completion_tokens": 0,
        }])
        result = analyze_cost(tmp_path)
        assert result["estimated_cost_usd"] == 3.0

    def test_multiple_models_aggregated_separately(self, tmp_path):
        """不同模型分别聚合计价"""
        _write_task_with_api_log(tmp_path, "task-20260103", [
            {"event": "api_call", "provider": "anthropic",
             "model": "claude-sonnet-4-20250514",
             "prompt_tokens": 1_000_000, "completion_tokens": 0},
            {"event": "api_call", "provider": "deepseek",
             "model": "deepseek-chat",
             "prompt_tokens": 1_000_000, "completion_tokens": 0},
        ])
        result = analyze_cost(tmp_path)
        assert result["by_model"]["claude-sonnet-4-20250514"] == 3.0
        assert result["by_model"]["deepseek-chat"] == 0.27
        assert result["estimated_cost_usd"] == 3.27


# ═══════════════════════════════════════════════════════════════
# D3: cmd_clean 按 repo→task_ids 清理 tags
# ═══════════════════════════════════════════════════════════════

class TestCmdCleanTags:
    def test_clean_deletes_tags_for_all_tasks(self, tmp_path, monkeypatch):
        """同一 repo 下多个任务的 tags 必须逐个清理（原只清理最后一个）"""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        task_dirs = []
        for i in (1, 2):
            tid = f"task-2026010{i}"
            td = tmp_path / tid
            td.mkdir()
            (td / "meta.json").write_text(json.dumps({
                "task_id": tid, "repo": str(repo), "status": "completed",
            }), encoding="utf-8")
            task_dirs.append(td)

        monkeypatch.setattr(cli_mod, "AGENT_GO_DIR", tmp_path)
        monkeypatch.setattr(cli_mod, "safe_input", lambda prompt="": "y")

        calls = []

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            r = FakeResult()
            if list(cmd)[:3] == ["git", "tag", "-l"]:
                r.stdout = f"{cmd[3].rstrip('/*')}/sub-1\n"
            return r

        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
        cli_mod.cmd_clean()

        tag_lists = [c for c in calls if c[:3] == ["git", "tag", "-l"]]
        assert ["git", "tag", "-l", "task-20260101/*"] in tag_lists
        assert ["git", "tag", "-l", "task-20260102/*"] in tag_lists
        tag_deletes = [c for c in calls if c[:3] == ["git", "tag", "-d"]]
        assert len(tag_deletes) == 2
        # 任务目录均被删除
        assert not task_dirs[0].exists() and not task_dirs[1].exists()


# ═══════════════════════════════════════════════════════════════
# D4: greywall 不得双重包装
# ═══════════════════════════════════════════════════════════════

class TestGreywallWrap:
    def test_no_double_wrap_with_agent(self, tmp_path, null_logger, monkeypatch):
        """agent 路径：get_claude_command 已含 greywall 包装，_run_claude 不得再包"""
        agent = AgentType(type_name="developer", claude_config={})
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeResult()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which",
                            lambda name: "/usr/bin/greywall" if name == "greywall" else None)
        _run_claude("task", tmp_path, {}, False, agent, "sub-1", set(), None, null_logger)

        cmd = captured["cmd"]
        assert sum(1 for tok in cmd if "greywall" in tok) == 1, f"双重包装: {cmd}"
        assert "claude" in cmd

    def test_no_agent_wraps_once(self, tmp_path, null_logger, monkeypatch):
        """无 agent 路径：greywall 只包装一次"""
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeResult()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which",
                            lambda name: "/usr/bin/greywall" if name == "greywall" else None)
        _run_claude("task", tmp_path, {}, False, None, "sub-1", set(), None, null_logger)

        cmd = captured["cmd"]
        assert sum(1 for tok in cmd if "greywall" in tok) == 1
        assert cmd[0] == "greywall" and cmd[1] == "--"

    def test_native_without_greywall(self, tmp_path, null_logger, monkeypatch):
        """无 greywall 时直接运行 claude，sandbox_type=native"""
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeResult()

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: None)
        agent = AgentType(type_name="developer", claude_config={})
        _, sandbox_type, _ = _run_claude("task", tmp_path, {}, False, agent,
                                         "sub-1", set(), None, null_logger)
        assert sandbox_type == "native"
        assert captured["cmd"][0] == "claude"
