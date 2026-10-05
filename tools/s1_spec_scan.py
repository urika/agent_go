#!/usr/bin/env python3
"""S-1 P0 仪器：spec/plan 覆盖扫描（离线、零外发、零依赖）。

用途（概念设计 §10 / O-15）：读一个任务的计划（meta.json 的 subtasks 或独立 plan JSON），
逐子任务判定"验收面是否锚定"，产出**缺口维度清单**——供 S-1 的 A/B 中"覆盖驱动"臂
作为起草输入；也可独立用作诊断报表。

判据（与 planning.validate_plan_quality 同源，保证口径一致）：
- 无核心文件改动 → 整仓/目录级测试可接受（视为已覆盖）；
- 有核心文件改动 → 验证命令须锚定到文件/函数（scope ∈ {file, function}），否则记为缺口；
- 无验证命令 → 缺口（missing_verification）。

四格分类（覆盖/风险矩阵的规则侧）：
- `rules=covered|gap`（本器可判）；`jev=unknown`（无 jev 数据时占位，试点后回填）。

用法：
    python3 tools/s1_spec_scan.py --meta <task_dir>/meta.json
    python3 tools/s1_spec_scan.py --meta <...>/meta.json --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent_go.planning import _is_core_file, _subtask_file_scope
from agent_go.utils import classify_verification_scope

ANCHORED_SCOPES = ("file", "function")


def scan_subtask(sub: Dict[str, Any]) -> Dict[str, Any]:
    sid = str(sub.get("id") or sub.get("subtask_id") or "?")
    command = str(sub.get("verification", "") or "").strip()
    scope_files = sorted(_subtask_file_scope(sub))
    core_files = [f for f in scope_files if _is_core_file(f)]
    scope = classify_verification_scope(command) if command else "none"
    if not command:
        status = "missing_verification"
    elif core_files and scope not in ANCHORED_SCOPES:
        status = "not_anchored"
    else:
        status = "covered"
    return {
        "subtask_id": sid,
        "verification": command,
        "scope": scope,
        "core_files": core_files,
        "status": status,
        "rules": "covered" if status == "covered" else "gap",
        "jev": "unknown",
    }


def scan_plan(subtasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [scan_subtask(s) for s in subtasks if isinstance(s, dict)]
    covered = sum(1 for r in rows if r["status"] == "covered")
    gaps = [r for r in rows if r["status"] != "covered"]
    return {
        "subtasks": len(rows),
        "covered": covered,
        "structural_coverage": round(covered / len(rows), 6) if rows else None,
        "gap_dimensions": [
            {"subtask_id": r["subtask_id"], "reason": r["status"], "scope": r["scope"],
             "core_files": r["core_files"], "verification": r["verification"][:120]}
            for r in gaps
        ],
        "rows": rows,
    }


def render_text(scan: Dict[str, Any], meta_path: str) -> str:
    lines = [f"# S-1 覆盖扫描（{meta_path}）",
             f"子任务 {scan['subtasks']}，已覆盖 {scan['covered']}，"
             f"结构性覆盖 = {scan['structural_coverage']}"]
    if scan["gap_dimensions"]:
        lines.append("")
        lines.append("## 缺口维度（起草输入候选）")
        for g in scan["gap_dimensions"]:
            lines.append(f"- {g['subtask_id']}：{g['reason']}（scope={g['scope']}，"
                         f"核心文件 {len(g['core_files'])} 个）")
    else:
        lines.append("")
        lines.append("## 无缺口（全部子任务验收面已锚定或无需锚定）")
    lines.append("")
    lines.append("## 逐子任务")
    for r in scan["rows"]:
        lines.append(f"- {r['subtask_id']}: scope={r['scope']} status={r['status']} "
                     f"rules={r['rules']} jev={r['jev']}")
    return "\n".join(lines)


def load_subtasks(meta_path: Path) -> List[Dict[str, Any]]:
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("subtasks"), list):
        return [s for s in data["subtasks"] if isinstance(s, dict)]
    if isinstance(data, dict) and isinstance(data.get("plan"), dict):
        plan = data["plan"]
        return [s for s in (plan.get("subtasks") or []) if isinstance(s, dict)]
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    return []


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="S-1 P0：spec/plan 覆盖扫描（离线零外发）")
    parser.add_argument("--meta", required=True, help="meta.json（含 subtasks）或 plan JSON")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    meta_path = Path(args.meta).expanduser()
    if not meta_path.is_file():
        print(f"[s1-scan] 文件不存在: {meta_path}", file=sys.stderr)
        return 2
    try:
        subtasks = load_subtasks(meta_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[s1-scan] 读取失败: {exc}", file=sys.stderr)
        return 2
    if not subtasks:
        print("[s1-scan] 未找到 subtasks（meta.json['subtasks'] 或 plan['subtasks']）", file=sys.stderr)
        return 2

    scan = scan_plan(subtasks)
    if args.json:
        print(json.dumps(scan, ensure_ascii=False, indent=2))
    else:
        print(render_text(scan, str(meta_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
