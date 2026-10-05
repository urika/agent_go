"""Problem ↔ GitHub Issue 联动（M5 后续；A6 决策：显式开启，默认关，避免 issue 洪水）。

设计要点（见 ADR-015）：
  - **默认关**：`issues.enabled=false`；`--track-issues` 或 `agent_go issues sync --yes`
    才产生外发动作（创建/评论/关闭 GitHub issue 是外向行为，必须有显式人闸门）。
  - **fail-open**：`gh` 缺失/未登录/网络失败/超时 → 记警告，绝不阻塞主链路；
    失败项不写 `issue_synced`，下次同步自然重试。
  - **幂等**：`Problem.issue_synced` 记录上次同步时的 `occurrence_count` 与 `status`；
    只有三种漂移才动作——无 issue（create）、复发计数增加（comment）、已 resolved（close）。
  - **不外发本地证据**：issue 正文默认不含 `evidence` 字段（`--include-evidence` 才带）；
    正文对家目录/`.agent_go` 路径做脱敏。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .problems import Problem, load, _save

logger = logging.getLogger(__name__)

ISSUE_LABEL = "agent_go:problem"
GH_TIMEOUT = 30
DEFAULT_LIMIT = 20

# 外发正文脱敏：家目录与本地运行时目录不进 issue
_SCRUB_PATTERNS = (
    (re.compile(r"/Users/[^\s:/]+"), "<home>"),
    (re.compile(r"/home/[^\s:/]+"), "<home>"),
    (re.compile(r"[^\s:]*\.agent_go[^\s:]*"), "<agent_go-dir>"),
)


def tracking_enabled(config: Optional[dict[str, Any]]) -> bool:
    """是否启用联动（默认关；A6）。"""
    return bool((config or {}).get("issues", {}).get("enabled", False))


def include_evidence(config: Optional[dict[str, Any]]) -> bool:
    return bool((config or {}).get("issues", {}).get("include_evidence", False))


def scrubbed(text: str) -> str:
    out = str(text or "")
    for pattern, repl in _SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def issue_title(problem: Problem) -> str:
    kind = problem.failure_class or problem.root_cause_category or "failure"
    return scrubbed(f"[agent_go] {kind}: {problem.failure_pattern[:80]}")


def issue_body(problem: Problem, *, with_evidence: bool = False) -> str:
    """构造 issue 正文；默认不含本地证据（不外发原始 fail 输出）。"""
    lines = [
        f"<!-- agent_go:problem id={problem.id} -->",
        f"**failure_pattern**: `{scrubbed(problem.failure_pattern)}`",
        f"**failure_class**: {problem.failure_class or '-'} ｜ "
        f"**root_cause_category**: {problem.root_cause_category or '-'}",
        f"**occurrence_count**: {problem.occurrence_count} ｜ "
        f"**status**: {problem.status}",
        f"**first_seen**: {problem.first_seen_at or '-'} ｜ **last_seen**: {problem.last_seen_at or '-'}",
        f"**task/subtask**: {scrubbed(problem.task_id or '-')}/{scrubbed(problem.subtask_id or '-')}",
    ]
    if problem.summary:
        lines.append(f"\n**summary**: {scrubbed(problem.summary)}")
    if problem.root_cause:
        lines.append(f"\n**root_cause**: {scrubbed(problem.root_cause)}")
    if problem.resolution_summary:
        lines.append(f"\n**resolution**: {scrubbed(problem.resolution_summary)}")
    if with_evidence and problem.evidence:
        lines.append(f"\n**evidence（本地原始输出，已脱敏）**:\n```\n{scrubbed(problem.evidence)[:1500]}\n```")
    lines.append(f"\n_由 agent_go 自动同步；本地详情：`agent_go problems show {problem.id}`_")
    return "\n".join(lines)


def needs_sync(problem: Problem) -> str:
    """判定需要的同步动作：""（已同步）/ create / comment（复发）/ close（已解决）。"""
    marker = problem.issue_synced or {}
    if not problem.github_issue:
        return "create"
    if problem.status == "resolved" and marker.get("status") != "resolved":
        return "close"
    if int(marker.get("occurrence_count") or 0) < int(problem.occurrence_count or 1):
        return "comment"
    return ""


def _gh(args: List[str], timeout: int = GH_TIMEOUT) -> Tuple[int, str, str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _issue_number(url: str) -> str:
    m = re.search(r"/issues/(\d+)", url or "")
    return m.group(1) if m else ""


def _mark(problem: Problem, *, number: str = "") -> None:
    import time
    problem.issue_synced = {
        "occurrence_count": int(problem.occurrence_count or 1),
        "status": problem.status,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "number": number or str((problem.issue_synced or {}).get("number") or ""),
    }


def sync_problem(problem: Problem, *, config: Optional[dict[str, Any]] = None,
                 dry_run: bool = False, repo: str = "") -> Tuple[bool, str, str]:
    """同步单条 Problem；返回 (ok, action, detail)。任何外部失败都只是 (False, ...)。"""
    action = needs_sync(problem)
    if not action:
        return True, "", "已同步"
    if dry_run:
        return True, action, "dry-run（未执行）"
    if not shutil.which("gh"):
        return False, action, "gh CLI 不可用（未安装或不在 PATH）"

    with_evidence = include_evidence(config)
    repo_args = ["--repo", repo] if repo else []
    try:
        if action == "create":
            args = ["issue", "create", "--title", issue_title(problem),
                    "--body", issue_body(problem, with_evidence=with_evidence), "--label", ISSUE_LABEL, *repo_args]
            rc, out, err = _gh(args)
            if rc != 0 and "label" in (err or "").lower():
                # label 不存在于仓库：去掉 --label 重试一次（不因标签缺失漏报 issue）
                args = [a for a in args if a not in ("--label", ISSUE_LABEL)]
                rc, out, err = _gh(args)
            if rc != 0:
                return False, action, f"gh issue create 失败: {err.strip()[:200]}"
            url = out.strip().splitlines()[-1].strip() if out.strip() else ""
            problem.github_issue = url
            _mark(problem, number=_issue_number(url))
            return True, action, url

        if action == "comment":
            body = (f"复发：occurrence_count → {problem.occurrence_count}"
                    f"（last_seen {problem.last_seen_at}，task {scrubbed(problem.task_id or '-')}）")
            rc, _out, err = _gh(["issue", "comment", problem.github_issue, "--body", body, *repo_args])
            if rc != 0:
                return False, action, f"gh issue comment 失败: {err.strip()[:200]}"
            _mark(problem, number=str((problem.issue_synced or {}).get("number") or ""))
            return True, action, problem.github_issue

        if action == "close":
            body = f"已修复：{scrubbed(problem.resolution_summary or problem.root_cause or '见本地 Problem 详情')}"
            rc, _out, err = _gh(["issue", "close", problem.github_issue, "--comment", body, *repo_args])
            if rc != 0:
                return False, action, f"gh issue close 失败: {err.strip()[:200]}"
            _mark(problem, number=str((problem.issue_synced or {}).get("number") or ""))
            return True, action, problem.github_issue
    except subprocess.TimeoutExpired:
        return False, action, f"gh 调用超时（{GH_TIMEOUT}s）"
    except OSError as e:
        return False, action, f"gh 调用失败: {e}"
    return False, action, f"未知动作: {action}"


def sync_problems(problems_path: Path | str, *, config: Optional[dict[str, Any]] = None,
                  task_id: str = "", dry_run: bool = False, repo: str = "",
                  limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """批量同步（幂等、fail-open）。

    task_id 非空时只处理该任务触达的 Problem（`run --track-issues` 的自动路径）；
    留空＝全量扫描（`agent_go issues sync` 回填路径）。
    """
    path = Path(problems_path)
    if not dry_run and not tracking_enabled(config) and not task_id:
        return {"enabled": False, "considered": 0, "created": 0, "commented": 0,
                "closed": 0, "failed": 0, "details": [],
                "note": "issues.enabled=false（A6 默认关）：加 --track-issues 或 issues sync --yes"}
    problems = load(path)
    targets: List[Problem] = []
    for p in problems:
        if task_id and p.task_id != task_id:
            continue
        if needs_sync(p):
            targets.append(p)
    targets = targets[: max(1, int(limit or DEFAULT_LIMIT))]
    summary: Dict[str, Any] = {"enabled": True, "considered": len(targets),
                               "created": 0, "commented": 0, "closed": 0, "failed": 0,
                               "details": []}
    changed = False
    for problem in targets:
        ok, action, detail = sync_problem(problem, config=config, dry_run=dry_run, repo=repo)
        if not action:
            continue
        summary["details"].append({"problem_id": problem.id, "action": action,
                                   "ok": ok, "detail": detail})
        if not ok:
            summary["failed"] += 1
            logger.warning("[issue_link] %s %s 失败: %s", problem.id, action, detail)
            continue
        if dry_run:
            continue
        changed = True
        if action == "create":
            summary["created"] += 1
        elif action == "comment":
            summary["commented"] += 1
        elif action == "close":
            summary["closed"] += 1
    if changed:
        _save(path, problems)
    return summary
