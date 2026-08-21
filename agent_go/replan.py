"""C3 局部重规划（PRD F-VERIFY-6 受控策略升级）。

验证循环出现无进展信号（verify_revert / verify_divergence / 失败模式重复）时，
生成一次 Plan 拆分建议，引导 agent 把卡住的任务拆成有序小步再试一次。

契约（F-VERIFY-6，不可违反）：
- 最多触发一次（executor 侧 _replan_state["triggered"] 保证）。
- 继承父任务预算和权限（复用同一 sub_id 计量，L2 成本上限继续约束，
  不新增预算条目；执行前若已超 L2 上限则不执行）。
- 记录 replan_triggered / replan_succeeded（log_event + verify_results["replan"]）。
- 默认人工确认（交互模式弹确认；headless/--yes 需显式 replan.auto_apply=true
  才自动执行，否则只记录建议等待人工处置）。
- 不递归扩大任务图（拆分步只注入修复 prompt，不创建新子任务节点）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

REPLAN_TRIGGERS = ("verify_revert", "verify_divergence", "failure_pattern_repeat")

TRIGGER_LABELS = {
    "verify_revert": "循环振荡（修复被撤销或无实际效果）",
    "verify_divergence": "打地鼠（连续修复指出不同缺陷，未收敛）",
    "failure_pattern_repeat": "失败模式重复（同一缺陷反复修不好）",
}


def should_trigger(reason: str) -> bool:
    """触发原因白名单——只有无进展类信号才允许触发局部重规划。"""
    return reason in REPLAN_TRIGGERS


def _heuristic_decomposition(subtask: dict, max_children: int) -> list[dict]:
    """确定性兜底拆分（零 LLM）：按 files_hint / verification 推导有序小步。

    LLM 不可用（无 API key / 调用失败 / 输出不可解析）时使用，保证功能
    在任意环境下可用且 bench 可复现。
    """
    title = subtask.get("title", "") or subtask.get("id", "")
    files_hint = subtask.get("files_hint", "") or ""
    files = [f.strip() for f in re.split(r"[,\s]+", files_hint) if f.strip()]

    steps: list[dict] = [
        {"step": 1, "title": "定位根因",
         "detail": f"阅读相关代码与失败输出，写下「{title}」失败的最小根因假设，"
                   f"先不动手写修复。"},
    ]
    if files:
        # 文件超过 2 个时按文件逐个修，否则单步最小修复
        if len(files) > 2 and max_children >= 4:
            for i, f in enumerate(files[: max_children - 2], start=2):
                steps.append({"step": i, "title": f"修复 {f}",
                              "detail": f"只修改 {f}，应用根因假设对应的最小修复，"
                                        f"不碰其他文件。"})
        else:
            steps.append({"step": 2, "title": "最小修复",
                          "detail": f"只在 {', '.join(files[:5])} 内做最小修复，"
                                    f"每改一处先确认它与根因假设直接相关。"})
    else:
        steps.append({"step": 2, "title": "最小修复",
                      "detail": "基于根因假设做最小修复，一次只改一处，"
                                "每处改动都要能对应到失败输出。"})
    steps.append({"step": len(steps) + 1, "title": "验证收敛",
                  "detail": "重新运行验证命令；若仍失败，对比失败输出与根因假设，"
                            "只修与假设直接相关的部分，不做无关重构。"})
    return steps[:max_children]


def _parse_llm_steps(text: str, max_children: int) -> list[dict]:
    """解析 LLM 输出的 JSON 步骤列表；不可解析返回空列表（走兜底）。"""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    steps: list[dict] = []
    if isinstance(raw, list):
        for i, item in enumerate(raw[:max_children], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            detail = str(item.get("detail", "")).strip()
            if title:
                steps.append({"step": i, "title": title[:60], "detail": detail[:300]})
    return steps


def build_decomposition(subtask: dict, failure_context: dict,
                        config: Optional[dict] = None, logger=None,
                        max_children: int = 4) -> list[dict]:
    """生成一次 Plan 拆分建议：优先 LLM（planner_api/plan_api），失败走确定性兜底。

    fail-open：任何 LLM 异常都降级为启发式拆分，绝不阻断验证循环。
    """
    cfg = config if isinstance(config, dict) else {}
    api_cfg = cfg.get("planner_api") or cfg.get("plan_api") or {}
    if api_cfg.get("model") and logger is not None:
        try:
            from .api import call_api
            prompt = (
                "一个编码子任务在自动修复循环中卡住。请把它拆成有序的小步，"
                "每步只做一件事，帮助 agent 收敛。\n"
                f"子任务标题: {subtask.get('title', '')}\n"
                f"子任务描述: {(subtask.get('description', '') or '')[:300]}\n"
                f"涉及文件: {subtask.get('files_hint', '')}\n"
                f"失败原因: {failure_context.get('reason', '')[:200]}\n"
                f"失败输出摘要: {failure_context.get('failed_output', '')[:300]}\n"
                f"只输出 JSON 数组（最多 {max_children} 项），格式: "
                '[{"title": "步骤名", "detail": "具体做什么"}]，不要输出其他内容。'
            )
            text = call_api(cfg, [{"role": "user", "content": prompt}], logger)
            steps = _parse_llm_steps(text or "", max_children)
            if len(steps) >= 2:
                logger.info(f"[replan] LLM 拆分建议 {len(steps)} 步")
                return steps
            logger.warning("[replan] LLM 拆分输出不可解析，降级为启发式拆分")
        except Exception as e:
            logger.warning(f"[replan] LLM 拆分失败（降级启发式）: {e}")
    return _heuristic_decomposition(subtask, max_children)


def render_replan_guidance(steps: list[dict], reason: str) -> str:
    """把拆分建议渲染成注入修复 prompt 的指引文本。"""
    label = TRIGGER_LABELS.get(reason, reason)
    lines = [
        "",
        "## ⚠️ 局部重规划（任务拆分执行）",
        "",
        f"此前的修复循环出现「{label}」，说明整体推进的方式无法收敛。",
        "停止整体重写，改为严格按下面的拆分步骤顺序执行，每完成一步再进入下一步：",
        "",
    ]
    for s in steps:
        lines.append(f"{s['step']}. **{s['title']}** — {s['detail']}")
    lines += [
        "",
        "约束：不扩大改动范围，不做步骤之外的重构；每步的改动必须能对应到失败输出。",
    ]
    return "\n".join(lines)


def confirm_replan(reason: str, steps: list[dict], input_fn=input) -> bool:
    """交互模式人工确认（F-VERIFY-6 默认人工确认）。返回 True = 批准执行。"""
    label = TRIGGER_LABELS.get(reason, reason)
    print(f"\n🧩 局部重规划建议（触发: {label}）:")
    for s in steps:
        print(f"   {s['step']}. {s['title']} — {s['detail'][:60]}")
    print("   [P] 按拆分执行一次  [其他] 不执行（按原流程终止/重试）")
    try:
        return input_fn("> ").strip().upper() in ("P", "REPLAN")
    except (EOFError, KeyboardInterrupt):
        return False
