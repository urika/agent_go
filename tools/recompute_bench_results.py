#!/usr/bin/env python3
"""用当前 bench._collect_result 逻辑重算历史 bench 结果文件。

背景（2026-08-24 三臂 bench 收尾）：判定逻辑两处修复——
1. _subtask_semantic_ok 取末次有效 semantic verdict（跨重试累积，旧取首个会误判）；
2. meta.failure_reason=="plan_quality_blocked" → kill_reason=plan_gate_blocked
   + failure_class 覆盖 system_error（不计能力失败）。
历史 JSONL 是旧逻辑产出，本脚本按 record 中保存的 task_dir/elapsed/exit_code
等输入重放收集，口径升级而原始执行数据（metering/meta）不变。

用法：
    python3 tools/recompute_bench_results.py <results.jsonl> [...]          # dry-run：只打印差异
    python3 tools/recompute_bench_results.py <results.jsonl> [...] --apply  # 备份原文件（.bak_prerecompute）后重写
    python3 tools/recompute_bench_results.py --merge <arm.jsonl> <rerun.jsonl> [--apply]
        # 把 rerun 的 (task_id, repeat) 记录重算后整体替换进臂文件（批次/套件口径对齐臂文件）
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_go.bench import _collect_result  # noqa: E402


def recompute_record(r: dict) -> dict:
    td = Path(r["task_dir"]) if r.get("task_dir") else None
    nr = _collect_result(
        r["task_id"], r["model"], r.get("elapsed_sec", 0.0),
        r.get("subprocess_exit", 0), r.get("stderr_tail", "") or "",
        exact_td=td, expected_task="",
        timed_out=bool(r.get("timed_out", False)),
        source_batch=r.get("source_batch", "") or "",
    )
    # 这四个字段由 run 循环从任务 YAML 注入（bench.py:520/537-540），
    # meta.json 不含，重算会落回默认值 —— 从旧 record 保留。
    for k in ("repeat", "suite", "task_version", "difficulty"):
        if k in r:
            nr[k] = r[k]
    return nr


def merge_rerun(arm_path: Path, rerun_path: Path, apply: bool) -> None:
    """把 rerun 文件中 (task_id, repeat) 对应的臂文件记录整体替换为重算后的 rerun 版本。

    批次对齐：source_batch / suite 采用臂文件口径（保证按批次聚合不分裂），
    task_version / difficulty 以臂内旧记录为准（同一任务 YAML）。
    """
    arm = [json.loads(line) for line in arm_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    idx = {(r["task_id"], r.get("repeat")): i for i, r in enumerate(arm)}
    arm_batch = arm[0].get("source_batch", "")
    arm_suite = arm[0].get("suite", "")
    replaced = 0
    for line in rerun_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        nr = recompute_record(json.loads(line))
        k = (nr["task_id"], nr.get("repeat"))
        if k not in idx:
            print(f"[merge] 警告：臂文件中无 {k}，跳过")
            continue
        old = arm[idx[k]]
        nr["source_batch"] = arm_batch
        nr["suite"] = arm_suite
        for kk in ("task_version", "difficulty"):
            if kk in old:
                nr[kk] = old[kk]
        print(f"[merge] {arm_path.name} {k}: binary_pass {old.get('binary_pass')} -> {nr.get('binary_pass')}, "
              f"kill_reason {old.get('kill_reason')} -> {nr.get('kill_reason')}, task_dir 换新={old['task_dir'] != nr['task_dir']}")
        arm[idx[k]] = nr
        replaced += 1
    print(f"[merge] {arm_path.name}: 替换 {replaced} 条（rerun 来源 {rerun_path.name}）")
    if apply and replaced:
        bak = arm_path.with_suffix(arm_path.suffix + ".bak_prerecompute")
        if not bak.exists():
            shutil.copy2(arm_path, bak)
        arm_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in arm), encoding="utf-8")
        print(f"[merge] {arm_path.name} 已重写（备份 {bak.name}）")


def main() -> int:
    apply = "--apply" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--apply"]
    if argv and argv[0] == "--merge":
        if len(argv) != 3:
            print(__doc__)
            return 2
        merge_rerun(Path(argv[1]), Path(argv[2]), apply)
        return 0
    paths = [Path(a) for a in argv]
    if not paths:
        print(__doc__)
        return 2
    for p in paths:
        old = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        new, changed = [], 0
        for r in old:
            nr = recompute_record(r)
            diffs = {k: (r.get(k), nr.get(k)) for k in nr if r.get(k) != nr.get(k)}
            # 重算不产生的字段（历史遗留）保留旧值
            for k in r:
                if k not in nr:
                    nr[k] = r[k]
            if diffs:
                changed += 1
                print(f"[{p.name}] {r['task_id']} repeat={r.get('repeat')}: "
                      + ", ".join(f"{k}: {v[0]!r} -> {v[1]!r}" for k, v in sorted(diffs.items())))
            new.append(nr)
        print(f"[{p.name}] {len(old)} 条记录，{changed} 条有变化")
        if apply:
            bak = p.with_suffix(p.suffix + ".bak_prerecompute")
            if not bak.exists():
                shutil.copy2(p, bak)
            p.write_text("".join(json.dumps(nr, ensure_ascii=False) + "\n" for nr in new), encoding="utf-8")
            print(f"[{p.name}] 已重写（备份 {bak.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
