"""llama-defender 诊断数据面客户端（R13-R16 消费侧，C1-C7 公共层）。

代理侧契约见 docs/design/llama-defender-integration-requirements.md §3.2：
- 会话 key：请求头 X-Claude-Code-Session-Id，代理内部截断前 8 字符作为台账/档案 join 键；
  无头时回退 md5(ip:ua:date) 按天合并（批跑必须显式发头，否则台账污染）。
- 只读端点：/api/sessions、/api/session/<key8>/{ledger,archive,metrics}、
  /api/status（ctx_config 段）、/api/backend/{props,slots}（不支持时 501 + {"supported": false}）。

本模块全部 fail-open：代理不可达 / 端点缺失 / 字段缺失一律返回 None，调用方跳过即可，
绝不阻断主流程（集成契约 §4 降级原则）。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from typing import Any

# 本机回环判定（与 api.py call_api 的 _is_local_url 口径一致）
LOCAL_URL_RE = re.compile(r"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])")

# 契约版本（X-2 契约单点化）：本模块实现对齐 llama-defender 集成契约 api_version="2"
# （llama.cpp/docs/llama-defender-integration-requirements.md §3.2，该文档为唯一权威）。
# 契约升级时同步本常量 + tools/check_llama_defender_contract.py F 组用例。
CONTRACT_API_VERSION = "2"

# 会话头名（契约 api_version=2 §3.2；构造/截断口径以契约文档为准，禁止本地另起口径）
SESSION_HEADER = "X-Claude-Code-Session-Id"

# 代理侧 key 截断长度（契约 api_version=2：anthropic_proxy session key = header[:8]）
PROXY_KEY_LEN = 8

_KEY_SAFE_RE = re.compile(r"[^a-zA-Z0-9\-_]")


def session_key(task_id: str, sub_id: str = "") -> str:
    """构造会话 key：8 位哈希前缀 + 可读后缀。

    代理内部只保留前 8 字符，因此前 8 位必须可区分会话——用 md5(task:sub)[:8]
    做前缀，后缀仅供人读（会随截断丢弃）。长度 ≤ 64，仅含 [a-zA-Z0-9-_]。
    """
    base = f"{task_id}:{sub_id}" if sub_id else str(task_id)
    prefix = hashlib.md5(base.encode("utf-8")).hexdigest()[:PROXY_KEY_LEN]
    readable = _KEY_SAFE_RE.sub("-", f"ag-{task_id}-{sub_id}".strip("-"))[:54]
    return f"{prefix}-{readable}" if readable else prefix


def session_key8(full_key: str) -> str:
    """代理侧实际使用的截断 key（ledger/archive/metrics 的 <key> 参数）。"""
    return (full_key or "")[:PROXY_KEY_LEN]


def local_proxy_base_url(config: dict[str, Any]) -> str:
    """从配置解析本地代理 base_url，无则返回 ""。

    优先级（A-1 收敛）：plan_api.worker_base_url（统一入口）→ worker_backends
    （deprecated 兼容）→ plan_api.base_url。仅匹配本机回环地址。
    """
    plan_api = config.get("plan_api", {}) or {}
    v = plan_api.get("worker_base_url") or ""
    if isinstance(v, str) and LOCAL_URL_RE.search(v):
        return v.rstrip("/")
    backends = config.get("worker_backends", {}) or {}
    for v in backends.values():
        if isinstance(v, str) and LOCAL_URL_RE.search(v):
            return v.rstrip("/")
    v = plan_api.get("base_url") or ""
    if isinstance(v, str) and LOCAL_URL_RE.search(v):
        return v.rstrip("/")
    return ""


def fetch_json(base_url: str, path: str, timeout: float = 3.0) -> Any | None:
    """GET JSON，fail-open。

    网络错误/超时/坏 JSON → None；HTTP 非 2xx 但 body 是合法 JSON（如 501 的
    {"supported": false}、404 的 {"error": ...}）→ 返回解析后的 body，由调用方
    按字段判断降级语义。
    """
    if not base_url:
        return None
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"User-Agent": "agent_go-diag/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return None
    except Exception:
        return None


def get_sessions(base_url: str, timeout: float = 3.0) -> list[dict[str, Any]]:
    """GET /api/sessions → 会话列表（无则 []）。"""
    data = fetch_json(base_url, "/api/sessions", timeout)
    if isinstance(data, dict) and isinstance(data.get("sessions"), list):
        return data["sessions"]
    return []


def get_session_ledger(base_url: str, key8: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET /api/session/<key8>/ledger；未知 key/未启用 → None。"""
    if not key8:
        return None
    data = fetch_json(base_url, f"/api/session/{key8}/ledger", timeout)
    return data if isinstance(data, dict) and "turns_seen" in data else None


def get_session_metrics(base_url: str, key8: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET /api/session/<key8>/metrics；未知 key/未启用 → None。"""
    if not key8:
        return None
    data = fetch_json(base_url, f"/api/session/{key8}/metrics", timeout)
    return data if isinstance(data, dict) and "turns" in data else None


def get_archive_index(base_url: str, key8: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET /api/session/<key8>/archive?view=sent（索引模式，不含 payload）。"""
    if not key8:
        return None
    data = fetch_json(base_url, f"/api/session/{key8}/archive?view=sent", timeout)
    return data if isinstance(data, dict) and "turns" in data else None


def get_ctx_config(base_url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET /api/status → {"ctx_config": ..., "route_config": ...}；缺失段为 None 值。"""
    data = fetch_json(base_url, "/api/status", timeout)
    if not isinstance(data, dict) or "state" not in data:
        return None
    return {
        "ctx_config": data.get("ctx_config"),
        "route_config": data.get("route_config"),
    }


def get_backend_props(base_url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET /api/backend/props；501 时返回 {"supported": false, ...}（结构化降级）。"""
    data = fetch_json(base_url, "/api/backend/props", timeout)
    return data if isinstance(data, dict) else None
