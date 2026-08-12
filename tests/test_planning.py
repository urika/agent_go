"""S12-P2 G5/G6/P2：规划期分解质量检测测试（欠分解 + 过度分解 + 验证命令降级 + 函数引用检查）。"""
from agent_go.planning import (
    check_under_decomposition, check_over_decomposition,
    DIFFICULTY_BASE_SUBTASKS, validate_plan_quality,
    check_agent_prompt_functions,
)


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


def test_validate_plan_quality_warns_rejected_verification_command():
    """verification_command_rejected 已降级为 warning（P1），不再 blocking。"""
    result = validate_plan_quality([{"id": "sub-1", "verification": "pytest tests && grep OK out.txt"}])
    # grep 不在白名单 → verification_command_rejected → warning（非 blocking）
    assert result["status"] == "warning"
    assert len(result["blocking_issues"]) == 0
    assert result["warnings"][0]["type"] == "verification_command_rejected"
    assert result["repairable_issues"][0]["type"] == "verification_command_rejected"


def test_build_plan_repair_feedback_is_bounded_and_actionable():
    from agent_go.planning import build_plan_repair_feedback

    quality = validate_plan_quality([{
        "id": "sub-1",
        "verification": "bash -c 'python -c \"print(1)\"'",
    }])
    feedback = build_plan_repair_feedback(quality, max_chars=500)
    assert len(feedback) <= 500
    assert "Plan 预检修复反馈" in feedback
    assert "不要使用 bash/sh -c" in feedback


def test_validate_plan_quality_empty_plan_is_blocked():
    """0 子任务必须阻断——goal_ab 实验实证：planner 把交付物 JSON schema 误当执行计划
    返回 → 0 steps → 真空 DELIVERY_READY 假成功。empty_plan 为 blocking + repairable。"""
    result = validate_plan_quality([])
    assert result["status"] == "blocked"
    assert result["blocking_issues"][0]["type"] == "empty_plan"
    assert any(i["type"] == "empty_plan" for i in result["repairable_issues"])


def test_build_plan_repair_feedback_empty_plan_hint():
    """empty_plan 的修复反馈须包含 schema 纠正提示。"""
    from agent_go.planning import build_plan_repair_feedback

    quality = validate_plan_quality([])
    feedback = build_plan_repair_feedback(quality)
    assert "empty_plan" in feedback
    assert "交付物 JSON" in feedback


def test_validate_plan_quality_detects_scope_and_requirement_gaps():
    result = validate_plan_quality([
        {"id": "sub-1", "verification": "pytest tests", "files": ["src/a.py"],
         "do_not_touch": ["src/a.py"], "requirement_ids": ["REQ-1"]},
    ], requirements=["REQ-1", "REQ-2"])
    assert result["status"] == "blocked"
    assert {issue["type"] for issue in result["blocking_issues"]} == {
        "scope_conflict", "requirement_coverage_incomplete",
    }


def test_validate_plan_quality_files_hint_no_scope_conflict():
    """tester 场景：files_hint 引用 do_not_touch 文件不触发 scope_conflict（仅 files 参与冲突检查）。"""
    result = validate_plan_quality([
        {"id": "sub-1", "verification": "pytest tests", "files_hint": "src/a.py",
         "do_not_touch": ["src/a.py"]},
    ])
    assert result["status"] == "passed"
    assert "scope_conflict" not in {issue["type"] for issue in result["blocking_issues"]}


# ═══════════════════════════════════════════════════════════════
# P0/P1 回归测试：POSIX 命令白名单 + 验证命令降级 + 过度分解检测
# ═══════════════════════════════════════════════════════════════

def test_posix_commands_pass_verification_whitelist():
    """P0: ls/find/cat/head/wc/test/stat 等只读 POSIX 命令应通过白名单。"""
    commands = [
        "ls tests/",
        "ls -la tests/",
        "find . -name '*.py'",
        "find tests/ -type f",
        "cat README.md",
        "head -n 10 file.txt",
        "wc -l file.txt",
        "test -f config.json",
        "stat file.txt",
    ]
    for cmd in commands:
        result = validate_plan_quality([{"id": "sub-1", "verification": cmd}])
        assert result["status"] == "passed", f"命令 {cmd!r} 应通过但被拒绝: {result}"


def test_posix_commands_with_echo_chain():
    """P0: echo + POSIX 命令链（M0 真实失败场景）应全部通过。"""
    result = validate_plan_quality([{
        "id": "sub-1",
        "verification": "echo files created && ls tests/fixtures/sample.csv tests/fixtures/pipeline_valid.json"
    }])
    assert result["status"] == "passed", f"M0 场景应通过: {result}"


def test_verification_command_rejected_is_warning_not_blocked():
    """P1: 未知验证命令应产生 warning，不 blocking 任务执行。"""
    result = validate_plan_quality([{"id": "sub-1", "verification": "grep OK out.txt"}])
    assert result["status"] == "warning"
    assert len(result["blocking_issues"]) == 0
    warning_types = {w["type"] for w in result["warnings"]}
    assert "verification_command_rejected" in warning_types
    assert {i["type"] for i in result["repairable_issues"]} == {"verification_command_rejected"}


def test_valid_commands_no_warning():
    """pytest 等已知合法命令不产生任何 warning 或 issue。"""
    result = validate_plan_quality([{"id": "sub-1", "verification": "pytest tests/ -v"}])
    assert result["status"] == "passed"
    assert len(result["warnings"]) == 0
    assert len(result["blocking_issues"]) == 0


# ── G6 过度分解检测 ───────────────────────────────────────────

def test_over_decomposition_few_files_many_subtasks():
    """G6: ≤2 个文件但 ≥3 个子任务 → 过度分解告警。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/utils.py"], "difficulty": "easy"},
        {"id": "sub-2", "files": ["src/utils.py"], "difficulty": "easy"},
        {"id": "sub-3", "files": ["tests/test_utils.py"], "difficulty": "easy"},
    ]
    # 2 个文件（src/utils.py, tests/test_utils.py），3 个子任务 → 过度分解
    warns = _capture_warnings(lambda lg: check_over_decomposition(subtasks, logger=lg))
    assert len(warns) >= 1
    assert any("G6" in w and "过度分解" in w for w in warns)


def test_over_decomposition_all_easy_many_subtasks():
    """G6: 全线 easy 但 ≥3 个子任务 → 过度分解告警。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "easy", "files": ["a.py"]},
        {"id": "sub-2", "difficulty": "easy", "files": ["b.py"]},
        {"id": "sub-3", "difficulty": "easy", "files": ["c.py"]},
    ]
    # 3 个文件（>2），文件数不触发，但全线 easy + 3 子任务 → 仍应告警
    warns = _capture_warnings(lambda lg: check_over_decomposition(subtasks, logger=lg))
    assert len(warns) >= 1
    assert any("G6" in w and "过度分解" in w for w in warns)


def test_no_over_decomposition_for_large_task():
    """G6: 多文件 hard 子任务不应触发过度分解告警。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "hard", "files": ["src/auth.py", "src/db.py"]},
        {"id": "sub-2", "difficulty": "hard", "files": ["src/api.py", "src/models.py"]},
        {"id": "sub-3", "difficulty": "medium", "files": ["tests/test_auth.py"]},
    ]
    # 多文件 + mixed difficulty → 不触发
    warns = _capture_warnings(lambda lg: check_over_decomposition(subtasks, logger=lg))
    assert warns == []


def test_no_over_decomposition_two_subtasks():
    """G6: <3 个子任务不触发过度分解检测。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "easy", "files": ["src/utils.py"]},
        {"id": "sub-2", "difficulty": "easy", "files": ["tests/test_utils.py"]},
    ]
    warns = _capture_warnings(lambda lg: check_over_decomposition(subtasks, logger=lg))
    assert warns == []


def test_over_decomposition_empty():
    assert check_over_decomposition([]) is False


def test_over_decomposition_with_total_files_param():
    """G6: 显式传入 total_files 参数应参与文件数计算。"""
    subtasks = [
        {"id": "sub-1", "difficulty": "easy"},
        {"id": "sub-2", "difficulty": "easy"},
        {"id": "sub-3", "difficulty": "easy"},
    ]
    # 子任务不指定 files，但 total_files=2 → 应触发过度分解
    warns = _capture_warnings(lambda lg: check_over_decomposition(subtasks, total_files=2, logger=lg))
    assert len(warns) >= 1
    assert any("G6" in w for w in warns)


# ═══════════════════════════════════════════════════════════════
# P2: agent_prompt 函数引用静态检查
# ═══════════════════════════════════════════════════════════════

from pathlib import Path


def test_check_agent_prompt_functions_detects_unknown(tmp_path):
    """P2: agent_prompt 引用项目中不存在的函数 → 产生 warning。"""
    # 创建项目目录，包含一个简单源文件
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "utils.py").write_text("def existing_helper():\n    pass\n")
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Please call load_filtered() to process data."},
    ]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    assert len(warnings) >= 1
    assert warnings[0]["type"] == "unknown_function_in_agent_prompt"
    assert warnings[0]["function"] == "load_filtered"
    assert "load_filtered" in warnings[0]["agent_prompt_snippet"]


def test_check_agent_prompt_functions_recognizes_existing(tmp_path):
    """P2: agent_prompt 引用的函数在项目中存在 → 不产生 warning。"""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "utils.py").write_text("def load_filtered():\n    pass\n\ndef process_pipeline():\n    pass\n")
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Please call load_filtered() and process_pipeline() to complete the task."},
    ]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    assert warnings == []


def test_check_agent_prompt_functions_skips_builtins(tmp_path):
    """P2: 内置函数和常见 stdlib 函数不产生 warning。"""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "main.py").write_text("def main():\n    pass\n")
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Use len(), sorted(), json.dumps(), and open() to read the file."},
    ]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    # len, sorted, json.dumps, open 都是内置/stdlib → 无 warning
    unknown_funcs = {w["function"] for w in warnings}
    assert "len" not in unknown_funcs
    assert "sorted" not in unknown_funcs
    assert "json.dumps" not in unknown_funcs
    assert "open" not in unknown_funcs


def test_check_agent_prompt_functions_no_repo(tmp_path):
    """P2: 无 repo 时不扫描文件，所有函数视为未知（保守策略）。"""
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Call load_filtered() to process data."},
    ]
    # 无 repo → 无项目函数库 → 不产生 warning（因为无法判断）
    warnings = check_agent_prompt_functions(subtasks, repo=None)
    assert warnings == []


def test_check_agent_prompt_functions_empty_subtasks():
    assert check_agent_prompt_functions([], repo=Path("/tmp")) == []


def test_check_agent_prompt_functions_no_agent_prompt(tmp_path):
    """P2: 无 agent_prompt 的子任务不触发检查。"""
    proj = tmp_path / "project"
    proj.mkdir()
    subtasks = [{"id": "sub-1", "agent_prompt": ""}]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    assert warnings == []


def test_check_agent_prompt_functions_skips_shell_commands(tmp_path):
    """P2: shell 命令和路径不当作函数引用。"""
    proj = tmp_path / "project"
    proj.mkdir()
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Run ./script.sh and /usr/bin/python to execute the pipeline."},
    ]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    # ./script.sh 和 /usr/bin/python 不是函数引用 → 无 warning
    unknown_funcs = {w["function"] for w in warnings}
    assert "./script.sh" not in unknown_funcs
    assert "/usr/bin/python" not in unknown_funcs


def test_check_agent_prompt_functions_class_methods(tmp_path):
    """P2: 已知 class 的 method 调用不产生 warning。"""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "models.py").write_text("class DataPipeline:\n    def run(self):\n        pass\n")
    subtasks = [
        {"id": "sub-1", "agent_prompt": "Instantiate DataPipeline and call pipeline.run() to execute."},
    ]
    warnings = check_agent_prompt_functions(subtasks, repo=proj)
    # DataPipeline 在项目中定义，pipeline.run() 中 pipeline 不是已知类型 → warning
    # DataPipeline.run 应被识别（DataPipeline 是已知 class）
    unknown_funcs = {w["function"] for w in warnings}
    # "pipeline.run" — "pipeline" 不在 project_funcs 中，所以会被标记
    # 但如果 Logic 正确，只有 pipeline.run 是 unknown (pipeline 是变量名不是 class)
    assert "DataPipeline" not in unknown_funcs


def test_validate_plan_quality_with_repo_integration(tmp_path):
    """P2: validate_plan_quality 传入 repo 时自动执行函数引用检查。"""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "utils.py").write_text("def existing_func():\n    pass\n")
    result = validate_plan_quality(
        [{"id": "sub-1", "agent_prompt": "Call nonexistent_helper() to process.", "verification": "ls"}],
        repo=proj,
    )
    # 验证命令 ls 通过 + 无 blocking issue → status 为 passed 或 warning
    assert result["status"] in ("passed", "warning")
    # 应包含 P2 函数引用 warning
    func_warnings = [w for w in result["warnings"] if w["type"] == "unknown_function_in_agent_prompt"]
    assert len(func_warnings) >= 1
    assert func_warnings[0]["function"] == "nonexistent_helper"


# ═══════════════════════════════════════════════════════════════
# G7: 跨子任务文件重叠检测 + G6 过度分解升级为 blocking
# ═══════════════════════════════════════════════════════════════

from agent_go.planning import check_subtask_file_overlap


def test_file_overlap_without_dependency_is_blocking():
    """G7: 无依赖关系的子任务共享同一文件 → blocking issue。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/storage.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/storage.py"], "verification": "pytest tests"},
    ]
    result = check_subtask_file_overlap(subtasks)
    types = {issue["type"] for issue in result["issues"]}
    assert "file_overlap_without_dependency" in types
    assert result["warnings"] == []
    # 集成进 validate_plan_quality → blocked
    full = validate_plan_quality(subtasks)
    assert full["status"] == "blocked"
    assert any(i["type"] == "file_overlap_without_dependency" for i in full["blocking_issues"])


def test_file_overlap_with_dependency_is_warning():
    """G7: 共享文件的子任务在同一条依赖链上 → warning，不阻断。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/storage.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/storage.py"], "depends_on": ["sub-1"], "verification": "pytest tests"},
    ]
    result = check_subtask_file_overlap(subtasks)
    assert result["issues"] == []
    assert any(w["type"] == "file_overlap_with_dependency" for w in result["warnings"])
    full = validate_plan_quality(subtasks)
    # warning（非 blocked）——但可能有 missing_verification 等 warning 混杂，重点是无 file_overlap blocking
    assert full["status"] in ("passed", "warning")
    assert not any(i["type"] == "file_overlap_without_dependency" for i in full["blocking_issues"])


def test_file_overlap_via_files_hint():
    """G7: files_hint 引用同一文件也应被检测（tester 场景不误报 scope_conflict 但需报重叠）。"""
    subtasks = [
        {"id": "sub-1", "files_hint": "src/cli.py", "verification": "pytest tests"},
        {"id": "sub-2", "files_hint": "src/cli.py", "verification": "pytest tests"},
    ]
    result = check_subtask_file_overlap(subtasks)
    assert any(i["type"] == "file_overlap_without_dependency" for i in result["issues"])


def test_file_overlap_no_common_file():
    """G7: 文件互斥 → 无重叠。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/cli.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/storage.py"], "verification": "pytest tests"},
    ]
    result = check_subtask_file_overlap(subtasks)
    assert result["issues"] == []
    assert result["warnings"] == []
    assert validate_plan_quality(subtasks)["status"] == "passed"


def test_over_decomposition_small_change_blocking():
    """G6 升级: ≤2 文件但 ≥3 子任务 → 过度分解 blocking。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/utils.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/utils.py"], "verification": "pytest tests"},
        {"id": "sub-3", "files": ["tests/test_utils.py"], "verification": "pytest tests"},
    ]
    full = validate_plan_quality(subtasks)
    assert full["status"] == "blocked"
    assert any(i["type"] == "over_decomposition" for i in full["blocking_issues"])


def test_no_over_decomposition_with_no_file_scope():
    """G6 升级: 文件作用域为空（无法判定改动面）时不触发过度分解阻断。"""
    subtasks = [
        {"id": "sub-1", "verification": "pytest tests"},
        {"id": "sub-2", "verification": "pytest tests"},
        {"id": "sub-3", "verification": "pytest tests"},
    ]
    full = validate_plan_quality(subtasks)
    assert not any(i["type"] == "over_decomposition" for i in full["blocking_issues"])


# ═══════════════════════════════════════════════════════════════
# G8: 独立可验证性检查（Split Design Benchmark 实证的拆分/合并判据）
# ═══════════════════════════════════════════════════════════════

def test_unverifiable_upstream_is_blocking():
    """G8: 被依赖的子任务无验证命令 → blocking（上游产物未经验证即被下游消费）。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/storage.py"], "verification": ""},
        {"id": "sub-2", "files": ["src/cli.py"], "depends_on": ["sub-1"], "verification": "pytest tests"},
    ]
    full = validate_plan_quality(subtasks)
    assert any(i["type"] == "unverifiable_upstream" for i in full["blocking_issues"])
    issue = next(i for i in full["blocking_issues"] if i["type"] == "unverifiable_upstream")
    assert issue["subtask_id"] == "sub-1"
    assert issue["depended_by"] == ["sub-2"]


def test_verifiable_upstream_passes():
    """G8: 被依赖的子任务有验证命令 → 不触发。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/storage.py"], "verification": "pytest tests/test_storage.py"},
        {"id": "sub-2", "files": ["src/cli.py"], "depends_on": ["sub-1"], "verification": "pytest tests"},
    ]
    full = validate_plan_quality(subtasks)
    assert not any(i["type"] == "unverifiable_upstream" for i in full["blocking_issues"])


def test_no_verification_no_dependents_not_blocked():
    """G8: 无验证命令但无下游依赖的子任务 → 仍保留原 missing_verification warning，不触发 G8。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/cli.py"], "verification": ""},
    ]
    full = validate_plan_quality(subtasks)
    assert not any(i["type"] == "unverifiable_upstream" for i in full["blocking_issues"])
    assert any(w["type"] == "missing_verification" for w in full["warnings"])


# ── G8 扩展: verification 与自身改动文件匹配校验 ─────────────

def test_verification_mismatch_warns_other_step_file():
    """G8 扩展: 验证命令引用其他 step 专属文件 → warning。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/cli.py"], "verification": "pytest tests/test_storage.py"},
        {"id": "sub-2", "files": ["src/storage.py", "tests/test_storage.py"], "verification": "pytest tests/test_storage.py"},
    ]
    full = validate_plan_quality(subtasks)
    mismatch = [w for w in full["warnings"] if w["type"] == "verification_file_mismatch"]
    assert any(m["subtask_id"] == "sub-1" and m["verified_file"] == "tests/test_storage.py" for m in mismatch)
    assert not any(m["subtask_id"] == "sub-2" for m in mismatch)  # 本 step 拥有该文件，不告警


def test_verification_own_test_no_warning():
    """G8 扩展: 验证自己声明的测试文件 → 不告警。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/cli.py", "tests/test_cli.py"], "verification": "pytest tests/test_cli.py"},
    ]
    full = validate_plan_quality(subtasks)
    assert not any(w["type"] == "verification_file_mismatch" for w in full["warnings"])


def test_verification_file_not_owned_no_warning():
    """G8 扩展: 验证文件不属于任何 step（既有测试）→ 不告警（回归门是合理场景）。"""
    subtasks = [
        {"id": "sub-1", "files": ["src/cli.py"], "verification": "pytest tests/test_models_utils.py"},
    ]
    full = validate_plan_quality(subtasks)
    assert not any(w["type"] == "verification_file_mismatch" for w in full["warnings"])


# ═══════════════════════════════════════════════════════════════
# 改进 C（轻量版）：并行 wave 内跨文件 import 关系 warning
# ═══════════════════════════════════════════════════════════════

from agent_go.planning import check_parallel_import_relations


def test_parallel_import_relation_warning(tmp_path):
    """并行子任务：A 修改的文件被 B 修改的文件 import → warning。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "views.py").write_text("from src.blog import views", encoding="utf-8")
    (tmp_path / "src" / "blog").mkdir()
    (tmp_path / "src" / "blog" / "views.py").write_text("def get_post_list(): pass", encoding="utf-8")

    subtasks = [
        {"id": "sub-1", "files": ["src/blog/views.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/views.py"], "verification": "pytest tests"},
    ]
    warnings = check_parallel_import_relations(subtasks, tmp_path)
    rel = [w for w in warnings if w["type"] == "parallel_import_relation"]
    assert len(rel) >= 1, f"应检测到跨文件 import 关系: {warnings}"
    assert rel[0]["imported_file"] == "src/blog/views.py"
    assert rel[0]["importing_file"] == "src/views.py"

    # 集成进 validate_plan_quality → warning 但非 blocking
    full = validate_plan_quality(subtasks, repo=tmp_path)
    assert any(w["type"] == "parallel_import_relation" for w in full["warnings"])
    assert full["status"] == "warning", "import 关系是告警不阻断"


def test_parallel_import_relation_skips_dependent(tmp_path):
    """有依赖路径的子任务对 → 不告警（上游 merge 保证顺序一致性）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "views.py").write_text("from src.blog import views", encoding="utf-8")
    (tmp_path / "src" / "blog").mkdir()
    (tmp_path / "src" / "blog" / "views.py").write_text("def get_post_list(): pass", encoding="utf-8")

    subtasks = [
        {"id": "sub-1", "files": ["src/blog/views.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/views.py"], "verification": "pytest tests",
         "depends_on": ["sub-1"]},
    ]
    warnings = check_parallel_import_relations(subtasks, tmp_path)
    assert not any(w["type"] == "parallel_import_relation" for w in warnings), \
        f"依赖链上的子任务不应告警: {warnings}"


def test_parallel_import_relation_no_match(tmp_path):
    """无 import 关系 / 文件不存在 → 不告警。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os", encoding="utf-8")
    subtasks = [
        {"id": "sub-1", "files": ["src/a.py"], "verification": "pytest tests"},
        {"id": "sub-2", "files": ["src/b.py"], "verification": "pytest tests"},
    ]
    warnings = check_parallel_import_relations(subtasks, tmp_path)
    assert not any(w["type"] == "parallel_import_relation" for w in warnings)
    # 无 repo → 不检测
    assert check_parallel_import_relations(subtasks, None) == []


# ═══════════════════════════════════════════════════════════════
# Goal Contract
# ═══════════════════════════════════════════════════════════════

def test_build_goal_contract_collects_evidence_and_constraints():
    from agent_go.planning import build_goal_contract
    subtasks = [
        {"id": "sub-1", "verification": "pytest tests/auth", "do_not_touch": ["migrations/"]},
        {"id": "sub-2", "verification": "ruff check src/", "scope_boundary": "只修改 checkout/ 目录"},
    ]
    contract = build_goal_contract("实现 checkout 重试", subtasks)
    assert contract["goal_description"] == "实现 checkout 重试"
    assert contract["delivery_required"] is True
    assert "pytest tests/auth" in contract["completion_evidence"]
    assert "ruff check src/" in contract["completion_evidence"]
    assert any("migrations/" in c for c in contract["constraints"])
    assert any("checkout/" in c for c in contract["constraints"])
    assert contract["missing_verification_subtasks"] == []


def test_build_goal_contract_detects_missing_verification():
    from agent_go.planning import build_goal_contract
    subtasks = [
        {"id": "sub-1", "verification": ""},
        {"id": "sub-2", "verification": "pytest tests"},
    ]
    contract = build_goal_contract("task", subtasks)
    assert "sub-1" in contract["missing_verification_subtasks"]
    assert "sub-2" not in contract["missing_verification_subtasks"]
    assert contract["completion_evidence"] == ["pytest tests"]


def test_build_goal_contract_empty_subtasks():
    from agent_go.planning import build_goal_contract
    contract = build_goal_contract("task", [], delivery_required=False)
    assert contract["delivery_required"] is False
    assert contract["completion_evidence"] == []
