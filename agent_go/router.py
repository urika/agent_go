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
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Any, Callable

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
    # 角色绑定的独立 API key（② 角色配置；空 = 用全局 plan_api.api_key / key_ref 解析）
    api_key: str = ""
    key_ref: str = ""
    # ② 场景绑定覆盖（三层设计 P1）：推理 thinking 开关/预算（覆盖 ① registry 默认值）。
    # None = 未覆盖（用 ① ModelEntity.reasoning.thinking 的声明式默认）。
    thinking: Optional[bool] = None
    thinking_budget: Optional[int] = None
    json_output: Optional[bool] = None
    reasoning_effort: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        model = data.get("model", "")
        entity = None
        key_resolver: Optional[Callable[[str], str]] = None
        try:
            from .models_registry import get_model, resolve_key
            key_resolver = resolve_key
            entity = get_model(model) if model else None
        except Exception:
            pass

        provider = data.get("provider") or (entity.provider if entity else "custom")
        base_url = data.get("base_url") or (entity.base_url if entity else "")
        key_ref = data.get("key_ref") or (entity.key_ref if entity else "")
        api_key = data.get("api_key", "") or ""
        if not api_key and key_ref and key_resolver is not None:
            api_key = key_resolver(key_ref)

        # Explicit role values override registry defaults.  Missing values inherit
        # model-intrinsic reasoning/output capabilities from Model Registry.
        thinking = data.get("thinking") if "thinking" in data else None
        thinking_budget = data.get("thinking_budget") if "thinking_budget" in data else None
        json_output = data.get("json_output") if "json_output" in data else None
        if entity is not None:
            if thinking is None and entity.thinking.required:
                thinking = True
            if thinking_budget is None and entity.thinking.required:
                thinking_budget = entity.thinking.budget_tokens
            if json_output is None:
                json_output = entity.output.needs_response_format or entity.output.json_compliance == "strict"

        return cls(
            provider=provider,
            base_url=base_url,
            model=model,
            max_tokens=data.get("max_tokens", 4096),
            temperature=data.get("temperature", 0.2),
            max_concurrency=data.get("max_concurrency", 4),
            timeout_ms=data.get("timeout_ms", 120000),
            api_key=api_key,
            key_ref=key_ref,
            thinking=thinking,
            thinking_budget=thinking_budget,
            json_output=json_output,
            reasoning_effort=data.get("reasoning_effort", ""),
        )


@dataclass
class RoleRoute:
    """一个角色的路由配置：primary provider + 可选 fallback。"""
    role: str
    primary: ProviderConfig
    fallback: Optional[ProviderConfig] = None  # None = 不降级
    fallbacks: tuple[ProviderConfig, ...] = ()  # P0 多级降级链（兼容旧 fallback）

    def providers(self) -> list[ProviderConfig]:
        """Return primary followed by unique fallback providers in order."""
        candidates = [self.primary]
        if self.fallback is not None:
            candidates.append(self.fallback)
        candidates.extend(self.fallbacks)
        result: list[ProviderConfig] = []
        seen: set[tuple[str, str, str]] = set()
        for provider in candidates:
            key = (provider.provider, provider.model, provider.base_url)
            if key not in seen:
                seen.add(key)
                result.append(provider)
        return result


# ═══════════════════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════════════════

class CircuitBreaker:
    """熔断器状态机：正常 → 熔断 → 半开 → 正常。

    可用性失败（超时/连接/429）触发熔断计数。
    连续 failure_threshold 次失败后进入熔断状态。
    cooldown_seconds 后进入半开状态，允许 half_open_requests 个试探请求。
    试探成功 → 恢复正常；失败 → 重新熔断。

    线程安全：所有公共方法使用 threading.Lock 保护。
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 60, half_open_requests: int = 2):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_requests = half_open_requests

        self._lock = threading.Lock()
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"          # "closed" | "open" | "half_open"
        self._half_open_count: int = 0

    def allow_request(self) -> bool:
        """当前是否允许请求通过。"""
        with self._lock:
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
        with self._lock:
            self._failure_count = 0
            if self._state == "half_open":
                self._state = "closed"
                self._half_open_count = 0

    def record_failure(self) -> None:
        """记录一次可用性失败。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == "half_open":
                self._state = "open"
            elif self._failure_count >= self.failure_threshold:
                self._state = "open"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
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

    return _build_role_route(role, role_cfg)


def resolve_role(role: str, config: dict) -> Optional[RoleRoute]:
    """按角色名直接解析路由（三层设计 P1：补齐 evaluator 等非 agent_type 角色）。

    与 resolve_provider（agent_type→role 映射）互补：call_api/evaluator 等
    非 agent_type 驱动的调用方按角色名（planner/evaluator/worker/reviewer）解析。

    router.enabled=false 或角色未配置 → None（调用方 fallback 到现有配置路径）。
    """
    router_cfg = config.get("router", {})
    if not router_cfg.get("enabled", False):
        return None
    roles = router_cfg.get("roles", {})
    role_cfg = roles.get(role)
    if not role_cfg:
        return None
    return _build_role_route(role, role_cfg)


def _build_role_route(role: str, role_cfg: dict) -> RoleRoute:
    """Build a route from legacy single fallback or P0 ``fallbacks`` list."""
    primary = ProviderConfig.from_dict(role_cfg)
    configs: list[ProviderConfig] = []
    fallback_cfg = role_cfg.get("fallback")
    if isinstance(fallback_cfg, dict):
        configs.append(ProviderConfig.from_dict(fallback_cfg))
    fallback_list = role_cfg.get("fallbacks", [])
    if isinstance(fallback_list, list):
        configs.extend(ProviderConfig.from_dict(item) for item in fallback_list if isinstance(item, dict))
    return RoleRoute(
        role=role,
        primary=primary,
        fallback=configs[0] if configs else None,
        fallbacks=tuple(configs),
    )


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
        if pc.thinking:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": pc.thinking_budget or 8192,
            }
    else:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": pc.max_tokens,
            "temperature": pc.temperature,
        }
        if pc.thinking:
            payload["thinking"] = {"type": "enabled"}
        if pc.reasoning_effort:
            payload["reasoning_effort"] = pc.reasoning_effort
        if pc.json_output:
            payload["response_format"] = {"type": "json_object"}

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
        _blocks = data.get("content", [])
        _text_block = next((b for b in _blocks if isinstance(b, dict) and b.get("type") == "text"), None)
        content = _text_block["text"] if _text_block else _blocks[0].get("text", str(_blocks[0]))
    else:
        content = data["choices"][0]["message"]["content"]

    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{provider}:{model} returned empty content")
    if pc.json_output:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{provider}:{model} returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{provider}:{model} returned non-object JSON")

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
    config: Optional[dict] = None,
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
    _policy_violation = ""  # 铁律违规标记（空字符串=无违规）

    # Planner 铁律检查：Planner 不允许配置 fallback 降级
    if route.role == "planner" and route.fallback is not None:
        logger.warning(
            f"[Router] 政策违规: Planner 角色配置了 fallback 降级 "
            f"({_provider_key(route.fallback)})。PRD 铁律禁止 Planner 降级。"
        )
        _policy_violation = "planner_fallback_configured"

    def _try_provider(pc: ProviderConfig, is_fallback: bool = False) -> Optional[str]:
        """尝试调用一个 provider，返回内容或 None（失败时）。"""
        nonlocal result, fallback_reason, actual_provider, actual_model, prompt_tokens, completion_tokens, _quality_fail

        cb = _get_circuit_breaker(_provider_key(pc), config or {})

        if not cb.allow_request():
            if is_fallback:
                return None
            logger.warning(f"[Router] Provider {_provider_key(pc)} 熔断中，跳过")
            return None

        actual_provider = pc.provider
        actual_model = pc.model
        _quality_fail = False

        provider_key = pc.api_key or api_key
        try:
            content, pt, ct = _call_api_internal(pc, messages, provider_key)
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

        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as e:
            _quality_fail = True
            if is_fallback:
                fallback_reason = f"fallback_quality_fail: {_error_summary(e)}"
                logger.warning(f"[Router] Fallback provider {_provider_key(pc)} 质量失败: {_error_summary(e)}")
            else:
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
                                         task_id, subtask_id, _policy_violation)
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
                                             task_id, subtask_id, _policy_violation)
            meter_event(metering_path, metering)
            return content, metering

    # 3. Fallback chain（P0：按配置顺序 K3 → GLM → v4-pro → local）
    fallback_candidates = route.providers()[1:]
    for fallback_index, fallback_provider in enumerate(fallback_candidates, start=1):
        logger.info(f"[Router] 降级到 fallback[{fallback_index}]: {_provider_key(fallback_provider)}")
        content = _try_provider(fallback_provider, is_fallback=True)
        if content is not None:
            latency_ms = round((time.time() - start) * 1000, 2)
            cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
            metering = _build_metering(route.role, actual_provider, actual_model,
                                             prompt_tokens, completion_tokens, cost,
                                             latency_ms, result, fallback_reason,
                                             task_id, subtask_id, _policy_violation)
            meter_event(metering_path, metering)
            return content, metering

    # 全失败：写入 metering 后 raise（保证审计可见性）
    _final_result = "failed"
    _final_fallback = (
        "primary_unavailable:fallback_chain_exhausted"
        if fallback_candidates else "primary_unavailable:fallback_not_configured"
    )
    _final_latency = round((time.time() - start) * 1000, 2)
    _final_model = actual_model or route.primary.model or "unknown"
    meter_event(metering_path, _build_metering(
        route.role, actual_provider or route.primary.provider,
        _final_model, prompt_tokens, completion_tokens,
        0.0, _final_latency, _final_result, _final_fallback,
        task_id, subtask_id, _policy_violation,
    ))
    failure_label = "均质量失败" if _quality_fail else "链路均失败"
    raise RuntimeError(
        f"路由调用失败：primary {_provider_key(route.primary)} 不可用，"
        f"fallback {'不可用' if not fallback_candidates else failure_label}"
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
    policy_violation: str = "",
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
    if policy_violation:
        metering["policy_violation"] = policy_violation
    return metering
