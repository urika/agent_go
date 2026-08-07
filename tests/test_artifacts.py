"""S9-B 产物导出测试：artifacts.py（collect_from_worktree / export / render_export_summary）

覆盖设计文档 office-capability-extension.md §5.2 验收项：
  B1  子任务写 __artifacts__/report.md，--artifact-dir 指定后文件出现在目标目录
  B2  不指定 --artifact-dir 时，产物留在 worktree（向后兼容，无导出行为）
  B3  失败保留的 worktree 中的产物也能被收集
"""

import os
from pathlib import Path

import pytest

from agent_go.artifacts import (
    ARTIFACT_DIR_NAME,
    MAX_ARTIFACT_BYTES,
    collect_from_worktree,
    export,
    render_export_summary,
)


class TestCollectFromWorktree:
    """B1: 声明制扫描 worktree/__artifacts__/**。"""

    def test_no_artifact_dir_returns_empty(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        assert collect_from_worktree(worktree, "sub-1") == []

    def test_empty_artifact_dir_returns_empty(self, tmp_path):
        worktree = tmp_path / "wt"
        (worktree / ARTIFACT_DIR_NAME).mkdir(parents=True)
        assert collect_from_worktree(worktree, "sub-1") == []

    def test_flat_artifact_collected(self, tmp_path):
        worktree = tmp_path / "wt"
        art_dir = worktree / ARTIFACT_DIR_NAME
        art_dir.mkdir(parents=True)
        f = art_dir / "report.md"
        f.write_text("# report", encoding="utf-8")
        result = collect_from_worktree(worktree, "sub-1")
        assert len(result) == 1
        assert result[0]["sub_id"] == "sub-1"
        assert result[0]["path"] == f
        assert result[0]["size_bytes"] == len("# report")

    def test_nested_artifacts_collected(self, tmp_path):
        worktree = tmp_path / "wt"
        (worktree / ARTIFACT_DIR_NAME / "sub").mkdir(parents=True)
        (worktree / ARTIFACT_DIR_NAME / "sub" / "a.xlsx").write_text("x", encoding="utf-8")
        (worktree / ARTIFACT_DIR_NAME / "b.pptx").write_text("y", encoding="utf-8")
        result = collect_from_worktree(worktree, "sub-1")
        assert len(result) == 2
        names = sorted(r["path"].name for r in result)
        assert names == ["a.xlsx", "b.pptx"]

    def test_non_artifact_files_ignored(self, tmp_path):
        """只有 __artifacts__/ 下的文件算产物；worktree 根目录的文件不算。"""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "code.py").write_text("print(1)", encoding="utf-8")
        (worktree / ARTIFACT_DIR_NAME).mkdir()
        (worktree / ARTIFACT_DIR_NAME / "out.md").write_text("hi", encoding="utf-8")
        result = collect_from_worktree(worktree, "sub-1")
        assert len(result) == 1
        assert result[0]["path"].name == "out.md"


class TestExport:
    """B1/B3: export 收集所有 worktree 产物到 artifact_dir。"""

    def _make_task_dir(self, tmp_path, subtasks, artifacts=None):
        """构造 task_dir/{sub_id}/work/__artifacts__/... 布局。artifacts: {sub_id: [filename,...]}"""
        task_dir = tmp_path / "tasks" / "t1"
        artifacts = artifacts or {}
        for sub_id in subtasks:
            work = task_dir / sub_id / "work"
            work.mkdir(parents=True)
            for name in artifacts.get(sub_id, []):
                (work / ARTIFACT_DIR_NAME).mkdir(parents=True, exist_ok=True)
                (work / ARTIFACT_DIR_NAME / name).write_text(f"content-{name}", encoding="utf-8")
        return task_dir

    def test_export_organizes_by_task_and_subtask(self, tmp_path):
        task_dir = self._make_task_dir(tmp_path, ["sub-1", "sub-2"], {
            "sub-1": ["report.md"],
            "sub-2": ["data.xlsx"],
        })
        artifact_dir = tmp_path / "out"
        results = {"sub-1": {"status": "completed"}, "sub-2": {"status": "completed"}}

        res = export("task-123", results, artifact_dir, task_dir)

        assert len(res["exported"]) == 2
        assert res["skipped"] == []
        assert (artifact_dir / "task-123" / "sub-1" / "report.md").exists()
        assert (artifact_dir / "task-123" / "sub-2" / "data.xlsx").exists()
        assert (artifact_dir / "task-123" / "sub-1" / "report.md").read_text(encoding="utf-8") == "content-report.md"

    def test_export_preserved_worktree_artifacts(self, tmp_path):
        """B3: 失败保留的 worktree 中的产物也能被收集。"""
        task_dir = self._make_task_dir(tmp_path, ["sub-1"], {"sub-1": ["partial.md"]})
        artifact_dir = tmp_path / "out"
        results = {"sub-1": {"status": "failed"}}

        res = export("task-123", results, artifact_dir, task_dir)
        assert len(res["exported"]) == 1
        assert (artifact_dir / "task-123" / "sub-1" / "partial.md").exists()

    def test_export_no_artifacts(self, tmp_path):
        task_dir = self._make_task_dir(tmp_path, ["sub-1"])
        artifact_dir = tmp_path / "out"
        res = export("task-123", {"sub-1": {"status": "completed"}}, artifact_dir, task_dir)
        assert res["exported"] == []
        assert res["skipped"] == []

    def test_export_nested_subdir_preserved(self, tmp_path):
        task_dir = tmp_path / "tasks" / "t1"
        nested = task_dir / "sub-1" / "work" / ARTIFACT_DIR_NAME / "charts"
        nested.mkdir(parents=True)
        (nested / "trend.png").write_text("img", encoding="utf-8")
        artifact_dir = tmp_path / "out"

        res = export("task-123", {"sub-1": {"status": "completed"}}, artifact_dir, task_dir)
        assert len(res["exported"]) == 1
        assert (artifact_dir / "task-123" / "sub-1" / "charts" / "trend.png").exists()

    def test_export_oversized_artifact_skipped(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path, ["sub-1"], {"sub-1": ["big.bin"]})
        big = task_dir / "sub-1" / "work" / ARTIFACT_DIR_NAME / "big.bin"
        big.write_bytes(b"x" * 1024)
        artifact_dir = tmp_path / "out"
        # 阈值压小到 100 字节，触发跳过分支
        monkeypatch.setattr("agent_go.artifacts.MAX_ARTIFACT_BYTES", 100)

        res = export("task-123", {"sub-1": {"status": "completed"}}, artifact_dir, task_dir)
        assert res["exported"] == []
        assert len(res["skipped"]) == 1
        assert res["skipped"][0]["sub_id"] == "sub-1"

    def test_export_missing_worktree_no_crash(self, tmp_path):
        """worktree 目录不存在 → 不 crash，无导出。"""
        task_dir = tmp_path / "tasks" / "t1"
        artifact_dir = tmp_path / "out"
        res = export("task-123", {"sub-1": {"status": "completed"}}, artifact_dir, task_dir)
        assert res["exported"] == []

    def test_export_unwritable_dir_returns_empty(self, tmp_path):
        task_dir = self._make_task_dir(tmp_path, ["sub-1"], {"sub-1": ["a.md"]})
        artifact_dir = tmp_path / "out"
        # 用一个文件路径作为目标目录 → mkdir 失败
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        res = export("task-123", {"sub-1": {"status": "completed"}}, blocker, task_dir)
        assert res["exported"] == []
        assert res["skipped"] == []


class TestRenderExportSummary:
    def test_empty_summary(self):
        assert render_export_summary({"exported": [], "skipped": [], "dir": "/tmp/out"}) == ""

    def test_summary_contains_exported_files(self):
        summary = render_export_summary({
            "exported": [{"sub_id": "sub-1", "src": "/w/__artifacts__/r.md", "dst": "/out/t/r.md", "size_bytes": 10}],
            "skipped": [],
            "dir": "/out",
        })
        assert "sub-1" in summary
        assert "r.md" in summary
        assert "/out" in summary

    def test_summary_contains_skipped(self):
        summary = render_export_summary({
            "exported": [],
            "skipped": [{"sub_id": "sub-1", "src": "/w/__artifacts__/big.bin", "reason": "超过大小阈值"}],
            "dir": "/out",
        })
        assert "big.bin" in summary
        assert "超过大小阈值" in summary


class TestConstants:
    def test_artifact_dir_name(self):
        assert ARTIFACT_DIR_NAME == "__artifacts__"

    def test_max_artifact_bytes_default(self):
        assert MAX_ARTIFACT_BYTES == 100 * 1024 * 1024
