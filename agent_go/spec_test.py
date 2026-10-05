"""spec_to_test：需求 → AI 起草验收测试 → 人审冻结 → worker 实现 → verify 重放（ADR-012）。

落地四条护栏（缺一不可，见 docs/design/adr/ADR-012-spec-to-test-pipeline.md）：

① 冻结先于执行：草稿经人审后冻结到 ``<task_dir>/acceptance/``（含 sha256 manifest），
   在 worker 启动前注入 worktree 并**先行提交**进子任务 base——worker 视其为只读契约；
   verify 前/评测前按 manifest 恢复冻结版（剥除 worker 对测试文件的改动）。
② 验证命令 per-task 预生成：验收命令由本模块一次起草、随 Plan 确认门一并人审后冻结，
   不再由 per-subtask 现场生成（ISSUE-29 暴露面从 N×retry 收缩到 1×人工审）。
③ 可执行冻结 oracle 优先于 LLM 语义评估：验收命令走既有 shell 验证链（安全门禁/
   沙箱/超时/失败回修全部复用），失败即阻断；语义评估降级为补充（executor 侧实现）。
④ 生产/评测两口径：``source="drafted"``（人审冻结即真值锚）／``source="task"``
   （评测模式由任务定义提供，出题人≠解题人，见 ``freeze_from_provided``）。

设计约束：本模块所有公开函数**fail-open**——起草/冻结/注入/恢复任一步异常都返回
None/空值并记日志，绝不阻塞主交付链路（ADR-012「约束」节）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

# ── 常量 ──────────────────────────────────────────────────────────────

ACCEPTANCE_DIRNAME = "acceptance"
MANIFEST_NAME = "manifest.json"
FILES_DIRNAME = "files"
MANIFEST_SCHEMA_VERSION = 1

#: 未在 config 中声明 spec_test 段时的兜底默认（与 config.DEFAULT_CONFIG 保持一致）
_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "frozen_dir": "tests/acceptance",
    "require_review": True,
    "draft_role": "planner",
    "provided_dir": "",
    "max_files": 8,
    "max_file_bytes": 20000,
    "max_commands": 5,
}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def cfg(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """读取 spec_test 配置段（缺省值与 DEFAULT_CONFIG 对齐）。"""
    merged = dict(_DEFAULTS)
    section = (config or {}).get("spec_test") or {}
    if isinstance(section, dict):
        merged.update({k: v for k, v in section.items() if v is not None})
    return merged


def is_enabled(config: Optional[dict[str, Any]]) -> bool:
    """管线总开关（默认关；CLI --accept-tests/--no-accept-tests 可覆盖）。"""
    return bool(cfg(config).get("enabled"))


def acceptance_root(task_dir: Path) -> Path:
    return Path(task_dir) / ACCEPTANCE_DIRNAME


def manifest_path(task_dir: Path) -> Path:
    return acceptance_root(task_dir) / MANIFEST_NAME


# ── 草稿清洗（安全面）─────────────────────────────────────────────────

def _safe_rel_path(raw: str) -> Optional[str]:
    """校验草稿文件路径：仅允许 frozen_dir 内的相对路径（绝对路径/越界一律拒绝）。"""
    if not raw or not isinstance(raw, str):
        return None
    p = raw.strip().replace("\\", "/")
    if p.startswith("/") or p.startswith("~"):
        return None
    if not p or p.endswith("/"):
        return None
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    if ":" in parts[0]:  # Windows 盘符 / URL scheme
        return None
    return "/".join(parts)


def sanitize_draft(raw: Any, config: Optional[dict[str, Any]], logger: logging.Logger) -> Optional[dict[str, Any]]:
    """清洗 LLM 草稿：路径白名单 + 命令安全门禁 + 体量上限。不可用 → None。"""
    from .utils import _is_safe_verification_command

    conf = cfg(config)
    if not isinstance(raw, dict):
        logger.warning("[spec_test] 草稿格式非法（非 JSON 对象），丢弃")
        return None

    files: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("files") or []:
        if len(files) >= int(conf["max_files"]):
            logger.warning(f"[spec_test] 草稿文件数超上限 {conf['max_files']}，截断")
            break
        if not isinstance(item, dict):
            continue
        rel = _safe_rel_path(str(item.get("path", "")))
        content = item.get("content")
        if not rel or not isinstance(content, str) or not content.strip():
            logger.warning(f"[spec_test] 丢弃非法草稿文件条目: path={item.get('path')!r}")
            continue
        if len(content.encode("utf-8")) > int(conf["max_file_bytes"]):
            logger.warning(f"[spec_test] 丢弃超体量草稿文件: {rel}")
            continue
        if rel in seen:
            continue
        seen.add(rel)
        files.append({"path": rel, "content": content})
    if not files:
        logger.warning("[spec_test] 草稿无可用文件，弃用")
        return None

    commands: list[str] = []
    for cmd in raw.get("commands") or []:
        if len(commands) >= int(conf["max_commands"]):
            break
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        cmd = cmd.strip()
        safe, reason = _is_safe_verification_command(cmd)
        if not safe:
            logger.warning(f"[spec_test] 丢弃不安全验收命令: {cmd[:120]}（{reason}）")
            continue
        if cmd not in commands:
            commands.append(cmd)
    if not commands:
        logger.warning("[spec_test] 草稿无安全验收命令，弃用（降级为现状验证行为）")
        return None

    return {
        "files": files,
        "commands": commands,
        "notes": str(raw.get("notes") or "")[:2000],
        "model": str(raw.get("_model") or ""),
        "cost_usd": float(raw.get("_cost_usd") or 0.0),
    }


# ── 起草（LLM，一次/任务）─────────────────────────────────────────────

_DRAFT_SYSTEM = (
    "你是验收测试起草器。给定需求与仓库上下文，写出「只依赖需求即可判定是否完成」的验收测试。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象，不要任何解释文字或 markdown 代码块之外的文本。\n"
    "2. JSON 结构：{\"notes\": str, \"files\": [{\"path\": str, \"content\": str}], \"commands\": [str]}。\n"
    "3. files[].path 必须是相对路径（不得绝对路径、不得含 ..）；content 是该文件完整内容；\n"
    "   测试文件数 ≤ {max_files}。\n"
    "4. commands 是从仓库根目录执行的非交互命令（每条都能独立判定通过/失败），\n"
    "   必须落在安全前缀白名单内（pytest / python3 -m pytest / npm test / go test / cargo test 等），\n"
    "   禁止 bash -c、重定向、管道、rm/curl/git push 等；条数 ≤ {max_commands}。\n"
    "5. 只写测试：不得修改或新增生产代码；测试必须「实现前失败、实现后通过」。\n"
    "6. 覆盖正常路径 + 至少 1 个边界/异常路径；断言具体行为，避免仅仅检查文件存在。\n"
    "7. 测试语言/框架跟随仓库既有约定（从上下文判断）。\n"
)


def draft_acceptance(
    task: str,
    config: dict[str, Any],
    logger: logging.Logger,
    *,
    spec_context: str = "",
    docs_context: str = "",
    repo_hint: str = "",
) -> Optional[dict[str, Any]]:
    """调用 LLM 起草验收测试；解析/清洗失败 → None（fail-open）。"""
    from . import api as _api

    conf = cfg(config)
    prompt = (_DRAFT_SYSTEM
              .replace("{max_files}", str(conf["max_files"]))
              .replace("{max_commands}", str(conf["max_commands"])))
    user_parts = [f"## 任务需求\n{task}"]
    if spec_context:
        user_parts.append(f"## Task Spec（需求/验收标准）\n{spec_context}")
    if docs_context:
        user_parts.append(f"## 参考文档（架构/设计，节选）\n{docs_context}")
    if repo_hint:
        user_parts.append(f"## 仓库上下文\n{repo_hint}")
    user_parts.append("请输出 JSON。")
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    role = str(conf.get("draft_role") or "planner")
    t0 = time.time()
    try:
        content = _api.call_api(config, messages, logger, role=role)
    except Exception as e:  # noqa: BLE001 - fail-open：起草失败不阻塞主链
        logger.warning(f"[spec_test] 起草调用失败（降级为现状验证行为）: {type(e).__name__}: {e}")
        return None
    latency_ms = (time.time() - t0) * 1000

    raw = _extract_json_object(content or "")
    if raw is None:
        logger.warning("[spec_test] 起草响应无法解析为 JSON，弃用草稿")
        return None

    draft = sanitize_draft(raw, config, logger)
    if draft is None:
        return None
    draft["model"] = str((config.get("plan_api") or {}).get("model") or role)
    draft["latency_ms"] = latency_ms
    logger.info(
        f"[spec_test] 验收测试草稿就绪: {len(draft['files'])} 个文件 / "
        f"{len(draft['commands'])} 条命令 / {latency_ms:.0f}ms"
    )
    return draft


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 响应提取首个 JSON 对象（容忍代码块/前后杂音）。"""
    text = (text or "").strip()
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except (ValueError, TypeError):
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# ── 冻结 / 读取 ───────────────────────────────────────────────────────

def sanitize_review(payload: Any, config: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """校验人审回执（CLI 编辑 / web 确认通道统一入口）。

    形态：``{"decision": "approved"|"skipped", "edits": n, "files": [{"path","content"}]}``。
    路径走白名单、内容限体量；任一非法 → None（调用方按"未人审"降级）。
    """
    if not isinstance(payload, dict):
        return None
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in ("approved", "skipped"):
        return None
    conf = cfg(config)
    out: dict[str, Any] = {
        "decision": decision,
        "edits": max(0, int(payload.get("edits") or 0)),
    }
    files_in = payload.get("files")
    if isinstance(files_in, list):
        files: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in files_in:
            if not isinstance(item, dict) or len(files) >= int(conf["max_files"]):
                continue
            rel = _safe_rel_path(str(item.get("path") or ""))
            content = item.get("content")
            if not rel or rel in seen or not isinstance(content, str) or not content.strip():
                return None
            if len(content.encode("utf-8")) > int(conf["max_file_bytes"]):
                return None
            seen.add(rel)
            files.append({"path": rel, "content": content})
        if files:
            out["files"] = files
    return out


def merge_review(
    draft: dict[str, Any], review: Optional[dict[str, Any]], logger: logging.Logger
) -> dict[str, Any]:
    """把人审回执合并进草稿（按 path 覆盖内容；回执未含的文件保持原样）。"""
    if not review or not review.get("files"):
        return draft
    by_path = {f.get("path"): f for f in (draft.get("files") or [])}
    for item in review["files"]:
        rel, content = item["path"], item["content"]
        if rel in by_path:
            by_path[rel]["content"] = content
        else:
            logger.warning(f"[spec_test] 人审回执含未知文件（忽略）: {rel}")
    return draft


def save_draft(task_dir: Path, draft: dict[str, Any], logger: logging.Logger, reason: str = "") -> None:
    """未冻结草稿留档（跳过/未人审场景），供后续 `--accept-tests` 复用与人工核查。"""
    try:
        root = acceptance_root(Path(task_dir))
        root.mkdir(parents=True, exist_ok=True)
        payload = dict(draft)
        payload["_saved_reason"] = reason
        payload["_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write_json_atomic(root / "DRAFT.json", payload)
        logger.info(f"[spec_test] 草稿已留档: {root / 'DRAFT.json'}（{reason or '未冻结'}）")
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"[spec_test] 草稿留档失败: {e}")


def freeze(
    task_dir: Path,
    draft: dict[str, Any],
    *,
    reviewed: bool,
    source: str = "drafted",
    logger: logging.Logger,
    review_edits: int = 0,
    frozen_dir: str = "",
) -> Optional[dict[str, Any]]:
    """把人审后的草稿冻结到 ``<task_dir>/acceptance/``（files/ + manifest.json）。"""
    task_dir = Path(task_dir)
    root = acceptance_root(task_dir)
    files_root = root / FILES_DIRNAME
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": source,
        "reviewed": bool(reviewed),
        "review_edits": int(review_edits),
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "frozen_dir": frozen_dir or str(_DEFAULTS["frozen_dir"]),
        "notes": draft.get("notes", ""),
        "draft_model": draft.get("model", ""),
        "draft_cost_usd": float(draft.get("cost_usd") or 0.0),
        "files": [],
        "commands": list(draft.get("commands") or []),
    }
    try:
        for item in draft.get("files") or []:
            rel = _safe_rel_path(str(item.get("path", "")))
            if not rel:
                continue
            dest = files_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = item["content"].encode("utf-8")
            dest.write_bytes(data)
            manifest["files"].append({
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            })
        if not manifest["files"] or not manifest["commands"]:
            logger.warning("[spec_test] 冻结内容不完整（无文件或无命令），放弃冻结")
            return None
        root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(manifest_path(task_dir), manifest)
        logger.info(
            f"[spec_test] 已冻结验收测试: {len(manifest['files'])} 文件 / "
            f"{len(manifest['commands'])} 命令（reviewed={manifest['reviewed']}, source={source}）"
        )
        return manifest
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.warning(f"[spec_test] 冻结失败（降级为现状验证行为）: {type(e).__name__}: {e}")
        return None


def load_manifest(task_dir: Optional[Path]) -> Optional[dict[str, Any]]:
    """读取冻结 manifest；不存在/损坏 → None。"""
    if not task_dir:
        return None
    p = manifest_path(Path(task_dir))
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("files"):
        return None
    return data


def usable_manifest(task_dir: Optional[Path], config: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """取「可用于验收」的冻结件：存在 + 命令非空 + 审阅要求满足（护栏①/④）。"""
    manifest = load_manifest(task_dir)
    if not manifest or not manifest.get("commands"):
        return None
    conf = cfg(config)
    if conf.get("require_review") and not manifest.get("reviewed"):
        return None
    return manifest


def acceptance_commands(task_dir: Optional[Path], config: Optional[dict[str, Any]]) -> list[str]:
    """冻结件里的验收命令（未启用/不可用时返回空列表）。"""
    if not is_enabled(config):
        return []
    manifest = usable_manifest(task_dir, config)
    return list(manifest.get("commands") or []) if manifest else []


def freeze_from_provided(
    task_dir: Path, config: Optional[dict[str, Any]], logger: logging.Logger
) -> Optional[dict[str, Any]]:
    """护栏④（评测口径）：从任务定义提供的目录冻结验收测试（出题人≠解题人）。

    ``spec_test.provided_dir`` 指向一个含测试文件与 ``manifest.json``/``commands.json``
    （或 ``commands.txt``）的目录；本函数只做「复制 + 记录」，不调用 LLM。
    """
    conf = cfg(config)
    raw_dir = str(conf.get("provided_dir") or "").strip()
    if not raw_dir:
        return None
    src = Path(raw_dir).expanduser()
    if not src.is_dir():
        logger.warning(f"[spec_test] provided_dir 不存在: {src}")
        return None
    try:
        commands: list[str] = []
        cmd_file = src / "commands.json"
        txt_file = src / "commands.txt"
        if cmd_file.exists():
            loaded = json.loads(cmd_file.read_text(encoding="utf-8"))
            commands = [str(c) for c in (loaded or [])]
        elif txt_file.exists():
            commands = [ln.strip() for ln in txt_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        files: list[dict[str, str]] = []
        for p in sorted(src.rglob("*")):
            if not p.is_file() or p.name in ("manifest.json", "commands.json", "commands.txt"):
                continue
            rel = _safe_rel_path(str(p.relative_to(src)))
            if not rel:
                continue
            files.append({"path": rel, "content": p.read_text(encoding="utf-8", errors="replace")})
        draft = {"files": files, "commands": commands, "notes": "provided by task definition", "model": "provided"}
        return freeze(task_dir, draft, reviewed=True, source="task", logger=logger,
                      frozen_dir=str(conf.get("frozen_dir") or ""))
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.warning(f"[spec_test] 从 provided_dir 冻结失败: {type(e).__name__}: {e}")
        return None


# ── 注入 / 重放（护栏①）───────────────────────────────────────────────

def inject_into_worktree(
    worktree: Path,
    manifest: dict[str, Any],
    task_dir: Path,
    logger: logging.Logger,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把冻结验收测试注入 worktree 并**先行提交**进 base（worker 只读契约）。

    先行提交的意义：冻结件成为子任务 base 的一部分，worker 的 diff 只含其自身改动；
    worker 若改动测试文件，将在 verify 前被 ``restore_frozen`` 剥除并恢复冻结版。
    """
    from .utils import _format_commit

    result: dict[str, Any] = {"injected": [], "commit": "", "error": ""}
    worktree = Path(worktree)
    frozen_dir = str(manifest.get("frozen_dir") or cfg(config).get("frozen_dir") or "").strip("/")
    if not frozen_dir:
        result["error"] = "frozen_dir 为空"
        return result
    files_root = acceptance_root(Path(task_dir)) / FILES_DIRNAME
    if not files_root.exists():
        result["error"] = "冻结件缺失"
        return result
    try:
        for item in manifest.get("files") or []:
            rel = _safe_rel_path(str(item.get("path", "")))
            if not rel:
                continue
            src = files_root / rel
            if not src.exists():
                continue
            dst = worktree / frozen_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            result["injected"].append(str(Path(frozen_dir) / rel))
        if not result["injected"]:
            result["error"] = "无文件注入"
            return result
        subprocess.run(["git", "add", "-A", "--", frozen_dir], cwd=str(worktree), capture_output=True)
        commit_msg = _format_commit("冻结验收测试（只读，spec-to-test）", sub_id="acceptance", scope="acceptance")
        subject = commit_msg.splitlines()[0]
        cp = subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(worktree), capture_output=True, text=True)
        # 回报「冻结提交」的 HEAD 哈希（供 executor 自提交判定排除，防空转误判 completed）：
        # 新提交 / 二次注入（nothing-to-commit，HEAD 已是冻结提交）两种情形都要报。
        hp = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(worktree), capture_output=True, text=True)
        head_hash = hp.stdout.strip() if hp.returncode == 0 else ""
        if cp.returncode != 0:
            mp = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=str(worktree), capture_output=True, text=True)
            if mp.stdout.strip() != subject:
                head_hash = ""  # HEAD 不是冻结提交（如已有 worker 提交）→ 不排除
        result["commit"] = head_hash
        logger.info(f"[spec_test] 验收测试已注入 worktree: {len(result['injected'])} 文件 → {frozen_dir}/")
    except Exception as e:  # noqa: BLE001 - fail-open
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"[spec_test] 注入失败（该子任务降级为无冻结 oracle）: {result['error']}")
    return result


def restore_frozen(
    worktree: Path, manifest: dict[str, Any], task_dir: Path, logger: logging.Logger
) -> dict[str, Any]:
    """按 manifest 恢复冻结版（剥除 worker 对测试文件的改动）。幂等，可多次调用。"""
    report: dict[str, Any] = {"checked": 0, "restored": [], "missing_source": []}
    worktree = Path(worktree)
    frozen_dir = str(manifest.get("frozen_dir") or "").strip("/")
    if not frozen_dir:
        return report
    files_root = acceptance_root(Path(task_dir)) / FILES_DIRNAME
    for item in manifest.get("files") or []:
        rel = _safe_rel_path(str(item.get("path", "")))
        if not rel:
            continue
        report["checked"] += 1
        src = files_root / rel
        dst = worktree / frozen_dir / rel
        try:
            frozen_bytes = src.read_bytes()
        except OSError:
            report["missing_source"].append(rel)
            continue
        try:
            current = dst.read_bytes() if dst.exists() else None
        except OSError:
            current = None
        if current != frozen_bytes:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(frozen_bytes)
                report["restored"].append(str(Path(frozen_dir) / rel))
            except OSError as e:
                logger.warning(f"[spec_test] 恢复冻结文件失败: {rel}: {e}")
    if report["restored"]:
        logger.warning(
            f"[spec_test] 护栏①拦截：worker 改动了冻结验收测试，已恢复 {len(report['restored'])} 个文件 "
            f"→ {', '.join(report['restored'][:5])}"
        )
    return report


def restore_for_verify(
    worktree: Optional[Path], task_dir: Optional[Path], config: Optional[dict[str, Any]], logger: logging.Logger
) -> dict[str, Any]:
    """executor 侧统一入口：未启用/无冻结件时零开销 no-op。"""
    if not is_enabled(config) or not worktree or not task_dir:
        return {}
    manifest = usable_manifest(task_dir, config)
    if not manifest:
        return {}
    return restore_frozen(Path(worktree), manifest, Path(task_dir), logger)


def runtime_manifest(
    task_dir: Optional[Path], config: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """executor 侧取冻结件（未启用/不可用时 None）。"""
    if not is_enabled(config):
        return None
    return usable_manifest(task_dir, config)


# ── 展示 / 元数据 ────────────────────────────────────────────────────

def review_text(manifest: dict[str, Any]) -> str:
    """Plan 确认门展示用摘要（文件清单 + 命令 + 内容预览）。"""
    lines = [
        f"验收测试草稿（source={manifest.get('source', 'drafted')}，reviewed={manifest.get('reviewed')}）",
        f"命令: {'; '.join(manifest.get('commands') or [])}",
    ]
    notes = (manifest.get("notes") or "").strip()
    if notes:
        lines.append(f"说明: {notes[:300]}")
    return "\n".join(lines)


def draft_files_for_display(draft: dict[str, Any], preview_lines: int = 20) -> str:
    """草稿展示（人审门用）：逐文件列出前 N 行内容。"""
    out: list[str] = []
    for item in draft.get("files") or []:
        rel = item.get("path", "?")
        body_lines = (item.get("content") or "").splitlines()
        head = body_lines[:preview_lines]
        more = "" if len(body_lines) <= preview_lines else f"\n    …（共 {len(body_lines)} 行）"
        out.append(f"  ── {rel} ──\n    " + "\n    ".join(head) + more)
    return "\n".join(out)


def meta_block(manifest: Optional[dict[str, Any]]) -> dict[str, Any]:
    """写入 meta.json 的 acceptance 段（冻结事实，不含运行态）。"""
    if not manifest:
        return {"enabled": False}
    return {
        "enabled": True,
        "source": manifest.get("source", ""),
        "reviewed": bool(manifest.get("reviewed")),
        "review_edits": int(manifest.get("review_edits") or 0),
        "frozen_at": manifest.get("frozen_at", ""),
        "frozen_dir": manifest.get("frozen_dir", ""),
        "files": [f.get("path") for f in manifest.get("files") or []],
        "sha256": {f.get("path"): f.get("sha256") for f in manifest.get("files") or []},
        "commands": list(manifest.get("commands") or []),
        "draft_model": manifest.get("draft_model", ""),
        "draft_cost_usd": float(manifest.get("draft_cost_usd") or 0.0),
    }


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def open_in_editor(path: Path, logger: logging.Logger) -> bool:
    """用 $EDITOR/$VISUAL（兜底 vi）打开文件供人审编辑。失败返回 False。"""
    import os
    import shlex

    editor = os.environ.get("AGENT_GO_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        rc = subprocess.run(shlex.split(editor) + [str(path)]).returncode
        return rc == 0
    except Exception as e:  # noqa: BLE001 - 编辑失败不影响主链（视为未修改）
        logger.warning(f"[spec_test] 打开编辑器失败: {e}")
        return False


__all__ = [
    "ACCEPTANCE_DIRNAME",
    "acceptance_commands",
    "acceptance_root",
    "cfg",
    "draft_acceptance",
    "draft_files_for_display",
    "freeze",
    "freeze_from_provided",
    "inject_into_worktree",
    "is_enabled",
    "load_manifest",
    "manifest_path",
    "merge_review",
    "meta_block",
    "open_in_editor",
    "restore_for_verify",
    "restore_frozen",
    "review_text",
    "runtime_manifest",
    "sanitize_draft",
    "sanitize_review",
    "save_draft",
    "usable_manifest",
]
