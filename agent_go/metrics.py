import subprocess
import json
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "collect_timing", "collect_change_stats",
    "collect_merge_result", "extract_usage",
    "estimate_cost", "DEFAULT_PRICING", "aggregate_metering",
]

def collect_timing(worktree_create_ms: float, merge_upstream_ms: float, claude_execute_ms: float,
                   verification_ms: float, git_commit_ms: float) -> dict[str, float]:
    return {
        "worktree_create_ms": round(worktree_create_ms),
        "merge_upstream_ms": round(merge_upstream_ms),
        "claude_execute_ms": round(claude_execute_ms),
        "verification_ms": round(verification_ms),
        "git_commit_ms": round(git_commit_ms),
    }


def collect_change_stats(worktree_path: Path) -> dict[str, Any]:
    files_changed = 0
    insertions = 0
    deletions = 0
    actual_files = []

    numstat = subprocess.run(
        ["git", "diff", "--numstat", "HEAD"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    for line in numstat.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            insertions += int(parts[0]) if parts[0] != "-" else 0
            deletions += int(parts[1]) if parts[1] != "-" else 0
            actual_files.append(parts[2])

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path), capture_output=True, text=True
    )
    new_files = 0
    for line in status_result.stdout.strip().split("\n"):
        if line.startswith("??"):
            new_files += 1
            filename = line[3:]
            if filename not in actual_files:
                actual_files.append(filename)

    return {
        "files_changed": len(actual_files),
        "insertions": insertions,
        "deletions": deletions,
        "new_files": new_files,
        "modified_files": len(actual_files) - new_files,
        "actual_files": actual_files,
    }


def collect_merge_result(upstream_id: str, success: bool, conflict_files: Optional[list[str]] = None) -> dict[str, Any]:
    result = {"upstream": upstream_id, "status": "success" if success else "conflict"}
    if conflict_files:
        result["conflict_files"] = conflict_files
    return result


def extract_usage(api_response: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    usage = api_response.get("usage", {})
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "model": model,
        "provider": provider,
    }


# ═══════════════════════════════════════════════════════════════
# Cost Estimation
# ═══════════════════════════════════════════════════════════════

# 定价表：每百万 token 的价格（input, output），单位美元
# 来源：各 provider 官方定价页，2026-07 采集
DEFAULT_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    ("anthropic", "claude-sonnet-4-20250514"):     (3.0, 15.0),
    ("anthropic", "claude-haiku-4-5-20251001"):    (0.80, 4.0),
    ("anthropic", "claude-opus-4-20250514"):        (15.0, 75.0),
    # OpenAI
    ("openai", "gpt-4o"):                           (2.50, 10.0),
    ("openai", "gpt-4o-mini"):                      (0.15, 0.60),
    # DeepSeek
    ("deepseek", "deepseek-chat"):                  (0.27, 1.10),
    ("deepseek", "deepseek-v4-pro"):                (0.55, 2.19),
    # 本地模型 — 成本为 0
    ("custom", "*"):                                 (0.0, 0.0),
}


def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 API 调用成本（美元）。

    按 provider + model 查找定价表，计算 input + output token 的总成本。
    未匹配的 provider/model 组合返回 0（不阻塞流程）。

    Args:
        provider: "anthropic" | "openai" | "deepseek" | "custom"
        model: 具体模型名
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数

    Returns:
        估算成本（美元）
    """
    key = (provider, model)
    wildcard = (provider, "*")
    prices = DEFAULT_PRICING.get(key) or DEFAULT_PRICING.get(wildcard, (0, 0))
    input_cost = (prompt_tokens / 1_000_000) * prices[0]
    output_cost = (completion_tokens / 1_000_000) * prices[1]
    return input_cost + output_cost


def aggregate_metering(metering_path: Path) -> dict[str, Any]:
    """汇总 metering.jsonl，返回总 token / 总成本 / 总延迟。

    返回:
        {
            "total_calls": int,
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_tokens": int,
            "cost_usd": float,
            "latency_ms": float,
            "by_role": {role: {"calls": int, "cost_usd": float}},
        }
    """
    totals = {
        "total_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0.0,
        "by_role": {},
    }
    if not metering_path.exists():
        return totals

    try:
        for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["total_calls"] += 1
            pt = event.get("prompt_tokens", 0) or 0
            ct = event.get("completion_tokens", 0) or 0
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["total_tokens"] += pt + ct
            totals["cost_usd"] += event.get("cost_usd", 0.0) or 0.0
            totals["latency_ms"] += event.get("latency_ms", 0.0) or 0.0

            role = event.get("role", "unknown")
            if role not in totals["by_role"]:
                totals["by_role"][role] = {"calls": 0, "cost_usd": 0.0}
            totals["by_role"][role]["calls"] += 1
            totals["by_role"][role]["cost_usd"] += event.get("cost_usd", 0.0) or 0.0
    except OSError:
        pass

    totals["cost_usd"] = round(totals["cost_usd"], 6)
    totals["latency_ms"] = round(totals["latency_ms"], 2)
    for role in totals["by_role"]:
        totals["by_role"][role]["cost_usd"] = round(totals["by_role"][role]["cost_usd"], 6)
    return totals
