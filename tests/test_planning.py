"""S12-P2 G5：规划期欠分解检测测试。"""
from agent_go.planning import check_under_decomposition, DIFFICULTY_BASE_SUBTASKS


def test_no_under_decomposition_normal_hard():
    """hard 任务 ≥3 子任务 → 不告警。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "hard"},
        {"id": "sub-2", "difficulty": "medium"},
        {"id": "sub-3", "difficulty": "hard"},
    ]
    assert check_under_decomposition(subtasks) is False


def test_under_decomposition_hard_single():
    """hard 子任务但总子任务数 1 < 3 → 告警。"""
    subtasks = [{"id": "sub-1", "difficulty": "hard"}]
    assert check_under_decomposition(subtasks) is True


def test_under_decomposition_hard_two():
    """hard 子任务但总子任务数 2 < 3 → 告警。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "hard"},
        {"id": "sub-2", "difficulty": "easy"},
    ]
    assert check_under_decomposition(subtasks) is True


def test_no_warning_without_hard():
    """无 hard 子任务（easy/medium）→ 不告警。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "easy"},
        {"id": "sub-2", "difficulty": "medium"},
    ]
    assert check_under_decomposition(subtasks) is False


def test_empty_subtasks():
    assert check_under_decomposition([]) is False


def test_threshold_hard_three():
    """hard 阈值 = 3（V1 硬编码）。"""
    assert DIFFICULTY_BASE_SUBTASKS["hard"] == 3


# ═══════════════════════════════════════════════════════════════
# CR-G4: 难度启发式 hint + planner 主观难度交叉核对
# ═══════════════════════════════════════════════════════════════

import logging

from agent_go.planning import difficulty_hint, check_difficulty_mismatch


def _capture_warnings(fn):
    """跑 fn(logger) 并返回触发的 warning 文本列表。"""
    log = logging.getLogger("g4-test")
    log.setLevel(logging.WARNING)
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    log.addHandler(_H())
    try:
        fn(log)
    finally:
        log.removeHandler(_H())
    return records


def test_difficulty_hint_hard_keywords():
    """跨模块/重构/架构等结构性关键词 → hard。"""
    assert difficulty_hint({"description": "跨模块重构认证架构"}) == "hard"
    assert difficulty_hint({"agent_prompt": "refactor the data pipeline across modules"}) == "hard"


def test_difficulty_hint_easy_keywords():
    """helper/格式化/单点小改 → easy。"""
    assert difficulty_hint({"description": "add a format helper in utils.py"}) == "easy"


def test_difficulty_hint_multi_file_signals_hard():
    """提及 ≥3 个不同源码路径 → 倾向 hard（多文件改动）。"""
    desc = "修改 a.py, b.py, c.py 三个模块"
    assert difficulty_hint({"description": desc}) == "hard"


def test_difficulty_hint_neutral_returns_none():
    """中性描述（无强信号）→ None，不与 planner 唱反调。"""
    assert difficulty_hint({"description": "实现一个功能"}) is None


def test_difficulty_hint_empty_returns_none():
    assert difficulty_hint({}) is None
    assert difficulty_hint({"description": ""}) is None


def test_check_difficulty_mismatch_cross_two_tiers_warns():
    """planner 标 easy 但信号强烈倾向 hard（跨两档）→ 告警。"""
    subtasks = [{"id": "s1", "difficulty": "easy",
                 "description": "跨模块重构整个架构，涉及 a.py b.py c.py"}]
    warns = _capture_warnings(lambda lg: check_difficulty_mismatch(subtasks, lg))
    assert len(warns) == 1
    assert "G4" in warns[0] and "easy" in warns[0] and "hard" in warns[0]


def test_check_difficulty_mismatch_single_tier_no_warn():
    """单档差异（medium vs hard）不报（噪声大）。"""
    subtasks = [{"id": "s1", "difficulty": "medium",
                 "description": "跨模块重构架构 a.py b.py c.py"}]  # hint=hard, planned=medium → 1档差不报
    warns = _capture_warnings(lambda lg: check_difficulty_mismatch(subtasks, lg))
    assert warns == []


def test_check_difficulty_mismatch_agrees_no_warn():
    """planner 标的与 hint 一致（都 easy）→ 不告警。"""
    subtasks = [{"id": "s1", "difficulty": "easy", "description": "add a format helper"}]
    warns = _capture_warnings(lambda lg: check_difficulty_mismatch(subtasks, lg))
    assert warns == []


def test_check_difficulty_mismatch_neutral_hint_no_warn():
    """hint=None（中性）→ 不告警。"""
    subtasks = [{"id": "s1", "difficulty": "hard", "description": "实现功能"}]
    warns = _capture_warnings(lambda lg: check_difficulty_mismatch(subtasks, lg))
    assert warns == []
