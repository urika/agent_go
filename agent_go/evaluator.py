"""LLM 语义评估器（Phase 3）— hybrid 验证模式的语义层。

用法:
    from .evaluator import evaluate_semantic

    result = evaluate_semantic(subtask, worktree, verification, history, config, logger)
    if not result["passed"]:
        # 进入 Phase 1 修复循环
"""

import json
import time
import logging
from pathlib import Path
from typing import Any

from .api import call_api
from .config import get_api_key, meter_event
from .metrics import estimate_cost

__all__ = ["evaluate_semantic"]


def _build_eval_prompt(subtask: dict, verification: str, diff: str, previous_attempts: list[dict]) -> str:
    """构建语义评估 prompt。"""
    title = subtask.get("title", "")
    description = subtask.get("description", "")[:2000]
    agent_prompt = subtask.get("agent_prompt", "")[:1000]

    history_text = ""
    if previous_attempts:
        history_parts = []
        for a in previous_attempts:
            history_parts.append(
                f"- 第 {a.get('attempt', '?')} 次: {a.get('fix_summary', '修复尝试')} "
                f"→ 失败原因: {a.get('failure_summary', '未知')}"
            )
        history_text = "\n".join(history_parts)

    if not diff.strip():
        diff = "（未检测到文件变更）"
    else:
        if len(diff) > 4000:
            diff = diff[:4000] + "\n... (diff 过长，已截断)"

    history_section = ""
    if history_text:
        history_section = f"## 历史修复尝试\n{history_text}\n\n"

    return f"""你是一位严格的代码审查员。请评估以下子任务的执行结果是否完整、正确地完成了目标。

## 子任务信息

**标题:** {title}

**描述:**
{description}

**执行指令:**
{agent_prompt}

**验证命令:**
```bash
{verification}
```

**已验证通过的 shell 命令。** 现在需要你从语义层面判断：变更是否真正完成了子任务目标，是否有遗漏、副作用或质量问题。

## 当前变更 (git diff)

```diff
{diff}
```

{history_section}## 评估要求

请从以下维度评估：
1. 变更是否完整实现了子任务描述中的目标？
2. 是否有明显的遗漏、副作用或回归？
3. 代码/文档是否与变更保持一致？
4. 验证命令通过是否可能为"假阳性"？

请用 JSON 格式返回，不要包含任何其他内容：

```json
{{
  "passed": true,
  "reason": "变更完整实现了目标...",
  "suggestions": ""
}}
```

如果未通过，reason 必须具体说明问题，suggestions 给出明确的修复方向。
"""


def _parse_eval_response(content: str) -> dict:
    """从 LLM 响应中解析 JSON 评估结果。"""
    # 尝试直接解析
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "passed" in data:
            return {
                "passed": bool(data.get("passed", False)),
                "reason": str(data.get("reason", "")),
                "suggestions": str(data.get("suggestions", "")),
            }
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown code block 中提取
    import re
    matches = re.findall(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
    for m in matches:
        try:
            data = json.loads(m)
            if isinstance(data, dict) and "passed" in data:
                return {
                    "passed": bool(data.get("passed", False)),
                    "reason": str(data.get("reason", "")),
                    "suggestions": str(data.get("suggestions", "")),
                }
        except json.JSONDecodeError:
            continue

    # 兜底：无法解析时视为通过（避免阻塞正常流程）
    return {
        "passed": True,
        "reason": f"评估响应无法解析为 JSON，按通过处理。原始响应: {content[:200]}",
        "suggestions": "",
    }


def _get_worktree_diff(worktree: Path) -> str:
    """获取 worktree 中 HEAD 相对于最近一次提交的 diff。"""
    import subprocess
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(worktree), capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout
    return ""


def evaluate_semantic(
    subtask: dict,
    worktree: Path,
    verification: str,
    previous_attempts: list[dict],
    config: dict,
    logger: logging.Logger,
) -> dict[str, Any]:
    """对子任务执行结果做 LLM 语义评估。

    Args:
        subtask: 子任务 dict
        worktree: worktree 路径
        verification: 验证命令
        previous_attempts: 历史修复尝试
        config: 完整配置
        logger: 日志记录器

    Returns:
        {"passed": bool, "reason": str, "suggestions": str,
         "cost_usd": float, "latency_ms": float, "raw_response": str}
    """
    start = time.time()
    evaluator_cfg = config.get("evaluator", {})

    # 构建评估用 API 配置（优先使用 evaluator 专用配置，否则复用 plan_api）
    eval_api_cfg = dict(config.get("plan_api", {}))
    if evaluator_cfg.get("provider"):
        eval_api_cfg["provider"] = evaluator_cfg["provider"]
    if evaluator_cfg.get("model"):
        eval_api_cfg["model"] = evaluator_cfg["model"]
    if evaluator_cfg.get("base_url"):
        eval_api_cfg["base_url"] = evaluator_cfg["base_url"]
    if evaluator_cfg.get("api_key"):
        eval_api_cfg["api_key"] = evaluator_cfg["api_key"]

    eval_config = dict(config)
    eval_config["plan_api"] = eval_api_cfg
    # D3 修复：抑制 call_api 的内部记账（它硬编码 role="planner"，会把 evaluator 调用误标）。
    # evaluator 自己写一条 role="evaluator" 的 metering（用 prompt 长度估算真实 token，而非硬编码）。
    eval_config.pop("_metering_path", None)

    diff = _get_worktree_diff(worktree)
    prompt = _build_eval_prompt(subtask, verification, diff, previous_attempts)

    messages = [{"role": "user", "content": prompt}]

    try:
        content = call_api(eval_config, messages, logger)
    except Exception as e:
        logger.warning(f"语义评估 API 调用失败: {e}")
        return {
            "passed": True,  # API 失败时不阻塞
            "reason": f"语义评估 API 调用失败: {e}",
            "suggestions": "",
            "cost_usd": 0.0,
            "latency_ms": round((time.time() - start) * 1000, 2),
            "raw_response": "",
        }

    parsed = _parse_eval_response(content)
    latency_ms = round((time.time() - start) * 1000, 2)

    # D3 修复：用 prompt 长度估算真实 token（替代硬编码 1000/200）。
    # 保守估算：~3 字符/token（中英混合）。completion 用实际响应长度。
    est_prompt_tokens = max(1, len(prompt) // 3)
    est_completion_tokens = max(1, len(content) // 3)
    cost_usd = 0.0
    try:
        cost_usd = estimate_cost(
            eval_api_cfg.get("provider", "anthropic"),
            eval_api_cfg.get("model", ""),
            est_prompt_tokens, est_completion_tokens
        )
    except Exception:
        pass

    logger.info(f"语义评估结果: passed={parsed['passed']}, reason={parsed['reason'][:80]}")

    metering_event = {
        "role": "evaluator",
        "virtual_model": "agentgo-evaluator",
        "actual_provider": eval_api_cfg.get("provider", "anthropic"),
        "actual_model": eval_api_cfg.get("model", ""),
        "prompt_tokens": est_prompt_tokens,
        "completion_tokens": est_completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "result": "success" if parsed["passed"] else "quality_fail",
        "fallback_reason": "",
        "task_id": config.get("_task_id", ""),
        "subtask_id": subtask.get("id", ""),
    }
    meter_event(config.get("_metering_path"), metering_event)

    return {
        "passed": parsed["passed"],
        "reason": parsed["reason"],
        "suggestions": parsed["suggestions"],
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "raw_response": content,
    }
