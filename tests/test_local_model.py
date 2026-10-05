"""本地模型生命周期管理 P0（只读管理面）测试——零真实进程、零外发。

边界断言（本文件的核心）：
- 只读：模块不 import subprocess；四个只读命令不改动 defender 目录任何文件；
- fail-open：探测全失败只降级字段/诊断级别，不抛异常；
- 默认关：enabled=false 时命令明确报错（CLI exit 1）。
"""
import argparse
import json
import os
import time
from pathlib import Path

import pytest

from agent_go import cli
from agent_go.local_model import (
    LEVELS,
    LocalModelManager,
    _conf_value,
    _fmt_duration,
    _strip_trailing_comment,
    format_status_text,
)


# ---------------------------------------------------------------------------
# 档位文件解析（只读）
# ---------------------------------------------------------------------------

def test_conf_value_quotes_and_trailing_comments():
    body = (
        'CONFIG_NAME="Splash 27B"\n'
        'CONFIG_DESC="desc # 保留引号内的井号"\n'
        'LLAMA_PORT=8081   # 行尾注释剥掉\n'
        'MODEL_NAME=$HOME/models/x   # 未加引号\n'
        'CONFIG_MEMORY="17.4GB"\n'
    )
    assert _conf_value(body, "CONFIG_NAME") == "Splash 27B"
    assert _conf_value(body, "CONFIG_DESC") == "desc # 保留引号内的井号"
    assert _conf_value(body, "LLAMA_PORT") == "8081"
    assert _conf_value(body, "MODEL_NAME") == "$HOME/models/x"
    assert _conf_value(body, "MISSING", "CONFIG_MEMORY") == "17.4GB"
    assert _conf_value(body, "NOPE") == ""
    assert _strip_trailing_comment("v  # c") == "v"
    assert _strip_trailing_comment("'a#b'") == "'a#b'"


def _make_defender(tmp: Path, *, with_manage: bool = True, with_pid: bool = True) -> Path:
    root = tmp / "defender"
    (root / "configs").mkdir(parents=True)
    if with_manage:
        (root / "manage.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "configs" / "alpha.conf").write_text(
        'CONFIG_NAME="Alpha"\nCONFIG_DESC="第一个档"\nCONFIG_MEMORY="10GB"\n'
        'MODEL_NAME="org/alpha-4bit"   # 注释\nLLAMA_PORT=8081\nLLAMA_BACKEND=rapid-mlx\n',
        encoding="utf-8")
    (root / "configs" / "beta.conf").write_text(
        'CONFIG_NAME="Beta"\nMODEL_NAME="org/beta-4bit"\nLLAMA_PORT=8084\n', encoding="utf-8")
    (root / "configs" / "active.conf").symlink_to("alpha.conf")
    if with_pid:
        (root / "anthropic_proxy.pid").write_text("4242\n", encoding="utf-8")
    return root


def _manager(root: Path, **over) -> LocalModelManager:
    cfg = {"local_model_manager": {"enabled": True, "manage_script": str(root / "manage.sh"),
                                   "proxy_url": "http://127.0.0.1:4000", "wait_ready_timeout": 120}}
    cfg["local_model_manager"].update(over)
    return LocalModelManager(cfg)


def _stub(mgr: LocalModelManager, *, api=None, metrics=None, models_ok=False, fallback=""):
    mgr._api_status = lambda: api
    mgr._metrics = lambda: metrics
    mgr._models_ok = lambda: models_ok
    mgr._loaded_model_fallback = lambda: fallback
    return mgr


def test_list_profiles_parses_fields_and_active_mark(tmp_path):
    mgr = _manager(_make_defender(tmp_path))
    rows = mgr.list_profiles()
    assert [r["profile"] for r in rows] == ["alpha", "beta"]  # active.conf 本身不算档位
    alpha, beta = rows
    assert alpha == {"profile": "alpha", "name": "Alpha", "desc": "第一个档", "memory": "10GB",
                     "model": "org/alpha-4bit", "port": "8081", "backend": "rapid-mlx", "active": True}
    assert beta["name"] == "Beta" and beta["active"] is False and beta["desc"] == ""
    assert mgr.current_profile() == "alpha"


def test_current_profile_broken_symlink_and_regular_file(tmp_path):
    root = _make_defender(tmp_path)
    link = root / "configs" / "active.conf"
    link.unlink()
    link.symlink_to("missing.conf")
    assert _manager(root).current_profile() == "missing"  # 坏链仍报"配置指向"
    link.unlink()
    link.write_text("MODEL_NAME=x\n", encoding="utf-8")
    assert _manager(root).current_profile() == "active"
    link.unlink()
    assert _manager(root).current_profile() == ""


def test_availability_reasons(tmp_path):
    mgr = LocalModelManager({})
    ok, reason = mgr.availability()
    assert ok is False and "enabled=true" in reason
    root = _make_defender(tmp_path)
    ok, reason = _manager(root, proxy_url="").availability()
    assert ok is False and "proxy_url" in reason
    ok, reason = _manager(root, manage_script="").availability()
    assert ok is False and "manage_script" in reason
    ok, reason = _manager(tmp_path / "gone").availability()
    assert ok is False and "manage.sh 不存在" in reason
    assert _manager(root).availability() == (True, "")


# ---------------------------------------------------------------------------
# 分级诊断（设计 §3.4 六个级别）
# ---------------------------------------------------------------------------

def _api(state="healthy", *, alive=True, model="org/alpha-4bit", profile="alpha", proxy_alive=True):
    return {"api_version": "2", "state": state, "ready": alive,
            "proxy": {"pid": 15430, "uptime_sec": 3600, "alive": proxy_alive},
            "backend": {"pid": 15279, "uptime_sec": 3600, "alive": alive, "model_name": model,
                        "backend_type": "local"},
            "active_profile": profile, "local_engines": []}


def test_diagnose_healthy(tmp_path):
    mgr = _stub(_manager(_make_defender(tmp_path)), api=_api())
    result = mgr.diagnose()
    assert result["level"] == "healthy" and result["advice"] == []
    assert "org/alpha-4bit" in result["reasons"][0]


def test_diagnose_model_drift(tmp_path):
    mgr = _stub(_manager(_make_defender(tmp_path)), api=_api(profile="beta"))
    result = mgr.diagnose()
    assert result["level"] == "model_drift"
    assert any("reload" in a for a in result["advice"])


def test_diagnose_backend_down(tmp_path):
    mgr = _stub(_manager(_make_defender(tmp_path)), api=_api(state="degraded", alive=False, model=""))
    result = mgr.diagnose()
    assert result["level"] == "backend_down"
    assert any("start-backend" in a for a in result["advice"])


def test_diagnose_starting_when_process_alive(tmp_path, monkeypatch):
    root = _make_defender(tmp_path)
    monkeypatch.setattr("agent_go.local_model._pid_alive", lambda pid: True)
    mgr = _stub(_manager(root), api=None, models_ok=False)
    assert mgr.diagnose()["level"] == "starting"


def test_diagnose_proxy_down_and_down(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_go.local_model._pid_alive", lambda pid: False)
    root = _make_defender(tmp_path)
    result = _stub(_manager(root), api=None, models_ok=False).diagnose()
    assert result["level"] == "proxy_down" and any("manage.sh start" in a for a in result["advice"])
    (root / "manage.sh").unlink()
    result = _stub(_manager(root), api=None, models_ok=False).diagnose()
    assert result["level"] == "down"
    assert all(level in LEVELS for level in (result["level"],))


def test_diagnose_legacy_proxy_without_api_status(tmp_path):
    root = _make_defender(tmp_path)
    mgr = _stub(_manager(root), api=None, models_ok=True, fallback="")
    assert mgr.diagnose()["level"] == "backend_down"  # 旧代理：/v1/models 可达但探不到模型名
    mgr = _stub(_manager(root), api=None, models_ok=True, fallback="org/alpha-4bit")
    assert mgr.diagnose()["level"] == "healthy"


# ---------------------------------------------------------------------------
# 状态聚合与降级
# ---------------------------------------------------------------------------

def test_status_aggregates_metrics_engines_and_drift(tmp_path):
    metrics = {"schema": "v2", "total": 100, "status": {"200": 90, "499": 10},
               "ttft": {"p50_ms": 11714.4, "p95_ms": 42601.0}, "quality_flags": {}}
    api = _api()
    api["local_engines"] = [{"provider": "local", "alive": True, "model_name": "org/alpha-4bit"}]
    mgr = _stub(_manager(_make_defender(tmp_path)), api=api, metrics=metrics)
    status = mgr.status()
    assert status["proxy_reachable"] and status["backend_alive"]
    assert status["loaded_model"] == "org/alpha-4bit"
    assert status["metrics"]["ttft"]["p95_ms"] == 42601.0
    assert status["engines"][0]["provider"] == "local"
    assert status["configured_profile"] == "alpha" and status["active_profile"] == "alpha"
    text = "\n".join(format_status_text(status))
    assert "21h" not in text  # uptime 3600s → 1h00m
    assert "1h00m" in text and "org/alpha-4bit" in text and "ttft p50=11714.4ms" in text


def test_status_degrades_without_network(tmp_path):
    mgr = _stub(_manager(_make_defender(tmp_path)), api=None, metrics=None, models_ok=False)
    status = mgr.status()
    assert status["proxy_reachable"] is False and status["backend_alive"] is False
    assert status["loaded_model"] == "" and status["metrics"] is None
    assert "不可达" in "\n".join(format_status_text(status))
    assert _fmt_duration(None) == "?" and _fmt_duration(-1) == "?" and _fmt_duration(59) == "59s"


# ---------------------------------------------------------------------------
# 只读性硬断言（P0 边界）
# ---------------------------------------------------------------------------

def test_module_never_shells_out_and_never_writes():
    """AST 级只读断言：不 import subprocess、不调 os.system/popen、不开写模式文件。"""
    import ast

    src = Path(__file__).resolve().parents[1] / "agent_go" / "local_model.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" not in imported
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(isinstance(c.func, ast.Attribute) and c.func.attr in ("system", "popen", "remove", "unlink",
                                                                        "write_text", "write_bytes", "mkdir")
                   for c in calls)
    assert not any(isinstance(c.func, ast.Name) and c.func.id == "open" for c in calls)


def test_readonly_commands_touch_no_files(tmp_path):
    root = _make_defender(tmp_path)
    mgr = _stub(_manager(root), api=_api(), metrics={"total": 1}, models_ok=True)

    def snapshot():
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in root.rglob("*") if p.is_file() or p.is_symlink()}

    before = snapshot()
    time.sleep(0.01)
    mgr.status(), mgr.list_profiles(), mgr.current_profile(), mgr.diagnose()
    assert snapshot() == before


# ---------------------------------------------------------------------------
# CLI（cmd_model 直调；不经过 cli.main 的重副作用路径）
# ---------------------------------------------------------------------------

def _args(**over):
    base = {"model_subcommand": None, "json": False, "json_mode": False, "config": None}
    base.update(over)
    return argparse.Namespace(**base)


def _patch_config(monkeypatch, root: Path, enabled: bool = True):
    cfg = {"local_model_manager": {"enabled": enabled, "manage_script": str(root / "manage.sh"),
                                   "proxy_url": "http://127.0.0.1:4000"}}
    monkeypatch.setattr("agent_go.config.load_config", lambda *a, **k: cfg)
    return cfg


def test_cmd_model_disabled_exits_1(monkeypatch, capsys):
    assert cli.cmd_model(_args()) == 1
    captured = capsys.readouterr()
    assert "未启用" in captured.out + captured.err


def test_cmd_model_json_list_and_status(monkeypatch, tmp_path, capsys):
    root = _make_defender(tmp_path)
    _patch_config(monkeypatch, root)
    assert cli.cmd_model(_args(model_subcommand="list", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["profile"] for r in rows] == ["alpha", "beta"] and rows[0]["active"] is True

    mgr_probe = LocalModelManager({"local_model_manager": {"enabled": True, "proxy_url": "x",
                                                           "manage_script": str(root / "manage.sh")}})
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._api_status", lambda self: _api())
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._metrics", lambda self: None)
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._models_ok", lambda self: True)
    assert cli.cmd_model(_args(model_subcommand="status", json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["loaded_model"] == "org/alpha-4bit"
    assert mgr_probe.current_profile() == "alpha"


def test_cmd_model_diagnose_exit_code(monkeypatch, tmp_path, capsys):
    root = _make_defender(tmp_path)
    _patch_config(monkeypatch, root)
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._api_status", lambda self: _api())
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._metrics", lambda self: None)
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._models_ok", lambda self: True)
    assert cli.cmd_model(_args(model_subcommand="diagnose")) == 0
    assert "healthy" in capsys.readouterr().out

    monkeypatch.setattr("agent_go.local_model.LocalModelManager._api_status", lambda self: None)
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._models_ok", lambda self: False)
    monkeypatch.setattr("agent_go.local_model._pid_alive", lambda pid: False)
    assert cli.cmd_model(_args(model_subcommand="diagnose", json=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["level"] == "proxy_down"


def test_cmd_model_current_reports_mismatch(monkeypatch, tmp_path, capsys):
    root = _make_defender(tmp_path)
    _patch_config(monkeypatch, root)
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._api_status",
                        lambda self: _api(profile="beta"))
    monkeypatch.setattr("agent_go.local_model.LocalModelManager._models_ok", lambda self: True)
    assert cli.cmd_model(_args(model_subcommand="current", json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"configured_profile": "alpha", "running_profile": "beta"}


def test_cli_parser_registers_model_commands():
    parser = cli._build_parser()
    for sub in ("status", "list", "current", "diagnose"):
        args = parser.parse_args(["model", sub, "--json"])
        assert args.command == "model" and args.model_subcommand == sub and args.json is True
    assert parser.parse_args(["model"]).model_subcommand is None


def test_config_default_section_exists():
    from agent_go.config import DEFAULT_CONFIG
    section = DEFAULT_CONFIG.get("local_model_manager")
    assert section == {"enabled": False, "manage_script": "", "proxy_url": "http://127.0.0.1:4000",
                       "wait_ready_timeout": 120}


@pytest.mark.parametrize("pid", [0, -3])
def test_pid_zero_or_negative_is_not_alive(pid):
    from agent_go.local_model import _pid_alive
    assert _pid_alive(pid) is False


def test_pid_alive_self():
    from agent_go.local_model import _pid_alive
    assert _pid_alive(os.getpid()) is True
