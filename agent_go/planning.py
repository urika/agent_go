"""规划阶段辅助：任务耗时预估（M4）。

从 eval.py 迁移而来——estimate_task_duration 逻辑上是「预执行估算」（在线、嵌入 cmd_run），
不是「离线评估」，放在 eval.py 会让核心流程（cli.cmd_run）反向依赖评估模块，违背解耦原则
（核心不依赖增强/评估）。本模块自包含，不 import eval.py，避免循环依赖。

依赖契约：读取 ~/.agent_go/task-*/meta.json 的 results[].duration_sec（核心管线写入）。
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

__all__ = ["estimate_task_duration", "check_under_decomposition",
           "check_difficulty_mismatch", "difficulty_hint"]

logger = logging.getLogger(__name__)

# 无历史数据时的默认子任务耗时（秒），与 eval.py 保持一致
_DEFAULT_SUBTASK_SEC = 240

# S12-P2 G5 V1：hard 任务的最小合理子任务数阈值（硬编码，V2 从 verify_state.json 历史学习）
DIFFICULTY_BASE_SUBTASKS = {"easy": 1, "medium": 2, "hard": 3}


def check_under_decomposition(subtasks: list[dict], logger: Optional[logging.Logger] = None) -> bool:
    """G5 规划期欠分解检测：hard 子任务 + 总子任务数过少 → 提示可能撞超时/单子任务过长。

    高难度任务被欠分解成 1-2 个长子任务 → 单子任务耗时长 → 撞 retry_timeout。
    根因在规划期，不在执行期（见 docs/design/timeout-kill-strategy-2026-08-06.md G5）。

    V1：硬编码阈值 difficulty_base_subtasks（hard=3）。仅告警提示，不强改 Plan
    （不覆盖 LLM 的分解决定——「确需少量子任务的 hard 任务」是合法场景）。

    Returns:
        True 若检测到欠分解（触发告警）；False 正常。
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return False
    total = len(subtasks)
    has_hard = any(
        isinstance(st, dict) and st.get("difficulty") == "hard"
        for st in subtasks
    )
    if not has_hard:
        return False
    threshold = DIFFICULTY_BASE_SUBTASKS.get("hard", 3)
    if total < threshold:
        _hard_ids = [
            st.get("id", "?") for st in subtasks
            if isinstance(st, dict) and st.get("difficulty") == "hard"
        ]
        _lg.warning(
            f"[G5] 欠分解告警: hard 子任务（{', '.join(_hard_ids)}）仅 {total} 个总子任务 "
            f"< 建议 {threshold} 个——单子任务可能过长并撞 retry_timeout。"
            f"建议再分解或为 hard 子任务配置强模型（worker_models.hard）。"
        )
        return True
    return False


# CR-G4 V1：难度启发式信号词（planner 主观难度的交叉核对）
# hard 信号：跨模块/重构/架构/迁移等结构性改动；easy 信号：单点小改/helper/格式化。
_DIFFICULTY_HARD_KW = (
    "重构", "架构", "跨模块", "跨文件", "重写", "迁移", "性能优化", "并发", "线程安全",
    "refactor", "architecture", "migrate", "rewrite", "concurrency", "thread-safe",
)
_DIFFICULTY_EASY_KW = (
    "helper", "格式化", "添加一个", "单点", "小改", "typo", "重命名", "改个",
    "format", "rename", "add a function", "add a helper", "one-liner",
)
# 文件路径启发式：从描述/agent_prompt 提及的源码路径数估计涉及面
_PATH_RE = re.compile(r"[\w./\-]+\.(?:py|js|ts|tsx|go|rs|java|rb|c|cpp|h|md|yaml|yml|json|toml)")


def difficulty_hint(subtask: dict) -> Optional[str]:
    """CR-G4 V1：从子任务元数据（title/description/agent_prompt）启发式估一档难度。

    信号：hard 关键词 +2/个；easy 关键词 -1/个；提及 ≥3 个不同源码路径 +2（多文件）。
    返回 'easy'/'medium'/'hard'；中性（无强信号）返回 None（不与 planner 唱反调）。
    纯启发式、warning-only——不覆盖 planner 的 difficulty（见设计稿 G4）。
    """
    if not isinstance(subtask, dict):
        return None
    text = " ".join(str(subtask.get(k, "")) for k in ("title", "description", "agent_prompt", "task")).lower()
    if not text.strip():
        return None
    score = 0
    for kw in _DIFFICULTY_HARD_KW:
        if kw.lower() in text:
            score += 2
    for kw in _DIFFICULTY_EASY_KW:
        if kw.lower() in text:
            score -= 1
    _paths = set(m.group(0).lower() for m in _PATH_RE.finditer(text))
    if len(_paths) >= 3:
        score += 2
    if score >= 2:
        return "hard"
    if score <= -1:
        return "easy"
    return None


# 难度档位序号（算跨档距离用）
_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def check_difficulty_mismatch(subtasks: list[dict], logger: Optional[logging.Logger] = None) -> int:
    """CR-G4：planner 标的 difficulty 与启发式 hint 跨两档不一致时告警（不覆盖 LLM）。

    如 planner 标 easy 但信号强烈倾向 hard（跨文件重构）→ 可能用错档模型（easy 槽能力不足）。
    仅"跨两档"（easy↔hard）才报——单档差异（easy↔medium）噪声大不报。
    与 check_under_decomposition 同风格：warning-only，不改 Plan。

    Returns: 告警条数。
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return 0
    _n = 0
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        _planned = st.get("difficulty", "medium")
        if _planned not in _DIFFICULTY_ORDER:
            continue
        _hinted = difficulty_hint(st)
        if _hinted is None:
            continue
        if abs(_DIFFICULTY_ORDER[_hinted] - _DIFFICULTY_ORDER[_planned]) >= 2:
            _n += 1
            _lg.warning(
                f"[G4] 难度交叉核对告警: {st.get('id', '?')} difficulty={_planned} "
                f"但描述信号倾向 {_hinted}（跨文件/重构/多文件启发式）——"
                f"可能用错档模型。建议人工复核 difficulty 或 worker_models 配置。"
            )
    return _n


def _read_meta(task_dir: Path) -> Optional[dict[str, Any]]:
    """读 meta.json（与 eval._read_meta 同构，自包含避免 import eval）。"""
    path = Path(task_dir) / "meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read meta.json from %s: %s", path, e)
        return None


def _scan_task_dirs(base_dir: Path) -> list[Path]:
    """扫描 task-* 目录（与 eval._scan_task_dirs 同构）。"""
    return sorted(Path(base_dir).glob("task-*"), reverse=True)


def estimate_task_duration(subtasks: list[dict], parallel: int, tasks_dir: Path) -> dict[str, Any]:
    """M4 时间预估：历史子任务耗时中位数 × 拓扑波次（考虑并行度）。

    Returns:
        {
            "subtasks": int, "waves": int, "parallel": int,
            "median_subtask_sec": float, "estimated_sec": int,
            "sample_size": int, "confidence": "high|medium|low|none",
        }
    """
    # 1. 拓扑波次数（依赖链深度，带环保护）
    ids = {st["id"] for st in subtasks}
    depth: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset) -> int:
        if sid in depth:
            return depth[sid]
        if sid in seen:
            return 0
        st = next((s for s in subtasks if s["id"] == sid), None)
        if not st:
            return 0
        deps = [d for d in st.get("depends_on", []) if d in ids]
        d = 1 + max((_depth(dep, seen | {sid}) for dep in deps), default=0)
        depth[sid] = d
        return d

    waves = max((_depth(st["id"], frozenset()) for st in subtasks), default=1)

    # 2. 历史子任务耗时中位数
    durations = sorted(
        r["duration_sec"]
        for td in _scan_task_dirs(tasks_dir)
        if (meta := _read_meta(td))
        for r in meta.get("results", [])
        if r.get("status") in ("completed", "no_changes") and r.get("duration_sec")
    )
    sample_size = len(durations)
    median = durations[sample_size // 2] if durations else _DEFAULT_SUBTASK_SEC

    # 3. 估算：受限于「关键路径（波次）」和「并行吞吐」两者较大者
    total_work = median * len(subtasks)
    est_sec = max(waves * median, total_work / max(1, parallel))

    confidence = ("high" if sample_size >= 20 else
                  "medium" if sample_size >= 5 else
                  "low" if sample_size > 0 else "none")
    return {
        "subtasks": len(subtasks),
        "waves": waves,
        "parallel": parallel,
        "median_subtask_sec": median,
        "estimated_sec": round(est_sec),
        "sample_size": sample_size,
        "confidence": confidence,
    }
