"""AgentLoop Backend — 简单子任务的直接 API 执行路径。"""

from __future__ import annotations

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry
from ..config import get_api_key


@BackendRegistry.register
class AgentLoopBackend(BaseBackend):
    """对简单子任务直接调用 AgentLoop（多轮 API + 工具执行），绕过 claude -p。

    触发条件由 executor 根据 config.agent_loop.enabled / headless / _is_simple_task 控制，
    本 backend 只负责执行。
    """

    name = "agent_loop"

    def run(self, ctx: BackendContext) -> SubtaskResult:
        from ..agent_loop import AgentLoop
        from ..router import resolve_provider, ProviderConfig

        # 与迁移前一致：路由依据 subtask 声明的 agent_type（agent 加载失败时也不丢失）。
        subtask = {"agent_type": ctx.agent_type}
        route = resolve_provider(subtask["agent_type"], ctx.config)
        if route:
            pc = route.primary
            _route_info = f"{route.role}:{pc.provider}/{pc.model}"
        else:
            _plan_api = ctx.config.get("plan_api", {}) if ctx.config else {}
            pc = ProviderConfig(
                provider=_plan_api.get("provider", "anthropic"),
                base_url=_plan_api.get("base_url", ""),
                model=_plan_api.get("model", ""),
            )
            _route_info = f"plan_api:{pc.provider}/{pc.model}"

        # S4 复杂度双通道：按 difficulty 路由模型（非空时覆盖）
        if ctx.routed_model:
            pc.model = ctx.routed_model
            _route_info += f" → {ctx.routed_model}"
            ctx.logger.info(f"[S4] AgentLoop {ctx.sub_id} difficulty={ctx.difficulty} → model={ctx.routed_model}")

        api_key = get_api_key(ctx.config)
        loop = AgentLoop(logger=ctx.logger)

        from ..console import _LazyConsole
        _console = _LazyConsole()
        _console.print(f"  🤖 直接 API 模式 ({_route_info})")

        result = loop.run(
            prompt=ctx.task_md,
            worktree=ctx.worktree,
            pc=pc,
            api_key=api_key,
            config=ctx.config,
            tag_name=ctx.tag_name,
            sub_id=ctx.sub_id,
            task_id=ctx.task_id,
            readonly=bool(ctx.extra.get("readonly", False)),
            scope_hint=str(ctx.extra.get("files_hint", "") or ""),
        )

        return SubtaskResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            sandbox_type="agent_loop",
            backend_time=0.0,
            kill_reason=getattr(result, "kill_reason", None),
        )
