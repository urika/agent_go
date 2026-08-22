"""pytest 单测：agent_go.task_report.generate_task_report"""

import json
from pathlib import Path

from agent_go.task_report import generate_task_report


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_normal_mixed(tmp_path: Path):
    f = tmp_path / "a.json"
    _write(f, [
        {"completed": True, "tags": ["bug", "P1"]},
        {"status": "done", "tags": "feature, p2"},
        {"completed": False, "tags": ["bug"]},
        {"status": "open"},
    ])
    r = generate_task_report([str(f)])
    assert r["total_count"] == 4
    assert r["completed_count"] == 2
    assert r["uncompleted_count"] == 2
    assert r["tags_distribution"]["bug"] == 2
    assert r["tags_distribution"]["p1"] == 1
    assert r["tags_distribution"]["feature"] == 1
    assert r["tags_distribution"]["p2"] == 1
    assert r["tags_distribution"]["untagged"] == 1


def test_empty_input():
    r = generate_task_report([])
    assert r["total_count"] == 0
    assert r["completed_count"] == 0
    assert r["uncompleted_count"] == 0
    assert r["tags_distribution"] == {}


def test_untagged(tmp_path: Path):
    f = tmp_path / "u.json"
    _write(f, [
        {"completed": True},
        {"status": "done", "tags": []},
        {"completed": False, "tags": ""},
    ])
    r = generate_task_report([str(f)])
    assert r["tags_distribution"]["untagged"] == 3


def test_tag_normalization_case_space(tmp_path: Path):
    f = tmp_path / "t.json"
    _write(f, [
        {"completed": True, "tags": ["P1"]},
        {"completed": True, "tags": [" p1 "]},
        {"completed": True, "tags": "P1,bug"},
    ])
    r = generate_task_report([str(f)])
    assert r["tags_distribution"]["p1"] == 3
    assert r["tags_distribution"]["bug"] == 1


def test_json_decode_error_skipped(tmp_path: Path):
    good = tmp_path / "good.json"
    _write(good, [{"completed": True, "tags": ["x"]}])
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    r = generate_task_report([str(bad), str(good)])
    assert r["total_count"] == 1
    assert r["completed_count"] == 1
    assert r["tags_distribution"]["x"] == 1


def test_three_shapes(tmp_path: Path):
    f_arr = tmp_path / "arr.json"
    f_dict = tmp_path / "dict.json"
    f_single = tmp_path / "single.json"
    _write(f_arr, [{"completed": True, "tags": ["a"]}, {"completed": False}])
    _write(f_dict, {"tasks": [{"completed": True, "tags": ["b"]}, {"status": "done"}]})
    _write(f_single, {"completed": True, "tags": ["c"]})
    r = generate_task_report([str(f_arr), str(f_dict), str(f_single)])
    assert r["total_count"] == 5
    assert r["completed_count"] == 4
    assert r["tags_distribution"]["a"] == 1
    assert r["tags_distribution"]["b"] == 1
    assert r["tags_distribution"]["c"] == 1
    assert r["tags_distribution"]["untagged"] == 2
