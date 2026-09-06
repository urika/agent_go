"""轨迹信号提取（ADR-010 阶段 3）：从平台轨迹事件提取归因信号。

只读消费 ``trajectory/*.jsonl``（dsh/opencode harvester 产物，同一 schema：
``{seq, time, type, data}``，tool/call 带 name + arguments_summary）。纯函数、
零 LLM、fail-open——信号是归因注解，**不改 pass/fail 判定**（测量口径稳定）。

信号词汇：

| 字段 | 含义 |
|---|---|
| steps | step/start 数（执行规模） |
| tool_calls | tool/call 总数 |
| tool_errors | tool/result is_error=True 数（工具健康度） |
| mutations | 写类工具调用数（write/edit/patch/create 等） |
| path_violations | 写类工具参数中落在 worktree 外的绝对路径（ISSUE-58 模式：worktree 隔离被绕过，worktree diff 观测不到） |
| repeated_edits | 同一文件被写类工具改 ≥3 次（打转征兆） |
| mutation_without_worktree_change | mutations>0 且全部写调用命中 worktree 外（配合 status=no_changes 即疑似「空通过」） |

消费端：executor（meta results + subtask_end 事件 + 可疑 warning）、
排障页（api_trajectory 响应 signals 字段 → 轨迹面板顶部归因横幅）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# 写类工具名片段（小写包含匹配）：write/edit/multiedit/patch/apply_patch/
# str_replace_editor/create_file 等；read/grep/list 等只读工具不含这些片段。
_MUTATION_NAME_HINTS = ("write", "edit", "patch", "create", "str_replace", "apply")

# 常见的文件路径参数键（opencode/dsh 工具入参约定）
_PATH_KEYS = ("filepath", "file_path", "path", "file", "filename", "notebook_path")

# worktree 外但属正常临时区的路径前缀——不计入违规（模型写草稿/临时文件常见）
_BENIGN_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", "/dev/")

# 绝对路径正则（arguments_summary JSON 截断无法解析时的兜底提取）
_ABS_PATH_RE = re.compile(r"/(?:[\w.\-+@~]+/)+[\w.\-+@~]*")

_REPEAT_EDIT_THRESHOLD = 3   # 同一文件写类修改次数阈值（打转判定）
_MAX_VIOLATIONS = 10         # path_violations 列表上限（去重后截断）
_MAX_REPEATED = 5            # repeated_edits 列表上限


def _is_mutation_tool(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _MUTATION_NAME_HINTS)


def _walk_strings(obj: Any) -> list:
    """递归收集 JSON 结构中的全部字符串值。"""
    out: list = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_walk_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_walk_strings(v))
    return out


def _extract_paths(arguments_summary: str) -> list:
    """从 tool/call 的 arguments_summary 提取绝对路径。

    优先按 JSON 解析（收集所有以 / 开头的字符串值）；截断导致解析失败时
    退化为正则提取。返回顺序保持出现次序。
    """
    text = arguments_summary or ""
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return _ABS_PATH_RE.findall(text)
    return [s for s in _walk_strings(parsed) if s.startswith("/")]


def _primary_file(arguments_summary: str) -> str:
    """提取写类调用的主文件路径（路径参数键优先，否则第一个绝对路径）。"""
    text = arguments_summary or ""
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        paths = _ABS_PATH_RE.findall(text)
        return paths[0] if paths else ""
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if isinstance(value, str) and value and any(k in key.lower() for k in _PATH_KEYS):
                return value
    paths = [s for s in _walk_strings(parsed) if s.startswith("/")]
    return paths[0] if paths else ""


def _is_outside(path: str, worktree: str) -> bool:
    """绝对路径是否落在 worktree 外（且不在正常临时区）。"""
    if not path.startswith("/"):
        return False
    if any(path.startswith(p) or path == p.rstrip("/") for p in _BENIGN_PREFIXES):
        return False
    if not worktree:
        return False  # 无 worktree 基准无法判定，不误报
    wt = worktree.rstrip("/")
    return path != wt and not path.startswith(wt + "/")


def extract_signals(events: list, worktree: str = "") -> dict:
    """从平台轨迹事件提取归因信号（纯函数）。

    events: trajectory jsonl 解析后的事件列表（坏行由调用方过滤）。
    worktree: 子任务 worktree 绝对路径（空则跳过路径违规判定）。
    返回全字段 dict（无轨迹时全零/空），schema 稳定供消费端直接读。
    """
    signals: dict = {
        "steps": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "mutations": 0,
        "path_violations": [],
        "repeated_edits": [],
        "mutation_without_worktree_change": False,
    }
    violations: list = []
    edit_counts: dict = {}
    mutation_paths_inside = 0

    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type", "")
        data = ev.get("data") or {}
        if not isinstance(data, dict):
            continue
        if etype == "step/start":
            signals["steps"] += 1
        elif etype == "tool/call":
            signals["tool_calls"] += 1
            if not _is_mutation_tool(str(data.get("name", ""))):
                continue
            signals["mutations"] += 1
            args_summary = str(data.get("arguments_summary", ""))
            paths = _extract_paths(args_summary)
            outside = [p for p in paths if _is_outside(p, worktree)]
            if not outside and paths:
                mutation_paths_inside += 1
            for p in outside:
                if p not in violations:
                    violations.append(p)
            primary = _primary_file(args_summary)
            if primary:
                edit_counts[primary] = edit_counts.get(primary, 0) + 1
        elif etype == "tool/result":
            if data.get("is_error"):
                signals["tool_errors"] += 1

    signals["path_violations"] = violations[:_MAX_VIOLATIONS]
    signals["repeated_edits"] = [
        {"file": f, "count": c}
        for f, c in sorted(edit_counts.items(), key=lambda kv: -kv[1])
        if c >= _REPEAT_EDIT_THRESHOLD
    ][:_MAX_REPEATED]
    # 全部写调用命中 worktree 外（有写动作但 worktree 内零落点）→ 疑似隔离绕过
    signals["mutation_without_worktree_change"] = bool(
        signals["mutations"] > 0 and violations and mutation_paths_inside == 0
    )
    return signals


def _merge_signals(acc: dict, new: dict) -> None:
    """把 new 的信号合并进 acc（跨 attempt 聚合）。"""
    for key in ("steps", "tool_calls", "tool_errors", "mutations"):
        acc[key] += new.get(key, 0)
    for p in new.get("path_violations", []):
        if p not in acc["path_violations"]:
            acc["path_violations"].append(p)
    counts = {e["file"]: e["count"] for e in acc["repeated_edits"]}
    for e in new.get("repeated_edits", []):
        counts[e["file"]] = max(counts.get(e["file"], 0), e["count"])
    acc["repeated_edits"] = [
        {"file": f, "count": c}
        for f, c in sorted(counts.items(), key=lambda kv: -kv[1])
    ][:_MAX_REPEATED]
    acc["path_violations"] = acc["path_violations"][:_MAX_VIOLATIONS]
    acc["mutation_without_worktree_change"] = bool(
        acc["mutation_without_worktree_change"] or new.get("mutation_without_worktree_change")
    )


def collect_subtask_signals(task_dir, sub_id: str, worktree: str = "") -> Optional[dict]:
    """聚合一个子任务全部 attempt 的轨迹信号（executor/排障页共用入口）。

    读取 <task_dir>/trajectory/ 下 <sub_id>.attempt-*.jsonl（优先，per-attempt），
    无 attempt 文件时回退旧格式 <sub_id>.jsonl；两者都有时**不重复计数**
    （attempt-1 与旧格式兼容副本内容相同）。无轨迹文件返回 None。
    fail-open：坏行跳过，文件读失败跳过。
    """
    traj_dir = Path(str(task_dir)) / "trajectory"
    if not traj_dir.exists():
        return None
    attempt_files = sorted(
        (p for p in traj_dir.glob(f"{sub_id}.attempt-*.jsonl")
         if p.stem.rsplit("-", 1)[1].isdigit()),
        key=lambda p: int(p.stem.rsplit("-", 1)[1]),
    )
    if attempt_files:
        files = attempt_files
    else:
        legacy = traj_dir / f"{sub_id}.jsonl"
        files = [legacy] if legacy.exists() else []
    if not files:
        return None

    merged: Optional[dict] = None
    for path in files:
        events: list = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # 坏行跳过（partial write 残留）
        except OSError:
            continue
        sig = extract_signals(events, worktree)
        if merged is None:
            merged = sig
        else:
            _merge_signals(merged, sig)
    return merged
