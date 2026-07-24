"""角色感知模型路由 — 按 Agent 角色选择不同成本/能力的模型。

核心概念：
- 角色（role）：planner / worker / reviewer，由 agent_type 映射而来
- 路由（route）：一个角色的 primary provider + 可选 fallback provider
- 熔断（circuit_breaker）：provider 连续失败后自动切断，冷却后恢复
- 计量日志（metering）：每次 API 调用的结构化成本记录

用法：
    from .router import resolve_provider, call_with_role

    route = resolve_provider("architect", config)
    if route:
        content, metering = call_with_role(route, messages, api_key, logger)
        log_event(logger, "api_call", metering)
    else:
        content = call_api(config, messages, logger)  # 回退到现有逻辑
"""

import json
import time
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional, Any

from .metrics import estimate_cost
from .config import meter_event

__all__ = [
    "ProviderConfig", "RoleRoute", "CircuitBreaker",
    "resolve_provider", "call_with_role",
]

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProviderConfig:
    """单个 LLM provider 的配置。"""
    provider: str               # "anthropic" | "openai" | "deepseek" | "custom"
    base_url: str
    model: str
    max_tokens: int = 4096
    temperature: float = 0.2
    max_concurrency: int = 4
    timeout_ms: int = 120000

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        return cls(
            provider=data.get("provider", "custom"),
            base_url=data.get("base_url", ""),
            model=data.get("model", ""),
            max_tokens=data.get("max_tokens", 4096),
            temperature=data.get("temperature", 0.2),
            max_concurrency=data.get("max_concurrency", 4),
            timeout_ms=data.get("timeout_ms", 120000),
        )


@dataclass
class RoleRoute:
    """一个角色的路由配置：primary provider + 可选 fallback。"""
    role: str
    primary: ProviderConfig
    fallback: Optional[ProviderConfig] = None  # None = 不降级


# ═══════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════

class CircuitBreaker:
    """熔断器状态机：正常 → 熔断 → 半开 → 正常。

    可用性失败（超时/连接/429）触发熔断计数。
    连续 failure_threshold 次失败后进入熔断状态。
    cooldown_seconds 后进入半开状态，允许 half_open_requests 个试探请求。
    试探成功 → 恢复正常；失败 → 重新熔断。
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 60, half_open_requests: int = 2):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_requests = half_open_requests

        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"          # "closed" | "open" | "half_open"
        self._half_open_count: int = 0

    def allow_request(self) -> bool:
        """当前是否允许请求通过。"""
        now = time.time()

        if self._state == "closed":
            return True

        if self._state == "open":
            if now - self._last_failure_time >= self.cooldown_seconds:
                self._state = "half_open"
                self._half_open_count = 0
            else:
                return False

        if self._state == "half_open":
            if self._half_open_count < self.half_open_requests:
                self._half_open_count += 1
                return True
            return False

        return True

    def record_success(self) -> None:
        """记录一次成功请求。"""
        self._failure_count = 0
        if self._state == "half_open":
            self._state = "closed"
            self._half_open_count = 0

    def record_failure(self) -> None:
        """记录一次可用性失败。"""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == "half_open":
            # 半开状态下的失败：立即重新熔断
            self._state = "open"
        elif self._failure_count >= self.failure_threshold:
            self._state = "open"

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count


# ═══════════════════════════════════════════════════════════════
# Provider Resolution
# ═══════════════════════════════════════════════════════════════

def resolve_provider(agent_type: str, config: dict) -> Optional[RoleRoute]:
    """根据 agent_type 解析路由配置。

    Args:
        agent_type: "developer" | "architect" | "reviewer" | "tester"
        config: 完整配置字典（含 router 块）

    Returns:
        RoleRoute 如果 router.enabled=true 且 agent_type 可映射到配置的角色
        None 如果 router.enabled=false（调用方应回退到现有 plan_api 路径）
    """
    router_cfg = config.get("router", {})
    if not router_cfg.get("enabled", False):
        return None

    # 映射 agent_type → role
    mapping = router_cfg.get("agent_type_mapping", {})
    role = mapping.get(agent_type, "worker")

    # 查找角色配置
    roles = router_cfg.get("roles", {})
    role_cfg = roles.get(role)
    if not role_cfg:
        return None

    # 构建 primary provider
    primary = ProviderConfig.from_dict(role_cfg)

    # 构建 fallback provider（如有）
    fallback = None
    fallback_cfg = role_cfg.get("fallback")
    if fallback_cfg:
        fallback = ProviderConfig.from_dict(fallback_cfg)

    return RoleRoute(role=role, primary=primary, fallback=fallback)


# ═══════════════════════════════════════════════════════════════
# API Call with Routing
# ═══════════════════════════════════════════════════════════════

# 全局熔断器实例（按 provider key 索引）
_circuit_breakers: dict[str, CircuitBreaker] = {}


def _get_circuit_breaker(provider_key: str, config: dict) -> CircuitBreaker:
    """获取或创建指定 provider 的熔断器。"""
    if provider_key not in _circuit_breakers:
        cb_cfg = config.get("router", {}).get("circuit_breaker", {})
        _circuit_breakers[provider_key] = CircuitBreaker(
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            cooldown_seconds=cb_cfg.get("cooldown_seconds", 60),
            half_open_requests=cb_cfg.get("half_open_requests", 2),
        )
    return _circuit_breakers[provider_key]


def _provider_key(pc: ProviderConfig) -> str:
    """生成 provider 的唯一标识。"""
    return f"{pc.provider}:{pc.model}"


def _call_api_internal(
    pc: ProviderConfig,
    messages: list[dict],
    api_key: str,
) -> tuple[str, int, int]:
    """底层 API 调用（不处理路由/降级/熔断）。

    Returns:
        (content, prompt_tokens, completion_tokens)
    """
    provider = pc.provider
    base_url = pc.base_url
    model = pc.model

    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider == "anthropic":
        payload = {
            "model": model,
            "max_tokens": pc.max_tokens,
            "temperature": pc.temperature,
            "messages": messages,
        }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": pc.max_tokens,
            "temperature": pc.temperature,
        }

    timeout_sec = pc.timeout_ms / 1000
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read())

    if provider == "anthropic":
        content = data["content"][0]["text"]
    else:
        content = data["choices"][0]["message"]["content"]

    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens", 0)

    return content, prompt_tokens, completion_tokens


def call_with_role(
    route: RoleRoute,
    messages: list[dict],
    api_key: str,
    logger: logging.Logger,
    task_id: str = "",
    subtask_id: str = "",
    metering_path: Any = None,
) -> tuple[str, dict]:
    """按角色路由调用 LLM API。

    路由决策流程：
    1. 尝试 primary provider（检查熔断器状态）
    2. 可用性失败 → 降级到 fallback（如有）
    3. 质量性失败 → 原 provider 重试 1 次 → 仍失败则升级到 fallback

    Args:
        route: 角色路由配置
        messages: LLM 消息列表
        api_key: API 密钥
        logger: 日志记录器
        task_id: 任务 ID
        subtask_id: 子任务 ID

    Returns:
        (content, metering_info)
    """
    start = time.time()
    result = "success"
    fallback_reason = ""
    actual_provider = ""
    actual_model = ""
    prompt_tokens = 0
    completion_tokens = 0
    _quality_fail = False  # 标记是否为质量性失败（用于重试判断）

    def _try_provider(pc: ProviderConfig, is_fallback: bool = False) -> Optional[str]:
        """尝试调用一个 provider，返回内容或 None（失败时）。"""
        nonlocal result, fallback_reason, actual_provider, actual_model, prompt_tokens, completion_tokens, _quality_fail

        cb = _get_circuit_breaker(_provider_key(pc), {})

        if not cb.allow_request():
            if is_fallback:
                return None
            logger.warning(f"[Router] Provider {_provider_key(pc)} 熔断中，跳过")
            return None

        actual_provider = pc.provider
        actual_model = pc.model
        _quality_fail = False

        try:
            content, pt, ct = _call_api_internal(pc, messages, api_key)
            prompt_tokens = pt
            completion_tokens = ct
            cb.record_success()
            if is_fallback:
                result = "fallback"
                fallback_reason = "primary_unavailable"
            return content

        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as e:
            cb.record_failure()
            _quality_fail = False
            if is_fallback:
                fallback_reason = f"fallback_failed: {_error_summary(e)}"
                raise RuntimeError(f"路由调用失败（primary + fallback 均不可用）: {e}") from e
            logger.warning(f"[Router] Primary provider {_provider_key(pc)} 失败: {_error_summary(e)}")
            return None

        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            _quality_fail = True
            if is_fallback:
                fallback_reason = f"fallback_quality_fail: {_error_summary(e)}"
                raise RuntimeError(f"路由调用失败（primary + fallback 均质量失败）: {e}") from e
            logger.warning(f"[Router] Primary provider {_provider_key(pc)} 质量失败: {_error_summary(e)}")
            return None

    def _error_summary(e: Exception) -> str:
        msg = str(e)[:100]
        if isinstance(e, urllib.error.HTTPError):
            return f"HTTP {e.code}"
        return msg

    # 1. 尝试 primary
    content = _try_provider(route.primary)
    if content is not None:
        latency_ms = round((time.time() - start) * 1000, 2)
        cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
        metering = _build_metering(route.role, actual_provider, actual_model,
                                         prompt_tokens, completion_tokens, cost,
                                         latency_ms, result, fallback_reason,
                                         task_id, subtask_id)
        meter_event(metering_path, metering)
        return content, metering

    # 2. 质量失败：primary 重试 1 次（仅质量失败，可用性失败直接降级）
    if _quality_fail:
        logger.info(f"[Router] Primary 质量失败，重试 1 次")
        content = _try_provider(route.primary)
        if content is not None:
            latency_ms = round((time.time() - start) * 1000, 2)
            cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
            metering = _build_metering(route.role, actual_provider, actual_model,
                                             prompt_tokens, completion_tokens, cost,
                                             latency_ms, result, fallback_reason,
                                             task_id, subtask_id)
            meter_event(metering_path, metering)
            return content, metering

    # 3. Fallback
    if route.fallback is not None:
        logger.info(f"[Router] 降级到 fallback: {_provider_key(route.fallback)}")
        content = _try_provider(route.fallback, is_fallback=True)
        if content is not None:
            latency_ms = round((time.time() - start) * 1000, 2)
            cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
            metering = _build_metering(route.role, actual_provider, actual_model,
                                             prompt_tokens, completion_tokens, cost,
                                             latency_ms, result, fallback_reason,
                                             task_id, subtask_id)
            meter_event(metering_path, metering)
            return content, metering

    raise RuntimeError(
        f"路由调用失败：primary {_provider_key(route.primary)} 不可用，"
        f"fallback {'不可用' if route.fallback is None else '也失败'}"
    )


def _build_metering(
    role: str,
    actual_provider: str,
    actual_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    latency_ms: float,
    result: str,
    fallback_reason: str,
    task_id: str,
    subtask_id: str,
) -> dict:
    """构建结构化计量信息。"""
    metering: dict[str, Any] = {
        "role": role,
        "virtual_model": f"agentgo-{role}",
        "actual_provider": actual_provider,
        "actual_model": actual_model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "result": result,
        "fallback_reason": fallback_reason,
    }
    if task_id:
        metering["task_id"] = task_id
    if subtask_id:
        metering["subtask_id"] = subtask_id
    return metering