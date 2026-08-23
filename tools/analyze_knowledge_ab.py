#!/usr/bin/env python3
"""C4 KnowledgeStore A/B 对比判定分析 CLI（N3-2）。

对 knowledge_arm 对照/注入两批 bench results 做 A/B 判定：
  - ADR 提升（注入臂 accepted_delivery 比例 > 对照臂）
  - 成本不劣化（注入臂 $/AD <= 对照臂 × (1 + cost_tolerance)）
  - 错误知识可淘汰（problems 中存在 dormant/suppressed 记录）
三门槛全过 → PRODUCTIZE；否则 ROLLBACK（仅保留埋点）。

用法:
  python3 tools/analyze_knowledge_ab.py --ctl <ctl.jsonl> --inj <inj.jsonl> \
      [--problems ~/.agent_go/problems.jsonl] [--cost-tol 0.10]

输出:
  JSON 报告（两臂汇总 + 三判定 + 结论）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_go.knowledge_ab import analyze_ab, load_results  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="C4 KnowledgeStore A/B 判定分析")
    parser.add_argument("--ctl", required=True, help="对照臂 results jsonl")
    parser.add_argument("--inj", required=True, help="注入臂 results jsonl")
    parser.add_argument("--problems", default=None, help="problems.jsonl 路径（可淘汰判定）")
    parser.add_argument("--cost-tol", type=float, default=0.10, help="成本容忍上浮比例（默认 0.10）")
    args = parser.parse_args()

    ctl = load_results(args.ctl)
    inj = load_results(args.inj)
    report = analyze_ab(ctl, inj, cost_tolerance=args.cost_tol, problems_path=args.problems)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    verdicts = report["verdicts"]
    print(
        f"\n判定: ADR↑={'✅' if verdicts['adr_up'] else '❌'}  "
        f"成本不劣化={'✅' if verdicts['cost_not_worse'] else '❌'}  "
        f"可淘汰机制={'✅' if verdicts['knowledge_eliminable'] else '—'}"
    )
    print(f"结论: {report['conclusion']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
