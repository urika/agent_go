"""LLM 语义评估器（Phase 3）— 策略模式重构。

设计：
  策略模式：评估算法通过 register() 注册，运行时根据配置路由。
  默认策略保留原有 evaluate_semantic 语义，增加 confidence 评分。
  自动持久化评估事件到 assessment.jsonl（与 assessment.py 数据层对接）。

用法：
    from .evaluator import evaluate

    result = evaluate(subtask, worktree, verification, history, config, logger,
                      assessment_path=str(task_dir),
                      verification_confidence={"level": "heuristic"})

策略注册：
    from .evaluator import register

    class MyStrategy:
        name = "strict"
        description = "更严格的代码审查"
        def __call__(self, subtask, worktree, verification, history, config, logger):
            ...
    register(MyStrategy())
"""

import json
import re
import time
import logging
from pathlib import Path
from typing import Any, Optional, Protocol

from .api import call_api
from .config import meter_event
from .metrics import estimate_cost
from .assessment import AssessmentEvent, write as write_assessment, ASSESSMENT_FILENAME

__all__ = ["evaluate", "register", "list_strategies", "evaluate_semantic"]


# ═══════════════════════════════════════════════════════════════
# 策略协议与注册表
# ═══════════════════════════════════════════════════════════════

class EvalStrategy(Protocol):
    """评估策略协议。实现此接口可注册为自定义评估策略。"""
    name: str
    description: str

    def __call__(
        self,
        subtask: dict,
        worktree: Path,
        verification: str,
        previous_attempts: list[dict],
        config: dict,
        logger: logging.Logger,
    ) -> dict:
        """执行评估。

        Returns:
            dict: {passed: bool, confidence: float, reason: str,
                   suggestions: str, cost_usd: float, latency_ms: float}
        """
        ...


_registry: dict[str, EvalStrategy] = {}


def register(strategy: EvalStrategy) -> None:
    """注册评估策略（供外部模块或用户自定义策略使用）。"""
    _registry[strategy.name] = strategy


def list_strategies() -> list[dict]:
    """列出所有已注册的策略。"""
    return [
        {"name": name, "description": getattr(s, "description", "")}
        for name, s in _registry.items()
    ]


# ═══════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════

def evaluate(
    subtask: dict,
    worktree: Path,
    verification: str,
    previous_attempts: list[dict],
    config: dict,
    logger: logging.Logger,
    assessment_path: str = "",
    verification_confidence: Optional[dict] = None,
) -> dict:
    """统一评估入口：路由到配置的策略 + 自动写 assessment.jsonl。

    Args:
        subtask: 子任务 dict
        worktree: worktree 路径
        verification: 验证命令
        previous_attempts: 历史修复尝试
        config: 完整配置
        logger: 日志记录器
        assessment_path: 写入 assessment.jsonl 的目录路径（空则不写）
        verification_confidence: 传此参数表示 L1 自动触发，用于记录来源

    Returns:
        {"passed": bool, "confidence": float, "reason": str,
         "suggestions": str, "cost_usd": float, "latency_ms": float}
    """
    strategy_name = config.get("evaluator", {}).get("strategy", "default")
    strategy = _registry.get(strategy_name)
    if strategy is None:
        strategy = _DefaultEvalStrategy()
        _registry["default"] = strategy

    result = strategy(subtask, worktree, verification, previous_attempts, config, logger)

    # 自动持久化到 assessment.jsonl
    if assessment_path:
        try:
            assessment_file = Path(assessment_path)
            if assessment_file.is_dir():
                assessment_file = assessment_file / ASSESSMENT_FILENAME
            vc_level = "unknown"
            if verification_confidence:
                vc_level = verification_confidence.get("level", "unknown")
            trigger = "auto" if verification_confidence else "manual"
            event = AssessmentEvent(
                task_id=config.get("_task_id", ""),
                subtask_id=subtask.get("id", ""),
                trigger_source=trigger,
                verification=verification,
                verification_confidence=vc_level,
                evaluator_strategy=strategy_name,
                evaluator_provider=config.get("evaluator", {}).get(
                    "provider", config.get("plan_api", {}).get("provider", "")),
                evaluator_model=config.get("evaluator", {}).get(
                    "model", config.get("plan_api", {}).get("model", "")),
                passed=result.get("passed", True),
                confidence=result.get("confidence", 1.0 if result.get("passed", True) else 0.0),
                reason=result.get("reason", ""),
                suggestions=result.get("suggestions", ""),
                cost_usd=result.get("cost_usd", 0.0),
                latency_ms=result.get("latency_ms", 0.0),
            )
            write_assessment(assessment_file, event)
        except Exception:
            logger.debug("写入 assessment.jsonl 失败（非关键）", exc_info=True)

    return result


# 向后兼容别名（现有 executor.py 通过 evaluate_semantic 调用）
evaluate_semantic = evaluate


# ═══════════════════════════════════════════════════════════════
# 默认策略
# ═══════════════════════════════════════════════════════════════

_EVAL_TEMPLATE = """你是一位严格的代码审查员。请评估以下子任务的执行结果是否完整、正确地完成了目标。

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
  "passed": true/false,
  "confidence": 0.0-1.0,
  "reason": "...",
  "suggestions": ""
}}
```

- **passed**: true 表示变更完成目标
- **confidence**: 你对自己判断的置信度。1.0=非常确定，0.7=较确定但有些细节不确定，0.3=可能通过但有隐患，0.0=完全不确定
- **reason**: 具体说明判断理由
- **suggestions**: 如果未通过（passed=false），给出明确的修复方向

**重要指引：**
- 如果验证命令包含确定性测试（pytest、assert 等），且全部通过，应优先视为任务完成。仅当 diff 明显遗漏核心功能或引入回归时才能判定 passed=false。
- 如果 shell 验证已覆盖核心功能（通过测试运行验证了行为正确性），你的语义评估应作为补充检查而非替代验证——不要因代码风格等非功能性问题拒绝。
- confidence ≤ 0.5 时优先判定 passed=true（不确定时按通过处理）。
- **函数签名/语义变更检查（2026-08-12 增强）**：如果 diff 修改了**已有函数**的签名、参数或语义（而非仅新增），你**不能仅凭 diff 推断"调用方已适配"**——git diff 只展示被改文件，调用方文件不在 diff 中。此时必须：
  - 明确在 reason 中标注"该变更修改了已有函数 <name> 的签名/语义，调用方兼容性**未经回归测试验证**"
  - 降低 confidence（≤0.5）
  - suggestions 中指示"需跑调用方回归测试（如 pytest 全量或相关模块测试）确认无破坏"
  - **不要**写"所有调用点已完成适配，无遗漏或回归"这类无法从 diff 验证的断言——除非调用方文件也在 diff 中且被同步修改。
"""


class _DefaultEvalStrategy:
    """默认评估策略 — 基于 LLM 的标准代码语义评估（含置信度评分）。"""
    name = "default"
    description = "基于 LLM 的标准代码语义评估（含置信度评分）"

    def __call__(self, subtask, worktree, verification, previous_attempts, config, logger):
        return _default_semantic_eval(subtask, worktree, verification, previous_attempts, config, logger)


# ═══════════════════════════════════════════════════════════════
# 视觉评估策略（M3 P1）—— 截图 + 多模态 LLM 评估 UI 是否满足需求
# ═══════════════════════════════════════════════════════════════

_VISUAL_TEMPLATE = """你是前端 UI 审查员。请根据以下任务要求和页面截图，判断页面是否满足任务要求。

## 任务信息

**标题:** {title}

**描述:**
{description}

**验证标准:**
{verification}

请从以下维度评估：
1. 页面是否渲染了任务要求的核心元素/组件
2. 布局/结构是否符合描述
3. 是否有明显的渲染错误、空白、错位

**仅回复 JSON：**
```json
{{"passed": true/false, "confidence": 0.0-1.0, "reason": "判断理由（简短）", "suggestions": "改进建议（如未通过）"}}
```"""


def _capture_screenshot(url: str, output_path: Path, config: dict, logger: logging.Logger) -> bool:
    """通过 playwright 截图。失败返回 False（fail-open）。"""
    import shutil
    import subprocess
    if not shutil.which("npx"):
        return False
    eval_cfg = config.get("evaluator", {})
    viewport = eval_cfg.get("visual_viewport", "1280,720")
    wait = eval_cfg.get("visual_wait", "2000")
    cmd = [
        "npx", "playwright", "screenshot",
        "--wait-for-timeout", str(wait),
        "--viewport-size", str(viewport),
        url, str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                cwd=str(output_path.parent))
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True
        logger.debug(f"视觉截图失败: rc={result.returncode} err={result.stderr[:200]}")
        return False
    except Exception as e:
        logger.debug(f"视觉截图异常: {e}")
        return False


def _build_visual_message(screenshot_path: Path, prompt: str, provider: str) -> list[dict]:
    """构建多模态消息（provider 适配 Anthropic/OpenAI 图片格式）。"""
    import base64
    img_data = base64.b64encode(screenshot_path.read_bytes()).decode("utf-8")
    if provider == "anthropic":
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_data}"}},
            {"type": "text", "text": prompt},
        ]
    return [{"role": "user", "content": content}]


class _VisualEvalStrategy:
    """视觉评估策略 — 截图 + 多模态 LLM 评估 UI 是否满足需求（fail-open）。"""
    name = "visual"
    description = "截图 + 多模态 LLM 视觉评估（需配置 evaluator.visual_url + playwright）"

    def __call__(self, subtask, worktree, verification, previous_attempts, config, logger):
        return _visual_eval(subtask, worktree, verification, previous_attempts, config, logger)


def _visual_eval(subtask, worktree, verification, previous_attempts, config, logger):
    """视觉评估：截图 → 多模态 LLM → 解析。无 visual_url/playwright 时 fail-open 降级为 default。"""
    start = time.time()
    eval_cfg = config.get("evaluator", {})
    url = eval_cfg.get("visual_url", "")

    # 无 URL 配置 → 降级 default 文本评估
    if not url:
        logger.debug("[visual] 未配置 evaluator.visual_url，降级 default 策略")
        return _default_semantic_eval(subtask, worktree, verification, previous_attempts, config, logger)

    # 截图（fail-open：playwright/浏览器/dev server 不可用则降级）
    import tempfile
    screenshot_path = Path(tempfile.mktemp(suffix=".png", dir=str(worktree)))
    if not _capture_screenshot(url, screenshot_path, config, logger):
        logger.warning(f"[visual] 截图失败（{url}），降级 default 策略")
        _fallback = _default_semantic_eval(subtask, worktree, verification, previous_attempts, config, logger)
        _fallback["reason"] = "[视觉评估降级] " + _fallback.get("reason", "")
        return _fallback

    try:
        # 构建 API 配置（复用 evaluator 的 provider/model 选择）
        eval_api_cfg = dict(config.get("plan_api", {}))
        # P1.5：evaluator 接入 router.roles（② 角色绑定）——router.enabled 且
        # router.roles.evaluator 配置时，优先用 router 解析（含 thinking/thinking_budget），
        # fallback 到 evaluator 配置块（现有逻辑）。
        _router_cfg = None
        try:
            from .router import resolve_role
            _router_cfg = resolve_role("evaluator", config)
        except Exception:
            _router_cfg = None
        if _router_cfg is not None:
            _p = _router_cfg.primary
            eval_api_cfg.update({
                "provider": _p.provider,
                "base_url": _p.base_url,
                "model": _p.model,
            })
            if _p.api_key:
                eval_api_cfg["api_key"] = _p.api_key
            if _p.thinking is not None:
                eval_api_cfg["thinking"] = _p.thinking
            if _p.thinking_budget is not None:
                eval_api_cfg["thinking_budget"] = _p.thinking_budget
        else:
            for key in ("provider", "model", "base_url", "api_key"):
                if eval_cfg.get(key):
                    eval_api_cfg[key] = eval_cfg[key]
        eval_api_cfg["timeout_ms"] = 90_000
        eval_config = dict(config)
        eval_config["plan_api"] = eval_api_cfg
        eval_config.pop("_metering_path", None)
        provider = eval_api_cfg.get("provider", "anthropic")

        prompt = _VISUAL_TEMPLATE.format(
            title=subtask.get("title", ""),
            description=(subtask.get("description", "") or "")[:2000],
            verification=verification or "（无验证命令）",
        )
        messages = _build_visual_message(screenshot_path, prompt, provider)

        try:
            content = call_api(eval_config, messages, logger)
        except Exception as e:
            logger.warning(f"[visual] 视觉评估 API 调用失败: {e}")
            return {
                "passed": True, "confidence": 0.5,
                "reason": f"视觉评估 API 失败（已跳过）: {e}",
                "suggestions": "", "cost_usd": 0.0,
                "latency_ms": round((time.time() - start) * 1000, 2),
                "evaluator_skipped": True,
            }

        parsed = _parse_eval_response(content)
        latency_ms = round((time.time() - start) * 1000, 2)
        cost_usd = 0.0
        try:
            cost_usd = estimate_cost(
                eval_api_cfg.get("provider", "anthropic"),
                eval_api_cfg.get("model", ""),
                max(1, _estimate_tokens(prompt) + 1000),  # 图片 token 粗估
                max(1, _estimate_tokens(content)),
            )
        except Exception:
            pass
        return {
            "passed": parsed.get("passed", True),
            "confidence": parsed.get("confidence", 0.8 if parsed.get("passed", True) else 0.2),
            "reason": "[视觉] " + parsed.get("reason", ""),
            "suggestions": parsed.get("suggestions", ""),
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "visual_url": url,
        }
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass


# 注册视觉策略
register(_VisualEvalStrategy())


def _default_semantic_eval(subtask, worktree, verification, previous_attempts, config, logger):
    """内置评估逻辑：调用 LLM → 解析响应 → 写 metering → 返回。"""
    start = time.time()
    evaluator_cfg = config.get("evaluator", {})

    # P0 provider 降级链/仲裁：router.roles.evaluator 可声明
    # fallback/fallbacks（例如 K3 → GLM → v4-pro → local）。没有降级链时
    # 保持现有 call_api 路径，避免改变旧配置的行为与 R8 归因。
    _evaluator_route = None
    try:
        from .router import resolve_role
        _evaluator_route = resolve_role("evaluator", config)
    except Exception:
        _evaluator_route = None

    def _provider_dict(provider_config) -> dict:
        return {
            "provider": provider_config.provider,
            "base_url": provider_config.base_url,
            "model": provider_config.model,
            "api_key": provider_config.api_key,
            "key_ref": getattr(provider_config, "key_ref", ""),
            "max_tokens": provider_config.max_tokens,
            "temperature": provider_config.temperature,
            "timeout_ms": provider_config.timeout_ms,
            "thinking": provider_config.thinking,
            "thinking_budget": provider_config.thinking_budget,
            "json_output": provider_config.json_output,
            "reasoning_effort": provider_config.reasoning_effort,
        }

    # 构建评估用 API 配置；router 角色优先，旧 evaluator/plan_api 作为 fallback。
    if _evaluator_route is not None:
        eval_api_cfg = _provider_dict(_evaluator_route.primary)
    else:
        eval_api_cfg = dict(config.get("plan_api", {}))
        for key in ("provider", "model", "base_url", "api_key"):
            if evaluator_cfg.get(key):
                eval_api_cfg[key] = evaluator_cfg[key]
    # 评估器 API 调用独立超时：不绑定 plan_api 的 timeout_ms（可能长达 3min）
    # 评估只需快速判断 pass/fail，90s 足够
    eval_api_cfg["timeout_ms"] = 90_000

    eval_config = dict(config)
    eval_config["plan_api"] = eval_api_cfg
    eval_config.pop("_metering_path", None)

    diff = _get_worktree_diff(worktree, base_commit=config.get("_base_commit", ""))
    no_truncation = bool(config.get("_no_diff_truncation"))
    prompt = _build_eval_prompt(subtask, verification, diff, previous_attempts, no_truncation)
    messages = [{"role": "user", "content": prompt}]

    _route_metering = None
    try:
        _has_fallback_chain = bool(_evaluator_route and len(_evaluator_route.providers()) > 1)
        if _has_fallback_chain:
            from .config import get_api_key
            from .router import call_with_role
            content, _route_metering = call_with_role(
                _evaluator_route, messages, get_api_key(eval_config), logger,
                task_id=config.get("_task_id", ""),
                subtask_id=subtask.get("id", ""),
                metering_path=None, config=eval_config,
            )
        else:
            content = call_api(eval_config, messages, logger, role="evaluator")
    except Exception as e:
        logger.warning(f"语义评估 API 调用失败: {e}")
        return {
            "passed": False,
            "confidence": 0.0,
            "reason": f"语义评估 API 调用失败无法执行: {e}",
            "suggestions": "",
            "cost_usd": 0.0,
            "latency_ms": round((time.time() - start) * 1000, 2),
            "raw_response": "",
            "evaluator_skipped": True,
        }

    parsed = _parse_eval_response(content)

    # P0 低置信度复核：primary 给出低置信度/不确定结论时，用降级链的下一个
    # evaluator 复核一次。高置信度结果不重复调用，避免正常任务成本翻倍。
    _arb_cfg = evaluator_cfg.get("arbitration", {}) or {}
    _arb_enabled = bool(
        _arb_cfg.get("enabled", bool(_evaluator_route and len(_evaluator_route.providers()) > 1))
    )
    _arb_threshold = float(_arb_cfg.get("confidence_threshold", 0.5) or 0.5)
    _arb_route = None
    if _arb_enabled and _evaluator_route is not None:
        _candidates = _evaluator_route.providers()[1:]
        if _candidates:
            from .router import RoleRoute
            _arb_route = RoleRoute(
                role="evaluator", primary=_candidates[0], fallbacks=tuple(_candidates[1:]))
    _parse_uncertain = "无法解析" in str(parsed.get("reason", ""))
    if _arb_route is not None and (
        parsed.get("confidence", 0.0) <= _arb_threshold or _parse_uncertain
    ):
        try:
            from .config import get_api_key
            from .router import call_with_role
            _arb_content, _arb_metering = call_with_role(
                _arb_route, messages, get_api_key(eval_config), logger,
                task_id=config.get("_task_id", ""),
                subtask_id=subtask.get("id", ""),
                metering_path=None, config=eval_config,
            )
            _arb_parsed = _parse_eval_response(_arb_content)
            _primary_score = float(parsed.get("confidence", 0.0) or 0.0)
            _arb_score = float(_arb_parsed.get("confidence", 0.0) or 0.0)
            # 复核有更高置信度，或将低置信度 false 改为高置信度 true 时采用。
            if _arb_score > _primary_score or (
                not parsed.get("passed", False) and _arb_parsed.get("passed", False)
            ):
                parsed = _arb_parsed
                content = _arb_content
                _route_metering = _arb_metering
                logger.info(
                    "evaluator 低置信度复核采用 fallback: model=%s confidence=%.2f",
                    _arb_metering.get("actual_model", ""), _arb_score,
                )
        except Exception as _arb_err:
            logger.warning("evaluator 低置信度复核失败（保留 primary 结果）: %s", _arb_err)
    latency_ms = round((time.time() - start) * 1000, 2)

    est_prompt_tokens = max(1, _estimate_tokens(prompt))
    est_completion_tokens = max(1, _estimate_tokens(content))
    cost_usd = 0.0
    try:
        cost_usd = estimate_cost(
            (_route_metering or {}).get("actual_provider", eval_api_cfg.get("provider", "anthropic")),
            (_route_metering or {}).get("actual_model", eval_api_cfg.get("model", "")),
            est_prompt_tokens, est_completion_tokens,
        )
    except Exception:
        pass

    logger.info(f"语义评估: passed={parsed['passed']} confidence={parsed['confidence']:.2f} reason={parsed['reason'][:80]}")

    # 写 metering（仅成本记录）
    _ev = {
        "role": "evaluator",
        "virtual_model": "agentgo-evaluator",
        "actual_provider": (_route_metering or {}).get("actual_provider", eval_api_cfg.get("provider", "anthropic")),
        "actual_model": (_route_metering or {}).get("actual_model", eval_api_cfg.get("model", "")),
        "difficulty": subtask.get("difficulty", ""),
        "prompt_tokens": est_prompt_tokens,
        "completion_tokens": est_completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "result": "success" if parsed["passed"] else "quality_fail",
        "fallback_reason": "",
        "task_id": config.get("_task_id", ""),
        "subtask_id": subtask.get("id", ""),
    }
    # R8 路由归因（call_api 共享）：evaluator 经代理时按真实后端模型/route_target 纠正
    # actual_model 与 is_local（force_fallback 回退云端不再误判 local；代理声称本地实走云端被覆盖）。
    # 注意：call_api 把 R8 写入 eval_config（dict(config) 浅拷贝的对象），须读 eval_config 而非 config。
    _ra = eval_config.get("_route_attribution") or {}
    if _ra.get("route_target"):
        _ev["route_target"] = _ra["route_target"]
        if _ra.get("route_actual_model"):
            _ev["route_actual_model"] = _ra["route_actual_model"]
            _ev["actual_model"] = _ra["route_actual_model"]
        _ev["route_reason"] = _ra.get("route_reason", "")
        _ev["is_local"] = _ra["route_target"] == "local"
    meter_event(config.get("_metering_path"), _ev)

    return {
        "passed": parsed["passed"],
        "confidence": parsed["confidence"],
        "reason": parsed["reason"],
        "suggestions": parsed["suggestions"],
        "cost_usd": round(cost_usd, 6),
        "latency_ms": latency_ms,
        "raw_response": content,
    }


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _build_eval_prompt(subtask, verification, diff, previous_attempts, no_diff_truncation=False):
    """构建语义评估 prompt（含 confidence 要求）。
    
    no_diff_truncation=True 时禁用 diff 长度截断，用于截断兜底重试。"""
    title = subtask.get("title", "")
    description = subtask.get("description", "")[:2000]
    agent_prompt = subtask.get("agent_prompt", "")[:1000]

    history_section = ""
    if previous_attempts:
        history_parts = []
        for a in previous_attempts:
            history_parts.append(
                f"- 第 {a.get('attempt', '?')} 次: {a.get('fix_summary', '修复尝试')} "
                f"→ 失败原因: {a.get('failure_summary', '未知')}"
            )
        history_section = "## 历史修复尝试\n" + "\n".join(history_parts) + "\n\n"

    if not diff.strip():
        diff = "（未检测到文件变更）"
    else:
        max_chars = None if no_diff_truncation else 12000
        diff = _format_diff_for_eval(diff, max_chars)

    return _EVAL_TEMPLATE.format(
        title=title, description=description, agent_prompt=agent_prompt,
        verification=verification, diff=diff, history_section=history_section,
    )


def _parse_eval_response(content: str) -> dict:
    """从 LLM 响应中解析 JSON 评估结果（支持 confidence 字段）。"""
    # 直接解析
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "passed" in data:
            return _extract(data)
    except json.JSONDecodeError:
        pass

    # 从 markdown code block 提取
    matches = re.findall(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
    for m in matches:
        try:
            data = json.loads(m)
            if isinstance(data, dict) and "passed" in data:
                return _extract(data)
        except json.JSONDecodeError:
            continue

    return {
        "passed": False,
        "confidence": 0.0,
        "reason": f"评估响应无法解析为 JSON，原始响应: {content[:200]}",
        "suggestions": "",
    }


def _extract(data: dict) -> dict:
    """从解析后的 dict 中提取标准字段。"""
    passed = bool(data.get("passed", False))
    return {
        "passed": passed,
        "confidence": float(data.get("confidence", 1.0 if passed else 0.0)),
        "reason": str(data.get("reason", "")),
        "suggestions": str(data.get("suggestions", "")),
    }


def _estimate_tokens(text: str) -> int:
    """估算混合中英文文本的 token 数。

    中文 ~1 char/token，非中文 ~4 chars/token。
    用于 metering 中 evaluator 的 token 估算（无真实 API usage 时兜底）。
    """
    import re
    cjk = re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', text)
    cjk_count = len(cjk)
    ascii_count = len(text) - cjk_count
    return cjk_count + max(0, ascii_count // 4)


def _get_worktree_diff(worktree: Path, base_commit: str = "") -> str:
    """获取 worktree 中的变更 diff。

    累积语义（ISSUE：修复分多次 commit 时语义评估误判）：
    1. 工作区未提交 → `git diff HEAD`（未提交变更）
    2. 工作区干净 → 优先用显式 base_commit（子任务基座，真实仓库有效）
       → 否则 `git diff <root>..HEAD`（根 commit 累积 diff，fixture 有效）。
    修复分多次 commit 时，评估器能看到此前已提交的签名/结构修改。
    """
    import subprocess
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    diff = result.stdout
    if not diff.strip():
        base = base_commit.strip()
        if base:
            result = subprocess.run(
                ["git", "diff", f"{base}..HEAD", "--no-renames"],
                cwd=str(worktree), capture_output=True, text=True,
            )
            diff = result.stdout
        if not diff.strip():
            # 定位根 commit（无 parent）作为 base，取累积 diff
            root_result = subprocess.run(
                ["git", "rev-list", "--max-parents=0", "HEAD"],
                cwd=str(worktree), capture_output=True, text=True,
            )
            if root_result.returncode == 0 and root_result.stdout.strip():
                root_base = root_result.stdout.strip().splitlines()[0]
                result = subprocess.run(
                    ["git", "diff", f"{root_base}..HEAD", "--no-renames"],
                    cwd=str(worktree), capture_output=True, text=True,
                )
                diff = result.stdout
    return diff if result.returncode == 0 else ""


def _format_diff_for_eval(diff: str, max_chars: int = 12000) -> str:
    """将 git diff 格式化为 evaluator 可消费的文本。

    替代硬截断 4000 字符（导致 evaluator 反馈"diff 截断，无法确认"）：
    - max_chars=None：不截断，返回完整 diff（截断兜底重试用）
    - ≤ max_chars：直接返回完整 diff
    - > max_chars：保留文件级 hunk 头部 + 每个文件的关键片段，尾部标注截断
    """
    if max_chars is None or len(diff) <= max_chars:
        return diff

    lines = diff.split("\n")
    result: list[str] = []
    # 使用 diff 的内部结构：以 "diff --git" 和 "---" / "+++" 为文件边界
    files = _split_diff_by_file(lines)

    budget = max_chars - 200  # 为尾部摘注留空间
    for fname, file_lines in files:
        # 每个文件最少保留：diff --git 行 + hunk 头部
        header_block = _extract_diff_header(file_lines)
        result.extend(header_block)
        budget -= sum(len(l) + 1 for l in header_block)  # +1 for newline

        if budget <= 0:
            break

        # 在剩余预算内尽可能多地保留 hunk 内容
        # 策略：保留前 N 个 hunk 直到预算耗尽；至少保一个完整 hunk
        hunks = _split_diff_hunks(file_lines)
        added_any = False
        for hunk in hunks:
            hunk_size = sum(len(l) + 1 for l in hunk)
            if budget >= hunk_size or not added_any:
                result.extend(hunk)
                budget -= hunk_size
                added_any = True
            else:
                result.append(f"... ({fname}: 省略 {len(hunks) - len([h for h in hunks if h[0] in result])} 个 hunk)")
                budget = 0
                break

    if budget > 0:
        result.append(f"\n(完整 diff 共 {len(diff)} 字符，以上为文件摘要 + 关键 hunks)")
    else:
        result.append(f"\n(完整 diff 共 {len(diff)} 字符，以上为预算内截断)")

    return "\n".join(result)


def _split_diff_by_file(lines: list[str]) -> list[tuple[str, list[str]]]:
    """将 diff 按文件分组。返回 [(文件名, 该文件的diff行)]。"""
    files: list[tuple[str, list[str]]] = []
    current_file = "<unknown>"
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if current_lines:
                files.append((current_file, current_lines))
            current_file = line  # "diff --git a/... b/..."
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        files.append((current_file, current_lines))
    return files


def _extract_diff_header(file_lines: list[str]) -> list[str]:
    """提取 diff 头部行（diff --git / --- / +++ / hunk 的第一行 @@）。"""
    header = []
    for line in file_lines:
        if line.startswith("diff --git") or line.startswith("--- ") or line.startswith("+++ "):
            header.append(line)
        elif line.startswith("@@ "):
            header.append(line)
            break  # 第一个 @@ 后是 hunk 内容
    return header


def _split_diff_hunks(file_lines: list[str]) -> list[list[str]]:
    """将单个文件的 diff 行按 hunk 拆分。每个 hunk 以 @@ 行开头。"""
    hunks = []
    current: list[str] = []
    for line in file_lines:
        if line.startswith("@@ "):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    return hunks


# 注册默认策略
register(_DefaultEvalStrategy())
