"""Execution timeline replay - reconstructs and visualizes task execution history.

P4-1: 执行回放
Reads meta.json, results_map, metering.jsonl to build a chronological
timeline of plan -> waves -> subtasks -> completion with cost/stats overlay.
"""

import json
from pathlib import Path
from typing import Any, Optional

from .console import _LazyConsole

console = _LazyConsole()


# ── Data loading ────────────────────────────────────────────────

def _load_task_data(task_dir: Path) -> Optional[dict[str, Any]]:
    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        return None

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    results = meta.get("results", [])

    results_map: dict[str, dict] = {}
    for r in results:
        results_map[r.get("subtask_id", r.get("id", ""))] = r

    metering_path = task_dir / "metering.jsonl"
    metering = []
    if metering_path.exists():
        for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    metering.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    plans_dir = task_dir / "plans"
    plan_versions = []
    if plans_dir.exists():
        for f in sorted(plans_dir.glob("v*.json"), key=lambda x: int(x.stem[1:])):
            try:
                plan_versions.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    subtasks = meta.get("subtasks", [])
    waves = _compute_waves(subtasks)

    return {
        "meta": meta,
        "results_map": results_map,
        "metering": metering,
        "plan_versions": plan_versions,
        "subtasks": subtasks,
        "waves": waves,
        "task_dir": task_dir,
    }


def _compute_waves(subtasks: list[dict]) -> list[list[str]]:
    deps: dict[str, list[str]] = {}
    all_ids: list[str] = []
    for st in subtasks:
        sid = st.get("id", "")
        all_ids.append(sid)
        raw = st.get("depends_on", st.get("dependencies", []))
        deps[sid] = list(raw) if isinstance(raw, (list, tuple)) else []

    remaining = set(all_ids)
    waves: list[list[str]] = []
    while remaining:
        wave = [sid for sid in remaining if not any(d in remaining for d in deps.get(sid, []))]
        if not wave:
            wave = list(remaining)
        waves.append(wave)
        remaining -= set(wave)
    return waves


# ── Timeline builder ────────────────────────────────────────────

def _build_timeline(data: dict[str, Any]) -> list[dict[str, Any]]:
    meta = data["meta"]
    results_map = data["results_map"]
    waves = data["waves"]
    subtasks = data["subtasks"]

    events: list[dict[str, Any]] = []

    created_str = meta.get("created", "")
    task_id = meta.get("task_id", "")
    events.append({
        "ts": created_str,
        "event": "task.start",
        "data": {
            "task_id": task_id,
            "task": meta.get("task", ""),
            "repo": meta.get("repo", ""),
            "agent_type": meta.get("agent_type", ""),
        }
    })

    if data["plan_versions"]:
        last_plan = data["plan_versions"][-1]
        saved_at = last_plan.get("saved_at", created_str)
        plan = last_plan.get("plan", {})
        steps_data = plan.get("steps", plan.get("subtasks", []))
        events.append({
            "ts": saved_at,
            "event": "plan.complete",
            "data": {
                "steps": len(steps_data),
                "waves": len(waves),
                "estimated_effort": plan.get("estimated_effort", ""),
                "versions": len(data["plan_versions"]),
            }
        })

    cumulative_time = 0.0
    for wave_idx, wave_ids in enumerate(waves):
        meta_results = []
        for wid in wave_ids:
            r = results_map.get(wid, {})
            if r:
                meta_results.append({
                    "id": wid,
                    "title": _subtask_title(subtasks, wid),
                    "status": r.get("status", "?"),
                    "duration_sec": r.get("duration_sec", 0),
                    "summary": r.get("summary", ""),
                    "failure_reason": r.get("failure_reason", ""),
                    "change_stats": r.get("change_stats", {}),
                    "verify_ok": r.get("verify_ok", False),
                    "retry_count": r.get("retry_count", 0),
                    "blocked_by": r.get("blocked_by", []),
                })

        if len(wave_ids) > 1:
            wave_duration = max(
                (r.get("duration_sec", 0) for r in results_map.values()
                 if r.get("subtask_id") in wave_ids),
                default=0.0,
            )
        else:
            wave_duration = sum(
                r.get("duration_sec", 0) for r in results_map.values()
                if r.get("subtask_id") in wave_ids
            )

        wave_end = cumulative_time + wave_duration

        events.append({
            "ts": f"+{cumulative_time:.1f}s",
            "event": "wave.start",
            "data": {"wave": wave_idx, "subtask_ids": wave_ids}
        })

        for m in meta_results:
            events.append({
                "ts": f"+{cumulative_time:.1f}s",
                "event": "subtask.start",
                "data": {"id": m["id"], "title": m["title"]}
            })
            events.append({
                "ts": f"+{cumulative_time + m['duration_sec']:.1f}s",
                "event": m["status"] if m["status"] in ("completed", "no_changes", "blocked", "failed") else "subtask.complete",
                "data": m,
            })

        cumulative_time = wave_end

    total_duration = sum(r.get("duration_sec", 0) for r in results_map.values())
    status_counts: dict[str, int] = {}
    for r in results_map.values():
        s = r.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    events.append({
        "ts": f"+{cumulative_time:.1f}s",
        "event": "pipeline.end",
        "data": {
            "total_duration_sec": total_duration,
            "subtask_count": len(results_map),
            "status_counts": status_counts,
            "meta_status": meta.get("status", ""),
        }
    })

    return events


def _subtask_title(subtasks: list[dict], sid: str) -> str:
    for st in subtasks:
        if st.get("id") == sid:
            return st.get("title", "")
    return sid


# ── Rendering helpers ───────────────────────────────────────────

def _format_duration(sec: float) -> str:
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m{s}s"


def _format_bar(pct: float, width: int = 30) -> str:
    filled = min(int(pct * width), width)
    return "█" * filled + "░" * (width - filled)


def _status_icon(status: str) -> str:
    if status in ("completed", "no_changes"):
        return "✅"
    if status == "blocked":
        return "⏭️"
    if status == "failed":
        return "❌"
    return "❓"


def _collect_summary(data: dict[str, Any]) -> dict[str, Any]:
    results_map = data["results_map"]
    metering = data["metering"]

    total_completed = sum(
        1 for r in results_map.values() if r.get("status") in ("completed", "no_changes")
    )
    total_blocked = sum(1 for r in results_map.values() if r.get("status") == "blocked")
    total_failed = sum(1 for r in results_map.values() if r.get("status") == "failed")
    total_subtasks = len(results_map)

    total_duration = sum(r.get("duration_sec", 0) for r in results_map.values())
    total_files = sum(
        r.get("change_stats", {}).get("files_changed", 0) for r in results_map.values()
    )
    total_ins = sum(
        r.get("change_stats", {}).get("insertions", 0) for r in results_map.values()
    )
    total_del = sum(
        r.get("change_stats", {}).get("deletions", 0) for r in results_map.values()
    )

    total_cost = sum(m.get("cost_usd", 0) or 0 for m in metering)
    total_prompt_tokens = sum(m.get("prompt_tokens", 0) or 0 for m in metering)
    total_completion_tokens = sum(m.get("completion_tokens", 0) or 0 for m in metering)

    role_costs: dict[str, float] = {}
    for m in metering:
        role = m.get("role", "unknown")
        role_costs[role] = role_costs.get(role, 0) + (m.get("cost_usd", 0) or 0)

    return {
        "total_completed": total_completed,
        "total_blocked": total_blocked,
        "total_failed": total_failed,
        "total_subtasks": total_subtasks,
        "total_duration_sec": total_duration,
        "total_files": total_files,
        "total_insertions": total_ins,
        "total_deletions": total_del,
        "total_cost_usd": total_cost,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "role_costs": role_costs,
        "waves": data["waves"],
        # M4 goal 回溯：合规度随摘要输出（ASCII 与 --json 均可见）
        "goal_adherence": (data.get("meta") or {}).get("goal_adherence") or {},
    }


# ── Rendering: ASCII ────────────────────────────────────────────

def _render_ascii_timeline(data: dict[str, Any]) -> str:
    meta = data["meta"]
    waves = data["waves"]
    subtasks = data["subtasks"]
    results_map = data["results_map"]
    summary = _collect_summary(data)

    lines: list[str] = []

    task_id = meta.get("task_id", "")
    created = meta.get("created", "")
    task_desc = meta.get("task", "")
    repo = meta.get("repo", "")
    agent_type = meta.get("agent_type", "developer")
    lines.append(f"📋 执行回放: {task_id}")
    lines.append(f"   任务: {task_desc}")
    lines.append(f"   仓库: {repo}")
    lines.append(f"   时间: {created}  |  代理: {agent_type}")
    _ga = summary.get("goal_adherence") or {}
    if _ga.get("level") and _ga["level"] != "unknown":
        _ga_icon = "✅" if _ga["level"] == "full" else "⚠️"
        _ga_line = f"   Goal 合规度: {_ga_icon} {_ga['level']}（score={_ga.get('score')}）"
        if _ga.get("needs_human_review"):
            _ga_line += "  ⚠️ 执行全过但验收存疑，建议人工补验收"
        lines.append(_ga_line)

    if data["plan_versions"]:
        last_plan = data["plan_versions"][-1]
        plan = last_plan.get("plan", {})
        steps_data = plan.get("steps", plan.get("subtasks", []))
        effort = plan.get("estimated_effort", "")
        lines.append(f"   Plan: {len(steps_data)} 步骤, {len(waves)} wave"
                     f"  |  版本: {len(data['plan_versions'])}"
                     f"  |  预估: {effort}")

    lines.append("")
    lines.append("═" * 72)

    bar_width = 40

    for wave_idx, wave_ids in enumerate(waves):
        if not wave_ids:
            continue

        wave_start = 0.0
        for wi in range(wave_idx):
            w_ids = waves[wi]
            if len(w_ids) > 1:
                w_dur = max(
                    (results_map.get(wid, {}).get("duration_sec", 0) for wid in w_ids),
                    default=0.0,
                )
            else:
                w_dur = sum(
                    results_map.get(wid, {}).get("duration_sec", 0) for wid in w_ids
                )
            wave_start += w_dur

        if len(wave_ids) > 1:
            w_dur = max(
                (results_map.get(wid, {}).get("duration_sec", 0) for wid in wave_ids),
                default=0.0,
            )
        else:
            w_dur = sum(
                results_map.get(wid, {}).get("duration_sec", 0) for wid in wave_ids
            )

        wave_end = wave_start + w_dur

        lines.append("")
        lines.append(f"  Wave {wave_idx + 1} ({len(wave_ids)} sub)"
                     f"  {_format_duration(wave_start)} -> {_format_duration(wave_end)}")
        lines.append(f"  {'─' * min(66, bar_width + 30)}")

        for wid in wave_ids:
            r = results_map.get(wid, {})
            if not r:
                lines.append(f"  ???  {'░' * bar_width}  ?")
                continue

            dur = r.get("duration_sec", 0)
            status = r.get("status", "?")
            verify_ok = r.get("verify_ok", False)
            retry_count = r.get("retry_count", 0)
            change = r.get("change_stats", {})
            files_changed = change.get("files_changed", 0)
            insertions = change.get("insertions", 0)
            deletions = change.get("deletions", 0)
            title = _subtask_title(subtasks, wid)

            icon = _status_icon(status)
            bar_pct = dur / max(w_dur, 0.1)
            bar = _format_bar(min(bar_pct, 1.0), bar_width)

            parts = []
            if files_changed:
                parts.append(f"+{insertions}/-{deletions}")
                parts.append(f"{files_changed}f")
            elif status == "no_changes":
                parts.append("no changes")
            if retry_count:
                parts.append(f"retryx{retry_count}")

            stats_str = " | ".join(p for p in parts if p)
            title_trunc = title[:25] if title else wid
            dur_str = _format_duration(dur)

            lines.append(f"  {title_trunc:<26} {icon} {bar} {dur_str:>8}  {stats_str}")

            if status == "failed" and r.get("failure_reason"):
                reason = r["failure_reason"][:80]
                lines.append(f"  {'':<26}    x {reason}")
            elif not verify_ok and status not in ("blocked", "failed", "no_changes"):
                lines.append(f"  {'':<26}    x verification failed")
            if status == "blocked":
                blocked_by = r.get("blocked_by", [])
                if blocked_by:
                    lines.append(f"  {'':<26}    blocked by: {', '.join(blocked_by)}")

    lines.append("")
    lines.append("═" * 72)

    s = summary
    status_parts = [
        f"subtasks: {s['total_completed']}/{s['total_subtasks']} completed"
    ]
    if s['total_blocked']:
        status_parts.append(f"blocked: {s['total_blocked']}")
    if s['total_failed']:
        status_parts.append(f"failed: {s['total_failed']}")
    status_parts.append(f"duration: {_format_duration(s['total_duration_sec'])}")

    lines.append(f"  Total  |  {'  |  '.join(status_parts)}")
    if s['total_files']:
        lines.append(f"  Files: {s['total_files']} changed (+{s['total_insertions']}/-{s['total_deletions']})")

    if s['total_cost_usd'] > 0:
        lines.append(f"  Cost:  ${s['total_cost_usd']:.4f}"
                     f"  |  Tokens: {s['total_prompt_tokens'] + s['total_completion_tokens']:,}"
                     f"  (prompt: {s['total_prompt_tokens']:,}, completion: {s['total_completion_tokens']:,})")

    if s['role_costs']:
        lines.append("")
        lines.append("  Cost Breakdown:")
        for role, cost in sorted(s['role_costs'].items(), key=lambda x: -x[1]):
            pct = cost / max(s['total_cost_usd'], 0.001) * 100
            cb = _format_bar(pct / 100, 20)
            lines.append(f"    {role:<16} {cb}  ${cost:.4f} ({pct:.0f}%)")

    lines.append("")
    waves_summary = ", ".join(
        f"W{wi+1}: {'/'.join(_subtask_title(subtasks, wid)[:18] for wid in w_ids)}"
        for wi, w_ids in enumerate(waves)
    )
    lines.append(f"  Wave Topology:  {waves_summary}")

    return "\n".join(lines)


# ── Rendering: JSON ─────────────────────────────────────────────

def _render_json_timeline(data: dict[str, Any]) -> str:
    timeline = data.get("_timeline", [])
    if not timeline:
        timeline = _build_timeline(data)
        data["_timeline"] = timeline

    return "\n".join(
        json.dumps(event, ensure_ascii=False, default=str) for event in timeline
    )


# ── CLI entry point ─────────────────────────────────────────────

def cmd_replay(args: Any) -> None:
    from .config import AGENT_GO_DIR

    task_id = args.task_id
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.error(f"Task not found: {task_id}")
        return

    data = _load_task_data(task_dir)
    if not data:
        console.error(f"Failed to load task data: {task_id}")
        return

    timeline = _build_timeline(data)
    data["_timeline"] = timeline

    json_mode = getattr(args, "json_mode", False)
    if json_mode:
        console.print(_render_json_timeline(data))
    else:
        console.print(_render_ascii_timeline(data))
