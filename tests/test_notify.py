"""测试 notify.py — M1 多通道通知

覆盖: 配置解析（含旧配置兼容）、payload 组装、事件订阅过滤、
${VAR} 插值、webhook 重试策略、URL 校验、故障隔离。
"""

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.notify import (
    notify_event, build_payload,
    _resolve_notify_config, _interpolate, _is_allowed_url, _render_webhook_body,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_notify")


@pytest.fixture
def task_context(tmp_path):
    """构造一个含失败子任务 + 保留标记 + 计量的任务上下文。"""
    task_dir = tmp_path / "task-t1"
    sub3 = task_dir / "sub-3"
    sub3.mkdir(parents=True)
    (sub3 / ".preserved").write_text(json.dumps({
        "subtask_id": "sub-3", "status": "failed",
        "failure_reason": "pytest exit=1", "branch": "agent_go/task-t1/sub-3",
    }), encoding="utf-8")
    (task_dir / "metering.jsonl").write_text("\n".join([
        json.dumps({"role": "planner", "prompt_tokens": 100, "completion_tokens": 50,
                    "cost_usd": 0.01, "latency_ms": 100}),
        json.dumps({"role": "worker", "prompt_tokens": 1000, "completion_tokens": 500,
                    "cost_usd": 0.07, "latency_ms": 5000}),
    ]), encoding="utf-8")

    meta = {
        "task_id": "task-t1", "task": "重构认证模块", "repo": "/tmp/proj",
        "created": "20260725-030125-545", "status": "failed",
        "subtasks": [{"id": "sub-1", "title": "步骤1"}, {"id": "sub-3", "title": "迁移 OAuth2"}],
    }
    results_map = {
        "sub-1": {"subtask_id": "sub-1", "status": "completed"},
        "sub-3": {"subtask_id": "sub-3", "status": "failed",
                  "failure_reason": "pytest tests/test_auth.py exit=1: assert failed"},
    }
    return {"meta": meta, "results_map": results_map, "task_dir": task_dir, "duration_sec": 612}


# ═══════════════════════════════════════════════════════════════
# 配置解析 / 兼容层
# ═══════════════════════════════════════════════════════════════

class TestResolveConfig:
    def test_legacy_default_desktop(self):
        """无 notify 块 → 旧配置路径，默认 desktop 通道"""
        cfg = _resolve_notify_config({"behavior": {}})
        assert cfg is not None
        assert [c["type"] for c in cfg["channels"]] == ["desktop"]

    def test_legacy_disabled(self):
        """behavior.notify_on_complete=false → 整体关闭"""
        assert _resolve_notify_config({"behavior": {"notify_on_complete": False}}) is None

    def test_legacy_command_mapped(self):
        """旧 notify_command 映射为 command 通道"""
        cfg = _resolve_notify_config({"behavior": {"notify_command": "curl {task_id}"}})
        assert [c["type"] for c in cfg["channels"]] == ["desktop", "command"]

    def test_notify_block_wins(self):
        """notify 块存在时以它为准"""
        cfg = _resolve_notify_config({
            "behavior": {"notify_command": "curl x"},
            "notify": {"enabled": True, "channels": [{"type": "webhook", "url": "https://h"}]},
        })
        assert [c["type"] for c in cfg["channels"]] == ["webhook"]

    def test_notify_block_disabled(self):
        assert _resolve_notify_config({"notify": {"enabled": False}}) is None


# ═══════════════════════════════════════════════════════════════
# Payload 组装
# ═══════════════════════════════════════════════════════════════

class TestBuildPayload:
    def test_full_payload(self, task_context):
        p = build_payload("on_failed", task_context)
        assert p["event"] == "on_failed"
        assert p["task_id"] == "task-t1"
        assert p["subtasks"] == {"total": 2, "completed": 1, "failed": 1, "blocked": 0}
        assert p["duration_sec"] == 612
        assert p["failures"] == [{
            "subtask_id": "sub-3", "title": "迁移 OAuth2",
            "failure_reason": "pytest tests/test_auth.py exit=1: assert failed",
        }]
        assert p["preserved_worktrees"][0]["branch"] == "agent_go/task-t1/sub-3"
        assert p["cost"]["total_usd"] == 0.08
        assert p["cost"]["by_role"] == {"planner": 0.01, "worker": 0.07}
        assert "truncated" not in p

    def test_failure_reason_truncated(self, task_context):
        task_context["results_map"]["sub-3"]["failure_reason"] = "x" * 600
        p = build_payload("on_failed", task_context)
        assert len(p["failures"][0]["failure_reason"]) <= 501
        assert p["truncated"] is True

    def test_duration_from_created(self, task_context):
        """未传 duration_sec 时从 meta.created 推算（兼容毫秒后缀）"""
        del task_context["duration_sec"]
        p = build_payload("on_failed", task_context)
        assert p["duration_sec"] > 0


# ═══════════════════════════════════════════════════════════════
# 插值 / URL 校验 / 适配器
# ═══════════════════════════════════════════════════════════════

class TestHelpers:
    def test_interpolate_ok(self, monkeypatch):
        monkeypatch.setenv("WH_URL", "https://hook")
        assert _interpolate("${WH_URL}/x") == "https://hook/x"

    def test_interpolate_missing(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _interpolate("${NOPE}/x") is None

    def test_url_rules(self):
        assert _is_allowed_url("https://hooks.slack.com/x")
        assert _is_allowed_url("http://localhost:8080/topic")
        assert _is_allowed_url("http://127.0.0.1/topic")
        assert not _is_allowed_url("http://evil.example.com/x")
        assert not _is_allowed_url("ftp://x")

    def test_render_generic(self, task_context):
        p = build_payload("on_failed", task_context)
        body, headers = _render_webhook_body("generic", p)
        assert json.loads(body)["task_id"] == "task-t1"

    def test_render_slack(self, task_context):
        p = build_payload("on_failed", task_context)
        body, _ = _render_webhook_body("slack", p)
        text = json.loads(body)["text"]
        assert "task-t1" in text and "迁移 OAuth2" in text

    def test_render_dingtalk(self, task_context):
        p = build_payload("on_failed", task_context)
        body, _ = _render_webhook_body("dingtalk", p)
        data = json.loads(body)
        assert data["msgtype"] == "markdown"
        assert "sub-3" in data["markdown"]["text"]

    def test_render_ntfy(self, task_context):
        p = build_payload("on_failed", task_context)
        body, headers = _render_webhook_body("ntfy", p)
        assert b"task-t1" in body
        assert "X-Title" in headers


# ═══════════════════════════════════════════════════════════════
# 事件派发 / 重试 / 故障隔离
# ═══════════════════════════════════════════════════════════════

class TestNotifyEvent:
    def _urlopen_ok(self):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_event_subscription_filter(self, task_context):
        """通道只收到订阅的事件"""
        config = {"notify": {"channels": [
            {"type": "webhook", "url": "https://hook/a", "events": ["on_failed"]},
            {"type": "webhook", "url": "https://hook/b", "events": ["on_complete"]},
        ]}}
        with patch("urllib.request.urlopen", return_value=self._urlopen_ok()) as mock_open:
            notify_event("on_failed", task_context, config)
        assert mock_open.call_count == 1
        assert "hook/a" in mock_open.call_args[0][0].full_url

    def test_retry_on_network_error(self, task_context):
        import urllib.error
        config = {"notify": {"retry": 2, "channels": [
            {"type": "webhook", "url": "https://hook/a"},
        ]}}
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("conn refused")) as mock_open:
            notify_event("on_failed", task_context, config)
        assert mock_open.call_count == 3  # 1 + 2 retry

    def test_no_retry_on_4xx(self, task_context):
        import urllib.error
        config = {"notify": {"retry": 2, "channels": [
            {"type": "webhook", "url": "https://hook/a"},
        ]}}
        err = urllib.error.HTTPError("https://hook/a", 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=err) as mock_open:
            notify_event("on_failed", task_context, config)
        assert mock_open.call_count == 1

    def test_http_url_rejected(self, task_context):
        """非 https（非 localhost）URL 跳过，不发请求"""
        config = {"notify": {"channels": [{"type": "webhook", "url": "http://evil.com/x"}]}}
        with patch("urllib.request.urlopen") as mock_open:
            notify_event("on_failed", task_context, config)
        mock_open.assert_not_called()

    def test_missing_env_var_skips_channel(self, task_context, monkeypatch):
        monkeypatch.delenv("UNSET_HOOK", raising=False)
        config = {"notify": {"channels": [{"type": "webhook", "url": "${UNSET_HOOK}"}]}}
        with patch("urllib.request.urlopen") as mock_open:
            notify_event("on_failed", task_context, config)
        mock_open.assert_not_called()

    def test_channel_failure_isolated(self, task_context):
        """一个通道抛异常不影响其他通道，也不向上传播"""
        config = {"notify": {"channels": [
            {"type": "webhook", "url": "https://hook/bad"},
            {"type": "webhook", "url": "https://hook/good"},
        ]}}
        def flaky(req, timeout=None):
            if "bad" in req.full_url:
                raise RuntimeError("boom")
            return self._urlopen_ok()
        with patch("urllib.request.urlopen", side_effect=flaky) as mock_open:
            notify_event("on_failed", task_context, config)  # 不抛异常
        assert mock_open.call_count == 2

    def test_disabled_no_side_effects(self, task_context):
        with patch("urllib.request.urlopen") as mock_open, \
             patch("subprocess.run") as mock_run:
            notify_event("on_failed", task_context, {"notify": {"enabled": False}})
        mock_open.assert_not_called()
        mock_run.assert_not_called()

    def test_legacy_desktop_called(self, task_context):
        """旧配置路径：desktop 通道走 osascript"""
        with patch("subprocess.run") as mock_run:
            notify_event("on_complete", task_context, {"behavior": {}})
        assert mock_run.called
        assert mock_run.call_args[0][0][0] == "osascript"

    def test_command_channel_safe_vars(self, task_context):
        """command 通道模板变量渲染"""
        config = {"notify": {"channels": [
            {"type": "command", "command": "echo {task_id} {status} {completed}/{total}"},
        ]}}
        with patch("subprocess.run") as mock_run:
            notify_event("on_failed", task_context, config)
        assert mock_run.call_args[0][0] == ["echo", "task-t1", "failed", "1/2"]
