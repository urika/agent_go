"""events.py（ADR-010 阶段 2 TaskEvent 骨架）单元测试。"""

import json
import threading

from agent_go.events import emit_event, read_events, EVENTS_FILENAME


class TestEmitEvent:
    def test_emit_and_read_roundtrip(self, tmp_path):
        emit_event(str(tmp_path), "subtask_start", sub_id="sub-1", title="t")
        emit_event(str(tmp_path), "subtask_end", sub_id="sub-1", status="completed")
        events = read_events(str(tmp_path))
        assert len(events) == 2
        assert [e["type"] for e in events] == ["subtask_start", "subtask_end"]
        assert [e["seq"] for e in events] == [1, 2]
        assert all(set(e.keys()) == {"seq", "ts", "type", "data"} for e in events)
        assert events[0]["data"]["sub_id"] == "sub-1"
        assert isinstance(events[0]["ts"], int)

    def test_seq_continues_across_writes(self, tmp_path):
        """seq 从既有文件尾部续接（append-only 语义）。"""
        for i in range(3):
            emit_event(str(tmp_path), "verify", sub_id="s", ok=True, n=i)
        events = read_events(str(tmp_path))
        assert [e["seq"] for e in events] == [1, 2, 3]

    def test_failopen_empty_task_dir(self):
        """task_dir 为空/None 时静默不抛。"""
        emit_event(None, "plan", steps=1)
        emit_event("", "plan", steps=1)

    def test_failopen_unwritable_path(self, tmp_path):
        """不可写路径（task_dir 是个文件）只降级，不抛异常。"""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        emit_event(str(blocker / "sub"), "plan", steps=1)  # 不应抛出

    def test_concurrent_emit_no_interleave(self, tmp_path):
        """pipeline 并发场景：多线程写同一文件，行不交错、seq 唯一。"""
        def worker(n):
            for i in range(20):
                emit_event(str(tmp_path), "model_attempt", sub_id=f"sub-{n}", attempt=i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = read_events(str(tmp_path))
        assert len(events) == 80
        assert len({e["seq"] for e in events}) == 80  # seq 无重复
        # 每行都是完整 JSON（read_events 全量解析成功即证明无交错）
        assert all(e["type"] == "model_attempt" for e in events)

    def test_read_events_bad_lines_skipped(self, tmp_path):
        path = tmp_path / EVENTS_FILENAME
        path.write_text('{"seq":1,"ts":1,"type":"plan","data":{}}\ngarbage-line\n', encoding="utf-8")
        events = read_events(str(tmp_path))
        assert len(events) == 1
        # 坏行后新事件 seq 续接正常
        emit_event(str(tmp_path), "decompose", count=2)
        events = read_events(str(tmp_path))
        assert events[-1]["seq"] == 2

    def test_read_events_missing_file(self, tmp_path):
        assert read_events(str(tmp_path)) == []
