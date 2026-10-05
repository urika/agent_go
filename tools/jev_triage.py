#!/usr/bin/env python3
"""jev 复核排序试点仪器（薄版）——见 docs/design/jev-review-triage-pilot-requirements-20261005.md §17。

零网络：本模块不含任何 provider API 实现；`--call` 仅 spawn 既有 MCP server（stdio JSON-RPC）。
零 runtime 写入：只读任务产物，只写 `--out` 目录。
真值仍是人：`--label`/`--import-labels` 只收人工标签（labels.jsonl append-only，带 questions_sha256）；
v0.8 的预标注/探针（prelabels.jsonl/probe.jsonl）本薄版未实现，实现后也永不覆盖人工标签。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_GATE = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

DEFAULT_TAU = 0.60
COARSE_RULE_MAIN = "loop_detected | retry_count>=2 | kill_reason in {stuck,hard_timeout,infra,system_error} | files_changed==0"
COARSE_RULE_FALLBACK = "loop_detected | retry_count>=2"

KILL_REASONS = (
    "none", "cleanup_race", "stuck", "hard_timeout", "over_budget_l2", "over_budget_l3",
    "metering_unavailable", "system_error", "infra",
)

WHITELIST_TOP_KEYS = {
    "run_ref", "status", "exit_code", "verify_ok", "retry_count", "kill_reason",
    "degraded", "loop_detected", "crash_but_verified", "duration_sec", "timing",
    "change_stats", "trajectory", "verification_confidence", "verification",
    "failure_reason", "patch_excerpt", "control",
}

FORBIDDEN_SUBSTRINGS = (
    "/Users/", "/home/", ".agent_go", "agent_go/task-", "failure_class",
    "instance_id", "test_patch", "gold_patch", "base_commit",
)

# ---------------------------------------------------------------------------
# 冻结题面（§6.1 英文全文；改动即需重开一轮）
# ---------------------------------------------------------------------------

FROZEN_QUESTIONS: Dict[str, Any] = {
    "review_class": {
        "type": "choice",
        "instructions": {
            "question": "What is the root cause of this failed subtask?",
            "focus": "Decide where the fix would have to be made: in the delivered content, or in the verification / harness / process around it.",
        },
        "criteria": {
            "content_fix": {
                "what": "The root cause is in the delivered content itself: a wrong fix, a missed change, a wrong location, or a partial implementation.",
                "not_for": "Defects in the verifier, sandbox, timeouts, dependencies, harness, or process.",
                "examples": [
                    "The implementation omitted an edge case required by the task.",
                    "The change was made in the wrong file.",
                ],
            },
            "infra_or_process": {
                "what": "The root cause is in the verifier, sandbox, timeout, tooling / network, budget, upstream dependency, harness, or process.",
                "not_for": "Errors in the delivered content itself.",
                "examples": [
                    "The verification command had a syntax error while the code was correct.",
                    "A sandbox environment mismatch failed correct code.",
                ],
            },
            "insufficient_evidence": {
                "what": "The provided state does not carry enough evidence to assign either root cause.",
                "not_for": "A case that merely looks difficult; use this only when the evidence is absent.",
            },
        },
    },
    "control_change_nonempty": {
        "type": "noul",
        "instructions": "Did this subtask produce a non-empty code change?",
        "criteria": {
            "true": {"what": "At least one file was added or modified.", "examples": ["2 files changed, 40 insertions"]},
            "false": {"what": "No file content changed.", "examples": ["0 files changed"]},
        },
    },
    # Q3 第二排序器（v1.1；官方 rerank 配方形态）——只作对照/探索，不进主判据。
    "ranking_noul": {
        "type": "noul",
        "instructions": {
            "question": "Is the root cause of this failed subtask in the delivered content itself?",
            "focus": "Answer yes only when the fix would have to change the delivered content (wrong fix, missed change, wrong location, partial implementation).",
        },
        "criteria": {
            "true": {
                "what": "The root cause is in the delivered content itself.",
                "examples": ["The implementation omitted an edge case required by the task."],
            },
            "false": {
                "what": "The root cause is outside the delivered content: verifier, sandbox, timeout, tooling, budget, dependency, harness, or process.",
                "examples": ["The verification command had a syntax error while the code was correct."],
            },
        },
    },
}

CJK_RE = re.compile(r"[\u3400-\u9fff]")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def questions_sha256() -> str:
    return sha256_text(canonical_json(FROZEN_QUESTIONS))


def run_ref_for(task_id: str, subtask_id: str) -> str:
    return sha256_text(f"{task_id}/{subtask_id}")[:16]


# ---------------------------------------------------------------------------
# 脱敏（§5.3）
# ---------------------------------------------------------------------------

_RE_PATH = re.compile(r"(?:/Users|/home|/tmp|/private/var|/var/folders)/[^\s\"'`,;)\]}]*")
_RE_URL = re.compile(r"https?://[^\s\"'`,;)\]}]+")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_HEX40 = re.compile(r"\b[0-9a-f]{40}\b")


def sanitize(text: Any, *, repo_path: Optional[str] = None, worktree: Optional[str] = None,
             task_id: Optional[str] = None) -> str:
    if not isinstance(text, str):
        return ""
    s = text
    for literal in (worktree, repo_path):
        if literal:
            s = s.replace(str(literal), "<path>")
    if task_id:
        s = s.replace(task_id, "<task>")
    s = _RE_PATH.sub("<path>", s)
    s = _RE_URL.sub("<url>", s)
    s = _RE_EMAIL.sub("<url>", s)
    s = _RE_HEX40.sub("<sha>", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# state 构建（§5.1 白名单）
# ---------------------------------------------------------------------------

def _normalize_kill_reason(value: Any) -> str:
    if value in (None, ""):
        return "none"
    return value if value in KILL_REASONS else "other"


def _pick_counts(d: Any, keys: Sequence[str]) -> Dict[str, Any]:
    src = d if isinstance(d, dict) else {}
    return {k: src.get(k) for k in keys}


def build_state(result: Dict[str, Any], *, run_ref: str, signals: Optional[Dict[str, Any]],
                repo_path: Optional[str], worktree: Optional[str], task_id: Optional[str],
                patch_excerpt: Optional[str] = None) -> Dict[str, Any]:
    """把 result dict（+ 轨迹信号）映射为外发 state（键 ⊆ WHITELIST_TOP_KEYS）。"""
    change_stats = _pick_counts(result.get("change_stats"),
                                ("files_changed", "insertions", "deletions", "new_files", "modified_files"))
    files_changed = change_stats.get("files_changed")
    sig = signals or {}
    path_violations = sig.get("path_violations") or []
    repeated = sig.get("repeated_edits") or []
    repeated_counts = [int(e.get("count") or 0) for e in repeated if isinstance(e, dict)]
    conf = _pick_counts(result.get("verification_confidence"), ("level", "anchoring", "warning"))
    if conf.get("warning"):
        conf["warning"] = truncate(sanitize(conf["warning"], repo_path=repo_path, worktree=worktree,
                                            task_id=task_id), 200)
    verification: List[Dict[str, Any]] = []
    for item in result.get("verification_results") or []:
        if not isinstance(item, dict) or "command" not in item:
            continue
        verification.append({
            "command": truncate(sanitize(item.get("command"), repo_path=repo_path, worktree=worktree,
                                         task_id=task_id), 300),
            "exit_code": item.get("exit_code"),
            "attempt": item.get("attempt"),
            "stdout_tail": truncate(sanitize(item.get("stdout_tail"), repo_path=repo_path,
                                             worktree=worktree, task_id=task_id), 800),
            "stderr_tail": truncate(sanitize(item.get("stderr_tail"), repo_path=repo_path,
                                             worktree=worktree, task_id=task_id), 800),
        })
    state: Dict[str, Any] = {
        "run_ref": run_ref,
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "verify_ok": bool(result.get("verify_ok")),
        "retry_count": result.get("retry_count"),
        "kill_reason": _normalize_kill_reason(result.get("kill_reason")),
        "degraded": bool(result.get("degraded")),
        "loop_detected": bool(result.get("loop_detected")),
        "crash_but_verified": bool(result.get("crash_but_verified")),
        "duration_sec": result.get("duration_sec"),
        "timing": _pick_counts(result.get("timing"), ("claude_execute_ms", "verification_ms")),
        "change_stats": change_stats,
        "trajectory": {
            "steps": sig.get("steps"),
            "tool_calls": sig.get("tool_calls"),
            "tool_errors": sig.get("tool_errors"),
            "mutations": sig.get("mutations"),
            "mutation_without_worktree_change": sig.get("mutation_without_worktree_change"),
            "path_violation_n": len(path_violations),
            "repeated_edit_max": max(repeated_counts) if repeated_counts else 0,
            "repeated_edit_files_n": len(repeated),
        },
        "verification_confidence": conf,
        "verification": verification,
        "failure_reason": truncate(sanitize(result.get("failure_reason"), repo_path=repo_path,
                                            worktree=worktree, task_id=task_id), 300),
        "control": {"change_nonempty": bool(files_changed)},
    }
    if patch_excerpt:
        state["patch_excerpt"] = truncate(sanitize(patch_excerpt, repo_path=repo_path,
                                                   worktree=worktree, task_id=task_id), 2000)
    return state


# ---------------------------------------------------------------------------
# 池构建与证据快照（§4）
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_subtask_signals(task_dir: Path, sub_id: str, worktree: str) -> Optional[Dict[str, Any]]:
    try:
        from agent_go.trajectory_signals import collect_subtask_signals  # 仓内复用（fail-open）
    except Exception:
        return None
    try:
        return collect_subtask_signals(task_dir, sub_id, worktree)
    except Exception:
        return None


def _git_diff(worktree: Path, base_commit: str) -> str:
    if not base_commit or not (worktree / ".git").exists():
        return ""
    try:
        proc = subprocess.run(["git", "-C", str(worktree), "diff", f"{base_commit}..HEAD"],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout or ""
    except Exception:
        return ""


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    evidence_root = out / "evidence"
    evidence_root.mkdir(exist_ok=True)
    state_dir = out / "state"
    state_dir.mkdir(exist_ok=True)

    batch_path = Path(args.results).expanduser()
    records = [json.loads(line) for line in batch_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pool: List[Dict[str, Any]] = []
    missing_task_dirs = 0
    state_keys_union: set = set()

    for rec in records:
        task_dir = Path(str(rec.get("task_dir") or ""))
        if not task_dir.is_dir() or not (task_dir / "meta.json").is_file():
            missing_task_dirs += 1
            continue
        meta = _load_json(task_dir / "meta.json")
        task_id = str(meta.get("task_id") or task_dir.name)
        base_commit = str(meta.get("base_commit") or "")
        repo_path = str(meta.get("repo") or "")
        for result in meta.get("results") or []:
            if not isinstance(result, dict) or result.get("status") != "failed":
                continue
            sub_id = str(result.get("subtask_id") or "")
            if not sub_id:
                continue
            ref = run_ref_for(task_id, sub_id)
            worktree = str(result.get("worktree") or "")
            wt_path = Path(worktree) if worktree else None
            ev_dir = evidence_root / ref
            ev_dir.mkdir(exist_ok=True)
            result_json = task_dir / sub_id / "result.json"
            if result_json.is_file():
                (ev_dir / "result.json").write_text(result_json.read_text(encoding="utf-8"), encoding="utf-8")
            (ev_dir / "meta_entry.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            diff_text = ""
            if wt_path and wt_path.is_dir():
                diff_text = _git_diff(wt_path, base_commit)
            if diff_text:
                (ev_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
            signals = _collect_subtask_signals(task_dir, sub_id, worktree)
            state = build_state(result, run_ref=ref, signals=signals, repo_path=repo_path,
                                worktree=worktree, task_id=task_id,
                                patch_excerpt=diff_text if args.patch_excerpt else None)
            (state_dir / f"{ref}.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            state_keys_union.update(state.keys())
            (ev_dir / "notes.json").write_text(json.dumps({
                "task_id": task_id, "subtask_id": sub_id,
                "worktree_present": bool(wt_path and wt_path.is_dir()),
                "trajectory_signals": bool(signals),
                "diff_bytes": len(diff_text),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            pool.append({
                "run_ref": ref, "task_id": task_id, "subtask_id": sub_id,
                "status": result.get("status"), "verify_ok": bool(result.get("verify_ok")),
                "evidence_dir": str(ev_dir.relative_to(out)),
            })

    limit = int(args.limit or 0)
    if limit and len(pool) > limit:
        pool = pool[:limit]
    (out / "pool.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in pool) + ("\n" if pool else ""), encoding="utf-8")
    (out / "questions.json").write_text(
        json.dumps(FROZEN_QUESTIONS, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stage": args.stage,
        "batch_results": str(batch_path),
        "batch_record_count": len(records),
        "missing_task_dirs": missing_task_dirs,
        "sample_count": len(pool),
        "sample_sha256": sha256_text(canonical_json([x["run_ref"] for x in pool])),
        "state_keys_sha256": sha256_text(canonical_json(sorted(state_keys_union))),
        "questions_sha256": questions_sha256(),
        "coarse_rule": COARSE_RULE_MAIN,
        "coarse_rule_fallback": COARSE_RULE_FALLBACK,
        "tau": DEFAULT_TAU,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[build] pool={len(pool)} 条 failed 子任务；batch 记录={len(records)}；"
          f"task_dir 缺失={missing_task_dirs}；out={out}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 门禁（§6.2/§6.3）
# ---------------------------------------------------------------------------

def lint_questions(questions: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    text = json.dumps(questions, ensure_ascii=False)
    if CJK_RE.search(text):
        problems.append("语言门：题面混入中文（必须英文）")
    if set(questions.keys()) != set(FROZEN_QUESTIONS.keys()):
        problems.append("题面键集与冻结版本不一致")
    q1 = questions.get("review_class") or {}
    if q1.get("type") != "choice":
        problems.append("review_class 必须是 choice")
    criteria = q1.get("criteria") or {}
    if not isinstance(criteria, dict) or len(criteria) < 2:
        problems.append("choice criteria 必须为非空 map（≥2 选项）")
    if "insufficient_evidence" not in criteria:
        problems.append("choice 缺兜底选项 insufficient_evidence")
    for label, spec in criteria.items():
        if not isinstance(spec, dict) or "what" not in spec or "not_for" not in spec:
            problems.append(f"结构化 criteria 缺 what/not_for：{label}")
    q2 = questions.get("control_change_nonempty") or {}
    if q2.get("type") != "noul":
        problems.append("control_change_nonempty 必须是 noul")
    for qname, q in questions.items():
        if q.get("type") == "noul" and set((q.get("criteria") or {}).keys()) != {"true", "false"}:
            problems.append(f"{qname}: noul criteria 必须恰为 true/false")
    for qname, q in questions.items():
        instr = q.get("instructions")
        probe = json.dumps(instr, ensure_ascii=False) if isinstance(instr, dict) else str(instr or "")
        low = probe.lower()
        for bad in ("how many", "count how", "how often"):
            if bad in low:
                problems.append(f"{qname}: 命中数学/计数措辞（'{bad}'）")
    return problems


def coarse_hit(entry: Dict[str, Any]) -> bool:
    return bool(
        entry.get("loop_detected")
        or (entry.get("retry_count") or 0) >= 2
        or entry.get("kill_reason") in ("stuck", "hard_timeout", "infra", "system_error")
        or (entry.get("change_stats") or {}).get("files_changed") == 0
    )


def control_base_rate(states: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    pos = sum(1 for s in states if (s.get("control") or {}).get("change_nonempty"))
    return pos, len(states) - pos


def cmd_check(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    stage = args.stage or "pilot"
    problems: List[str] = []
    warnings: List[str] = []

    manifest = _load_json(out / "manifest.json")
    pool = [json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    states = [_load_json(p) for p in sorted((out / "state").glob("*.json"))]

    # 指纹复算
    if sha256_text(canonical_json([x["run_ref"] for x in pool])) != manifest.get("sample_sha256"):
        problems.append("R-J2c：sample_sha256 不匹配（池被改）")
    if questions_sha256() != manifest.get("questions_sha256"):
        problems.append("R-J2a：questions_sha256 不匹配（题面被改）")
    keys_union = sorted({k for s in states for k in s.keys()})
    if sha256_text(canonical_json(keys_union)) != manifest.get("state_keys_sha256"):
        warnings.append("R-J2b：state_keys_sha256 不匹配（软告警，检查是否新增字段）")

    # 白名单与禁入
    for state in states:
        extra = set(state.keys()) - WHITELIST_TOP_KEYS
        if extra:
            problems.append(f"R-J1b：state 越界键 {sorted(extra)}（ref={state.get('run_ref')}）")
        blob = json.dumps(state, ensure_ascii=False)
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in blob:
                problems.append(f"R-J1a：禁入项命中 '{bad}'（ref={state.get('run_ref')}）")

    # lint
    problems.extend(lint_questions(FROZEN_QUESTIONS))

    # 对照基率
    pos, neg = control_base_rate(states)
    if stage == "pilot":
        if pos < 3 or neg < 3:
            problems.append(f"H6 对照基率退化：两类各需 ≥3（正={pos}／负={neg}）——补样或更换对照题")
    else:
        minority = min(pos, neg)
        if states and minority / len(states) < 0.20:
            problems.append(f"H6 对照基率退化：少数类需 ≥20%（正={pos}／负={neg}）")

    # 粗规则选择性
    hits = sum(1 for s in states if coarse_hit(s))
    rate = (hits / len(states)) if states else 0.0
    if rate > 0.80:
        problems.append(f"粗规则命中率 {rate:.0%} >80%：视为退化基线——改用 fallback（{COARSE_RULE_FALLBACK}）并重冻")
    elif rate < 0.20:
        warnings.append(f"粗规则命中率 {rate:.0%} <20%：与默认序可能无差异，须人工确认后重冻")

    for w in warnings:
        print(f"[check][warn] {w}")
    for p in problems:
        print(f"[check][FAIL] {p}")
    if problems:
        print(f"[check] 未通过（{len(problems)} 项）——禁止耗调用")
        return EXIT_GATE
    print(f"[check] 通过：样本={len(states)}；粗规则命中率={rate:.0%}；对照正/负={pos}/{neg}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 问题包 / 调用 / 记录 / 标注
# ---------------------------------------------------------------------------

def cmd_packets(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    pool = [json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = ["# jev 复核排序 · 人审阅包（外发前）", "",
             "> 每条一行：外发类别自评必须人工填写。state 全文见 `state/<run_ref>.json`。", ""]
    for entry in pool:
        ref = entry["run_ref"]
        state = _load_json(out / "state" / f"{ref}.json")
        lines.append(f"## {ref}（{entry['task_id']} / {entry['subtask_id']}）")
        lines.append(f"- status={state.get('status')} verify_ok={state.get('verify_ok')} "
                     f"retry={state.get('retry_count')} kill={state.get('kill_reason')} "
                     f"files_changed={(state.get('change_stats') or {}).get('files_changed')}")
        cmd_tail = (state.get("verification") or [{}])[0].get("command", "")
        lines.append(f"- 验证命令（脱敏截断）：`{cmd_tail}`")
        lines.append("- 外发类别自评：____（模板：规范化派生指标〔补丁形态/验证输出片段〕，无人名、无绝对路径、无题面与 gold）")
        lines.append("")
    (out / "packets.md").write_text("\n".join(lines), encoding="utf-8")
    payload = {"questions": FROZEN_QUESTIONS,
               "items": [{"run_ref": e["run_ref"], "state_path": f"state/{e['run_ref']}.json"} for e in pool]}
    (out / "packets.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[packets] {len(pool)} 条 → packets.json / packets.md（人审阅＋自评后调用）")
    return EXIT_OK


def _rpc(proc: subprocess.Popen, msg: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """发一条 JSON-RPC 并等同 id 回包；超时真生效（后台读线程 + join）。

    读线程是 daemon：超时后本连接即视为不可用，调用方须销毁进程——否则残留线程
    会继续吞掉 stdout，后续调用将永远等不到回包。
    """
    if proc.stdin is None or proc.stdout is None:
        raise TimeoutError("MCP server 管道不可用")
    try:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise TimeoutError(f"MCP server 已退出：{exc}") from exc
    holder: Dict[str, Any] = {}

    def _reader() -> None:
        while True:
            try:
                line = proc.stdout.readline()
            except (ValueError, OSError):
                return
            if not line:
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == msg.get("id"):
                holder["resp"] = obj
                return

    thread = threading.Thread(target=_reader, name="jev-rpc-reader", daemon=True)
    thread.start()
    thread.join(timeout)
    if "resp" in holder:
        return holder["resp"]
    raise TimeoutError(f"MCP server 无响应（{timeout:.0f}s 超时）")


def _terminate(proc: subprocess.Popen) -> None:
    """terminate → wait(5s) → kill 兜底，避免僵尸/孤儿进程。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def cmd_call(args: argparse.Namespace) -> int:
    if not args.confirmed:
        print("[call] 需 --confirmed 显式确认（B 情境人闸门）", file=sys.stderr)
        return EXIT_USAGE
    server = args.server or os.environ.get("JEV_MCP_SERVER", "")
    if not server:
        print("[call] 缺少 --server 或 JEV_MCP_SERVER（跨仓锚点须显式固定）", file=sys.stderr)
        return EXIT_USAGE
    out = Path(args.out).expanduser()
    pool = [json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    results_path = out / "results.json"
    results: List[Dict[str, Any]] = json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else []
    done = {r.get("run_ref") for r in results
            if not r.get("error") and _extract_answers(r.get("response") or {})}
    todo = [e for e in pool if e["run_ref"] not in done]
    if not todo:
        print("[call] 无待调用样本")
        return EXIT_OK
    rpc_timeout = float(args.rpc_timeout) if args.rpc_timeout is not None else 90.0
    print(f"[call] 串行调用 {len(todo)} 条（单次超时 {rpc_timeout:.0f}s；H6 不过即整轮作废）")
    # stderr 落文件而非 PIPE：server 的报错输出无人读时会写满管道把 server 顶死
    err_log = (out / "caller-server.err.log").open("a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=err_log, text=True)
    audit = out / "caller-audit.log"
    code = EXIT_OK
    try:
        try:
            _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "jev_triage", "version": "0.1"}}},
                 timeout=rpc_timeout)
        except TimeoutError as exc:
            print(f"[call] initialize 失败：{exc}（详见 caller-server.err.log）", file=sys.stderr)
            return EXIT_INCOMPLETE
        for i, entry in enumerate(todo):
            ref = entry["run_ref"]
            state = _load_json(out / "state" / f"{ref}.json")
            try:
                resp = _rpc(proc, {"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                                   "params": {"name": "jev_decide",
                                              "arguments": {"state": state, "questions": FROZEN_QUESTIONS}}},
                            timeout=rpc_timeout)
                item = {"run_ref": ref, "response": resp, "error": "",
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            except TimeoutError as exc:
                # 连接已不可信：落盘一条带 error 的记录（--analyze 完整性门会挡住），人工决定续跑
                item = {"run_ref": ref, "response": {}, "error": str(exc),
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                results[:] = [r for r in results if r.get("run_ref") != ref]
                results.append(item)
                results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[call] {i + 1}/{len(todo)} {ref} 失败：{exc}——已落盘 {len(results)} 条，"
                      f"修复后重跑可续（成功条目不会重复调用）", file=sys.stderr)
                code = EXIT_INCOMPLETE
                break
            results[:] = [r for r in results if r.get("run_ref") != ref]
            results.append(item)
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            with audit.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_ref": ref,
                                     "state_keys": sorted(state.keys())}, ensure_ascii=False) + "\n")
            print(f"[call] {i + 1}/{len(todo)} {ref} ok")
    finally:
        _terminate(proc)
        err_log.close()
    return code


def _extract_answers(response: Dict[str, Any]) -> Dict[str, Any]:
    """从 MCP tools/call 回包中提取 answers（兼容 structuredContent / content[].text）。"""
    result = response.get("result") or {}
    payload = result.get("structuredContent")
    if isinstance(payload, dict) and "answers" in payload:
        return payload
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                obj = json.loads(item.get("text") or "")
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "answers" in obj:
                return obj
    return {}


def cmd_record(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    response = _load_json(Path(args.response).expanduser())
    results_path = out / "results.json"
    results: List[Dict[str, Any]] = json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else []
    results = [r for r in results if r.get("run_ref") != args.record]
    error = ""
    if response.get("error") or not _extract_answers(response):
        error = "回包错误或无可解析 answers"
    results.append({"run_ref": args.record, "response": response, "error": error,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if error:
        print(f"[record] {args.record} 已记录但标记为不完整（{error}）——--analyze 完整性门将拦截",
              file=sys.stderr)
        return EXIT_INCOMPLETE
    print(f"[record] {args.record} 已记录（共 {len(results)} 条）")
    return EXIT_OK


def _load_labels(out: Path) -> Dict[str, Dict[str, Any]]:
    """读 labels.jsonl（append-only 跨轮标签库；后写覆盖前写，便于人工改判）。"""
    path = out / "labels.jsonl"
    rows: Dict[str, Dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("run_ref"):
                rows[row["run_ref"]] = row
    return rows


def _append_labels(out: Path, rows: Sequence[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with (out / "labels.jsonl").open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


_LABEL_KEYS = {"c": "content_fix", "i": "infra_or_process", "u": "undecidable"}
LABELS = tuple(_LABEL_KEYS.values())


def _label_summary(state: Dict[str, Any]) -> str:
    stats = state.get("change_stats") or {}
    ver = (state.get("verification") or [{}])[0]
    return (f"status={state.get('status')} verify_ok={state.get('verify_ok')} "
            f"retry={state.get('retry_count')} kill={state.get('kill_reason')} "
            f"files_changed={stats.get('files_changed')} "
            f"failure_reason={(state.get('failure_reason') or '')[:160]!r} "
            f"command={(ver.get('command') or '')[:160]!r}")


def cmd_label(args: argparse.Namespace) -> int:
    """人工盲标（先于查看 results.json，流程纪律）+ 记录单条耗时。"""
    out = Path(args.out).expanduser()
    pool = [json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if (out / "results.json").is_file():
        print("[label] 提示：results.json 已存在。盲标纪律要求先写标签后看结果——请自证顺序（§7）")
    existing = _load_labels(out)
    todo = [e for e in pool if e["run_ref"] not in existing]
    if not todo:
        print(f"[label] 全部 {len(pool)} 条已有标签（labels.jsonl，重跑自动跳过）")
        return EXIT_OK
    print(f"[label] 待标 {len(todo)}/{len(pool)} 条；c=content_fix / i=infra_or_process / "
          f"u=undecidable / s=跳过 / q=退出（跳过项可重跑补标）")
    rows: List[Dict[str, Any]] = []
    for idx, entry in enumerate(todo, start=1):
        ref = entry["run_ref"]
        state = _load_json(out / "state" / f"{ref}.json")
        print(f"\n[{idx}/{len(todo)}] {ref}（{entry.get('task_id')}/{entry.get('subtask_id')}）")
        print(f"  {_label_summary(state)}")
        raw = input("  标签[c/i/u/s/q]：").strip().lower()
        if raw in ("q", "quit"):
            break
        if raw in ("", "s"):
            continue
        if raw not in _LABEL_KEYS:
            print("  无效输入——本条跳过（可重跑补标）")
            continue
        minutes_raw = input("  本条目耗时(分钟，可空)：").strip()
        try:
            minutes: Optional[float] = float(minutes_raw) if minutes_raw else None
        except ValueError:
            minutes = None
        rows.append({"run_ref": ref, "label": _LABEL_KEYS[raw], "origin": "human",
                     "minutes": minutes, "questions_sha256": questions_sha256(),
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    written = _append_labels(out, rows)
    print(f"\n[label] 已写 {written} 条 → labels.jsonl（累计 {len(existing) + written} 条）")
    return EXIT_OK


def cmd_import_labels(args: argparse.Namespace) -> int:
    """批量导入人工标签（跨轮合并/离线补录）；整批校验，一处不合规即拒收（fail-closed）。"""
    out = Path(args.out).expanduser()
    pool_refs = {e["run_ref"] for e in
                 (json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())}
    src = Path(args.import_labels).expanduser()
    if not src.is_file():
        print(f"[import-labels] 文件不存在：{src}", file=sys.stderr)
        return EXIT_USAGE
    existing = _load_labels(out)
    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    seen: set = set()
    for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[import-labels] L{lineno} 非 JSON（{exc}）——整批拒收", file=sys.stderr)
            return EXIT_USAGE
        ref, label = row.get("run_ref"), row.get("label")
        if ref not in pool_refs:
            print(f"[import-labels] L{lineno} run_ref 不在本轮池中：{ref!r}——整批拒收", file=sys.stderr)
            return EXIT_USAGE
        if label not in LABELS:
            print(f"[import-labels] L{lineno} 非法标签 {label!r}（允许：{'/'.join(LABELS)}）——整批拒收",
                  file=sys.stderr)
            return EXIT_USAGE
        if ref in existing or ref in seen:
            skipped.append(ref)
            continue
        seen.add(ref)
        rows.append({"run_ref": ref, "label": label, "origin": "human",
                     "minutes": row.get("minutes"), "questions_sha256": questions_sha256(),
                     "source": f"import:{src.name}", "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    written = _append_labels(out, rows)
    print(f"[import-labels] 新增 {written} 条、跳过已标/重复 {len(skipped)} 条 → labels.jsonl")
    return EXIT_OK


def _parse_choice(answers: Dict[str, Any], qname: str) -> Tuple[Optional[str], Dict[str, float], Optional[float]]:
    ans = (answers.get("answers") or {}).get(qname) or {}
    probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items() if isinstance(v, (int, float))}
    choice = ans.get("choice")
    conf = ans.get("confidence")
    return choice, probs, conf


def rank_entries(results: Sequence[Dict[str, Any]], states: Dict[str, Dict[str, Any]],
                 tau: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in results:
        ref = item.get("run_ref")
        answers = _extract_answers(item.get("response") or {})
        choice, probs, conf = _parse_choice(answers, "review_class")
        noul_score = ((answers.get("answers") or {}).get("ranking_noul") or {}).get("noul")
        p_content = probs.get("content_fix")
        p_max = max(probs.values()) if probs else None
        abstain = (choice == "insufficient_evidence") or (p_max is not None and p_max < tau)
        rows.append({"run_ref": ref, "choice": choice, "probabilities": probs,
                     "confidence": conf, "p_content": p_content, "p_max": p_max,
                     "noul_score": float(noul_score) if isinstance(noul_score, (int, float)) else None,
                     "abstain": bool(abstain), "model": (answers.get("model") or None)})
    ranked = [r for r in rows if not r["abstain"] and r["p_content"] is not None]
    ranked.sort(key=lambda r: (-(r["p_content"] or 0.0), str(r["run_ref"])))
    ranked_refs = {r["run_ref"] for r in ranked}
    others = [r for r in rows if r["run_ref"] not in ranked_refs]
    others.sort(key=lambda r: str(r["run_ref"]))  # 弃权/低置信单列，按 ref 稳定排序
    return ranked + others


def _control_hit_ratio(results: Sequence[Dict[str, Any]], states: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], int, int]:
    hits = 0
    total = 0
    for item in results:
        ref = item.get("run_ref")
        state = states.get(ref)
        if not state:
            continue
        answers = _extract_answers(item.get("response") or {})
        noul = ((answers.get("answers") or {}).get("control_change_nonempty") or {}).get("noul")
        truth = bool((state.get("control") or {}).get("change_nonempty"))
        total += 1
        if not isinstance(noul, (int, float)):
            # 回包缺该问项＝仪器失效样本，按未命中计——不得静默缩小分母（H6 是"整轮作废"闸门）
            continue
        if (float(noul) >= 0.5) == truth:
            hits += 1
    return (hits / total if total else None), hits, total


# ---------------------------------------------------------------------------
# 统计（§9.1）
# ---------------------------------------------------------------------------

def effort_at_recall(ranked_refs: Sequence[str], targets: Dict[str, int], r: float) -> Optional[int]:
    total_targets = sum(1 for v in targets.values() if v == 1)
    if total_targets == 0:
        return None
    need = max(1, math.ceil(r * total_targets))
    seen = 0
    for idx, ref in enumerate(ranked_refs, start=1):
        if targets.get(ref) == 1:
            seen += 1
            if seen >= need:
                return idx
    return None


def precision_at_k(ranked_refs: Sequence[str], targets: Dict[str, int], k: int) -> Optional[float]:
    top = ranked_refs[:k]
    if not top:
        return None
    return sum(1 for ref in top if targets.get(ref) == 1) / len(top)


def _avg_ranks(values: Sequence[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_mean_rank(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return (num / den) if den else None


def cluster_bootstrap(clusters: Sequence[Sequence[float]], metric, rounds: int = 2000,
                      seed: int = 20261005) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """按任务聚类 bootstrap CI（指标函数吃展平后的样本序列）。"""
    rng = random.Random(seed)
    point = metric([v for c in clusters for v in c])
    if point is None:
        return None, None, None
    samples: List[float] = []
    for _ in range(rounds):
        picked: List[float] = []
        for _ in range(len(clusters)):
            picked.extend(clusters[rng.randrange(len(clusters))])
        value = metric(picked)
        if value is not None:
            samples.append(value)
    if not samples:
        return point, None, None
    samples.sort()
    lo = samples[int(0.025 * (len(samples) - 1))]
    hi = samples[int(0.975 * (len(samples) - 1))]
    return point, lo, hi


# ---------------------------------------------------------------------------
# 分析（§9.1/§9.2/§9.3；pilot 只出四件事）
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    stage = args.stage or "pilot"
    tau = float(args.tau if args.tau is not None else DEFAULT_TAU)
    pool = [json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    states = {p.stem: _load_json(p) for p in sorted((out / "state").glob("*.json"))}
    results_path = out / "results.json"
    results: List[Dict[str, Any]] = json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else []

    # 完整性门（fail-closed）：按 run_ref 集合比对而非计数——重复/串入样本同样不放行；
    # 错误回包（超时、isError、无 answers）按不完整计，宁可重跑也不出判据
    pool_refs = {e["run_ref"] for e in pool}
    result_refs = {r.get("run_ref") for r in results}
    if result_refs != pool_refs:
        missing = sorted(pool_refs - result_refs)
        extra = sorted(str(x) for x in result_refs - pool_refs)
        print(f"[analyze] 完整性门不过：缺 {missing}；多/串入 {extra}——拒绝出判据（exit 3）")
        return EXIT_INCOMPLETE
    errored = sorted(str(r.get("run_ref")) for r in results
                     if r.get("error") or not _extract_answers(r.get("response") or {}))
    if errored:
        print(f"[analyze] 完整性门不过：{len(errored)} 条错误/空回包 {errored}——"
              f"重跑补齐后（已完成条目不会重复调用）再分析（exit 3）")
        return EXIT_INCOMPLETE

    labels: Dict[str, str] = {}
    stale: List[str] = []
    for ref, row in _load_labels(out).items():
        if row.get("origin", "human") != "human" or row.get("label") not in LABELS:
            continue
        sha = row.get("questions_sha256")
        if sha and sha != questions_sha256():
            # 题面变过＝靶定义变过：跨 rubric 标签不得混入同一轮判据
            stale.append(ref)
            continue
        labels[ref] = row["label"]
    if stale:
        print(f"[analyze] 完整性门不过：{len(stale)} 条标签的 questions_sha256 与本轮冻结题面不一致 "
              f"{sorted(stale)[:5]}——按 §7 改题面即重开一轮（exit 3）")
        return EXIT_INCOMPLETE

    h6, hits, total = _control_hit_ratio(results, states)
    ranked = rank_entries(results, states, tau)
    abstained = [r for r in ranked if r["abstain"]]

    coarse = _coarse_order(pool, states)
    targets = {ref: (1 if labels.get(ref) == "content_fix" else 0)
               for ref in labels if labels.get(ref) in ("content_fix", "infra_or_process")}
    n_valid = len(targets)
    n_content = sum(1 for v in targets.values() if v == 1)

    report: Dict[str, Any] = {
        "stage": stage, "tau": tau, "samples": len(pool), "calls": len(results),
        "h6": {"ratio": h6, "hits": hits, "total": total, "pass": bool(h6 is not None and h6 >= 0.80)},
        "abstain": {"count": len(abstained), "refs": [r["run_ref"] for r in abstained]},
        "labels": {"valid": n_valid, "content": n_content,
                   "distribution": {k: sum(1 for v in labels.values() if v == k)
                                    for k in ("content_fix", "infra_or_process", "undecidable")}},
    }

    if stage == "pilot":
        # 四件事：H6／靶方差／对齐／粗规则选择性
        dist = report["labels"]["distribution"]
        degenerate = bool(n_valid and max(dist.values()) / max(1, sum(dist.values())) >= 0.90)
        aligned = _alignment_check(ranked, labels)
        report["pilot_gates"] = {
            "h6": report["h6"]["pass"],
            "target_variance": {"valid": n_valid, "degenerate": degenerate,
                                "pass": (n_valid >= 6 and not degenerate)},
            "alignment": aligned,
            "coarse_selectivity": _coarse_selectivity(pool, states),
        }
        gates = report["pilot_gates"]
        ok = (gates["h6"] and gates["target_variance"]["pass"]
              and gates["alignment"].get("pass") and gates["coarse_selectivity"]["pass"])
        report["verdict"] = "M0.5-进入 M1" if ok else "M0.5-停（归档负结果）"
    else:
        non_abstain = [r for r in ranked if not r["abstain"]]
        metrics = _full_metrics(non_abstain, coarse, targets)
        report.update(metrics)
        report["exploratory"] = {"noul_ranker": _noul_exploratory(non_abstain, targets)}
        report["verdict"] = _four_state(report, stage=stage)

    (out / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK if report.get("h6", {}).get("pass", False) or stage == "pilot" else EXIT_OK


def _coarse_order(pool: Sequence[Dict[str, Any]], states: Dict[str, Dict[str, Any]]) -> List[str]:
    def sort_key(entry: Dict[str, Any]) -> Tuple[int, str]:
        state = states.get(entry["run_ref"]) or {}
        return (0 if coarse_hit(state) else 1, entry["run_ref"])
    return [e["run_ref"] for e in sorted(pool, key=sort_key)]


def _coarse_selectivity(pool: Sequence[Dict[str, Any]], states: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    hits = sum(1 for e in pool if coarse_hit(states.get(e["run_ref"]) or {}))
    rate = hits / len(pool) if pool else 0.0
    return {"hit_rate": rate, "pass": 0.20 <= rate <= 0.80}


def _alignment_check(ranked: Sequence[Dict[str, Any]], labels: Dict[str, str]) -> Dict[str, Any]:
    content = [r["p_content"] for r in ranked if labels.get(r["run_ref"]) == "content_fix" and r["p_content"] is not None]
    infra = [r["p_content"] for r in ranked if labels.get(r["run_ref"]) == "infra_or_process" and r["p_content"] is not None]
    if len(content) < 2 or len(infra) < 2:
        return {"pass": None, "reason": "样本不足（两类各需 ≥2）"}
    cm, im = statistics.mean(content), statistics.mean(infra)
    return {"pass": cm > im, "content_mean": cm, "infra_mean": im,
            "reason": "内容型平均 P(content) 应高于环境型（方向性）"}


def _full_metrics(ranked_rows: Sequence[Dict[str, Any]], coarse_refs: Sequence[str],
                  targets: Dict[str, int]) -> Dict[str, Any]:
    if not targets:
        return {"metrics": None, "reason": "无有效人工标签"}
    ranked_refs = [r["run_ref"] for r in ranked_rows]
    jev_effort = effort_at_recall(ranked_refs, targets, 0.5)
    coarse_effort = effort_at_recall(list(coarse_refs), targets, 0.5)
    # ρ 用评分（P(content_fix)）vs 标签：评分高↔内容型 ⇒ 正相关（JEP-1 口径）
    scored = [(r["p_content"], targets[r["run_ref"]]) for r in ranked_rows
              if r["run_ref"] in targets and r["p_content"] is not None]
    rho = None
    if len(scored) >= 3:
        rho = spearman_mean_rank([s for s, _ in scored], [float(t) for _, t in scored])
    return {
        "metrics": {
            "effort_at_recall_0.5": {"jev": jev_effort, "coarse": coarse_effort},
            "precision_at_4": precision_at_k(ranked_refs, targets, 4),
            "spearman_mean_rank": rho,
            "ci": {"status": "未实现（薄版）", "note": "effort/ρ 的按任务聚类 bootstrap CI 在 M1 补齐（§17.4）"},
        }
    }


def _noul_exploratory(non_abstain: Sequence[Dict[str, Any]],
                      targets: Dict[str, int]) -> Dict[str, Any]:
    """Q3 noul 第二排序器（v1.1）：与主排序同覆盖口径，**只作探索对照、不进主判据**。"""
    available = [r for r in non_abstain if r.get("noul_score") is not None]
    if len(available) < 3:
        return {"status": "样本不足", "available": len(available)}
    ordered = sorted(available, key=lambda r: (-float(r["noul_score"]), str(r["run_ref"])))
    refs = [r["run_ref"] for r in ordered]
    scored = [(r["run_ref"], float(r["noul_score"])) for r in ordered if r["run_ref"] in targets]
    rho = None
    if len(scored) >= 3:
        rho = spearman_mean_rank([s for _, s in scored],
                                 [float(targets[ref]) for ref, _ in scored])
    return {
        "available": len(available),
        "effort_at_recall_0.5": effort_at_recall(refs, targets, 0.5),
        "precision_at_4": precision_at_k(refs, targets, 4),
        "spearman_noul_vs_label": rho,
        "note": "探索性对照（官方 rerank 配方形态）；不进判据、不影响 Go/No-Go（O-11）",
    }


def _four_state(report: Dict[str, Any], *, stage: str) -> str:
    h6 = report.get("h6") or {}
    metrics = report.get("metrics") or {}
    if not h6.get("pass"):
        return "No-Go（仪器失效：H6 不过）"
    rho = (metrics.get("spearman_mean_rank"))
    if rho is not None and rho < 0:
        return "No-Go（ρ<0）"
    m = metrics.get("effort_at_recall_0.5") or {}
    jev, coarse = m.get("jev"), m.get("coarse")
    if jev is None or coarse is None:
        return "不可判（标签不足/口径缺失）"
    if jev < coarse:
        return "Go（effort 优于粗规则；n/CI/采纳条件另见 §9.2）"
    return "不可判（未超出粗规则——按 §9.3 需 H2 佐证）"


def cmd_queue(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    pool = {e["run_ref"]: e for e in
            (json.loads(line) for line in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())}
    results: List[Dict[str, Any]] = json.loads((out / "results.json").read_text(encoding="utf-8"))
    states = {p.stem: _load_json(p) for p in sorted((out / "state").glob("*.json"))}
    tau = float(args.tau if args.tau is not None else DEFAULT_TAU)
    ranked = rank_entries(results, states, tau)
    lines = ["# jev 复核队列（本地物料，禁止外发）", "",
             f"> 排序口径：P(content_fix) 降序、run_ref 破并列；τ={tau}；弃权单列。", "", "| rank | run_ref | P(content) | task/subtask |", "|---|---|---|---|"]
    for idx, row in enumerate([r for r in ranked if not r["abstain"]], start=1):
        entry = pool.get(row["run_ref"]) or {}
        p = row["p_content"]
        lines.append(f"| {idx} | {row['run_ref']} | {p if p is None else round(p, 3)} | "
                     f"{entry.get('task_id')}/{entry.get('subtask_id')} |")
    abstained = [r["run_ref"] for r in ranked if r["abstain"]]
    if abstained:
        lines += ["", "## 探针清单（弃权/低置信；不进主队列、不计入覆盖）", ""] + [f"- {r}" for r in abstained]
    (out / "review-queue.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[queue] {len(ranked)} 条 → review-queue.md（含探针清单 {len(abstained)} 条）")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="jev 复核排序试点仪器（零网络；详见需求文档 §17）")
    p.add_argument("--out", required=True, help="pilot 目录（~/.agent_go/jev/<pilot-id>）")
    p.add_argument("--stage", choices=["pilot", "full"], default=None)
    p.add_argument("--tau", type=float, default=None, help=f"低置信阈值（默认 {DEFAULT_TAU}）")
    p.add_argument("--build", action="store_true", help="从冻结批构建池与 state")
    p.add_argument("--results", help="bench 结果 JSONL（--build）")
    p.add_argument("--limit", type=int, default=0, help="最多取 N 条 failed 子任务（0=全部）")
    p.add_argument("--patch-excerpt", action="store_true", help="把 diff 摘要加入 state（实验变量）")
    p.add_argument("--check", action="store_true", help="机械门（R-J1/R-J2＋基率＋选择性）")
    p.add_argument("--packets", action="store_true", help="生成人审阅包")
    p.add_argument("--call", action="store_true", help="串行调用既有 MCP server（需 --server 与 --confirmed）")
    p.add_argument("--server", help="既有 jev MCP server 路径（跨仓锚点）")
    p.add_argument("--confirmed", action="store_true", help="人工确认（B 情境人闸门）")
    p.add_argument("--rpc-timeout", type=float, default=None, help="单次 JSON-RPC 超时秒数（默认 90）")
    p.add_argument("--record", metavar="RUN_REF", help="记录一次手工调用回包")
    p.add_argument("--response", help="--record 的回包 JSON 文件")
    p.add_argument("--label", action="store_true", help="人工盲标（先于查看 results）+ 单条耗时 → labels.jsonl")
    p.add_argument("--import-labels", metavar="FILE",
                   help="批量导入人工标签 JSONL（跨轮合并；一处不合规整批拒收）")
    p.add_argument("--analyze", action="store_true", help="分析（pilot=四件事；full=指标＋四态）")
    p.add_argument("--queue", action="store_true", help="生成 review-queue.md")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    actions = [a for a in ("build", "check", "packets", "call", "analyze", "queue", "label") if getattr(args, a)]
    if args.record:
        actions.append("record")
    if args.import_labels:
        actions.append("import_labels")
    if len(actions) != 1:
        print("[usage] 需要且仅需要一个动作（--build/--check/--packets/--call/--record/--label/"
              "--import-labels/--analyze/--queue）", file=sys.stderr)
        return EXIT_USAGE
    action = actions[0]
    if action == "build":
        if not args.results:
            print("[usage] --build 需要 --results", file=sys.stderr)
            return EXIT_USAGE
        return cmd_build(args)
    if action == "check":
        return cmd_check(args)
    if action == "packets":
        return cmd_packets(args)
    if action == "call":
        return cmd_call(args)
    if action == "record":
        if not args.response:
            print("[usage] --record 需要 --response", file=sys.stderr)
            return EXIT_USAGE
        return cmd_record(args)
    if action == "label":
        return cmd_label(args)
    if action == "import_labels":
        return cmd_import_labels(args)
    if action == "analyze":
        return cmd_analyze(args)
    return cmd_queue(args)


if __name__ == "__main__":
    sys.exit(main())
