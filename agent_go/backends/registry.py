"""Backend 注册表与解析逻辑。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Type

from .base import BaseBackend


class BackendRegistry:
    """Backend 类注册表。

    通过 @BackendRegistry.register 装饰器登记具体 Backend，
    executor 按 name 分发。
    """

    _registry: dict[str, Type[BaseBackend]] = {}

    @classmethod
    def register(cls, backend_cls: Type[BaseBackend]) -> Type[BaseBackend]:
        """注册一个 Backend 类；可用作装饰器。"""
        if not backend_cls.name:
            raise ValueError(f"Backend class {backend_cls.__name__} must define 'name'")
        cls._registry[backend_cls.name] = backend_cls
        return backend_cls

    @classmethod
    def get(cls, name: str) -> Type[BaseBackend]:
        """按 name 获取 Backend 类；未找到时抛出 KeyError。"""
        try:
            return cls._registry[name]
        except KeyError as exc:
            raise KeyError(f"Unknown backend: {name!r}") from exc

    @classmethod
    def list(cls) -> list[str]:
        """返回已注册 backend 名称列表。"""
        return sorted(cls._registry.keys())

    @classmethod
    def clear(cls) -> None:
        """清空注册表 — 仅供测试使用。"""
        cls._registry.clear()


def _promo_time_active(promo: dict, now: Optional[datetime] = None) -> bool:
    """促销窗口时间判定（纯函数，now 可注入便于测试）。

    promo 字段：start/end（日期闭区间，YYYY-MM-DD）、daily_start/daily_end
    （每日时段，HH:MM，支持跨午夜如 23:00-09:00）、tz_offset（默认 +8 北京时间，
    固定偏移即可——北京时间无夏令时）。
    """
    tz = timezone(timedelta(hours=int(promo.get("tz_offset", 8))))
    now = now or datetime.now(tz)
    now = now.astimezone(tz)
    d = now.date().isoformat()
    if promo.get("start") and d < promo["start"]:
        return False
    if promo.get("end") and d > promo["end"]:
        return False
    ds = promo.get("daily_start", "00:00")
    de = promo.get("daily_end", "23:59")
    hm = now.strftime("%H:%M")
    if ds <= de:
        return ds <= hm <= de
    # 跨午夜时段（如 23:00-09:00）
    return hm >= ds or hm <= de


def _promo_backend(config: dict, headless: bool) -> str:
    """促销窗口路由：窗口内且 backend 可用时返回 promo backend 名，否则 ""。"""
    promo = (config or {}).get("backend_promo") or {}
    name = promo.get("backend", "")
    if not name:
        return ""
    if name != "claude" and not headless:
        return ""
    if not _promo_time_active(promo):
        return ""
    try:
        if not BackendRegistry.get(name).available():
            return ""
    except KeyError:
        return ""
    return name


def resolve_backend_name(
    config: dict,
    subtask: dict,
    headless: bool,
    is_simple: bool,
    agent_backend: str = "claude",
) -> str:
    """根据运行时条件解析应使用的 backend 名称。

    B1 约束：默认路径与 AgentLoop 混合策略路径行为完全保持。
    - AgentLoop 仅在配置启用、headless 模式、且子任务被判定为简单时启用。
    - B3：显式声明优先——subtask.backend 或 config.worker_backend 可指定
      backend（如 pi）；非 claude 的显式 backend 仅 headless 模式生效，
      交互模式回退 claude（pi/opencode 均为非交互 CLI）。
    - B4：声明式路由——worker_backend_by_type（按 agent_type）与
      worker_backend_by_difficulty（按 difficulty），全部默认空，不改变既有行为。
    - agent_backend 字段已预留，供后续阶段按 Agent 类型指定 backend（B4 不启用）。

    解析优先级（高→低）：
      1. subtask.backend（单子任务显式）
      2. config.worker_backend（全局显式）
      3. config.worker_backend_by_type[agent_type]
      4. config.worker_backend_by_difficulty[difficulty]
      5. config.backend_promo（促销窗口路由：时间窗内且 backend 本机可用时生效）
      6. agent_loop 自动规则（B1 既有）
      7. claude 兜底
    以上 1-5 解析出非 claude 时均需 headless，否则回退 claude。

    Args:
        config: 运行时生效配置（已合并 CLI 覆盖）。
        subtask: 子任务字典（读取 backend / agent_type / difficulty 字段）。
        headless: 是否为无头模式。
        is_simple: 子任务是否被 _is_simple_task 判定为简单。
        agent_backend: Agent 类型声明的 backend（默认 claude）。

    Returns:
        backend 名称："claude" / "agent_loop" / 显式或路由声明的名称（如 "pi"）。
    """
    subtask = subtask or {}
    config = config or {}

    # 1-2：显式声明优先（默认空，不改变既有行为）
    explicit = subtask.get("backend") or config.get("worker_backend", "")
    # 3-4：B4 声明式路由（按 agent_type 优先于按 difficulty）
    if not explicit:
        _by_type = config.get("worker_backend_by_type", {}) or {}
        explicit = _by_type.get(subtask.get("agent_type", "developer"), "")
    if not explicit:
        _by_diff = config.get("worker_backend_by_difficulty", {}) or {}
        explicit = _by_diff.get(subtask.get("difficulty", "medium"), "")
    # 4.5：backend_promo 促销窗口路由（如 GLM flash 夜间免费仅 ZCode 本体可用）——
    # 仅在无任何显式声明时生效，且要求 backend 本机可用
    if not explicit:
        explicit = _promo_backend(config, headless)

    if explicit:
        if explicit != "claude" and not headless:
            return "claude"
        return explicit

    # 5：B1 保持原有 agent_loop 触发条件，不受 agent_backend 影响
    _agent_loop_enabled = config.get("agent_loop", {}).get("enabled", False)
    if _agent_loop_enabled and headless and is_simple:
        return "agent_loop"
    # 6：兜底
    return "claude"
