import json
from pathlib import Path
from datetime import datetime


def _read_meta(task_dir):
    path = Path(task_dir) / "meta.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_log_events(log_path, event_name):
    events = []
    if not log_path.exists():
        return events
    for line in log_path.read_text(encoding="utf-8").strip().split("\n"):
        if f'"event":"{event_name}"' in line:
            try:
                json_part = line.split(" | ")[-1]
                events.append(json.loads(json_part))
            except (json.JSONDecodeError, IndexError):
                pass
    return events


# ═══════════════════════════════════════════════════════════════
# Quality
# ═══════════════════════════════════════════════════════════════

def analyze_quality(meta):
    if meta is None:
        return None
    results = meta.get("results", [])
    subtasks = meta.get("subtasks", [])
    if not results:
        return None

    total = len(results)
    completed = sum(1 for r in results if r.get("status") == "completed")
    no_changes = sum(1 for r in results if r.get("status") == "no_changes")
    failed = sum(1 for r in results if r.get("status") == "failed")

    q1 = round(completed / total * 100) if total else 0
    q2 = round((completed + no_changes) / total * 100) if total else 0

    first_pass = sum(1 for r in results if r.get("retry_count", 0) == 0)
    q3 = round(first_pass / total * 100) if total else 0

    with_changes = [r for r in results if r.get("status") != "no_changes"]
    q4 = round(sum(1 for r in with_changes if r.get("verify_ok")) / len(with_changes) * 100) if with_changes else 100

    q5_no_changes_with_new = sum(
        1 for r in results
        if r.get("status") == "no_changes" and r.get("change_stats", {}).get("new_files", 0) > 0
    )
    q5 = round(q5_no_changes_with_new / total * 100) if total else 0

    merge_success = 0
    merge_total = 0
    for r in results:
        for m in r.get("merge_results", []):
            merge_total += 1
            if m.get("status") == "success":
                merge_success += 1
    q6 = round(merge_success / merge_total * 100) if merge_total else 100

    avg_files = avg_insertions = avg_deletions = 0
    with_stats = [r.get("change_stats", {}) for r in results if r.get("change_stats")]
    if with_stats:
        avg_files = round(sum(c.get("files_changed", 0) for c in with_stats) / len(with_stats), 1)
        avg_insertions = round(sum(c.get("insertions", 0) for c in with_stats) / len(with_stats), 1)
        avg_deletions = round(sum(c.get("deletions", 0) for c in with_stats) / len(with_stats), 1)

    q7_precision = q7_recall = 100
    if subtasks and with_stats:
        planned = set()
        for st in subtasks:
            fh = st.get("files_hint", "")
            if fh and fh != "*":
                for f in fh.split(","):
                    planned.add(f.strip())
        actual = set()
        for r in results:
            for f in r.get("change_stats", {}).get("actual_files", []):
                actual.add(f)
        if planned and actual:
            inter = planned & actual
            q7_precision = round(len(inter) / len(planned) * 100)
            q7_recall = round(len(inter) / len(actual) * 100)

    return {
        "task_id": meta.get("task_id", ""),
        "status": meta.get("status", ""),
        "subtasks": {"total": total, "completed": completed, "no_changes": no_changes, "failed": failed},
        "Q1_task_success_rate": q1,
        "Q2_subtask_success_rate": q2,
        "Q3_first_pass_rate": q3,
        "Q4_verify_pass_rate": q4,
        "Q5_new_file_miss_rate": q5,
        "Q6_merge_success_rate": q6,
        "Q7_plan_accuracy_precision": q7_precision,
        "Q7_plan_accuracy_recall": q7_recall,
        "Q8_change_scale": {"avg_files": avg_files, "avg_insertions": avg_insertions, "avg_deletions": avg_deletions},
        "score": round(q1 * 0.4 + q3 * 0.3 + q4 * 0.3),
    }


# ═══════════════════════════════════════════════════════════════
# Performance
# ═══════════════════════════════════════════════════════════════

def analyze_performance(meta, log_path=None):
    if meta is None:
        return None
    results = meta.get("results", [])
    if not results:
        return None

    durations = [r.get("duration_sec", 0) for r in results if r.get("duration_sec")]
    p3 = round(sum(durations) / len(durations), 1) if durations else 0
    p4 = _percentiles(durations, [50, 95, 99])

    timing_totals = {"worktree_create_ms": 0, "merge_upstream_ms": 0, "claude_execute_ms": 0,
                     "verification_ms": 0, "git_commit_ms": 0}
    timing_count = 0
    for r in results:
        t = r.get("timing")
        if t:
            timing_count += 1
            for k in timing_totals:
                timing_totals[k] += t.get(k, 0)

    p5 = {}
    if timing_count:
        total_ms = sum(timing_totals.values()) or 1
        for k, v in timing_totals.items():
            p5[k] = round(v / total_ms * 100, 1)

    p1 = 0
    p6 = 100
    sum_duration = sum(durations)
    p2 = 0

    if log_path:
        plan_events = _read_log_events(log_path, "plan_complete")
        for ev in plan_events:
            p2 = ev.get("plan_duration_ms", p2)

        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        try:
            first_ts = datetime.strptime(lines[0].split(" | ")[0], "%Y-%m-%d %H:%M:%S")
            last_ts = datetime.strptime(lines[-1].split(" | ")[0], "%Y-%m-%d %H:%M:%S")
            p1 = round((last_ts - first_ts).total_seconds(), 1)
        except (ValueError, IndexError):
            pass
    if p1 > 0:
        p6 = round(sum_duration / p1 * 100)

    return {
        "task_id": meta.get("task_id", ""),
        "P1_total_duration_sec": p1,
        "P2_plan_duration_ms": p2,
        "P3_avg_subtask_sec": p3,
        "P4_duration_percentiles": p4,
        "P5_phase_breakdown_pct": p5,
        "P6_concurrency_efficiency_pct": p6,
        "score": _perf_score(p1, p4.get(95, p3), p6),
    }


def _perf_score(p1, p95, p6):
    if p1 <= 0:
        return 50
    p1_score = max(0, min(100, 100 - p1 / 3))
    p95_score = max(0, min(100, 100 - p95 / 6))
    p6_score = max(0, min(100, p6))
    return round(p1_score * 0.3 + p95_score * 0.3 + p6_score * 0.4)


def _percentiles(data, percents):
    if not data:
        return {p: 0 for p in percents}
    s = sorted(data)
    result = {}
    for p in percents:
        k = (p / 100) * (len(s) - 1)
        f = int(k)
        c = f + 1 if f + 1 < len(s) else f
        result[p] = round(s[f] + (s[c] - s[f]) * (k - f), 1) if c != f else round(s[f], 1)
    return result


# ═══════════════════════════════════════════════════════════════
# Aggregation
# ═══════════════════════════════════════════════════════════════

def _scan_task_dirs(base_dir):
    return sorted(Path(base_dir).glob("task-*"), reverse=True)


def aggregate_quality(tasks_dir):
    items = []
    for td in _scan_task_dirs(tasks_dir):
        meta = _read_meta(td)
        q = analyze_quality(meta)
        if q:
            items.append(q)
    if not items:
        return None
    return {
        "tasks_analyzed": len(items),
        "avg_success_rate": round(sum(r["Q1_task_success_rate"] for r in items) / len(items)),
        "avg_first_pass": round(sum(r["Q3_first_pass_rate"] for r in items) / len(items)),
        "avg_verify_pass": round(sum(r["Q4_verify_pass_rate"] for r in items) / len(items)),
        "avg_new_file_miss": round(sum(r["Q5_new_file_miss_rate"] for r in items) / len(items)),
        "avg_merge_success": round(sum(r["Q6_merge_success_rate"] for r in items) / len(items)),
        "avg_score": round(sum(r["score"] for r in items) / len(items)),
    }


def aggregate_performance(tasks_dir):
    all_durations = []
    p1_values = []
    for td in _scan_task_dirs(tasks_dir):
        meta = _read_meta(td)
        if meta:
            for r in meta.get("results", []):
                d = r.get("duration_sec")
                if d:
                    all_durations.append(d)
            log_path = td / "execution.log"
            p = analyze_performance(meta, log_path)
            if p and p.get("P1_total_duration_sec"):
                p1_values.append(p["P1_total_duration_sec"])

    p4 = _percentiles(all_durations, [50, 95, 99]) if all_durations else {}
    return {
        "tasks_analyzed": len(p1_values),
        "subtasks_total": len(all_durations),
        "avg_duration_sec": round(sum(all_durations) / len(all_durations), 1) if all_durations else 0,
        "P50_sec": p4.get(50, 0),
        "P95_sec": p4.get(95, 0),
        "P99_sec": p4.get(99, 0),
        "avg_task_duration_sec": round(sum(p1_values) / len(p1_values), 1) if p1_values else 0,
    }


# ═══════════════════════════════════════════════════════════════
# CLI output
# ═══════════════════════════════════════════════════════════════

def cmd_eval():
    import sys
    from .config import AGENT_GO_DIR

    if len(sys.argv) < 3:
        print("Usage: agent_go eval <quality|perf> [task-id|--all]")
        return

    sub = sys.argv[2]
    task_id = sys.argv[3] if len(sys.argv) > 3 else ""
    all_mode = task_id == "--all"

    if sub == "quality":
        if all_mode:
            _print_aggregate_quality(aggregate_quality(AGENT_GO_DIR))
        else:
            td = _resolve_task_dir(AGENT_GO_DIR, task_id)
            if td:
                _print_quality_report(analyze_quality(_read_meta(td)))
            else:
                print("暂无任务")
    elif sub == "perf":
        if all_mode:
            _print_aggregate_perf(aggregate_performance(AGENT_GO_DIR))
        else:
            td = _resolve_task_dir(AGENT_GO_DIR, task_id)
            if td:
                _print_perf_report(analyze_performance(_read_meta(td), td / "execution.log"))
            else:
                print("暂无任务")
    else:
        print(f"未知子命令: {sub}。可用: quality, perf")


def _resolve_task_dir(base_dir, task_id):
    if task_id:
        td = Path(base_dir) / task_id
        return td if td.exists() else None
    tasks = _scan_task_dirs(base_dir)
    return tasks[0] if tasks else None


def _print_quality_report(q):
    if q is None:
        print("无数据")
        return
    print(f"\n质量报告 — {q['task_id']}")
    print("─" * 50)
    s = q["subtasks"]
    print(f"  Subtask: {s['total']} total | {s['completed']} ok | {s['no_changes']} no-op | {s['failed']} fail")
    print(f"  Q1 任务成功率:       {q['Q1_task_success_rate']}%")
    print(f"  Q2 Subtask成功率:    {q['Q2_subtask_success_rate']}%")
    print(f"  Q3 首次通过率:       {q['Q3_first_pass_rate']}%")
    print(f"  Q4 验证通过率:       {q['Q4_verify_pass_rate']}%")
    print(f"  Q5 新文件遗漏率:     {q['Q5_new_file_miss_rate']}%")
    print(f"  Q6 产物传递成功率:   {q['Q6_merge_success_rate']}%")
    print(f"  Q7 计划准确性:       P={q['Q7_plan_accuracy_precision']}% R={q['Q7_plan_accuracy_recall']}%")
    cs = q["Q8_change_scale"]
    print(f"  Q8 变更规模:         avg {cs['avg_files']} files, +{cs['avg_insertions']}/-{cs['avg_deletions']}")
    print(f"  ─────────────────────────────")
    print(f"  评分: {q['score']}/100")
    print("─" * 50)


def _print_perf_report(p):
    if p is None:
        print("无数据")
        return
    print(f"\n性能报告 — {p['task_id']}")
    print("─" * 50)
    print(f"  P1 端到端耗时:       {p['P1_total_duration_sec']}s")
    print(f"  P2 Plan耗时:         {p['P2_plan_duration_ms']}ms")
    print(f"  P3 平均Subtask耗时:  {p['P3_avg_subtask_sec']}s")
    p4 = p["P4_duration_percentiles"]
    print(f"  P4 耗时分布:         P50={p4.get(50,0)}s P95={p4.get(95,0)}s P99={p4.get(99,0)}s")
    p5 = p.get("P5_phase_breakdown_pct", {})
    if p5:
        claude = p5.get("claude_execute_ms", 0)
        verify = p5.get("verification_ms", 0)
        print(f"  P5 阶段占比:         claude={claude}% verify={verify}% other={100-claude-verify}%")
    print(f"  P6 并发效率:         {p['P6_concurrency_efficiency_pct']}%")
    print(f"  ─────────────────────────────")
    print(f"  评分: {p['score']}/100")
    print("─" * 50)


def _print_aggregate_quality(agg):
    if agg is None:
        print("无历史数据")
        return
    print(f"\n质量聚合 — {agg['tasks_analyzed']} 个任务")
    print("─" * 50)
    print(f"  平均成功率:          {agg['avg_success_rate']}%")
    print(f"  平均首次通过率:      {agg['avg_first_pass']}%")
    print(f"  平均验证通过率:      {agg['avg_verify_pass']}%")
    print(f"  平均新文件遗漏率:    {agg['avg_new_file_miss']}%")
    print(f"  平均产物传递成功率:  {agg['avg_merge_success']}%")
    print(f"  平均评分:            {agg['avg_score']}/100")
    print("─" * 50)


def _print_aggregate_perf(agg):
    if agg is None or agg["tasks_analyzed"] == 0:
        print("无历史数据")
        return
    print(f"\n性能聚合 — {agg['tasks_analyzed']} 任务, {agg['subtasks_total']} subtasks")
    print("─" * 50)
    print(f"  平均耗时:            {agg['avg_duration_sec']}s")
    print(f"  耗时分布:            P50={agg['P50_sec']}s P95={agg['P95_sec']}s P99={agg['P99_sec']}s")
    print(f"  平均任务耗时:        {agg['avg_task_duration_sec']}s")
    print("─" * 50)
