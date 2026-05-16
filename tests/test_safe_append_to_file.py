"""测试 _safe_append_to_file — 线程安全文件追加"""

import time
import threading
from pathlib import Path
from agent_go import _safe_append_to_file


class TestSafeAppendToFile:
    """_safe_append_to_file 基础功能测试"""

    def test_append_to_new_file(self, temp_dir, logger):
        """追加到一个不存在的文件"""
        fp = temp_dir / "test.txt"
        _safe_append_to_file(fp, "hello", logger)
        assert fp.read_text(encoding="utf-8") == "hello"

    def test_append_to_existing_file(self, temp_dir, logger):
        fp = temp_dir / "test.txt"
        fp.write_text("line1\n", encoding="utf-8")
        _safe_append_to_file(fp, "line2\n", logger)
        assert fp.read_text(encoding="utf-8") == "line1\nline2\n"

    def test_multiple_appends(self, temp_dir, logger):
        fp = temp_dir / "test.txt"
        for i in range(5):
            _safe_append_to_file(fp, f"line{i}\n", logger)
        content = fp.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 5

    def test_lock_file_cleaned_up(self, temp_dir, logger):
        """锁文件在操作后应被清理"""
        fp = temp_dir / "test.txt"
        _safe_append_to_file(fp, "data", logger)
        lock = fp.with_suffix(fp.suffix + ".lock")
        assert not lock.exists(), "锁文件应被清理"

    def test_concurrent_safety(self, temp_dir, logger):
        """并发写入时数据不丢失"""
        fp = temp_dir / "concurrent.txt"
        n_threads = 10
        results = []

        def worker(idx):
            _safe_append_to_file(fp, f"thread-{idx}\n", logger)
            results.append(idx)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = fp.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == n_threads, f"期望 {n_threads} 行，实际 {len(lines)} 行"
        for i in range(n_threads):
            assert f"thread-{i}" in content, f"缺少 thread-{i}"

    def test_append_unicode(self, temp_dir, logger):
        """中文字符正确追加"""
        fp = temp_dir / "unicode.txt"
        _safe_append_to_file(fp, "你好世界\n", logger)
        _safe_append_to_file(fp, "Hello World\n", logger)
        content = fp.read_text(encoding="utf-8")
        assert "你好世界" in content
        assert "Hello World" in content
