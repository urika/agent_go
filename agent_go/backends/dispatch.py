"""修复类执行（fix/replan/reload）的 Backend 分发。

B2（阶段十三）：executor 的三条修复路径原本直接调用 subtask._run_headless，
现统一走 BackendRegistry 分发，与初始执行（executor._run_with_backend）
保持同一解析策略与容错语义。
"""

from __future__ import annotations

from .base import BackendContext, SubtaskResult
from .registry import BackendRegistry, resolve_backend_name


def repair_timeout(cfg: dict, difficulty: str, env: dict) -> int:
    """验证修复类执行的超时计算（retry_timeout × 难度倍数封顶，本地模型 ×2）。

    与迁移前 executor 三处内联逻辑逐一相等：
    - 倍数：easy 1 / medium 1.5 / hard 2.5（未知难度按 medium）
    - 封顶：easy 600 / medium 900 / hard 1500
      （CR-建议#2：hard 任务修复重试 900s 偏紧，放宽到 1500s）
    - 本地模型（AGENT_GO_IS_LOCAL=1）×2，封顶 3000
    """
    base = (cfg or {}).get("verification", {}).get("retry_timeout", 300)
    mult = {"easy": 1, "medium": 1.5, "hard": 2.5}.get(difficulty, 1.5)
    cap = {"easy": 600, "medium": 900, "hard": 1500}.get(difficulty, 900)
    timeout = min(int(base * mult), cap)
    if (env or {}).get("AGENT_GO_IS_LOCAL", "") == "1":
        timeout = min(timeout * 2, 3000)
    return timeout


def run_repair(ctx: BackendContext, is_simple: bool) -> SubtaskResult:
    """修复类执行（fix/replan/reload）的 backend 分发。

    与初始执行同一解析策略：resolve_backend_name(ctx.config, {}, ctx.headless, is_simple)；
    非默认 backend（agent_loop / 显式声明的 pi 等）路径异常时回退 claude（log warning），
    与 executor 初始路径的容错一致
    （初始路径额外做的 worktree reset 是防 AgentLoop 半改状态污染首跑验证；
    修复路径随后必经 git add/commit，由 executor 的完成边界逻辑兜底，此处不重复 reset）。
    """
    # tag_name 必须为空：AgentLoopBackend 会把 ctx.tag_name 传给 AgentLoop.run，
    # 非空时 AgentLoop 自行 git add/commit/tag。修复路径强制留空，
    # 让 executor 的修复后 commit/tag 逻辑继续独占完成边界。
    ctx.tag_name = ""
    backend_name = resolve_backend_name(ctx.config, {}, ctx.headless, is_simple)
    if backend_name != "claude":
        # 非默认 backend（agent_loop / 显式声明的 pi 等）：执行失败时回退 claude
        try:
            return BackendRegistry.get(backend_name)().run(ctx)
        except Exception as _loop_err:
            ctx.logger.warning(f"Backend {backend_name} 修复执行失败，回退到 claude -p（不中断任务）: {_loop_err}")
    return BackendRegistry.get("claude")().run(ctx)
