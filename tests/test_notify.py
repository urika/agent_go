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
    _send_desktop, _send_command,
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



# ═══════════════════════════════════════════════════════════════
# _send_desktop 平台分支 / 容错
# ═══════════════════════════════════════════════════════════════

class TestSendDesktop:
    def test_osascript_missing_silent(self, task_context):
        """非 macOS 环境（osascript 不存在）静默跳过，不抛异常"""
        payload = build_payload("on_complete", task_context)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            _send_desktop(payload, 5)  # 不抛异常

    def test_osascript_timeout_silent(self, task_context):
        """osascript 超时只记 debug，不抛异常"""
        import subprocess
        payload = build_payload("on_complete", task_context)
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("osascript", 5)):
            _send_desktop(payload, 5)  # 不抛异常

    def test_quotes_escaped(self, task_context):
        """task_id 含双引号时转义，防 AppleScript 注入"""
        payload = build_payload("on_complete", task_context)
        payload["task_id"] = 'ta"sk'
        with patch("subprocess.run") as mock_run:
            _send_desktop(payload, 5)
        script = mock_run.call_args[0][0][2]
        assert 'ta\\"sk' in script


# ═══════════════════════════════════════════════════════════════
# webhook 发送异常容错
# ═══════════════════════════════════════════════════════════════

class TestWebhookFaultTolerance:
    def _urlopen_ok(self):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_header_env_missing_skips_channel(self, task_context, monkeypatch):
        """header 插值失败（环境变量未设置）→ 跳过该通道，不发请求"""
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        config = {"notify": {"channels": [
            {"type": "webhook", "url": "https://hook/a",
             "headers": {"Authorization": "Bearer ${NOPE_TOKEN}"}},
        ]}}
        with patch("urllib.request.urlopen") as mock_open:
            notify_event("on_failed", task_context, config)
        mock_open.assert_not_called()

    def test_5xx_retried_until_exhausted(self, task_context):
        """5xx 会重试，次数用尽后仅 warning 留痕，不抛异常"""
        import urllib.error
        config = {"notify": {"retry": 1, "channels": [
            {"type": "webhook", "url": "https://hook/a"},
        ]}}
        err = urllib.error.HTTPError("https://hook/a", 500, "Server Error", {}, None)
        with patch("urllib.request.urlopen", side_effect=err) as mock_open:
            notify_event("on_failed", task_context, config)
        assert mock_open.call_count == 2  # 1 + 1 retry

    def test_5xx_then_success(self, task_context):
        """5xx 后重试成功 → 正常返回，不再重试"""
        import urllib.error
        config = {"notify": {"retry": 2, "channels": [
            {"type": "webhook", "url": "https://hook/a"},
        ]}}
        err = urllib.error.HTTPError("https://hook/a", 503, "Unavailable", {}, None)
        with patch("urllib.request.urlopen",
                   side_effect=[err, self._urlopen_ok()]) as mock_open:
            notify_event("on_failed", task_context, config)
        assert mock_open.call_count == 2


# ═══════════════════════════════════════════════════════════════
# command 通道执行 / 模板安全约定
# ═══════════════════════════════════════════════════════════════

class TestSendCommand:
    def test_failure_reason_not_exposed(self, task_context):
        """安全约定：模板变量不含 failure_reason（LLM 输出不可信，防 shell 注入）"""
        config = {"notify": {"channels": [
            {"type": "command", "command": "echo {failure_reason}"},
        ]}}
        with patch("subprocess.run") as mock_run:
            notify_event("on_failed", task_context, config)
        mock_run.assert_not_called()

    def test_unknown_template_var_skips(self, task_context):
        """未知模板变量 → warning 并跳过，不执行命令"""
        payload = build_payload("on_failed", task_context)
        with patch("subprocess.run") as mock_run:
            _send_command(payload, {"type": "command", "command": "echo {nope}"}, 5)
        mock_run.assert_not_called()

    def test_command_env_missing_skips(self, task_context, monkeypatch):
        """命令含未设置的环境变量 → 跳过通道"""
        monkeypatch.delenv("UNSET_NOTIFY_CMD", raising=False)
        payload = build_payload("on_failed", task_context)
        with patch("subprocess.run") as mock_run:
            _send_command(payload, {"type": "command",
                                    "command": "${UNSET_NOTIFY_CMD} {task_id}"}, 5)
        mock_run.assert_not_called()

    def test_subprocess_failure_tolerated(self, task_context):
        """命令执行失败只记 debug，不向上传播"""
        payload = build_payload("on_failed", task_context)
        with patch("subprocess.run", side_effect=OSError("boom")):
            _send_command(payload, {"type": "command", "command": "echo {task_id}"}, 5)

    def test_message_and_cost_vars(self, task_context):
        """{message} 摘要行与 {cost_usd} 等安全标量可用于模板"""
        config = {"notify": {"channels": [
            {"type": "command", "command": "echo {cost_usd}"},
        ]}}
        with patch("subprocess.run") as mock_run:
            notify_event("on_failed", task_context, config)
        assert mock_run.call_args[0][0] == ["echo", "0.08"]
