"""独立只读审查 subagent — 两阶段审查模式。

核心价值（subagent-design-research.md 改进方向 2）：
当前验证循环是同一个 Claude Code 进程自修复——实现者看不到自己的盲区
（"修 A 漏 B"、"路径死锁"等问题在实现视角里不自知）。本模块提供
一个**独立的只读审查 agent**：不参与实现，只做黑盒验证——基于失败输出
与当前 diff 分析失败根因，产出修复方向，注入修复 prompt。

关键设计：
- 只读：审查 agent 只读代码/失败上下文，永不修改文件（无 Write/Edit/Bash 能力）。
- 独立模型：默认复用 evaluator 模型，可用 verification.readonly_review.model 覆盖。
- fail-open：API 失败/解析失败 → 返回 None，不阻断验证循环（由 executor 兜底）。
- 计量：成本写入 metering.jsonl（role=reviewer），默认关闭（readonly_review.enabled）。

用法：
    from .review_agent import run_readonly_review
    review = run_readonly_review(subtask, worktree, verification, failed_cmds,
                                 failed_outputs, git_diff, config, logger)
    if review:
        fix_prompt += review["suggestions"]
"""

import json
import time
import logging
from typing import Optional, Any
from pathlib import Path

__all__ = ["run_readonly_review"]

logger = logging.getLogger(__name__)

_REVIEW_TEMPLATE = """你是一位独立的只读代码审查 agent。你的任务是**黑盒分析**为什么验证失败。

你不参与实现，也不修改任何文件。你只分析给定的失败证据，找出**实现者视角容易忽略的根因**。

## 子任务目标

**标题:** {title}

**描述:**
{description}

**验证命令:**
```bash
{verification}
```

## 失败证据（验证命令输出）

```
{failed_outputs}
```

## 当前变更 (git diff)

```diff
{diff}
```

## 审查要求

请以**独立审查者**视角回答（不要复述失败输出，要给出判断）：

1. **失败根因判断**：基于失败输出与 diff，判断验证失败是：
   - 实现缺陷（代码逻辑错误、遗漏用例、接口不匹配）——给出具体根因
   - 测试自身问题（验证命令写错、测试断言与需求不符）——指出并说明如何修正验证
   - 环境/基础设施问题（依赖缺失、超时、路径错误）——给出规避方式
2. **实现者盲区**：diff 里是否暗示实现者可能「修了表面症状但漏了根本原因」？
   是否有未覆盖的边界、假阳性通过、接口不一致？
3. **修复方向**：给出**具体、可执行**的下一步（改哪个函数、加什么守卫、补哪条断言）。

## 输出格式

只输出 JSON，不要包含其他任何内容：

```json
{{
  "root_cause": "失败根因分类（实现缺陷/测试问题/环境问题）+ 一句话判断",
  "blind_spot": "实现者视角容易忽略的点（无则写 '无'）",
  "suggestions": "具体修复方向，2-4 条要点"
}}
```

如果信息不足以判断，请把 root_cause 设为"信息不足"，suggestions 给出如何补充证据。
"""


def _infer_repo_root(worktree: Path) -> Optional[Path]:
    """从 worktree 推断项目根目录（git worktree 的 .git 是文件，指向主仓库）。

    用于项目级 skill 查找（<repo>/.agent_go/skills/）。worktree 路径形如
    ~/.agent_go/task-xxx/sub-1/work/<repo-name>/，向上找含 .git 的目录即可。
    """
    try:
        cur = Path(worktree).resolve()
        for parent in [cur, *cur.parents]:
            if (parent / ".git").exists():
                return parent
    except Exception:
        pass
    return None


def _load_review_skill(skill_name: str, config: dict, worktree: Optional[Path] = None) -> str:
    """加载审查维度 skill 的 body（作为审查维度指引注入 prompt）。

    查找路径（与 skills.py 一致）：~/.agent_go/skills/<name>/SKILL.md，
    其次 <repo>/.agent_go/skills/<name>/SKILL.md（repo 从 config._repo 或 worktree 推断）。
    skill 不存在/加载失败 → 返回空字符串（回退内置通用模板，fail-open）。
    """
    if not skill_name:
        return ""
    try:
        from .skills import load_skill
        repo = (config or {}).get("_repo")
        if not repo and worktree:
            repo = _infer_repo_root(worktree)
        s = load_skill(skill_name, repo)
        if s and s.body and s.body.strip():
            return s.body.strip()
        logger.warning(f"[readonly_review] skill '{skill_name}' 不存在或为空，回退内置模板")
    except Exception as e:
        logger.warning(f"[readonly_review] skill '{skill_name}' 加载失败，回退内置模板: {e}")
    return ""


def run_readonly_review(
    subtask: dict,
    worktree: Path,
    verification: str,
    failed_cmds: list[str],
    failed_outputs: list[str],
    git_diff: str,
    config: dict,
    logger: logging.Logger,
    metering_path: Any = None,
) -> Optional[dict]:
    """独立只读审查：黑盒分析失败根因，产出修复方向。

    Args:
        subtask: 子任务 dict
        worktree: worktree 路径（保留参数，用于后续只读文件浏览扩展）
        verification: 验证命令
        failed_cmds: 失败的验证命令列表
        failed_outputs: 对应的失败输出列表
        git_diff: 当前 diff（git diff HEAD 累积）
        config: 完整配置
        logger: 日志记录器
        metering_path: metering.jsonl 路径（空则不写）

    Returns:
        dict {root_cause, blind_spot, suggestions, cost_usd, latency_ms}
        或 None（配置关闭 / API 失败 / 解析失败——fail-open，不阻断验证循环）
    """
    rr_cfg = (config or {}).get("verification", {}).get("readonly_review", {}) or {}
    if not rr_cfg.get("enabled", False):
        return None

    from .api import call_api
    from .metrics import estimate_cost
    from .config import meter_event

    start = time.time()

    # 独立 API 配置：readonly_review > evaluator > plan_api（模型独立是核心）
    api_cfg = dict((config or {}).get("plan_api", {}) or {})
    eval_cfg = (config or {}).get("evaluator", {}) or {}
    for key, src in (("model", rr_cfg), ("provider", rr_cfg), ("base_url", rr_cfg)):
        if rr_cfg.get(key):
            api_cfg[key] = rr_cfg[key]
        elif eval_cfg.get(key):
            api_cfg[key] = eval_cfg[key]
    api_cfg["timeout_ms"] = rr_cfg.get("timeout_ms", 90_000)
    api_cfg["max_tokens"] = rr_cfg.get("max_tokens", 2048)

    # 失败证据组装
    evidence_parts = []
    for cmd, out in zip(failed_cmds, failed_outputs):
        evidence_parts.append(f"$ {cmd}\n{out[:2000]}")
    failed_outputs_txt = "\n---\n".join(evidence_parts)[:12000] or "（无失败输出）"

    diff = git_diff or ""
    if not diff.strip():
        try:
            from .evaluator import _get_worktree_diff
            diff = _get_worktree_diff(worktree)
        except Exception:
            diff = ""
    if not diff.strip():
        diff = "（未检测到变更）"
    else:
        diff = diff[:15000] + "\n... (diff 过长，已截断)" if len(diff) > 15000 else diff

    prompt = _REVIEW_TEMPLATE.format(
        title=subtask.get("title", ""),
        description=(subtask.get("description", "") or "")[:2000],
        verification=verification or "（无验证命令）",
        failed_outputs=failed_outputs_txt,
        diff=diff,
    )

    # 领域化审查维度（方案 A）：配置的 skill 加载后追加为「审查维度指引」。
    # 空 skill / 加载失败 → 仅内置通用模板，不改变行为。
    _skill_body = _load_review_skill(rr_cfg.get("skill", ""), config, worktree)
    if _skill_body:
        prompt += (
            "\n\n## 领域审查维度指引（Skill 注入）\n"
            "以下是你所在的领域审查专家给你的专属检查清单。审查时必须逐条对照：\n"
            f"{_skill_body}\n"
        )

    messages = [{"role": "user", "content": prompt}]

    # call_api 从 config["plan_api"]/["planner_api"] 读取 API 配置，因此把定制后的
    # api_cfg 包回 config（与 evaluator 的 eval_config 处理一致）。
    call_cfg = dict(config or {})
    call_cfg["plan_api"] = api_cfg
    call_cfg.pop("planner_api", None)

    try:
        content = call_api(call_cfg, messages, logger)
    except Exception as e:
        logger.warning(f"[readonly_review] API 调用失败，跳过审查（fail-open）: {e}")
        return None

    parsed = _parse_review_response(content)
    if parsed is None:
        logger.warning("[readonly_review] 响应解析失败，跳过审查（fail-open）")
        return None

    latency_ms = round((time.time() - start) * 1000, 2)
    cost_usd = 0.0
    try:
        pt = max(1, len(prompt) // 4)
        ct = max(1, len(content) // 4)
        cost_usd = estimate_cost(api_cfg.get("provider", "anthropic"),
                                 api_cfg.get("model", ""), pt, ct)
    except Exception:
        pass

    result = {
        "root_cause": parsed.get("root_cause", ""),
        "blind_spot": parsed.get("blind_spot", ""),
        "suggestions": parsed.get("suggestions", ""),
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
    }

    logger.info(f"[readonly_review] 根因: {result['root_cause'][:80]}")
    meter_event(metering_path, {
        "role": "reviewer",
        "virtual_model": "agentgo-reviewer",
        "actual_provider": api_cfg.get("provider", "anthropic"),
        "actual_model": api_cfg.get("model", ""),
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(content) // 4),
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "result": "success",
        "fallback_reason": "",
        "task_id": (config or {}).get("_task_id", ""),
        "subtask_id": subtask.get("id", ""),
    })

    return result


def _parse_review_response(content: str) -> Optional[dict]:
    """从 LLM 响应中解析 JSON（支持裸 JSON / markdown code block）。"""
    try:
        data = json.loads(content)
        if isinstance(data, dict) and ("root_cause" in data or "suggestions" in data):
            return data
    except json.JSONDecodeError:
        pass
    import re
    matches = re.findall(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
    for m in matches:
        try:
            data = json.loads(m)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None
