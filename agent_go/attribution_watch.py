"""P2 盲区归因监视（opt-in）：watch index + Stop Hook 注入 + 会话聚合提醒。

设计（docs/design/blind-spot-attribution-workflow.md §5，2026-08-29 拍板 ① opt-in）：
- 生命周期：`agent_go trust --watch-repo <repo>` 显式开启（观察信任后可转自动）；
  `--off` 卸载。不做交付时自动注入。
- 时机：仅 Stop Hook——会话结束时聚合「未提交改动 ∩ 监视任务交付文件集」
  输出一条提醒（每会话最多一次，噪声最低）；PostToolUse/agent 代办留 MVP2。
- 注入安全（主 repo 与隔离 worktree 的关键差异）：
  * 合并式：保留用户已有 hooks/其他 settings 字段，仅 append 一条 Stop entry；
  * 幂等：command 已存在则跳过；
  * 可卸载：--off 精确移除本工具注入的 entry，其余原样保留；
  * hook 脚本放 ~/.agent_go/hooks/（repo 内零新增文件），settings.json 首次
    注入前备份。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .config import AGENT_GO_DIR

WATCH_INDEX_PATH = AGENT_GO_DIR / "attribution_watch.json"
HOOK_SCRIPT_PATH = AGENT_GO_DIR / "hooks" / "agent_go_attribution_stop.py"
HOOK_MARK = "agent_go_attribution_stop"

_OK_DELIVERY_STATES = {"completed", "DELIVERY_READY", "ACCEPTED_DELIVERY"}
_SIG_LABELS = (
    ("weakly_anchored_subtasks", "弱锚定"),
    ("inconclusive_evaluations", "评估不定"),
    ("uncovered_acceptance_ids", "漏验收"),
)


def load_index() -> dict:
    try:
        data = json.loads(WATCH_INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"repos": {}}
    except (OSError, json.JSONDecodeError):
        return {"repos": {}}


def save_index(index: dict) -> None:
    WATCH_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCH_INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_repo_tasks(repo: Path) -> dict[str, dict]:
    """扫描某 repo 的已交付任务 → {task_id: {files, blind_items}}。

    口径与 trust 返工信号一致：状态 ∈ 交付三态、meta.repo 匹配；交付文件集
    取 results[].summary diffstat 解析（复用 metrics._files_from_diff_stat），
    盲区项取三类预测性标注（wa/inc/uac）。无文件集或无标注的任务不登记
    （无可归因项 / 不可观察——避免空提醒）。
    """
    from .metrics import _files_from_diff_stat

    tasks: dict[str, dict] = {}
    for td in sorted(AGENT_GO_DIR.glob("task-*")):
        mp = td / "meta.json"
        if not mp.exists():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") not in _OK_DELIVERY_STATES:
            continue
        meta_repo = str(meta.get("repo", "") or meta.get("repo_path", ""))
        try:
            same = Path(meta_repo).resolve() == repo.resolve()
        except OSError:
            same = False
        if not same:
            continue
        files = sorted({f for r in (meta.get("results") or [])
                        if isinstance(r, dict)
                        for f in _files_from_diff_stat(str(r.get("summary", "")))
                        if f and not f.startswith("/")})[:50]
        if not files:
            continue
        blind = meta.get("blind_spots") or {}
        items = [f"{sig}:{k}" for sig, _ in _SIG_LABELS
                 for k in (blind.get(sig) or [])]
        if not items:
            continue
        tasks[str(meta.get("task_id") or td.name)] = {"files": files, "blind_items": items}
    return tasks



def install_hook(repo: Path, agent_go_dir: Optional[Path] = None) -> tuple[bool, str]:
    """opt-in 安装：登记 watch index + 写 hook 脚本 + 合并式注入 settings.json。

    幂等：重复调用只刷新 index，不重复注入 hook entry。
    """
    ag_dir = Path(agent_go_dir) if agent_go_dir else AGENT_GO_DIR
    repo = Path(repo).resolve()
    if not (repo / ".git").exists():
        return False, f"不是 git 仓库: {repo}"
    index = load_index()
    repos = index.setdefault("repos", {})
    entry = repos.setdefault(str(repo), {})
    entry.update({"watching": True, "installed_at": entry.get("installed_at") or _now(),
                  "tasks": scan_repo_tasks(repo)})
    save_index(index)

    hook_script = ag_dir / "hooks" / "agent_go_attribution_stop.py"
    hook_script.parent.mkdir(parents=True, exist_ok=True)
    hook_script.write_text(HOOK_SCRIPT_TEMPLATE, encoding="utf-8")
    hook_script.chmod(0o755)

    settings_path = repo / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, json.JSONDecodeError):
            backup = settings_path.with_suffix(".json.agent_go_corrupt_bak")
            settings_path.replace(backup)
            settings = {}
    cmd = f'python3 "{hook_script}" --repo "{repo}"'
    stop_list = settings.setdefault("hooks", {}).setdefault("Stop", [])
    for group in stop_list:
        for h in group.get("hooks", []):
            if HOOK_MARK in str(h.get("command", "")):
                break
        else:
            continue
        break
    else:
        if settings_path.exists():
            backup = settings_path.with_suffix(".json.agent_go_bak")
            if not backup.exists():
                backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
        stop_list.append({"matcher": "", "hooks": [{"type": "command", "command": cmd}]})
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(entry.get("tasks") or {})
    return True, f"已开启监视 {repo}（{n} 个交付任务可归因；Stop Hook 合并注入完成）"


def uninstall_hook(repo: Path) -> tuple[bool, str]:
    """卸载：index watching=False + 精确移除注入的 Stop entry（其余原样保留）。"""
    repo = Path(repo).resolve()
    index = load_index()
    entry = (index.get("repos") or {}).get(str(repo))
    if entry is None:
        return False, f"该 repo 未开启过监视: {repo}"
    entry["watching"] = False
    save_index(index)
    settings_path = repo / ".claude" / "settings.json"
    if not settings_path.exists():
        return True, "已关闭监视（无 settings.json 需清理）"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, "已关闭监视（settings.json 不可读，跳过清理）"
    stop_list = (settings.get("hooks") or {}).get("Stop") or []
    kept = [g for g in stop_list
            if not any(HOOK_MARK in str(h.get("command", ""))
                       for h in g.get("hooks", []))]
    if kept or not stop_list:
        settings.setdefault("hooks", {})["Stop"] = kept
    else:
        settings.get("hooks", {}).pop("Stop", None)
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"已关闭监视 {repo}（注入的 Stop Hook 已移除，其余配置保留）"


def stop_hook_report(repo: Path) -> str:
    """Stop Hook 报告：未提交改动 ∩ 监视任务交付文件集 → 聚合提醒文本。

    无命中返回空串（hook 静默退出，零噪声）。
    """
    repo = Path(repo).resolve()
    index = load_index()
    entry = (index.get("repos") or {}).get(str(repo))
    if not entry or not entry.get("watching"):
        return ""
    try:
        st = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(repo), capture_output=True, text=True, timeout=10)
        diff = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                              cwd=str(repo), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    changed = set()
    for line in (st.stdout or "").splitlines():
        p = line[3:].strip().strip('"')
        if p:
            changed.add(p)
    for p in (diff.stdout or "").splitlines():
        if p.strip():
            changed.add(p.strip())
    if not changed:
        return ""
    hits: list[tuple[str, list[str]]] = []
    for tid, info in (entry.get("tasks") or {}).items():
        touched = sorted(set(info.get("files") or []) & changed)
        if touched:
            hits.append((tid, touched))
    if not hits:
        return ""
    lines = ["[agent_go] 本次会话修改了以下已交付任务的交付文件（含盲区标注）："]
    for tid, touched in hits[:5]:
        items = (entry["tasks"][tid].get("blind_items") or [])
        lines.append(f"  {tid}（触碰 {', '.join(touched[:3])}…）")
        if items:
            lines.append("    若此修复与交付问题相关，请归因（重算即时生效）：")
            lines.append(f"    agent_go trust --annotate {tid} --item {items[0]} "
                         f"--attribution <confirmed|false-hit|false-clear>")
    lines.append("  （与此交付无关则忽略；任务级漏报：agent_go trust --annotate <tid> --attribution missed）")
    return "\n".join(lines)


def _now() -> float:
    import time
    return time.time()


HOOK_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""agent_go attribution stop-hook（由 trust --watch-repo 注入，勿手编）。

聚合输出会话改动与监视任务交付文件集的交集提醒；无命中静默退出
（exit 0 零噪声）。依赖 agent_go 已安装（pip install -e）。
"""
import sys


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        from agent_go.attribution_watch import stop_hook_report
    except ImportError:
        return 0
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args, _ = ap.parse_known_args()
    report = stop_hook_report(args.repo)
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
