"""trajectory_signals 单元测试（ADR-010 阶段 3：轨迹驱动归因信号）。"""

import json

from agent_go.trajectory_signals import (
    collect_subtask_signals,
    extract_signals,
)

WT = "/repo/task-x/sub-1/work"


def _call(name, args, step=1):
    return {"seq": 1, "time": 0, "type": "tool/call",
            "data": {"turn": 1, "step": step, "call_id": "c1", "name": name,
                     "arguments_summary": json.dumps(args, ensure_ascii=False)}}


def _result(is_error=False):
    return {"seq": 2, "time": 0, "type": "tool/result",
            "data": {"turn": 1, "step": 1, "call_id": "c1", "is_error": is_error,
                     "result_summary": "ok"}}


def test_empty_events_all_zero():
    sig = extract_signals([], WT)
    assert sig == {
        "steps": 0, "tool_calls": 0, "tool_errors": 0, "mutations": 0,
        "path_violations": [], "repeated_edits": [],
        "mutation_without_worktree_change": False,
    }


def test_mutation_inside_worktree_no_violation():
    events = [_call("write", {"filePath": WT + "/src/utils.py", "content": "x"})]
    sig = extract_signals(events, WT)
    assert sig["mutations"] == 1
    assert sig["tool_calls"] == 1
    assert sig["path_violations"] == []
    assert sig["mutation_without_worktree_change"] is False


def test_absolute_path_outside_worktree_flagged():
    """ISSUE-58 模式：写类工具命中宿主仓库绝对路径。"""
    events = [_call("write", {"filePath": "/Users/x/workspace/agent_go/hello.txt",
                              "content": "x"})]
    sig = extract_signals(events, WT)
    assert sig["path_violations"] == ["/Users/x/workspace/agent_go/hello.txt"]
    assert sig["mutation_without_worktree_change"] is True


def test_tmp_path_benign():
    sig = extract_signals([_call("write", {"filePath": "/tmp/scratch.txt"})], WT)
    assert sig["path_violations"] == []
    assert sig["mutation_without_worktree_change"] is False


def test_read_tool_not_mutation():
    sig = extract_signals([_call("read", {"filePath": "/etc/hosts"})], WT)
    assert sig["mutations"] == 0
    assert sig["path_violations"] == []


def test_truncated_json_falls_back_to_regex():
    """arguments_summary 截断（JSON 无法解析）时正则兜底提取绝对路径。"""
    truncated = '{"filePath": "/Users/x/workspace/agent_go/hello.txt", "content": "aaaa'
    ev = {"seq": 1, "time": 0, "type": "tool/call",
          "data": {"name": "edit", "arguments_summary": truncated}}
    sig = extract_signals([ev], WT)
    assert "/Users/x/workspace/agent_go/hello.txt" in sig["path_violations"]


def test_no_worktree_no_violation_judgement():
    """无 worktree 基准时不做路径违规判定（不误报）。"""
    events = [_call("write", {"filePath": "/Users/x/anywhere.txt"})]
    sig = extract_signals(events, "")
    assert sig["path_violations"] == []
    assert sig["mutations"] == 1


def test_repeated_edits_flagged_at_threshold():
    events = [_call("edit", {"filePath": WT + "/src/a.py"}) for _ in range(3)]
    sig = extract_signals(events, WT)
    assert sig["repeated_edits"] == [{"file": WT + "/src/a.py", "count": 3}]
    # 2 次不达阈值
    sig2 = extract_signals(events[:2], WT)
    assert sig2["repeated_edits"] == []


def test_tool_errors_counted():
    events = [_result(is_error=True), _result(is_error=False), _result(is_error=True)]
    sig = extract_signals(events, WT)
    assert sig["tool_errors"] == 2


def test_mixed_inside_and_outside_not_full_violation():
    """部分写 worktree 内、部分在外 → 违规列出但不是「全部落空」。"""
    events = [
        _call("write", {"filePath": WT + "/ok.py"}),
        _call("write", {"filePath": "/Users/x/bad.py"}),
    ]
    sig = extract_signals(events, WT)
    assert sig["path_violations"] == ["/Users/x/bad.py"]
    assert sig["mutation_without_worktree_change"] is False


def _write_traj(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_collect_no_trajectory_returns_none(tmp_path):
    assert collect_subtask_signals(tmp_path, "sub-1", WT) is None
    (tmp_path / "trajectory").mkdir()
    assert collect_subtask_signals(tmp_path, "sub-1", WT) is None


def test_collect_legacy_format(tmp_path):
    _write_traj(tmp_path / "trajectory" / "sub-1.jsonl",
                [_call("write", {"filePath": "/bad/x.py"})])
    sig = collect_subtask_signals(tmp_path, "sub-1", WT)
    assert sig["mutations"] == 1
    assert sig["path_violations"] == ["/bad/x.py"]


def test_collect_attempt_files_no_double_count(tmp_path):
    """attempt-1 与旧格式兼容副本内容相同——有 attempt 文件时不重复计数。"""
    events = [_call("write", {"filePath": WT + "/a.py"})]
    _write_traj(tmp_path / "trajectory" / "sub-1.attempt-1.jsonl", events)
    _write_traj(tmp_path / "trajectory" / "sub-1.jsonl", events)  # 兼容副本
    sig = collect_subtask_signals(tmp_path, "sub-1", WT)
    assert sig["mutations"] == 1  # 不是 2


def test_collect_merges_across_attempts(tmp_path):
    _write_traj(tmp_path / "trajectory" / "sub-1.attempt-1.jsonl",
                [_call("write", {"filePath": "/bad/x.py"})])
    _write_traj(tmp_path / "trajectory" / "sub-1.attempt-2.jsonl",
                [_call("edit", {"filePath": WT + "/a.py"}), _result(is_error=True)])
    sig = collect_subtask_signals(tmp_path, "sub-1", WT)
    assert sig["mutations"] == 2
    assert sig["tool_errors"] == 1
    assert sig["path_violations"] == ["/bad/x.py"]


def test_collect_skips_bad_lines(tmp_path):
    p = tmp_path / "trajectory" / "sub-1.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"type": "step/start", "data": {"step": 1}}\n{bad json\n',
                 encoding="utf-8")
    sig = collect_subtask_signals(tmp_path, "sub-1", WT)
    assert sig["steps"] == 1
