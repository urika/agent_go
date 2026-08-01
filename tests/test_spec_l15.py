"""测试 spec.py 的 L1.5 AST 冲突检测（S11 L1.5，学术驱动）。

论文 arXiv:2603.24284（The Specification Gap）证明多 Agent 协调失败的主因是
「同一文件/同一符号被独立修改」。本测试验证 detect_step_conflicts 能纯静态
（零 LLM）检测这类冲突。

测试隔离：用 tmp_path 构造临时 Python 文件，不改 agent_go 自身。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.spec import detect_step_conflicts


def _write_py(repo: Path, name: str, content: str) -> None:
    """在临时 repo 写一个 Python 文件。"""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content, encoding="utf-8")


# ═══ 符号级冲突 ══════════════════════════════════════════════════════

class TestSymbolConflict:
    """两个 step 引用同一顶层符号 → 高置信冲突。"""

    def test_same_symbol_in_two_steps(self, tmp_path):
        _write_py(tmp_path, "user.py",
                  "def get_user():\n    return {}\n\nclass User:\n    pass\n")
        steps = [
            {"id": 1, "title": "改 get_user", "description": "modify get_user", "files": ["user.py"]},
            {"id": 2, "title": "改 get_user 同符号", "description": "change get_user", "files": ["user.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "symbol"
        assert "get_user" in conflicts[0].symbols
        assert conflicts[0].file == "user.py"
        assert 1 in conflicts[0].steps and 2 in conflicts[0].steps

    def test_different_symbols_same_file(self, tmp_path):
        """两个 step 改同一文件但不同符号 → 只报文件级（非符号级）。"""
        _write_py(tmp_path, "user.py",
                  "def get_user():\n    return {}\n\nclass User:\n    pass\n")
        steps = [
            {"id": 1, "title": "改 get_user", "description": "modify get_user", "files": ["user.py"]},
            {"id": 2, "title": "改 User 类", "description": "update User class", "files": ["user.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        # 1 个文件冲突，但符号级为空（get_user 和 User 不同符号）
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"
        assert conflicts[0].symbols == []

    def test_multi_symbol_conflict(self, tmp_path):
        """多个同名符号冲突同时报告。"""
        _write_py(tmp_path, "mod.py",
                  "def helper():\n    pass\n\nclass Helper:\n    pass\n")
        steps = [
            {"id": 1, "description": "change helper", "files": ["mod.py"]},
            {"id": 2, "description": "modify Helper class", "files": ["mod.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert conflicts
        # step1 引用 helper（小写函数），step2 引用 Helper（类）——不同符号 → 文件级
        assert conflicts[0].severity == "file"

    def test_word_boundary_no_false_positive(self, tmp_path):
        """词边界：verify 不应匹配 verified。"""
        _write_py(tmp_path, "auth.py", "def verify():\n    pass\n")
        steps = [
            {"id": 1, "description": "add email_verified field", "files": ["auth.py"]},
            {"id": 2, "description": "add phone_verified field", "files": ["auth.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        # 两个 step 都没有提到 verify（只提到 verified），不应符号级冲突
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"  # 仅文件级
        assert conflicts[0].symbols == []


# ═══ 文件级冲突 ══════════════════════════════════════════════════════

class TestFileConflict:
    """多个 step 改同一文件但无同名符号 → 文件级冲突。"""

    def test_two_steps_same_file_no_symbol(self, tmp_path):
        _write_py(tmp_path, "main.py", "x = 1\n")
        steps = [
            {"id": 1, "description": "add feature a", "files": ["main.py"]},
            {"id": 2, "description": "add feature b", "files": ["main.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"
        assert conflicts[0].file == "main.py"

    def test_same_file_thrice(self, tmp_path):
        _write_py(tmp_path, "app.py", "def run():\n    pass\n")
        steps = [
            {"id": 1, "files": ["app.py"]},
            {"id": 2, "files": ["app.py"]},
            {"id": 3, "files": ["app.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert sorted(conflicts[0].steps) == [1, 2, 3]


# ═══ 无冲突 ══════════════════════════════════════════════════════════

class TestNoConflict:
    def test_no_shared_files(self, tmp_path):
        _write_py(tmp_path, "a.py", "def a():\n    pass\n")
        _write_py(tmp_path, "b.py", "def b():\n    pass\n")
        steps = [
            {"id": 1, "description": "change a", "files": ["a.py"]},
            {"id": 2, "description": "change b", "files": ["b.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert conflicts == []

    def test_empty_steps(self, tmp_path):
        assert detect_step_conflicts([], tmp_path) == []

    def test_single_step(self, tmp_path):
        _write_py(tmp_path, "a.py", "def a():\n    pass\n")
        steps = [{"id": 1, "description": "change a", "files": ["a.py"]}]
        assert detect_step_conflicts(steps, tmp_path) == []

    def test_no_files_key(self, tmp_path):
        steps = [{"id": 1, "description": "no files"}]
        assert detect_step_conflicts(steps, tmp_path) == []


# ═══ 边界情况 ════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_non_python_file_skipped(self, tmp_path):
        """非 .py 文件不做符号提取，只报文件级。"""
        _write_py(tmp_path, "config.json", '{"a": 1}')
        steps = [
            {"id": 1, "description": "update config", "files": ["config.json"]},
            {"id": 2, "description": "update config too", "files": ["config.json"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"
        assert conflicts[0].symbols == []

    def test_missing_file_no_crash(self, tmp_path):
        """文件不存在（如新增文件）→ 不崩溃，报文件级。"""
        steps = [
            {"id": 1, "description": "create new module", "files": ["new_module.py"]},
            {"id": 2, "description": "also create new module", "files": ["new_module.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"

    def test_files_as_string(self, tmp_path):
        """files 字段可能是字符串而非列表。"""
        _write_py(tmp_path, "a.py", "def a():\n    pass\n")
        steps = [
            {"id": 1, "description": "change a", "files": "a.py"},
            {"id": 2, "description": "change a too", "files": "a.py"},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1

    def test_syntax_error_file(self, tmp_path):
        """语法错误的文件不崩溃，报文件级。"""
        _write_py(tmp_path, "broken.py", "def broken(:\n")
        steps = [
            {"id": 1, "description": "fix broken", "files": ["broken.py"]},
            {"id": 2, "description": "also fix broken", "files": ["broken.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        assert len(conflicts) == 1
        assert conflicts[0].severity == "file"
        assert conflicts[0].symbols == []

    def test_absolute_path_normalized(self, tmp_path):
        """绝对路径被规范化到尾部 3 段。"""
        repo = tmp_path / "repo"
        _write_py(repo / "src" / "mod", "user.py", "def get_user():\n    pass\n")
        abs_path = repo / "src" / "mod" / "user.py"
        steps = [
            {"id": 1, "description": "modify get_user", "files": [str(abs_path)]},
            {"id": 2, "description": "also get_user", "files": ["src/mod/user.py"]},
        ]
        conflicts = detect_step_conflicts(steps, tmp_path)
        # 两者应被归一到同一个 key（尾部 3 段）→ 检测到冲突
        assert len(conflicts) == 1
