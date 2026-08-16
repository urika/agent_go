import logging
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIFACT_DIR_NAME", "MAX_ARTIFACT_BYTES",
    "collect_from_worktree", "export", "render_export_summary",
]

# worktree 内约定产物目录名：子任务写入此目录的文件视为交付物
ARTIFACT_DIR_NAME = "__artifacts__"

# 单文件大小阈值（默认 100MB）——超限产物导出时告警但不阻断
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


def _task_dir_worktree(task_dir: Path, sub_id: str) -> Path:
    """按 pipeline 的 worktree 布局定位子任务 worktree：task_dir/{sub_id}/work。"""
    return task_dir / sub_id / "work"


def collect_from_worktree(worktree_path: Any, sub_id: str) -> list[dict[str, Any]]:
    """扫描 worktree/__artifacts__/**，返回产物文件列表。

    返回 list[dict]：{"path": Path, "sub_id": str, "size_bytes": int}
    目录不存在或为空时返回空列表（不报错）。
    """
    artifacts: list[dict[str, Any]] = []
    artifact_dir = Path(worktree_path) / ARTIFACT_DIR_NAME
    if not artifact_dir.is_dir():
        return artifacts
    for p in artifact_dir.rglob("*"):
        if p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            artifacts.append({"path": p, "sub_id": sub_id, "size_bytes": size})
    return artifacts


def _copy_artifact(src: Path, dst_dir: Path, artifact_dir_name: str) -> Optional[Path]:
    """把单个产物复制到目标目录，保留 __artifacts__/ 下的相对子目录结构。

    返回目标文件路径；失败返回 None（不中断整个导出）。
    """
    # 保留 __artifacts__/ 内的相对路径（含子目录）
    try:
        art_root_idx = src.parts.index(artifact_dir_name)
        rel = Path(*src.parts[art_root_idx + 1:]) or Path(src.name)
    except (ValueError, IndexError):
        rel = Path(src.name)
    dst = dst_dir / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def export(task_id: str, results: dict[str, dict[str, Any]], artifact_dir: Any, task_dir: Any) -> dict[str, Any]:
    """遍历所有子任务的 worktree（含保留的），收集 __artifacts__/ 到 artifact_dir。

    参数：
      task_id      任务 ID（用于导出路径组织 {task_id}/{sub_id}/...）
      results      results_map — {sub_id: result}
      artifact_dir 目标目录（字符串或 Path）
      task_dir     任务目录，worktree 位于 task_dir/{sub_id}/work

    返回：
      {"exported": [{sub_id, src, dst, size_bytes}], "skipped": [{sub_id, src, reason}], "dir": str}
    """
    out_dir = Path(artifact_dir)
    exported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # CR-P1-4：K13 完整率字段（无产物/无导出目录时 None = 无声明可判）
    _empty = {"exported": exported, "skipped": skipped, "dir": str(out_dir),
              "completeness": None, "total_found": 0, "missing": []}
    if not out_dir:
        return _empty

    # 组织方式：artifact_dir/{task_id}/{sub_id}/{filename}
    task_export_dir = out_dir / task_id
    try:
        task_export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"[artifacts] 无法创建导出目录 {task_export_dir}: {e}")
        return _empty

    for sub_id in results:
        worktree = _task_dir_worktree(Path(task_dir), sub_id)
        for art in collect_from_worktree(worktree, sub_id):
            src = art["path"]
            dst_dir = task_export_dir / sub_id
            if art["size_bytes"] > MAX_ARTIFACT_BYTES:
                skipped.append({
                    "sub_id": sub_id, "src": str(src),
                    "reason": f"超过大小阈值 {MAX_ARTIFACT_BYTES // (1024 * 1024)}MB",
                })
                continue
            dst = _copy_artifact(src, dst_dir, ARTIFACT_DIR_NAME)
            if dst is None:
                skipped.append({"sub_id": sub_id, "src": str(src), "reason": "复制失败"})
                continue
            exported.append({
                "sub_id": sub_id,
                "src": str(src),
                "dst": str(dst),
                "size_bytes": art["size_bytes"],
            })
            logger.info(f"[artifacts] 导出 {sub_id}: {src.name} → {dst}")

    # CR-P1-4：K13 完整率 = exported/(exported+skipped)。skipped（超限/复制失败）=
    # 声明产物（写入 __artifacts__/）未达用户目录 → 完整率 < 100%，明示而非静默。
    total_found = len(exported) + len(skipped)
    completeness = round(len(exported) / total_found, 4) if total_found else None
    return {
        "exported": exported, "skipped": skipped, "dir": str(out_dir),
        "completeness": completeness, "total_found": total_found,
        "missing": [s["src"] for s in skipped],
    }


def render_export_summary(export_result: dict[str, Any]) -> str:
    """生成可读的导出清单（供 final report 展示）。无产物时返回空字符串。"""
    exported = export_result.get("exported", [])
    skipped = export_result.get("skipped", [])
    if not exported and not skipped:
        return ""
    lines = ["", "📦 产物导出"]
    lines.append("─" * 60)
    if exported:
        for e in exported:
            size = _human_size(e.get("size_bytes", 0))
            lines.append(f"  ✅ {e['sub_id']}/{Path(e['dst']).name} ({size}) → {e['dst']}")
    if skipped:
        for s in skipped:
            lines.append(f"  ⚠️ {s['sub_id']}/{Path(s['src']).name}: {s['reason']}")
    # CR-P1-4：K13 完整率——声明产物未全部达用户目录时明示（反"静默跳过"）
    comp = export_result.get("completeness")
    if comp is not None and comp < 1.0:
        lines.append(f"  ⚠️ K13 完整率 {comp:.0%}（{len(exported)}/{export_result.get('total_found', 0)}）"
                     f"—— 声明产物未全部达用户目录，需人工处理")
    lines.append("─" * 60)
    lines.append(f"  导出目录: {export_result.get('dir', '')}")
    return "\n".join(lines)


def _human_size(num: float) -> str:
    """把字节数格式化为可读单位。"""
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"
