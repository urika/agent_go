"""S-1 覆盖扫描器测试——合成计划、零外发。"""
import json

from tools.s1_spec_scan import load_subtasks, main, scan_plan, scan_subtask


def _sub(sid, files=None, verification="", hint=""):
    sub = {"id": sid, "files": files or []}
    if verification:
        sub["verification"] = verification
    if hint:
        sub["files_hint"] = hint
    return sub


def test_scan_subtask_classifies_anchored_file():
    row = scan_subtask(_sub("sub-1", ["src/a.py"], "pytest tests/test_a.py"))
    assert row["scope"] == "file" and row["status"] == "covered" and row["rules"] == "covered"


def test_scan_subtask_flags_suite_level_on_core_file():
    row = scan_subtask(_sub("sub-2", ["src/b.py"], "pytest tests/"))
    assert row["scope"] == "suite" and row["status"] == "not_anchored" and row["rules"] == "gap"


def test_scan_subtask_allows_suite_when_no_core_files():
    row = scan_subtask(_sub("sub-3", ["docs/readme.md"], "pytest tests/"))
    assert row["status"] == "covered"  # 无核心文件改动 → 整仓测试可接受


def test_scan_subtask_missing_verification():
    row = scan_subtask(_sub("sub-4", ["src/c.py"]))
    assert row["status"] == "missing_verification" and row["scope"] == "none"


def test_scan_plan_lists_gap_dimensions():
    scan = scan_plan([
        _sub("s1", ["src/a.py"], "pytest tests/test_a.py"),
        _sub("s2", ["src/b.py"], "pytest tests/"),
        _sub("s3", ["src/c.py"]),
    ])
    assert scan["subtasks"] == 3 and scan["covered"] == 1
    assert scan["structural_coverage"] == round(1 / 3, 6)
    reasons = {g["subtask_id"]: g["reason"] for g in scan["gap_dimensions"]}
    assert reasons == {"s2": "not_anchored", "s3": "missing_verification"}
    assert all(r["jev"] == "unknown" for r in scan["rows"])


def test_load_subtasks_accepts_plan_shape(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"plan": {"subtasks": [{"id": "p1"}]}}), encoding="utf-8")
    assert load_subtasks(meta) == [{"id": "p1"}]


def test_main_text_and_error_paths(tmp_path, capsys):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"subtasks": [{"id": "s1", "files": ["src/a.py"],
                                              "verification": "pytest tests/test_a.py"}]}),
                    encoding="utf-8")
    assert main(["--meta", str(meta)]) == 0
    assert "结构性覆盖" in capsys.readouterr().out
    assert main(["--meta", str(tmp_path / "missing.json")]) == 2
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"subtasks": []}), encoding="utf-8")
    assert main(["--meta", str(empty)]) == 2
