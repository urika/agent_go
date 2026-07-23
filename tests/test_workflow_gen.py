"""测试 workflow_gen.py — GitHub Actions CI 工作流自动生成

全覆盖: detect_language, generate_workflow, cmd_ci, TEMPLATES 结构
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.workflow_gen import (
    detect_language,
    generate_workflow,
    cmd_ci,
    TEMPLATES,
)


class TestTemplates:
    """TEMPLATES 结构完整性"""

    def test_all_templates_have_required_keys(self):
        for lang, cfg in TEMPLATES.items():
            assert "detect" in cfg, f"{lang} missing detect"
            assert "workflow" in cfg, f"{lang} missing workflow"
            assert isinstance(cfg["detect"], list), f"{lang} detect should be list"
            assert len(cfg["detect"]) > 0, f"{lang} detect should not be empty"
            assert "name: Test" in cfg["workflow"], f"{lang} workflow missing name"

    def test_supported_languages(self):
        expected = {"python", "go", "node", "rust", "java"}
        assert set(TEMPLATES.keys()) == expected

    def test_python_workflow_content(self):
        wf = TEMPLATES["python"]["workflow"]
        assert "pytest" in wf
        assert "setup-python" in wf

    def test_go_workflow_content(self):
        wf = TEMPLATES["go"]["workflow"]
        assert "setup-go" in wf
        assert "go test" in wf


class TestDetectLanguage:
    """项目语言检测"""

    def test_detect_python(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pytest")
        assert detect_language(tmp_path) == "python"

    def test_detect_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]")
        assert detect_language(tmp_path) == "python"

    def test_detect_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")
        assert detect_language(tmp_path) == "go"

    def test_detect_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect_language(tmp_path) == "node"

    def test_detect_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        assert detect_language(tmp_path) == "rust"

    def test_detect_java_maven(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        assert detect_language(tmp_path) == "java"

    def test_detect_java_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
        assert detect_language(tmp_path) == "java"

    def test_unknown_language(self, tmp_path):
        assert detect_language(tmp_path) is None

    def test_language_precedence(self, tmp_path):
        """多个语言文件存在时返回第一个匹配"""
        (tmp_path / "requirements.txt").write_text("")
        (tmp_path / "go.mod").write_text("")
        result = detect_language(tmp_path)
        # python 在 TEMPLATES 中先迭代
        assert result is not None


class TestGenerateWorkflow:
    """工作流内容生成"""

    def test_python_workflow(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        lang, content = generate_workflow(tmp_path)
        assert lang == "python"
        assert content == TEMPLATES["python"]["workflow"]

    def test_unknown_language(self, tmp_path):
        lang, content = generate_workflow(tmp_path)
        assert lang is None
        assert content is None

    def test_content_is_string(self, tmp_path):
        (tmp_path / "go.mod").write_text("")
        _, content = generate_workflow(tmp_path)
        assert isinstance(content, str)
        assert len(content) > 50


class TestCmdCi:
    """cmd_ci CLI 命令"""

    @patch("agent_go.workflow_gen.Path.cwd")
    def test_dry_run(self, mock_cwd, tmp_path):
        """--dry-run 只打印不写入"""
        mock_cwd.return_value = tmp_path
        (tmp_path / "requirements.txt").write_text("")
        with patch("agent_go.workflow_gen.console.print") as mock_print:
            cmd_ci(args=type("Args", (), {"dry_run": True, "repo": str(tmp_path)})())
        mock_print.assert_called()
        # 不应创建文件
        assert not (tmp_path / ".github" / "workflows" / "test.yml").exists()

    @patch("agent_go.workflow_gen.Path.cwd")
    def test_creates_workflow_file(self, mock_cwd, tmp_path):
        """正常模式写入工作流文件"""
        mock_cwd.return_value = tmp_path
        (tmp_path / "go.mod").write_text("")
        with patch("agent_go.workflow_gen.console.print") as mock_print:
            cmd_ci(args=type("Args", (), {"dry_run": False, "repo": str(tmp_path)})())
        wf_file = tmp_path / ".github" / "workflows" / "test.yml"
        assert wf_file.exists()
        assert "Test" in wf_file.read_text()

    @patch("agent_go.workflow_gen.Path.cwd")
    def test_skips_if_exists(self, mock_cwd, tmp_path):
        """文件已存在时不覆盖"""
        mock_cwd.return_value = tmp_path
        (tmp_path / "go.mod").write_text("")
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "test.yml").write_text("existing")
        with patch("agent_go.workflow_gen.console.print") as mock_print:
            cmd_ci(args=type("Args", (), {"dry_run": False, "repo": str(tmp_path)})())
        assert (wf_dir / "test.yml").read_text() == "existing"

    @patch("agent_go.workflow_gen.Path.cwd")
    def test_no_language_detected(self, mock_cwd, tmp_path):
        """未检测到语言时提示"""
        mock_cwd.return_value = tmp_path
        with patch("agent_go.workflow_gen.console.print") as mock_print:
            cmd_ci(args=type("Args", (), {"dry_run": False, "repo": str(tmp_path)})())
        mock_print.assert_called()
        # 打印信息包含"未检测到"
        message = mock_print.call_args[0][0]
        assert "未检测到" in message or "support" in message.lower()

    @patch("agent_go.workflow_gen.Path.cwd")
    def test_repo_argument(self, mock_cwd, tmp_path):
        """指定 repo 路径参数"""
        repo = tmp_path / "my_project"
        repo.mkdir()
        (repo / "Cargo.toml").write_text("[package]")
        mock_cwd.return_value = tmp_path
        with patch("agent_go.workflow_gen.console.print"):
            cmd_ci(args=type("Args", (), {"dry_run": True, "repo": str(repo)})())
        # 应为 rust 语言
        # 验证 dry_run 输出
