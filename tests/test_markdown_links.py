from pathlib import Path

from tools.check_markdown_links import find_broken_links


def test_docs_have_no_broken_local_markdown_links():
    root = Path(__file__).resolve().parents[1] / "docs"
    assert find_broken_links(root) == []


def test_checker_reports_missing_relative_link(tmp_path):
    (tmp_path / "a.md").write_text("[missing](missing.md)\n", encoding="utf-8")
    assert find_broken_links(tmp_path) == [("a.md", "missing.md")]
