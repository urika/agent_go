"""checkpoint.py 测试 — SnapshotManager 文件快照（take/list/restore/delete）。

被 executor 用于子任务回滚；此前整模块零测试，bug = 回滚时静默丢数据。
用真实文件系统（不 mock glob/copy），覆盖往返、files_hint 解析、删除幂等、
损坏容错、便利函数。
"""
import json
from pathlib import Path

from agent_go.checkpoint import (
    SnapshotManager, take_snapshot, list_checkpoints, restore_checkpoint,
)


class TestSnapshotTakeRestore:
    def test_take_then_restore_roundtrip(self, tmp_path):
        """take 多文件（含嵌套）→ list 有记录 → 改写 → restore 还原内容 + 计数。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        (work / "src").mkdir(parents=True)
        (work / "src" / "a.py").write_text("a=1", encoding="utf-8")
        (work / "src" / "nested").mkdir()
        (work / "src" / "nested" / "b.py").write_text("b=2", encoding="utf-8")
        mgr = SnapshotManager(task_dir)

        snap = mgr.take("sub-1", work)
        assert snap == "sub-1"

        snaps = mgr.list_snapshots()
        assert len(snaps) == 1
        assert snaps[0]["subtask_id"] == "sub-1"
        assert snaps[0]["file_count"] == 2  # a.py + nested/b.py（默认 hint 含 *.py）

        # 改写 + 删一个，验证 restore 能还原
        (work / "src" / "a.py").write_text("CHANGED", encoding="utf-8")
        n = mgr.restore("sub-1", work)
        assert n == 2
        assert (work / "src" / "a.py").read_text(encoding="utf-8") == "a=1"
        assert (work / "src" / "nested" / "b.py").read_text(encoding="utf-8") == "b=2"

    def test_take_writes_snapshot_json_with_hashes(self, tmp_path):
        """snapshot.json 含 files[].sha256_prefix + size（完整性元数据）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "x.py").write_text("print(1)", encoding="utf-8")
        SnapshotManager(task_dir).take("s1", work)
        snap = json.loads((task_dir / "checkpoints" / "s1" / "snapshot.json").read_text(encoding="utf-8"))
        assert snap["files"][0]["rel_path"] == "x.py"
        assert "sha256_prefix" in snap["files"][0]
        assert snap["files"][0]["size"] == 8
        assert snap["errors"] == 0


class TestFilesHintResolution:
    def test_default_hint_matches_code_files_not_arbitrary(self, tmp_path):
        """空 hint → 默认代码文件 glob（.py 命中，.txt 不命中）。"""
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("x", encoding="utf-8")
        (work / "b.txt").write_text("y", encoding="utf-8")
        rels = SnapshotManager._resolve_files(work, "")
        assert "a.py" in rels
        assert "b.txt" not in rels

    def test_explicit_hint_copies_only_listed(self, tmp_path):
        """files_hint 指定 → 只快照匹配文件（逗号/空格分隔）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "keep.py").write_text("1", encoding="utf-8")
        (work / "skip.py").write_text("2", encoding="utf-8")
        (work / "data.json").write_text("{}", encoding="utf-8")
        mgr = SnapshotManager(task_dir)
        mgr.take("s", work, files_hint="keep.py, data.json")
        snap = json.loads((task_dir / "checkpoints" / "s" / "snapshot.json").read_text(encoding="utf-8"))
        rels = {f["rel_path"] for f in snap["files"]}
        assert rels == {"keep.py", "data.json"}

    def test_explicit_hint_glob_pattern(self, tmp_path):
        """hint 支持 glob（*.py）。"""
        work = tmp_path / "work"
        (work / "pkg").mkdir(parents=True)
        (work / "pkg" / "m.py").write_text("1", encoding="utf-8")
        (work / "pkg" / "n.py").write_text("2", encoding="utf-8")
        (work / "pkg" / "x.txt").write_text("3", encoding="utf-8")
        rels = SnapshotManager._resolve_files(work, "pkg/*.py")
        assert rels == ["pkg/m.py", "pkg/n.py"]


class TestTakeEdgeCases:
    def test_no_matching_files_returns_none_and_cleans_dir(self, tmp_path):
        """无匹配文件 → 返回 None，且不留空 snap_dir（list 看不到）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "readme.txt").write_text("x", encoding="utf-8")  # 默认 hint 不含 .txt
        mgr = SnapshotManager(task_dir)
        snap = mgr.take("s", work)  # 默认 hint 找不到 .py 等
        assert snap is None
        assert mgr.list_snapshots() == []

    def test_take_overwrites_previous_snapshot(self, tmp_path):
        """同 sub_id 再次 take → 覆盖旧 snapshot（list 仍只一条）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("v1", encoding="utf-8")
        mgr = SnapshotManager(task_dir)
        mgr.take("s", work)
        (work / "a.py").write_text("v2", encoding="utf-8")
        mgr.take("s", work)
        snaps = mgr.list_snapshots()
        assert len(snaps) == 1


class TestDelete:
    def test_delete_existing_returns_true(self, tmp_path):
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("1", encoding="utf-8")
        mgr = SnapshotManager(task_dir)
        mgr.take("s", work)
        assert mgr.delete("s") is True
        assert mgr.list_snapshots() == []

    def test_delete_missing_returns_false(self, tmp_path):
        """删除不存在的 checkpoint → False（幂等，不抛）。"""
        assert SnapshotManager(tmp_path / "task").delete("ghost") is False


class TestRestoreEdgeCases:
    def test_restore_missing_snapshot_returns_zero(self, tmp_path):
        """restore 不存在的 sub → 0（不抛）。"""
        mgr = SnapshotManager(tmp_path / "task")
        assert mgr.restore("ghost", tmp_path / "target") == 0

    def test_restore_skips_missing_cached_file(self, tmp_path):
        """快照记录了文件但缓存文件被删 → restore 跳过该文件（不抛）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("1", encoding="utf-8")
        mgr = SnapshotManager(task_dir)
        mgr.take("s", work)
        # 删掉缓存文件
        (task_dir / "checkpoints" / "s" / "files" / "a.py").unlink()
        target = tmp_path / "target"
        target.mkdir()
        assert mgr.restore("s", target) == 0  # 缓存缺失 → 0 还原


class TestCorruptSnapshot:
    def test_list_skips_corrupt_snapshot_json(self, tmp_path):
        """损坏的 snapshot.json → list 跳过（不崩）。"""
        task_dir = tmp_path / "task"
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("1", encoding="utf-8")
        mgr = SnapshotManager(task_dir)
        mgr.take("good", work)
        # 写一个损坏的 snapshot
        bad_dir = task_dir / "checkpoints" / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "snapshot.json").write_text("{not json", encoding="utf-8")
        snaps = mgr.list_snapshots()
        assert len(snaps) == 1
        assert snaps[0]["subtask_id"] == "good"  # 损坏的被跳过

    def test_restore_corrupt_snapshot_returns_zero(self, tmp_path):
        task_dir = tmp_path / "task"
        bad_dir = task_dir / "checkpoints" / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "snapshot.json").write_text("{not json", encoding="utf-8")
        assert SnapshotManager(task_dir).restore("bad", tmp_path / "t") == 0


class TestListEmpty:
    def test_list_no_checkpoints_dir(self, tmp_path):
        """无 checkpoints 目录 → []。"""
        assert SnapshotManager(tmp_path / "task").list_snapshots() == []


class TestConvenienceFunctions:
    """take_snapshot / list_checkpoints / restore_checkpoint（经 AGENT_GO_DIR）。"""

    def test_take_list_restore_via_convenience(self, tmp_path, monkeypatch):
        import agent_go.checkpoint as ckpt
        monkeypatch.setattr(ckpt, "AGENT_GO_DIR", tmp_path)
        task_dir = tmp_path / "task-1"
        task_dir.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.py").write_text("orig", encoding="utf-8")

        assert take_snapshot(task_dir, "sub-1", work) == "sub-1"
        # list_checkpoints 按 task_id（拼 AGENT_GO_DIR）
        snaps = list_checkpoints("task-1")
        assert len(snaps) == 1 and snaps[0]["subtask_id"] == "sub-1"

        (work / "a.py").write_text("mut", encoding="utf-8")
        # restore_checkpoint 默认 target = task_dir/sub_id/work
        assert restore_checkpoint("task-1", "sub-1") == 1
        # 注意：restore 默认写 task_dir/sub-1/work，不是原 work；验证默认路径被还原
        assert (task_dir / "sub-1" / "work" / "a.py").read_text(encoding="utf-8") == "orig"

    def test_list_checkpoints_missing_task(self, tmp_path, monkeypatch):
        import agent_go.checkpoint as ckpt
        monkeypatch.setattr(ckpt, "AGENT_GO_DIR", tmp_path)
        assert list_checkpoints("nope") == []

    def test_restore_checkpoint_missing_task(self, tmp_path, monkeypatch):
        import agent_go.checkpoint as ckpt
        monkeypatch.setattr(ckpt, "AGENT_GO_DIR", tmp_path)
        assert restore_checkpoint("nope", "sub-1") == 0
