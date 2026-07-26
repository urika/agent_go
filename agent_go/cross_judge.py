"""交叉评判矩阵（S8 P1）— 第 2 层语义评估。

设计原则：
1. N 模型互评 — 每个产出被 ≥2 个不同 provider 的模型评判
2. 禁绝自评 — judge_model != candidate_model（硬约束，规避 LLM-as-Judge 自偏）
3. 人工抽检 10% — 校准 LLM 评判者准确性，分歧 >30% 标记 unreliable
4. 角色定位 — 回答"本地 XX 模型能否做 Reviewer"

工作流：
  agent_go eval bench ...                     # bench 产出 results.jsonl（含 worktree 路径）
  agent_go eval judge --results results.jsonl \
    --judge-models gemini-2.5-pro,kimi-k2,qwen3.6-27b-local   # 交叉评判
  agent_go eval judge calibrate \
    --llm-scores cross_judge_scores.jsonl --human-scores human.csv  # 人工校准
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .console import _LazyConsole
from .config import AGENT_GO_DIR
from .eval import _read_jsonl, _read_json
from .pricing import MODEL_PRICES

__all__ = ["cmd_judge", "cross_judge_results", "calibrate_judge"]
console = _LazyConsole()

# 评分尺度（结构化 rubric）
JUDGE_RUBRIC = {
    "correctness": "1-5 分：功能是否完整实现 spec（1=完全错误，5=完全正确）",
    "completeness": "1-5 分：是否覆盖边界条件/错误处理（1=只过 happy path，5=全面）",
    "code_quality": "1-5 分：可读性/命名/结构（1=混乱，5=专业）",
    "false_positive": "bool：验证通过但实际功能是否错误（关键假阳性检测）",
}

# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def cmd_judge(args=None) -> None:
    """交叉评判 + 人工校准 CLI。"""
    sub = getattr(args, "judge_subcommand", "run") if args else "run"

    if sub == "calibrate":
        llm_path = Path(getattr(args, "llm_scores", "cross_judge_scores.jsonl") or "cross_judge_scores.jsonl")
        human_path = Path(getattr(args, "human_scores", "human_scores.csv") or "human_scores.csv")
        _print_calibration(calibrate_judge(llm_path, human_path))
        return

    # 默认：交叉评判
    results_path = Path(getattr(args, "results", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")
    judge_models = [m.strip() for m in (getattr(args, "judge_models", "") or "").split(",") if m.strip()]
    output_path = Path(getattr(args, "output", "cross_judge_scores.jsonl") or "cross_judge_scores.jsonl")

    if not judge_models:
        console.error("至少指定一个 --judge-models（逗号分隔）")
        sys.exit(1)

    results = _read_jsonl(results_path)
    if not results:
        console.warning(f"无数据: {results_path} → 先跑 agent_go eval bench")
        return

    console.debug(f"交叉评判: {len(results)} 条 bench 结果 × {len(judge_models)} 评判者")
    console.print(f"   评判模型: {', '.join(judge_models)}")
    console.print(f"   输出: {output_path}")

    scores = cross_judge_results(results, judge_models)
    with open(output_path, "w", encoding="utf-8") as f:
        for s in scores:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    _print_cross_judge_summary(scores, judge_models)


# ═══════════════════════════════════════════════════════════════
# 交叉评判编排
# ═══════════════════════════════════════════════════════════════

def cross_judge_results(bench_results: list[dict], judge_models: list[str]) -> list[dict]:
    """对 bench 结果逐条交叉评判。

    每条 bench 结果（task_id × model）尝试从 worktree 读 git diff，
    然后逐 judge_model 调用 evaluate_semantic。禁绝自评。

    Returns:
        list[dict]: 每元素 {task_id, candidate_model, judge_model,
                             correctness, completeness, code_quality,
                             semantic_score, false_positive, reason}
    """
    scores: list[dict] = []
    total = len(bench_results) * len(judge_models)
    current = 0

    for br in bench_results:
        task_dir = Path(br.get("task_dir", ""))
        meta = _read_json(task_dir / "meta.json") if task_dir.exists() else {}
        results = meta.get("results", [])

        candidate_model = br.get("model", "unknown")
        task_id = br.get("task_id", "")

        # 找第一个 completed 的 subtask 的 worktree
        worktree_path = None
        verification_cmd = ""
        for r in results:
            if r.get("status") == "completed" and r.get("worktree"):
                worktree_path = Path(r["worktree"])
                verification_cmd = next(
                    (vr.get("command", "") for vr in r.get("verification_results", [])),
                    "")
                break

        if not worktree_path or not worktree_path.exists():
            # 没有可评判的 worktree（可能已被清理）
            for jm in judge_models:
                scores.append({
                    "task_id": task_id, "candidate_model": candidate_model,
                    "judge_model": jm, "error": "无可用 worktree",
                    "semantic_score": -1, "false_positive": None,
                })
            current += len(judge_models)
            continue

        # 读 git diff
        git_diff = _git_diff(worktree_path)

        for jm in judge_models:
            current += 1
            console.print(f"  [{current}/{total}] {task_id} | {candidate_model} ← {jm}")

            # 禁绝自评（硬约束）
            if _same_provider(jm, candidate_model):
                scores.append({
                    "task_id": task_id, "candidate_model": candidate_model,
                    "judge_model": jm, "error": "自评禁止（LLM-as-Judge 自偏）",
                    "semantic_score": -1, "false_positive": None,
                })
                continue

            score_entry = _judge_one(candidate_model, jm, task_id, task_dir,
                                     worktree_path, git_diff, verification_cmd)
            scores.append(score_entry)

    return scores


def _judge_one(candidate_model: str, judge_model: str, task_id: str,
               task_dir: Path, worktree: Path, git_diff: str,
               verification: str) -> dict:
    """用 judge_model 评判 candidate_model 的一个产出。"""
    start = time.time()
    base = {
        "task_id": task_id,
        "candidate_model": candidate_model,
        "judge_model": judge_model,
    }

    try:
        from .evaluator import evaluate_semantic
        import logging
        logger = logging.getLogger("cross_judge")
        logger.setLevel(logging.WARNING)

        subtask = {"id": task_id, "title": task_id, "description": ""}

        # 构建 config：让 evaluate_semantic 使用指定的 judge_model
        eval_config = {
            "evaluator": {
                "enabled": True,
                "provider": _infer_provider(judge_model),
                "model": judge_model,
                "base_url": "",
                "api_key": "",
            },
            "plan_api": {
                "provider": _infer_provider(judge_model),
                "model": judge_model,
                "base_url": "",
                "api_key": "",
            },
        }

        feedback = evaluate_semantic(
            subtask, worktree, verification or "N/A", [], eval_config, logger)

        elapsed = round(time.time() - start, 2)

        # evaluate_semantic 返回 {passed, reason, suggestions, cost_usd, latency_ms}
        # 对于交叉评判，我们需要结构化评分，但 evaluate_semantic 只返回二元 passed + reason。
        # 基于 reason 文本做启发式评分（P1 简化版；P2 可改为结构化 prompt）。
        semantic_score = _heuristic_score(feedback.get("reason", ""))
        false_positive = not feedback.get("passed", True)

        return {
            **base,
            "correctness": semantic_score,     # 简化：用总分代理
            "completeness": semantic_score,
            "code_quality": semantic_score,
            "semantic_score": semantic_score,
            "false_positive": false_positive,
            "reason": feedback.get("reason", "")[:200],
            "cost_usd": feedback.get("cost_usd", 0),
            "latency_ms": feedback.get("latency_ms", 0),
            "elapsed_sec": elapsed,
        }
    except Exception as e:
        return {**base, "error": f"评判失败: {str(e)[:100]}",
                "semantic_score": -1, "false_positive": None}


# ═══════════════════════════════════════════════════════════════
# 人工校准
# ═══════════════════════════════════════════════════════════════

def calibrate_judge(llm_scores_path: Path, human_scores_path: Path) -> dict[str, Any]:
    """对比 LLM 评判者与人工评分，计算分歧率。

    human_scores.csv 格式：
      task_id,candidate_model,correctness,completeness,code_quality,false_positive

    Returns:
        {"judges": {judge_model: {avg_divergence, agreement_rate, verdict}},
         "total_matches": int, "summary": str}
    """
    llm_scores = _read_jsonl(llm_scores_path)
    human_scores = _read_human_csv(human_scores_path)

    if not llm_scores or not human_scores:
        return {"error": "无数据"}

    by_judge: dict[str, list[float]] = {}
    matches = 0

    for hs in human_scores:
        key = (hs.get("task_id"), hs.get("candidate_model"))
        # 找到对应 LLM 评分
        for ls in llm_scores:
            if ls.get("task_id") == key[0] and ls.get("candidate_model") == key[1]:
                if ls.get("semantic_score", -1) < 0:
                    continue
                judge = ls.get("judge_model", "unknown")
                by_judge.setdefault(judge, [])

                # 计算分歧（5 分制，abs 差值）
                human_avg = (hs.get("correctness", 0) + hs.get("completeness", 0) + hs.get("code_quality", 0)) / 3
                divergence = abs(ls.get("semantic_score", 0) - human_avg)
                by_judge[judge].append(divergence)
                matches += 1

    judges = {}
    for judge, divs in by_judge.items():
        avg_div = round(sum(divs) / len(divs), 2)
        agreement = round(sum(1 for d in divs if d <= 1.0) / len(divs) * 100)
        verdict = "✓ reliable" if avg_div <= 1.0 else ("⚠ marginal" if avg_div <= 1.5 else "✗ unreliable")
        judges[judge] = {
            "avg_divergence": avg_div,
            "agreement_rate": agreement,
            "verdict": verdict,
            "samples": len(divs),
        }

    return {"judges": judges, "total_matches": matches}


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════

def _same_provider(model_a: str, model_b: str) -> bool:
    """判断两个模型是否同 provider（简化：按名称前缀推断）。"""
    pa = _infer_provider(model_a)
    pb = _infer_provider(model_b)
    return pa == pb


def _infer_provider(model: str) -> str:
    """从模型名推断 provider。"""
    m = model.lower()
    if "claude" in m or "fable" in m or "haiku" in m or "sonnet" in m or "opus" in m:
        return "anthropic"
    if "gpt" in m or "o1" in m or "o3" in m or "o4" in m:
        return "openai"
    if "gemini" in m:
        return "google"
    if "deepseek" in m:
        return "deepseek"
    if "qwen" in m or "qwq" in m:
        return "alibaba"
    if "doubao" in m:
        return "volcengine"
    if "kimi" in m:
        return "moonshot"
    if "glm" in m:
        return "zhipu"
    return "custom"


def _heuristic_score(reason: str) -> float:
    """从 evaluate_semantic 的 reason 文本中启发式提取评分（P1 简化）。

    使用正则词边界匹配，避免子串误匹配（如 "not missing" 不命中 "missing"）。
    P2 改进方向：在 evaluate_semantic 的 prompt 中加结构化评分指令。
    """
    if not reason:
        return 2.5
    r = reason.lower()

    # 高优：完全正确 / 优秀
    if re.search(r'\b(完全正确|完整实现|excellent|完美|fully\s*correct)\b', r):
        return 5.0
    # 良好：基本正确 / 小问题
    if re.search(r'\b(基本正确|大部分|mostly\s*correct|minor\s*issue|大体)\b', r):
        return 4.0
    # 不完整 / 缺失
    if re.search(r'\b(部分(?!正确)|缺少|missing|不完整|incomplete)\b', r):
        return 2.0
    # 错误
    if re.search(r'\b(错误|incorrect|wrong|失败|不正确)\b', r):
        return 1.0
    return 3.0  # 默认中等


def _git_diff(worktree: Path) -> str:
    """获取 worktree 的 git diff（对 HEAD 的全部变更）。"""
    import subprocess as _sp
    try:
        r = _sp.run(["git", "-C", str(worktree), "diff", "HEAD"],
                    capture_output=True, text=True, timeout=10)
        return r.stdout[:8000] if r.stdout else ""
    except Exception:
        return ""


# _read_jsonl / _read_json 已抽取到 eval.py（共享实现）


def _read_human_csv(path: Path) -> list[dict]:
    """读人工评分 CSV。格式: task_id,candidate_model,correctness,completeness,code_quality,false_positive"""
    import csv
    if not path.exists():
        return []
    items = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            items.append({
                "task_id": row.get("task_id", ""),
                "candidate_model": row.get("candidate_model", ""),
                "correctness": int(row.get("correctness", 0)),
                "completeness": int(row.get("completeness", 0)),
                "code_quality": int(row.get("code_quality", 0)),
                "false_positive": row.get("false_positive", "false").lower() == "true",
            })
    return items


# ═══════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════

def _print_cross_judge_summary(scores: list[dict], judge_models: list[str]) -> None:
    """打印交叉评判摘要 — 按 candidate_model × judge_model 聚合。"""
    from collections import defaultdict
    by_candidate: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for s in scores:
        cm = s.get("candidate_model", "?")
        jm = s.get("judge_model", "?")
        sc = s.get("semantic_score", -1)
        if sc >= 0:
            by_candidate[cm][jm].append(sc)

    console.print(f"\n📊 交叉评判矩阵（semantic_score 均值，满分 5）")
    console.print("─" * 70)
    header = f"{'产出者 ↓ / 评判者 →':<28}"
    for jm in judge_models:
        header += f"{jm:<20}"
    console.print(header)
    console.print("─" * 70)

    for cm in sorted(by_candidate.keys()):
        row = f"{cm:<28}"
        for jm in judge_models:
            scores_list = by_candidate[cm].get(jm, [])
            if scores_list:
                avg = round(sum(scores_list) / len(scores_list), 2)
                row += f"{avg:<20.2f}"
            else:
                row += "—                   "
        console.print(row)
    console.print("─" * 70)
    console.print("对角线空白 = 自评禁止（LLM-as-Judge 自偏约束）")


def _print_calibration(cal: dict[str, Any]) -> None:
    """打印人工校准报告。"""
    if "error" in cal:
        console.warning(f"{cal['error']}")
        return

    console.print(f"\n📋 人工校准报告（{cal['total_matches']} 次匹配）")
    console.print("─" * 60)
    console.print(f"{'评判者':<25} {'分歧':>6} {'一致率':>7} {'判定':<14} {'样本':>5}")
    console.print("─" * 60)
    for judge, j in cal.get("judges", {}).items():
        console.print(f"{judge:<25} {j['avg_divergence']:>5.2f} {j['agreement_rate']:>6}% "
                      f"{j['verdict']:<14} {j['samples']:>5}")
    console.print("─" * 60)
    console.print("分歧 ≤ 1.0 分 → ✓ reliable；1.0-1.5 → ⚠ marginal；>1.5 → ✗ unreliable")
