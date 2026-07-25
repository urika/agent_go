"""GoalInjector — 在 worktree 内注入 Claude Code /goal 机制（设计稿 §3.4）。

注入内容：
  .claude/settings.json  ← Stop Hook 配置
  scripts/verify-goal.sh ← 验证脚本（仅包含通过 4 级安全白名单的命令）

安全约束（设计稿「关键约束」）：Stop Hook 脚本与验证命令一样必须过白名单，
未通过的命令不写入脚本；全部不合格则不注入。
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .utils import _is_safe_verification_command

logger = logging.getLogger(__name__)

__all__ = ["GoalInjector"]


class GoalInjector:
    """在 worktree 中注入 /goal 所需的配置文件和脚本。"""

    GOAL_HOOK_SCRIPT = "scripts/verify-goal.sh"

    @staticmethod
    def build_goal_condition(verification_cmds: list[str], custom_condition: str = "") -> str:
        """从验证命令自动生成 /goal condition 字符串。"""
        if custom_condition:
            return custom_condition
        cmds = " && ".join(c.strip() for c in verification_cmds if c.strip())
        return f"以下验证命令全部退出码为0: {cmds}"

    @staticmethod
    def inject(
        worktree: Path,
        verification_cmds: list[str],
        condition: str = "",
        evaluator_config: Optional[dict] = None,
    ) -> bool:
        """在 worktree 中创建 .claude/settings.json + scripts/verify-goal.sh。

        Returns:
            True = 注入成功；False = 无安全命令可注入（调用方降级为仅文本指令）
        """
        safe_cmds = []
        for cmd in verification_cmds:
            safe, reason = _is_safe_verification_command(cmd)
            if safe:
                safe_cmds.append(cmd)
            else:
                logger.warning(f"[goal] 验证命令未过白名单，不写入 Stop Hook: {cmd[:60]} ({reason})")
        if not safe_cmds:
            logger.warning("[goal] 无安全验证命令，跳过 Stop Hook 注入")
            return False

        claude_dir = worktree / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "Stop": {
                    "command": GoalInjector.GOAL_HOOK_SCRIPT,
                    "type": "script",
                }
            }
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")

        scripts_dir = worktree / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        # set -e：任一命令失败即非零退出 → Stop Hook 判定 goal 未达成，Claude 继续循环
        script_lines = [
            "#!/bin/bash",
            "# 由 agent_go GoalInjector 生成 — goal 完成条件校验",
            "set -e",
            "",
            *safe_cmds,
            "",
        ]
        script_path = scripts_dir / "verify-goal.sh"
        script_path.write_text("\n".join(script_lines), encoding="utf-8")
        script_path.chmod(0o755)

        logger.info(f"[goal] Stop Hook 已注入: {GoalInjector.GOAL_HOOK_SCRIPT} ({len(safe_cmds)} 条命令)")
        return True

    @staticmethod
    def cleanup(worktree: Path) -> None:
        """清理注入的文件（保留现场除外——worktree 删除时天然清理）。"""
        for p in (worktree / ".claude" / "settings.json",
                  worktree / GoalInjector.GOAL_HOOK_SCRIPT):
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                logger.debug(f"[goal] 清理失败 {p}: {e}")
