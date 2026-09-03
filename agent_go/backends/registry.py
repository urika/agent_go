"""Backend 注册表与解析逻辑。"""

from __future__ import annotations

from typing import Type

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
    - agent_backend 字段已预留，供后续阶段按 Agent 类型指定 backend（B4 启用）。

    Args:
        config: 运行时生效配置（已合并 CLI 覆盖）。
        subtask: 子任务字典。
        headless: 是否为无头模式。
        is_simple: 子任务是否被 _is_simple_task 判定为简单。
        agent_backend: Agent 类型声明的 backend（默认 claude）。

    Returns:
        backend 名称，当前为 "claude" / "agent_loop" / 显式声明的名称（如 "pi"）。
    """
    # B3：显式声明优先（默认空，不改变既有行为）。
    explicit = (subtask or {}).get("backend") or (config or {}).get("worker_backend", "")
    if explicit:
        if explicit != "claude" and not headless:
            return "claude"
        return explicit
    # B1：保持原有 agent_loop 触发条件，不受 agent_backend 影响。
    _agent_loop_enabled = (config or {}).get("agent_loop", {}).get("enabled", False)
    if _agent_loop_enabled and headless and is_simple:
        return "agent_loop"
    return "claude"
