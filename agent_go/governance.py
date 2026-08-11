"""M1.4 SDD 最小治理闭环：规范可追踪 + 架构可审查。

三个能力（全部 fail-open，不阻塞核心执行）：

1. M1-4 Spec 追踪：从任务描述提取稳定 requirement/acceptance criterion ID
   （REQ-xxx / AC-xxx 前缀），并与 Plan step 引用的 ID 对照，输出覆盖率。
2. M1-5 架构审查：执行前生成最小 Architecture Decision（边界 / 依赖方向 /
   关键约束），由独立 LLM 审查产生 approved / rejected / changes_requested
   决策。结果持久化到 meta.architecture_review。
3. M1-6 追踪输出：从 meta 生成任务级 traceability_matrix（requirement →
   subtask → verification → delivery）与 architecture_compliance 摘要。

设计约束：
- 治理是增强而非门禁：architecture_review 默认 enabled=false，不改变默认成功语义。
- 所有外部调用（LLM / 读仓库）均 try/except 容错，失败返回 None / 降级值。
- 追踪判定是确定性的（纯函数），只有架构审查需要 LLM。
"""

import json
import logging
import re
from typing import Any, Optional

__all__ = [
    "extract_spec_requirements",
    "architecture_review",
    "build_traceability_matrix",
    "assess_traceability",
]

logger = logging.getLogger(__name__)

# 稳定 ID 前缀约定（与 api.py plan prompt 中 requirement_ids / acceptance_criteria_ids 一致）
_REQ_ID = re.compile(r"\b(?:REQ-(\w+)|req[-_]?(\w+))\b")
_AC_ID = re.compile(r"\b(?:AC-(\w+)|ac[-_]?(\w+))\b")

# 架构审查 Prompt 模板
_ARCH_REVIEW_TEMPLATE = """你是一位独立的架构审查 agent。请审查以下任务的执行计划是否满足**最小架构约束**。

## 任务描述

{task}

## 执行计划（Plan steps）

{steps}

## 你的输出格式（严格 JSON）

```json
{{
  "decision": "approved | rejected | changes_requested",
  "summary": "一句话结论",
  "boundaries": ["本任务允许修改的边界，每条一句话"],
  "dependency_direction": ["关键依赖方向约束，每条一句话"],
  "constraints": ["关键约束，如 do_not_touch、接口稳定、性能底线"],
  "risks": ["风险项，最多 3 条，无则空数组"]
}}
```

判定规则：
- 计划改动仅限任务要求范围内、无越界文件 → approved。
- 计划遗漏关键需求 / 依赖方向错误 → rejected。
- 计划方向正确但需补充约束或调整边界 → changes_requested。
"""


def _canonical_id(raw: str) -> str:
    """将用户可能的 ID 写法归一化为 REQ-xxx / AC-xxx 规范形式（编号补零到 3 位）。"""
    s = str(raw).strip()
    if re.match(r"^(REQ|AC)-\w+$", s):
        return s
    m = re.match(r"^(?:req|ac)[-_]?(\w+)$", s, re.IGNORECASE)
    if m:
        prefix = "REQ" if s.lower().startswith("req") else "AC"
        num = m.group(1)
        if num.isdigit():
            num = num.zfill(3)
        return f"{prefix}-{num}"
    return s


def extract_spec_requirements(task_text: str) -> dict[str, Any]:
    """从任务描述中提取稳定需求 ID 与验收标准 ID。

    纯函数，不调用外部服务。支持 REQ-001 / AC-001 / req1 / ac2 等写法。

    Returns:
        {"requirement_ids": [...], "acceptance_criteria_ids": [...], "count": int}
    """
    task_text = task_text or ""
    req_ids = sorted({
        _canonical_id(m[0] if m[0] else (m[1] if m[1] else ""))
        for m in _REQ_ID.finditer(task_text)
    })
    ac_ids = sorted({
        _canonical_id(m[0] if m[0] else (m[1] if m[1] else ""))
        for m in _AC_ID.finditer(task_text)
    })

    # 兜底：未显式编号时，从「要求 / 需求 / 必须 / 验收」等条款式描述提取序号
    if not req_ids:
        numbered = re.findall(
            r"^\s*(?:[0-9]+[.)、]\s*|[-*]\s*)\s*(要求|需求|必须|应)\s*[:：]?\s*\S+",
            task_text, re.M)
        if numbered:
            req_ids = [f"REQ-{i:03d}" for i in range(1, len(numbered) + 1)]

    if not ac_ids:
        numbered_ac = re.findall(
            r"^\s*(?:[0-9]+[.)、]\s*|[-*]\s*)\s*(验收|验证)\s*(?:标准|条件)?\s*[:：]?\s*\S+",
            task_text, re.M)
        if numbered_ac:
            ac_ids = [f"AC-{i:03d}" for i in range(1, len(numbered_ac) + 1)]

    return {
        "requirement_ids": req_ids,
        "acceptance_criteria_ids": ac_ids,
        "count": len(req_ids) + len(ac_ids),
    }


def _format_steps_for_review(subtasks: list[dict]) -> str:
    """将 subtasks 格式化为架构审查可读的文本。"""
    lines = []
    for st in subtasks or []:
        sid = st.get("id", "?")
        title = st.get("title", "")
        files = st.get("files", []) or st.get("files_hint", []) or []
        scope = st.get("scope_boundary", "") or ""
        dnt = st.get("do_not_touch", []) or []
        deps = st.get("depends_on", []) or []
        reqs = st.get("requirement_ids", []) or []
        lines.append(
            f"- **{sid}** {title}\n"
            f"  files: {', '.join(map(str, files)) or '-'}\n"
            f"  depends_on: {', '.join(map(str, deps)) or '-'}\n"
            f"  scope: {scope or '-'}\n"
            f"  do_not_touch: {', '.join(map(str, dnt)) or '-'}\n"
            f"  requirements: {', '.join(map(str, reqs)) or '-'}"
        )
    return "\n".join(lines) or "-"


def _load_review_config(config: dict) -> dict:
    """读取 architecture_review 配置段，兼容扁平与嵌套两种写法。"""
    cfg = config.get("architecture_review") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return cfg


def architecture_review(task: str, subtasks: list[dict], config: dict,
                        logger: logging.Logger) -> Optional[dict]:
    """执行前架构审查：生成最小 Architecture Decision 并产生决策。

    fail-open：配置未启用 / LLM 失败 / 解析失败均返回 None，不阻断主流程。

    Returns:
        {"decision": str, "summary": str, "boundaries": [...],
         "dependency_direction": [...], "constraints": [...], "risks": [...]}
    """
    cfg = _load_review_config(config)
    if not cfg.get("enabled", False):
        return None

    try:
        from .api import call_api
    except Exception as _ie:
        logger.debug(f"[governance] 架构审查依赖加载失败，跳过: {_ie}")
        return None

    steps_text = _format_steps_for_review(subtasks)
    prompt = _ARCH_REVIEW_TEMPLATE.format(task=task or "", steps=steps_text)

    # 独立 API 配置：architecture_review > plan_api（与 readonly_review 模式一致）
    api_cfg = dict(config.get("plan_api", {}) or {})
    for key, src in (("model", cfg), ("provider", cfg), ("base_url", cfg)):
        if src.get(key):
            api_cfg[key] = src[key]

    messages = [{"role": "user", "content": prompt}]
    call_cfg = dict(config or {})
    call_cfg["plan_api"] = api_cfg
    call_cfg.pop("planner_api", None)

    try:
        content = call_api(call_cfg, messages, logger)
        parsed = _extract_json_decision(content)
        if parsed is None:
            logger.debug("[governance] 架构审查输出无法解析为 JSON，跳过")
            return None
        parsed["_source"] = "llm"
        return parsed
    except Exception as _le:
        logger.warning(f"[governance] 架构审查失败，跳过（不阻断）: {_le}")
        return None


def _extract_json_decision(raw: str) -> Optional[dict]:
    """从 LLM 输出中提取决策 JSON（容忍围栏与前后缀文本）。"""
    if not raw:
        return None
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    decision = str(parsed.get("decision", "")).strip().lower()
    if decision not in ("approved", "rejected", "changes_requested"):
        return None
    return {
        "decision": decision,
        "summary": str(parsed.get("summary", "")),
        "boundaries": list(parsed.get("boundaries", []) or []),
        "dependency_direction": list(parsed.get("dependency_direction", []) or []),
        "constraints": list(parsed.get("constraints", []) or []),
        "risks": list(parsed.get("risks", []) or []),
    }


def assess_traceability(meta: dict) -> dict[str, Any]:
    """判定任务追踪完整性：requirement → subtask → verification → delivery。

    纯函数。缺少 requirement/acceptance criterion 映射的任务标记为
    `traceability_incomplete`（而不是静默通过）。

    Returns:
        {
          "status": "complete" | "incomplete" | "no_spec_ids",
          "requirement_count": int,
          "covered_requirement_ids": [...],
          "missing_requirement_ids": [...],
          "unmapped_subtask_ids": [...],
          "verification_coverage": float,
          "delivery_coverage": bool,
          "issues": [...]
        }
    """
    issues: list[str] = []
    subtasks = meta.get("subtasks") or []
    plan_quality = meta.get("plan_quality") or {}

    # 1. Spec 级需求 ID（从 plan_quality 或 meta 提取）
    spec_req_ids = _meta_spec_ids(meta, plan_quality)

    # 2. Plan 覆盖的需求 ID
    covered: set[str] = set()
    unmapped: list[str] = []
    verification_ok = 0
    for st in subtasks:
        sid = str(st.get("id", ""))
        st_reqs = [str(v) for v in (st.get("requirement_ids", []) or [])]
        st_acs = [str(v) for v in (st.get("acceptance_criteria_ids", []) or [])]
        st_ids = set(st_reqs) | set(st_acs)
        covered |= st_ids
        if spec_req_ids and not st_ids:
            unmapped.append(sid)
        vres = st.get("verification_results") or []
        if isinstance(vres, list):
            passed = [v for v in vres if isinstance(v, dict) and v.get("passed")]
            verification_ok += len(passed)

    missing = sorted(spec_req_ids - covered) if spec_req_ids else []
    if missing:
        issues.append(f"未覆盖的需求/验收 ID: {', '.join(missing)}")
    if unmapped:
        issues.append(f"无需求映射的子任务: {', '.join(unmapped)}")

    # 3. 验证覆盖率（有验证命令的子任务比例）
    total = len(subtasks)
    with_verification = sum(
        1 for st in subtasks if str(st.get("verification", "") or "").strip())
    verification_coverage = round(with_verification / total, 4) if total else 1.0
    if total and with_verification < total:
        issues.append(f"验证覆盖不完整: {with_verification}/{total} 个子任务有验证命令")

    # 4. 交付覆盖（delivery branch 或 PR 存在）
    delivery_branch = meta.get("delivery_branch") or ""
    pr_url = meta.get("pr_url") or ""
    explicit_merge = meta.get("explicit_merge_commit") or ""
    delivery_coverage = bool(delivery_branch or pr_url or explicit_merge)
    if not delivery_coverage:
        issues.append("无交付记录（delivery_branch / pr_url / explicit_merge_commit 均缺失）")

    if not spec_req_ids:
        status = "no_spec_ids"
    elif not issues:
        status = "complete"
    else:
        status = "incomplete"

    return {
        "status": status,
        "requirement_count": len(spec_req_ids),
        "covered_requirement_ids": sorted(covered),
        "missing_requirement_ids": missing,
        "unmapped_subtask_ids": unmapped,
        "verification_coverage": verification_coverage,
        "delivery_coverage": delivery_coverage,
        "issues": issues,
    }


def _meta_spec_ids(meta: dict, plan_quality: dict) -> set[str]:
    """从 meta 与 plan_quality 提取任务级需求 ID 集合。"""
    ids: set[str] = set()
    for key in ("requirement_ids", "acceptance_criteria_ids", "requirements"):
        for val in (plan_quality.get(key) or []):
            ids.add(str(val))
        for val in (meta.get(key) or []):
            ids.add(str(val))
    return {_canonical_id(v) for v in ids}


def build_traceability_matrix(meta: dict) -> dict[str, Any]:
    """从 meta 生成任务级 traceability_matrix 与 architecture_compliance 摘要。

    纯函数。输出用于 CLI / MCP / 报告展示。

    Returns:
        {
          "traceability": {"requirement": [...], "subtask": [...], "verification": [...], "delivery": {...}},
          "architecture_compliance": {...},
          "assessment": {...}
        }
    """
    subtasks = meta.get("subtasks") or []
    matrix: dict[str, Any] = {"requirements": [], "subtasks": []}

    # requirement → 覆盖它的 subtask
    req_to_subtask: dict[str, list[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        for rid in (st.get("requirement_ids", []) or []):
            req_to_subtask.setdefault(_canonical_id(str(rid)), []).append(sid)
        for ac in (st.get("acceptance_criteria_ids", []) or []):
            req_to_subtask.setdefault(_canonical_id(str(ac)), []).append(sid)

    for rid in sorted(req_to_subtask):
        matrix["requirements"].append({
            "id": rid,
            "subtasks": sorted(set(req_to_subtask[rid])),
        })

    for st in subtasks:
        vres = st.get("verification_results") or []
        matrix["subtasks"].append({
            "id": str(st.get("id", "")),
            "title": st.get("title", ""),
            "requirements": sorted(
                {_canonical_id(str(v)) for v in (st.get("requirement_ids", []) or [])}
                | {_canonical_id(str(v)) for v in (st.get("acceptance_criteria_ids", []) or [])}),
            "verification": str(st.get("verification", "") or ""),
            "verification_passed": bool(
                vres and any(isinstance(v, dict) and v.get("passed") for v in vres)),
            "commit_hash": st.get("commit_hash", "") or "",
        })

    matrix["delivery"] = {
        "delivery_branch": meta.get("delivery_branch", "") or "",
        "pr_url": meta.get("pr_url", "") or "",
        "explicit_merge_commit": meta.get("explicit_merge_commit", "") or "",
        "accepted_delivery": bool(meta.get("accepted_delivery")),
    }

    arch_review = meta.get("architecture_review")
    if isinstance(arch_review, dict):
        arch_compliance = {
            "reviewed": True,
            "decision": arch_review.get("decision", ""),
            "summary": arch_review.get("summary", ""),
            "constraints": list(arch_review.get("constraints", []) or []),
            "risks": list(arch_review.get("risks", []) or []),
        }
    else:
        arch_compliance = {
            "reviewed": False,
            "decision": "not_reviewed",
            "summary": "未执行架构审查（architecture_review.enabled=false 或审查失败）",
            "constraints": [],
            "risks": [],
        }

    assessment = assess_traceability(meta)

    return {
        "traceability": matrix,
        "architecture_compliance": arch_compliance,
        "assessment": assessment,
    }
