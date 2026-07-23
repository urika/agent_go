"""测试 read_reference_docs — 参考文档读取"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.utils import read_reference_docs


class TestReadReferenceDocs:
    """read_reference_docs 功能测试"""

    def test_read_single_file(self, temp_dir, logger):
        """读取单个 .md 文件"""
        doc_file = temp_dir / "README.md"
        doc_file.write_text("# Hello World", encoding="utf-8")

        result = read_reference_docs(["README.md"], temp_dir, logger)
        assert "Hello World" in result
        assert "README.md" in result

    def test_read_directory_files(self, temp_dir, logger):
        """读取目录下所有 .md 文件"""
        docs_dir = temp_dir / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("# Guide", encoding="utf-8")
        (docs_dir / "api.md").write_text("# API", encoding="utf-8")

        result = read_reference_docs(["docs"], temp_dir, logger)
        assert "Guide" in result
        assert "API" in result
        assert "guide.md" in result
        assert "api.md" in result

    def test_nonexistent_file(self, temp_dir, logger):
        """不存在的文件被忽略"""
        result = read_reference_docs(["nonexistent.md"], temp_dir, logger)
        assert result == ""

    def test_path_traversal_prevented(self, temp_dir, logger):
        """路径穿越应被拒绝"""
        result = read_reference_docs(["../etc/passwd"], temp_dir, logger)
        assert result == ""

    def test_file_too_long_truncation(self, temp_dir, logger):
        """超过 15000 字符的文件被截断"""
        long_content = "x" * 20000
        doc_file = temp_dir / "long.md"
        doc_file.write_text(long_content, encoding="utf-8")

        result = read_reference_docs(["long.md"], temp_dir, logger)
        assert "截断" in result
        assert len(result) < 20000

    def test_multiple_doc_paths(self, temp_dir, logger):
        """多个文档路径"""
        (temp_dir / "a.md").write_text("file a", encoding="utf-8")
        (temp_dir / "b.md").write_text("file b", encoding="utf-8")

        result = read_reference_docs(["a.md", "b.md"], temp_dir, logger)
        assert "file a" in result
        assert "file b" in result


class TestReadReferenceDocsTraversal:
    """路径穿越防护（回归 docs/ISSUES.md ISSUE-8）"""

    def test_sibling_prefix_dir_rejected(self, temp_dir, logger):
        """兄弟前缀目录不得绕过校验：repo=/tmp/x/proj 时 ../proj-secret 应被拒绝"""
        repo = temp_dir / "proj"
        repo.mkdir()
        sibling = temp_dir / "proj-secret"
        sibling.mkdir()
        (sibling / "leak.md").write_text("secret-content", encoding="utf-8")

        result = read_reference_docs(["../proj-secret/leak.md"], repo, logger)
        assert result == ""
        assert "secret-content" not in result

    def test_repo_file_still_allowed(self, temp_dir, logger):
        """is_relative_to 校验下 repo 内文件仍正常读取"""
        repo = temp_dir / "proj"
        repo.mkdir()
        (repo / "ok.md").write_text("inside", encoding="utf-8")

        result = read_reference_docs(["ok.md"], repo, logger)
        assert "inside" in result
