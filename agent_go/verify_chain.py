"""verify_chain.py — 验证机械前置层（AG-2，吸收 llama-defender verification_chain L1）。

来源：llama-defender `verification_chain.py` 的 L1 MechanicalHandler（职责链 +
确定性检查），按双端评审（docs/in/protocol-layer-ownership-review-feedback-20260902.md
§三 AG-2）适配 agent_go 语境后吸收：

- 空 diff / 畸形 diff → 零成本直接判失败，不进入 LLM 语义评估；
- topic_relevance 只做**建议性**检查（中文描述按 \\W+ 分词易误判，只记日志不拦截）；
- 通过闸后委托默认 LLM 语义策略。

与上游的差异（适配说明）：
- 无 citation 概念（agent_go 的 Patch 无引用锚），citation_existence 不吸收；
- 无 L3 台账验证（台账数据在代理侧，待 R17 signals 端点落地后再评估）；
- "编译错/测试红"不重复实现——agent_go 的 shell 验证命令在语义评估之前执行，
  已覆盖该类拦截；本层补的是 shell 验证**空转通过**（heuristic/manual 验证）
  时的确定性兜底。

用法（opt-in）：config.json 中 `evaluator.strategy = "chain"`。
"""
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

# ═══════════════════════════════════════════════════════════════
# 机械闸（吸收自 MechanicalHandler，适配 agent_go）
# ═══════════════════════════════════════════════════════════════

class MechanicalGate:
    """机械验证闸——确定性规则，零成本，永远先于 LLM 评估执行。

    检查项（blocking = 拦截进入 LLM 评估）：
    - non_empty_diff（blocking）：变更内容为空 = 失败（空补丁无评估价值）
    - diff_well_formed（blocking）：diff 结构宽松合法性（与上游同规则）
    - topic_relevance（advisory）：任务关键词覆盖，只记日志不拦截
    """

    def verify(self, diff: str, task_description: str = "",
               logger: Optional[logging.Logger] = None) -> dict:
        """返回 {"passed": bool, "checks": [CheckResult...]}。"""
        checks: List[dict] = []

        # 1. 空 diff（blocking）
        empty = not diff or not diff.strip()
        checks.append({
            "rule_name": "non_empty_diff",
            "passed": not empty,
            "detail": "" if not empty else "empty diff (no changes to evaluate)",
        })

        # 2. diff 结构合法性（blocking，宽松——与上游 _diff_well_formed 同规则）
        if not empty:
            ok = self._diff_well_formed(diff)
            checks.append({
                "rule_name": "diff_well_formed",
                "passed": ok,
                "detail": "" if ok else "malformed diff (missing ---/+++ headers or hunk structure)",
            })

        # 3. 任务关键词覆盖（advisory——中文描述易误判，只记录不拦截）
        desc = (task_description or "").lower()
        if desc and not empty:
            keywords = [w for w in re.split(r"\W+", desc) if len(w) >= 4][:20]
            if keywords and not any(w in diff.lower() for w in keywords):
                checks.append({
                    "rule_name": "topic_relevance",
                    "passed": True,  # advisory：永不拦截
                    "detail": "advisory: diff shows no overlap with task description keywords",
                })
                if logger is not None:
                    logger.info("[verify_chain] advisory: diff 与任务描述关键词无重叠"
                                "（不拦截，供语义评估参考）")

        passed = all(c["passed"] for c in checks)
        return {"passed": passed, "checks": checks}

    @staticmethod
    def _diff_well_formed(diff: str) -> bool:
        """宽松检查——空 diff 或带 ---/+++ 头的 unified diff 片段视为合法。"""
        if not diff or not diff.strip():
            return False
        if diff.startswith("---") or "\n---" in diff:
            return True
        # 非 diff 类输出（text 任务）只要有内容即合法
        return True

    @staticmethod
    def first_failure(verdict: dict) -> str:
        """取第一条失败检查的 detail（供 reason/suggestions 使用）。"""
        for c in verdict.get("checks", []):
            if not c.get("passed", True):
                return c.get("detail") or c.get("rule_name", "mechanical_check_failed")
        return "mechanical_check_failed"


# ═══════════════════════════════════════════════════════════════
# EvalStrategy：机械前置 + 委托 LLM 语义评估
# ═══════════════════════════════════════════════════════════════

class ChainEvalStrategy:
    """链式评估策略——机械闸先行，通过后委托默认 LLM 语义策略。

    机械闸失败时零成本短路（cost_usd=0，不发起 LLM 调用）；
    通过时行为与 default 策略完全一致。
    """
    name = "chain"
    description = "机械前置层 + LLM 语义评估（AG-2，吸收 verification_chain L1）"

    def __call__(self, subtask: dict, worktree: Path, verification: str,
                 previous_attempts: list, config: dict, logger: logging.Logger) -> dict:
        start = time.time()
        # 延迟 import：避免 evaluator ↔ verify_chain 模块级循环依赖
        from .evaluator import _default_semantic_eval, _get_worktree_diff

        diff_base = config.get("_pre_work_head") or config.get("_base_commit", "")
        diff = _get_worktree_diff(worktree, base_commit=diff_base)

        description = f"{subtask.get('title', '')} {subtask.get('description', '')}"
        verdict = MechanicalGate().verify(diff, description, logger)

        if not verdict["passed"]:
            reason = MechanicalGate.first_failure(verdict)
            logger.info(f"[verify_chain] 机械闸拦截（零成本短路）: {reason}")
            return {
                "passed": False,
                "confidence": 1.0,
                "reason": f"[mechanical] {reason}",
                "suggestions": "子任务未产生可评估的有效变更；请确认执行是否真正修改了文件。",
                "cost_usd": 0.0,
                "latency_ms": (time.time() - start) * 1000,
            }

        result = _default_semantic_eval(subtask, worktree, verification,
                                        previous_attempts, config, logger)
        # 保留机械检查痕迹（审计用），不改变语义评估结论
        result.setdefault("mechanical_checks", [
            {"rule_name": c["rule_name"], "passed": c["passed"]}
            for c in verdict["checks"]])
        return result


__all__ = ["MechanicalGate", "ChainEvalStrategy"]
