"""本地后台队列批量执行（§7.13 后续）测试——mock 看板与子进程，零真实启动。"""
import json
import subprocess

import pytest

from agent_go import batch_runner
from agent_go.batch_runner import run_batch, select_cards


def _board(tmp_path, cards):
    board = {"schema": 1, "cards": cards}
    path = tmp_path / "kanban.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    return path


def _card(cid, *, stage="implementation", ctype="implementation", repo="", **kw):
    base = {"id": cid, "title": f"卡片 {cid}", "type": ctype, "stage": stage, "repo": repo,
            "automation": "auto"}
    base.update(kw)
    return base


@pytest.fixture()
def kanban_board(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _board(tmp_path, [
        _card("c-1", repo=str(repo)),
        _card("c-2", repo=str(repo), description="补充说明"),
        _card("c-3", repo=str(repo), automation="manual"),
        _card("c-4", repo=str(tmp_path / "missing")),          # repo 不存在 → 跳过
        _card("c-5", repo=str(repo), stage="design"),          # 不在 implementation 列
        _card("c-6", repo=str(repo), type="discussion"),       # 非可执行类型
        _card("c-7", repo=str(repo), archived=True),           # 已归档
    ])
    monkeypatch.setattr(batch_runner.kanban, "board_path", lambda: path)
    monkeypatch.setattr(batch_runner.kanban, "_board_cache", None, raising=False)
    return path, repo


def test_select_cards_filters(kanban_board):
    items = select_cards(stage="implementation", limit=10)
    assert [i.card_id for i in items] == ["c-1", "c-2", "c-3"]
    assert items[1].task.endswith("补充说明")            # 标题 + 描述
    assert [i.card_id for i in select_cards(limit=10, automation="manual")] == ["c-3"]
    assert [i.card_id for i in select_cards(limit=1)] == ["c-1"]
    assert select_cards(card_ids=["c-5"], limit=10)[0].card_id == "c-5"   # 显式指定不看列


def test_run_batch_dry_run_starts_nothing(kanban_board, monkeypatch):
    called = []
    monkeypatch.setattr(batch_runner.subprocess, "run", lambda *a, **k: called.append(a) or None)
    items = select_cards(limit=10)
    result = run_batch(items, dry_run=True)
    assert result.dry_run is True and result.ran == 0 and called == []
    assert [i["status"] for i in result.items] == ["planned"] * 3


def test_run_batch_serial_success_moves_card_to_operations(kanban_board, monkeypatch):
    moves, links, argv_seen = [], [], []

    def fake_run(argv, capture_output=True, text=True, timeout=None):
        argv_seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"task_id": "task-ok"}) + "\n", "")

    monkeypatch.setattr(batch_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(batch_runner, "_task_status", lambda tid: "DELIVERY_READY")
    monkeypatch.setattr(batch_runner.kanban, "dispatch_card",
                        lambda cid, tid, to_stage="", note="": moves.append((cid, tid, to_stage)))
    monkeypatch.setattr(batch_runner.kanban, "link_task", lambda cid, tid: links.append((cid, tid)))

    result = run_batch(select_cards(limit=2), dry_run=False)
    assert result.ran == 2 and result.succeeded == 2 and result.failed == 0
    assert moves == [("c-1", "task-ok", "operations"), ("c-2", "task-ok", "operations")]
    assert len(argv_seen) == 2                                  # 串行：逐张启动
    assert argv_seen[0][:4] == [argv_seen[0][0], "-m", "agent_go", "--json"]
    assert "--yes" in argv_seen[0] and argv_seen[0][argv_seen[0].index("--parallel") + 1] == "1"


def test_run_batch_failure_stops_unless_keep_going(kanban_board, monkeypatch):
    def fake_run(argv, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(argv, 1, json.dumps({"task_id": "task-bad"}) + "\n", "")

    links = []
    monkeypatch.setattr(batch_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(batch_runner, "_task_status", lambda tid: "VERIFICATION_FAILED")
    monkeypatch.setattr(batch_runner.kanban, "link_task", lambda cid, tid: links.append(cid))
    monkeypatch.setattr(batch_runner.kanban, "dispatch_card",
                        lambda *a, **k: pytest.fail("失败任务不应流转到 operations"))

    result = run_batch(select_cards(limit=3), dry_run=False)
    assert result.ran == 1 and result.failed == 1               # 默认失败即停
    assert links == ["c-1"]

    result = run_batch(select_cards(limit=3), dry_run=False, keep_going=True)
    assert result.ran == 3 and result.failed == 3
    assert links == ["c-1", "c-1", "c-2", "c-3"]   # 两轮各自的链接调用


def test_run_batch_timeout_and_spawn_failure(kanban_board, monkeypatch):
    def fake_timeout(argv, **k):
        raise subprocess.TimeoutExpired(argv, 10)
    monkeypatch.setattr(batch_runner.subprocess, "run", fake_timeout)
    result = run_batch(select_cards(limit=1), dry_run=False, timeout=10)
    assert result.items[0]["status"] == "timeout" and result.failed == 1

    def fake_oserror(argv, **k):
        raise OSError("spawn boom")
    monkeypatch.setattr(batch_runner.subprocess, "run", fake_oserror)
    result = run_batch(select_cards(limit=1), dry_run=False)
    assert result.items[0]["status"] == "spawn_failed" and result.failed == 1


def test_run_batch_flow_failure_does_not_rollback(kanban_board, monkeypatch):
    monkeypatch.setattr(batch_runner.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0,
                                                                      json.dumps({"task_id": "t"}), ""))
    monkeypatch.setattr(batch_runner, "_task_status", lambda tid: "DELIVERY_READY")

    def boom(*_a, **_k):
        raise RuntimeError("kanban 写失败")
    monkeypatch.setattr(batch_runner.kanban, "dispatch_card", boom)
    result = run_batch(select_cards(limit=1), dry_run=False)
    assert result.succeeded == 1 and result.failed == 0          # 流转失败不回滚任务
