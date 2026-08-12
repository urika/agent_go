"""Profile 管理：云端 ⇄ 本地配置一键切换（Web 操作台 M1 / R1-R4）。

存储布局：
  ~/.agent_go/profiles/<name>.json   profile 配置文件（load_config 读取）
  ~/.agent_go/profiles/backup-<ts>.json  切换前自动备份
  ~/.agent_go/.current_profile       当前激活 profile 名（不存在/空 = 默认 config.json）

生效优先级（load_config）：--config > AGENT_GO_PROFILE env > .current_profile > config.json

本地 profile 语义：plan/planner/worker/evaluator 全部指向本地 OpenAI 兼容代理，
api_key 置空（本地免 key 已支持），goal force 补偿本地模型质量。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .config import AGENT_GO_DIR, CONFIG_PATH, load_config

LOCAL_PROFILE_NAME = "local"
DEFAULT_LOCAL_URL = "http://localhost:4000"
PROBE_TIMEOUT = 2.5
# 本地模型 TCO 默认估值（$/次），用户可在生成后自行调整
DEFAULT_LOCAL_COST = 0.0005
# worker 路由名固定家族（claude CLI --model 接受的名字，由 worker_backends 映射到本地代理）
ROUTE_MODELS = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]


class ProfileError(Exception):
    """profile 操作失败（消息面向用户可读）。"""


# ── 路径与状态 ───────────────────────────────────────────────

def profiles_dir() -> Path:
    d = AGENT_GO_DIR / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_profile_file() -> Path:
    return AGENT_GO_DIR / ".current_profile"


def read_current_profile() -> str:
    """当前激活 profile 名；无（默认 config.json）返回空串。"""
    f = current_profile_file()
    if not f.exists():
        return ""
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def profile_path(name: str) -> Path:
    return profiles_dir() / f"{name}.json"


def active_config_source() -> Path:
    """当前生效配置的原始文件（用于备份）。"""
    cur = read_current_profile()
    if cur and profile_path(cur).exists():
        return profile_path(cur)
    return CONFIG_PATH


# ── 本地代理探测 ─────────────────────────────────────────────

def normalize_local_url(url: str) -> str:
    """规范化为代理根地址：去掉尾部 /、/v1、/v1/chat/completions 等后缀。"""
    u = url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1/models", "/v1"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u


def _http_get_json(url: str, headers: Optional[dict] = None,
                   timeout: float = PROBE_TIMEOUT) -> tuple[int, Any]:
    """GET JSON，返回 (status_code, payload)。网络异常抛 ProfileError。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError) as e:
        raise ProfileError(f"无法连接 {url}: {e}") from e


def probe_local_models(local_url: str) -> list[str]:
    """探测本地代理可用模型列表（GET {base}/v1/models）。不可达抛 ProfileError。"""
    base = normalize_local_url(local_url)
    code, payload = _http_get_json(f"{base}/v1/models")
    if code != 200 or not isinstance(payload, dict):
        raise ProfileError(f"本地代理响应异常 ({base}/v1/models → HTTP {code})")
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")]


# ── 本地 profile 模板 ────────────────────────────────────────

def generate_local_profile(local_url: str, real_model: str = "") -> dict[str, Any]:
    """生成纯本地 profile 配置（v2 设计 §5.3）。

    real_model：探测到的代理真实模型名，用于 local_model_cost TCO 估算。
    """
    base = normalize_local_url(local_url)
    chat_url = f"{base}/v1/chat/completions"
    profile: dict[str, Any] = {
        "plan_api": {
            "provider": "openai",
            "base_url": chat_url,
            "model": "claude-sonnet-4-6",
            "api_key": "",
            "worker_base_url": base,
            "local_models": list(ROUTE_MODELS),
        },
        "planner_api": {
            "provider": "openai",
            "base_url": chat_url,
            "model": "claude-sonnet-4-6",
        },
        "worker_models": {
            "easy": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "hard": "claude-opus-4-7",
        },
        "worker_backends": {m: base for m in ROUTE_MODELS},
        "evaluator": {
            "enabled": True,
            "provider": "openai",
            "base_url": chat_url,
            "model": "claude-sonnet-4-6",
        },
        "goal": {"enabled": True, "policy": "force", "max_turns": 50, "timeout_seconds": 3600},
        "fallback": {
            "local_model_url": chat_url,
            "local_model_name": "claude-sonnet-4-6",
            "enable_rules": True,
        },
    }
    if real_model:
        profile["local_model_cost"] = {real_model: DEFAULT_LOCAL_COST}
    return profile


# ── 激活 / 恢复 ─────────────────────────────────────────────

def _backup_current() -> Path:
    """备份当前生效配置（merged 结果，恢复可直接使用）到 profiles/backup-<ts>.json。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = profiles_dir() / f"backup-{ts}.json"
    config = load_config()
    # 备份剥离运行时注入的私有键（_parallel 等）
    config = {k: v for k, v in config.items() if not k.startswith("_")}
    dest.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def activate_local(local_url: str = DEFAULT_LOCAL_URL) -> dict[str, Any]:
    """一键生成并激活纯本地 profile（R1）。

    流程：探测代理 /v1/models（不可达即中止）→ 备份当前配置 →
          写 profiles/local.json → 写 .current_profile。
    """
    base = normalize_local_url(local_url)
    try:
        models = probe_local_models(base)
    except ProfileError as e:
        raise ProfileError(
            f"本地代理不可达，已中止（未修改任何配置）。请先启动本地代理后重试。\n  原因: {e}"
        ) from e
    real_model = models[0] if models else ""
    backup = _backup_current()
    profile = generate_local_profile(base, real_model)
    path = profile_path(LOCAL_PROFILE_NAME)
    path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    current_profile_file().write_text(LOCAL_PROFILE_NAME, encoding="utf-8")
    return {
        "profile": LOCAL_PROFILE_NAME,
        "profile_path": str(path),
        "backup_path": str(backup),
        "local_url": base,
        "real_model": real_model,
        "models": models,
    }


def activate_cloud() -> dict[str, Any]:
    """恢复云端配置（R2）：备份当前 → 清除 .current_profile（回退 config.json）。"""
    backup = _backup_current()
    cur = read_current_profile()
    f = current_profile_file()
    if f.exists():
        f.unlink()
    return {
        "profile": "",
        "backup_path": str(backup),
        "previous_profile": cur,
    }


def activate_profile(name: str) -> dict[str, Any]:
    """激活已存在的 profile（profiles/<name>.json）。"""
    path = profile_path(name)
    if not path.exists():
        raise ProfileError(f"Profile 不存在: {path}")
    backup = _backup_current()
    current_profile_file().write_text(name, encoding="utf-8")
    return {"profile": name, "profile_path": str(path), "backup_path": str(backup)}


def list_profiles() -> dict[str, Any]:
    """列出全部 profile + 当前生效（R3 配置中心数据源）。"""
    current = read_current_profile()
    d = profiles_dir()
    items = []
    for p in sorted(d.glob("*.json")):
        name = p.stem
        items.append({
            "name": name,
            "path": str(p),
            "active": name == current,
            "is_backup": name.startswith("backup-"),
            "mode": _profile_mode(p),
        })
    return {
        "current": current,
        "mode": "local" if current == LOCAL_PROFILE_NAME else ("custom" if current else "cloud"),
        "profiles": items,
    }


def _profile_mode(path: Path) -> str:
    """粗判 profile 模式：worker_backends 全指向本机 → local，否则 cloud/custom。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "unknown"
    backends = data.get("worker_backends") or {}
    if backends and all(
        isinstance(v, str) and ("localhost" in v or "127.0.0.1" in v)
        for v in backends.values()
    ):
        return "local"
    return "cloud"


# ── 健康检查（R4）────────────────────────────────────────────

def _models_url(base_url: str) -> str:
    """从 chat/messages 端点推导 /models 探测地址；根地址补 /v1/models。"""
    u = base_url.rstrip("/")
    for suffix in ("/chat/completions", "/messages"):
        if u.endswith(suffix):
            return u[: -len(suffix)] + "/models"
    if not u.endswith("/v1"):
        return u + "/v1/models"
    return u + "/models"


def probe_endpoint(base_url: str, api_key: str = "") -> dict[str, Any]:
    """探测单个模型端点可达性 + 首个模型名（R4 面板数据源）。"""
    if not base_url:
        return {"ok": False, "error": "未配置 base_url"}
    headers: dict[str, str] = {}
    if api_key:
        if "anthropic" in base_url:
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    url = _models_url(base_url)
    started = time.monotonic()
    try:
        code, payload = _http_get_json(url, headers=headers)
    except ProfileError as e:
        return {"ok": False, "url": url, "error": str(e)}
    latency = round((time.monotonic() - started) * 1000)
    # 404/401 也说明端点活着（服务在，只是路径/鉴权问题）
    ok = code < 500
    model = ""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            model = str(data[0].get("id", ""))
    result: dict[str, Any] = {"ok": ok, "url": url, "http": code, "latency_ms": latency}
    if model:
        result["model"] = model
    if not ok:
        result["error"] = f"HTTP {code}"
    return result


def health_check(config: Optional[dict] = None) -> dict[str, Any]:
    """plan / worker / evaluator / 本地代理 四端点健康检查 + mismatch 检测（R4）。

    mismatch：本地代理探测到的真实模型名既不在 local_models（路由名集合）也不在
    local_model_cost keys（真实模型集合）→ 配置可能过期，建议重新生成 profile（D6）。
    """
    cfg = config if config is not None else load_config()
    plan_api = cfg.get("plan_api", {}) or {}
    planner_api = cfg.get("planner_api", {}) or {}
    evaluator = cfg.get("evaluator", {}) or {}
    backends = cfg.get("worker_backends", {}) or {}
    local_models = set(cfg.get("local_models") or plan_api.get("local_models") or [])
    local_cost_models = set((cfg.get("local_model_cost") or {}).keys())

    plan_url = (planner_api.get("base_url") or plan_api.get("base_url") or "")
    plan_key = (planner_api.get("api_key") or plan_api.get("api_key") or "")
    result: dict[str, Any] = {
        "profile": read_current_profile(),
        "plan": probe_endpoint(plan_url, plan_key),
    }

    # worker：优先第一个指向本机的 backend，否则 worker_base_url / plan base_url
    worker_url = ""
    for v in backends.values():
        if isinstance(v, str) and ("localhost" in v or "127.0.0.1" in v):
            worker_url = v
            break
    if not worker_url:
        worker_url = plan_api.get("worker_base_url") or plan_url
    result["worker"] = probe_endpoint(worker_url, plan_key)

    if evaluator.get("enabled"):
        result["evaluator"] = probe_endpoint(
            evaluator.get("base_url", ""), evaluator.get("api_key", "")
        )
    else:
        result["evaluator"] = {"ok": None, "skipped": True, "reason": "evaluator 未启用"}

    local_proxy: dict[str, Any] = {"ok": None, "skipped": True, "reason": "无本地 worker 后端"}
    mismatch = False
    if worker_url and ("localhost" in worker_url or "127.0.0.1" in worker_url):
        try:
            models = probe_local_models(worker_url)
            local_proxy = {"ok": True, "url": normalize_local_url(worker_url), "models": models}
            if models:
                local_proxy["model"] = models[0]
                mismatch = models[0] not in local_models and models[0] not in local_cost_models
        except ProfileError as e:
            local_proxy = {"ok": False, "url": normalize_local_url(worker_url), "error": str(e)}
    result["local_proxy"] = local_proxy
    result["mismatch"] = mismatch
    if mismatch:
        result["suggestion"] = (
            "本地代理模型与 profile 记录不一致，建议重新生成 local profile"
            "（agent_go config local 或配置中心「一键本地」）"
        )
    return result
