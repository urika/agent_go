"""看板数据层单测（agent_go/kanban.py）。

AGENT_GO_DIR monkeypatch 到 tmp_path；覆盖 create/update/move/archive/delete/link
正常路径与校验异常、history 追加、mtime 缓存与原子写。
"""
import json
import re
from pathlib import Path

import pytest

import agent_go.kanban as kb


@pytest.fixture
def kb_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    monkeypatch.setattr(kb, "AGENT_GO_DIR", adir)
    return adir


class TestCreate:
    def test_create_discussion_no_repo(self, kb_env):
        card = kb.create_card("讨论：新看板", "discussion")
        assert card["id"].startswith("card-")
        assert re.match(r"^card-[a-z0-9]{12}$", card["id"])
        assert card["stage"] == "brainstorm"
        assert card["type"] == "discussion"
        assert card["repo"] == ""
        assert card["archived"] is False
        assert card["task_ids"] == []
        assert card["created"] and card["updated"]
        # history 记 create
        assert len(card["history"]) == 1
        assert card["history"][0]["action"] == "create"
        assert card["history"][0]["ts"]

    def test_create_implementation_requires_repo(self, kb_env):
        with pytest.raises(kb.KanbanError, match="repo"):
            kb.create_card("实施任务", "implementation")

    def test_create_periodic_requires_repo(self, kb_env):
        with pytest.raises(kb.KanbanError, match="repo"):
            kb.create_card("周期任务", "periodic", cron="0 9 * * *")
        # 带 repo 可建
        card = kb.create_card("周期任务", "periodic", repo="/tmp/repo", cron="0 9 * * *")
        assert card["cron"] == "0 9 * * *"

    def test_create_invalid_type(self, kb_env):
        with pytest.raises(kb.KanbanError, match="type"):
            kb.create_card("x", "unknown-type")

    def test_create_invalid_stage(self, kb_env):
        with pytest.raises(kb.KanbanError, match="stage"):
            kb.create_card("x", "discussion", stage="no-such-stage")

    def test_create_empty_title(self, kb_env):
        with pytest.raises(kb.KanbanError, match="title"):
            kb.create_card("  ", "discussion")

    def test_create_custom_stage(self, kb_env):
        card = kb.create_card("x", "discussion", stage="design")
        assert card["stage"] == "design"


class TestUpdate:
    def test_update_fields(self, kb_env):
        card = kb.create_card("旧标题", "discussion", description="旧描述")
        updated = kb.update_card(card["id"], title="新标题", description="新描述",
                                 spec_path="docs/tasks/t.md")
        assert updated["title"] == "新标题"
        assert updated["description"] == "新描述"
        assert updated["spec_path"] == "docs/tasks/t.md"
        assert updated["history"][-1]["action"] == "update"
        assert "title" in updated["history"][-1]["note"]

    def test_update_clear_repo_rejected_for_implementation(self, kb_env):
        card = kb.create_card("实施", "implementation", repo="/tmp/repo")
        with pytest.raises(kb.KanbanError, match="repo"):
            kb.update_card(card["id"], repo="")

    def test_update_whitelist_enforced(self, kb_env):
        card = kb.create_card("x", "discussion")
        with pytest.raises(kb.KanbanError, match="不可更新字段"):
            kb.update_card(card["id"], stage="design")  # stage 走 move_card

    def test_update_not_found(self, kb_env):
        with pytest.raises(kb.KanbanError, match="不存在"):
            kb.update_card("card-ghost000000", title="x")


class TestMove:
    def test_move_records_history(self, kb_env):
        card = kb.create_card("x", "discussion")
        moved = kb.move_card(card["id"], "design", note="讨论完毕")
        assert moved["stage"] == "design"
        h = moved["history"][-1]
        assert h["action"] == "move"
        assert h["from"] == "brainstorm"
        assert h["to"] == "design"
        assert h["note"] == "讨论完毕"

    def test_move_invalid_stage(self, kb_env):
        card = kb.create_card("x", "discussion")
        with pytest.raises(kb.KanbanError, match="stage"):
            kb.move_card(card["id"], "done")

    def test_move_invalid_card_id(self, kb_env):
        with pytest.raises(kb.KanbanError):
            kb.move_card("../../etc/passwd", "design")

    def test_move_not_found(self, kb_env):
        with pytest.raises(kb.KanbanError, match="不存在"):
            kb.move_card("card-ghost000000", "design")


class TestArchiveDeleteLink:
    def test_archive_and_unarchive(self, kb_env):
        card = kb.create_card("x", "discussion")
        archived = kb.archive_card(card["id"])
        assert archived["archived"] is True
        assert archived["history"][-1]["action"] == "archive"
        restored = kb.archive_card(card["id"], archived=False)
        assert restored["archived"] is False
        assert restored["history"][-1]["action"] == "unarchive"

    def test_link_task_dedupe(self, kb_env):
        card = kb.create_card("x", "discussion")
        tid = "task-20260815-100000-111-aaaa"
        kb.link_task(card["id"], tid)
        linked = kb.link_task(card["id"], tid)  # 重复链接去重
        assert linked["task_ids"] == [tid]
        assert linked["history"][-1]["action"] == "link"
        assert linked["history"][-1]["note"] == tid

    def test_delete_fresh_card(self, kb_env):
        card = kb.create_card("x", "discussion")
        kb.delete_card(card["id"])
        assert kb.get_card(card["id"]) is None

    def test_delete_dispatched_card_rejected(self, kb_env):
        card = kb.create_card("x", "implementation", repo="/tmp/repo")
        kb.link_task(card["id"], "task-20260815-100000-111-aaaa")
        with pytest.raises(kb.KanbanError, match="不可删除"):
            kb.delete_card(card["id"])

    def test_delete_not_found(self, kb_env):
        with pytest.raises(kb.KanbanError, match="不存在"):
            kb.delete_card("card-ghost000000")


class TestDispatchCard:
    def test_dispatch_atomic_link_and_move(self, kb_env):
        card = kb.create_card("实施", "implementation", repo="/tmp/repo", stage="design")
        tid = "task-20260816-100000-111-bbbb"
        out = kb.dispatch_card(card["id"], tid)
        assert out["stage"] == "implementation"
        assert out["task_ids"] == [tid]
        acts = [h["action"] for h in out["history"]]
        assert acts[-2:] == ["link", "move"]
        mv = out["history"][-1]
        assert mv["from"] == "design" and mv["to"] == "implementation"
        assert mv.get("note", "") == ""

    def test_dispatch_with_note_and_dedupe(self, kb_env):
        card = kb.create_card("实施", "implementation", repo="/tmp/repo")
        tid = "task-20260816-100000-111-bbbb"
        kb.dispatch_card(card["id"], tid, note="派发任务 x")
        out = kb.dispatch_card(card["id"], tid)  # 重复派发同一 task_id → task_ids 去重
        assert out["task_ids"] == [tid]
        assert out["history"][-1].get("note", "") == ""

    def test_dispatch_invalid_args(self, kb_env):
        card = kb.create_card("实施", "implementation", repo="/tmp/repo")
        with pytest.raises(kb.KanbanError, match="task_id"):
            kb.dispatch_card(card["id"], "")
        with pytest.raises(kb.KanbanError, match="stage"):
            kb.dispatch_card(card["id"], "task-x", to_stage="done")
        with pytest.raises(kb.KanbanError, match="不存在"):
            kb.dispatch_card("card-ghost000000", "task-x")


class TestLoadSave:
    def test_empty_board_when_file_missing(self, kb_env):
        board = kb.load_board()
        assert board == {"version": 1, "cards": []}

    def test_cache_and_force_reload(self, kb_env):
        kb.create_card("x", "discussion")
        b1 = kb.load_board()
        b2 = kb.load_board()
        assert b1 is b2  # mtime 缓存命中返回同一对象
        b3 = kb.load_board(force=True)
        assert b3 is not b1
        assert b3["cards"][0]["title"] == "x"

    def test_external_write_invalidates_cache(self, kb_env):
        kb.create_card("x", "discussion")
        kb.load_board()
        # 外部直接改文件（mtime 变化 → 缓存失效）
        path = kb.board_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["cards"][0]["title"] = "外部修改"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        import os
        os.utime(path, None)  # 确保 mtime 前进
        board = kb.load_board()
        assert board["cards"][0]["title"] == "外部修改"

    def test_atomic_write_produces_valid_json(self, kb_env):
        kb.create_card("x", "discussion")
        path = kb.board_path()
        # 原子写后文件可被 json.load；无 tmp 残留
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert len(data["cards"]) == 1
        assert not path.with_suffix(".json.tmp").exists()

    def test_corrupt_file_falls_back_empty(self, kb_env):
        kb.board_path().write_text("{not json", encoding="utf-8")
        board = kb.load_board(force=True)
        assert board == {"version": 1, "cards": []}

    def test_get_card_invalid_id_raises(self, kb_env):
        with pytest.raises(kb.KanbanError):
            kb.get_card("bad id with spaces")
