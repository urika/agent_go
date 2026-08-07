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
