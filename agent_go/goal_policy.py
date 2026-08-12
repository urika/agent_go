"""Goal Policy Resolver — 决定 Goal Loop 的最终执行策略（goal-mechanism-design.md §3.3/§4）。

四层模型中的第三层：Goal Policy。Goal Contract 默认存在，Goal Loop 不对所有
任务默认开启。决策优先级：

    用户明确覆盖 > 配置明确策略 > 系统确定性策略 > Planner recommendation > 默认策略

Planner 只能提供建议（goal_recommendation），不能静默强制开启；系统用确定性
规则复核（有无安全 verification、是否 headless、难度、预算边界）。
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["GOAL_MODES", "resolve_goal_policy"]

GOAL_MODES = ("off", "auto", "force", "hook")


def resolve_goal_policy(
    user_mode: Optional[str] = None,
    *,
    config_policy: Optional[str] = None,
    goal_recommendation: Optional[dict] = None,
    subtasks: Optional[list[dict]] = None,
    headless: bool = False,
    logger_: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """Resolve the effective Goal Policy for a task.

    Args:
        user_mode: CLI 显式覆盖（--goal-mode / --goal / --no-goal / --goal-hook 归一后的模式）。
        config_policy: config.goal.policy（默认 off）。
        goal_recommendation: Planner 建议（可选，仅作为最弱信号）。
        subtasks: 已确认子任务（提取难度/验证命令特征）。
        headless: 是否无头执行（交互模式默认不开 Goal）。

    Returns:
        {
          "mode": "off|auto|force|hook",      # 最终有效模式
          "enabled": bool,                     # 是否注入 Goal 指令
          "enable_hook": bool,                 # 是否注入 Stop Hook
          "reason_codes": [str],               # 决策依据（可审计）
          "backend": "claude_cli|internal|unsupported",
        }
    """
    lg = logger_ or logger
    reasons: list[str] = []

    # ── 1. 用户明确覆盖（最高优先级）──
    if user_mode in GOAL_MODES:
        reasons.append("user_override")
        effective = user_mode
    elif config_policy in GOAL_MODES and config_policy != "off":
        reasons.append("config_policy")
        effective = config_policy
    else:
        # ── 2. 系统确定性策略（auto 语义）──
        effective = "off"
        if headless and subtasks:
            has_evidence = any(str(st.get("verification", "") or "").strip() for st in subtasks)
            difficulties = {str(st.get("difficulty", "medium")) for st in subtasks}
            long_running = bool(difficulties & {"medium", "hard"})
            if has_evidence and long_running:
                effective = "auto"
                reasons.append("headless_task")
                reasons.append("clear_verification")
                if long_running:
                    reasons.append("long_running_candidate")
            elif not has_evidence:
                reasons.append("no_completion_evidence")
            else:
                reasons.append("simple_task")
        else:
            reasons.append("interactive_or_default_off")

    # Planner recommendation 只在系统未给出启用决定时记录为参考信号
    rec_mode = (goal_recommendation or {}).get("mode")
    if rec_mode in GOAL_MODES and rec_mode != "off" and effective == "off":
        reasons.append(f"planner_suggested_{rec_mode}_ignored")

    # ── 3. 映射为启用标志 ──
    enabled = effective in ("auto", "force", "hook")
    enable_hook = effective == "hook"
    backend = "claude_cli" if enabled else ("internal" if effective == "off" else "unsupported")

    result = {
        "mode": effective,
        "enabled": enabled,
        "enable_hook": enable_hook,
        "reason_codes": reasons,
        "backend": backend,
    }
    lg.info(f"[goal_policy] mode={effective} enabled={enabled} hook={enable_hook} reasons={reasons}")
    return result
