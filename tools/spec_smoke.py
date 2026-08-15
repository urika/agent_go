#!/usr/bin/env python3
"""spec 冒烟验证（阶段 B / B3 决策）：对 eval_suite/spec_smoke/tasks/*.md 逐个运行 agent_go --spec。

测量 ROI 门禁 4 指标（B3 决策，spec-closed-loop-design.md §9.2）：
  C1 填写成本       —— 人工指标，本脚本不测（spec 模板 + 5 个示例已提供，需人工填写计时）
  R1 交付成功率     —— meta.status in (ACCEPTED_DELIVERY, DELIVERY_READY) 的任务比例
  R2 追踪完整率     —— traceability.status == "complete" 的比例（基线 ≈ 0，门禁 ≥ 90%）
  R3 Plan 编辑次数  —— 代理指标：plan_warning_count（headless --yes 无人工编辑）

用法：
  python3 tools/spec_smoke.py --repo <repo-path> [--only 01] [--keep] [--timeout-min 15]

依赖：AGENT_GO_API_KEY 环境或 ~/.agent_go/config.json 已配置；claude CLI 可用。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TASKS_DIR = Path(__file__).resolve().parent.parent / "eval_suite" / "spec_smoke" / "tasks"
RESULTS_FILE = Path(__file__).resolve().parent.parent / "eval_suite" / "spec_smoke" / "results.jsonl"

STATUS_PASS = {"ACCEPTED_DELIVERY", "DELIVERY_READY"}


def _agent_go_dir() -> Path:
    from agent_go.config import AGENT_GO_DIR
    return AGENT_GO_DIR


def run_one(repo: Path, spec: Path, timeout_min: int) -> dict | None:
    """运行单个 spec 任务，返回收集的指标；失败返回 None。"""
    before = set(p.name for p in _agent_go_dir().glob("task-*"))
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_go", "run", str(repo), "--spec", str(spec), "--yes"],
            capture_output=True, text=True, timeout=timeout_min * 60,
        )
    except subprocess.TimeoutExpired:
        return {"spec": spec.name, "error": "timeout", "elapsed_sec": round(time.time() - t0, 1)}
    elapsed = round(time.time() - t0, 1)
    after = set(p.name for p in _agent_go_dir().glob("task-*"))
    new_dirs = after - before
    task_id = ""
    m = re.search(r"任务ID:\s*(task-\S+)", proc.stdout)
    if m:
        task_id = m.group(1)
    elif len(new_dirs) == 1:
        task_id = new_dirs.pop()
    if not task_id:
        return {"spec": spec.name, "error": "no_task_id", "elapsed_sec": elapsed,
                "stdout_tail": proc.stdout[-300:]}

    meta_path = _agent_go_dir() / task_id / "meta.json"
    if not meta_path.exists():
        return {"spec": spec.name, "task_id": task_id, "error": "no_meta", "elapsed_sec": elapsed}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    trace = meta.get("traceability") or {}
    results = meta.get("results") or []
    cost = 0.0
    mp = _agent_go_dir() / task_id / "metering.jsonl"
    if mp.exists():
        for line in mp.read_text(encoding="utf-8").splitlines():
            try:
                cost += json.loads(line).get("cost_usd", 0) or 0
            except (json.JSONDecodeError, AttributeError):
                pass
    return {
        "spec": spec.name,
        "task_id": task_id,
        "status": meta.get("status"),
        "failure_class": meta.get("failure_class"),
        "elapsed_sec": elapsed,
        "cost_usd": round(cost, 4),
        "traceability_status": trace.get("status"),
        "requirement_count": trace.get("requirement_count"),
        "missing_requirement_ids": trace.get("missing_requirement_ids") or [],
        "plan_warning_count": meta.get("plan_warning_count"),
        "plan_conflict_count": meta.get("plan_conflict_count"),
        "total_retries": sum(r.get("retry_count", 0) or 0 for r in results),
        "blind_spots": meta.get("blind_spots") or {},
        "layer_attribution": meta.get("layer_attribution") or {},
        "spec_snapshot": bool(meta.get("spec_snapshot")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="spec 冒烟验证（阶段 B ROI 门禁）")
    ap.add_argument("--repo", required=True, help="目标仓库路径")
    ap.add_argument("--only", default="", help="只跑指定 spec 前缀（如 01）")
    ap.add_argument("--timeout-min", type=int, default=15)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    specs = sorted(TASKS_DIR.glob("*.md"))
    if args.only:
        specs = [s for s in specs if s.name.startswith(args.only)]
    if not specs:
        print(f"未找到 spec：{TASKS_DIR}")
        return 1

    print(f"spec 冒烟验证：{len(specs)} 个任务 → {repo}")
    print(f"AGENT_GO_DIR: {_agent_go_dir()}")
    print("=" * 70)

    rows = []
    for spec in specs:
        print(f"\n▶ 运行: {spec.name}")
        row = run_one(repo, spec, args.timeout_min)
        if row is None:
            print("  ✗ 运行失败（无结果）")
            continue
        rows.append(row)
        icon = "✅" if row.get("status") in STATUS_PASS else "❌"
        print(f"  {icon} {row.get('status')} | 耗时 {row.get('elapsed_sec')}s | "
              f"$ {row.get('cost_usd')} | trace={row.get('traceability_status')} | "
              f"warn={row.get('plan_warning_count')} | err={row.get('error', '')}")

    # 落盘结果
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ── ROI 汇总 ──
    if not rows:
        print("\n无有效结果")
        return 1
    n = len(rows)
    r1_pass = sum(1 for r in rows if r.get("status") in STATUS_PASS)
    r2_complete = sum(1 for r in rows if r.get("traceability_status") == "complete")
    total_cost = sum(r.get("cost_usd", 0) or 0 for r in rows)
    total_elapsed = sum(r.get("elapsed_sec", 0) or 0 for r in rows)
    print("\n" + "=" * 70)
    print("ROI 汇总（B3 门禁：R1 ≥ 基线 91.7% / R2 ≥ 90% / R3 ≤ 基线）")
    print(f"  R1 交付成功率: {r1_pass}/{n} = {r1_pass / n * 100:.1f}%  (基线 M3 91.7%)")
    print(f"  R2 追踪完整率: {r2_complete}/{n} = {r2_complete / n * 100:.1f}%  (基线 ≈ 0%)")
    print(f"  R3 Plan 警告代理: 平均 {sum(r.get('plan_warning_count') or 0 for r in rows) / n:.1f} 个/任务")
    print(f"  总成本: ${total_cost:.4f}   总耗时: {total_elapsed / 60:.1f} 分钟")
    for r in rows:
        if r.get("missing_requirement_ids"):
            print(f"    ⚠️ {r['spec']}: 未覆盖 {r['missing_requirement_ids']}")
    print(f"\n结果已追加: {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
