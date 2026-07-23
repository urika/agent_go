"""测试 analyze_project / get_git_info / get_resource_map — 项目分析"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.git_utils import analyze_project, get_git_info, get_resource_map


class TestAnalyzeProject:
    """analyze_project 功能测试"""

    def test_git_repo(self, temp_dir):
        """有 .git 目录时使用 git ls-files"""
        (temp_dir / ".git").mkdir()
        # 创建一些文件
        (temp_dir / "main.py").write_text("")
        (temp_dir / "utils.py").write_text("")

        # mock git ls-files 输出
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "main.py\nutils.py\n"
            mock_result.returncode = 0
            mock_run.return_value = mock_result

            result = analyze_project(temp_dir)
            assert "main.py" in result
            assert "utils.py" in result
            mock_run.assert_called_once()

    def test_non_git_repo(self, temp_dir):
        """无 .git 目录时使用 find"""
        (temp_dir / "main.py").write_text("")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "./main.py\n"
            mock_result.returncode = 0
            mock_run.return_value = mock_result

            result = analyze_project(temp_dir)
            assert "main.py" in result

    def test_subprocess_failure(self, temp_dir):
        """subprocess 失败时返回空字符串"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = analyze_project(temp_dir)
            assert result == ""


class TestGetGitInfo:
    """get_git_info 功能测试"""

    def test_success(self, temp_dir):
        """正常获取 git 信息"""
        with patch("subprocess.run") as mock_run:
            def side_effect(*args, **kwargs):
                mock = MagicMock()
                cmd = args[0]
                if "remote" in cmd:
                    mock.returncode = 0
                    mock.stdout = "https://github.com/user/repo.git\n"
                elif "branch" in cmd:
                    mock.returncode = 0
                    mock.stdout = "main\n"
                elif "rev-parse" in cmd:
                    mock.returncode = 0
                    mock.stdout = "abc1234\n"
                else:
                    mock.returncode = 1
                return mock
            mock_run.side_effect = side_effect

            info = get_git_info(temp_dir)
            assert info["remote"] == "https://github.com/user/repo.git"
            assert info["branch"] == "main"
            assert info["commit"] == "abc1234"

    def test_no_git(self, temp_dir):
        """无 git 仓库时返回空信息"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            info = get_git_info(temp_dir)
            assert info == {"remote": "", "branch": "", "commit": ""}

    def test_partial_failure(self, temp_dir):
        """部分 git 命令失败时已有信息保留"""
        with patch("subprocess.run") as mock_run:
            def side_effect(*args, **kwargs):
                mock = MagicMock()
                cmd = args[0]
                if "remote" in cmd:
                    mock.returncode = 0
                    mock.stdout = "https://github.com/user/repo.git\n"
                else:
                    mock.returncode = 1  # branch 和 commit 失败
                return mock
            mock_run.side_effect = side_effect

            info = get_git_info(temp_dir)
            assert info["remote"] == "https://github.com/user/repo.git"
            assert info["branch"] == ""
            assert info["commit"] == ""


class TestGetResourceMap:
    """get_resource_map 功能测试"""

    def test_with_directories_and_files(self, temp_dir):
        """扫描到目录和关键文件"""
        (temp_dir / "src").mkdir()
        (temp_dir / "tests").mkdir()
        (temp_dir / "README.md").write_text("# Project")
        (temp_dir / "package.json").write_text("{}")

        git_info = {"remote": "https://github.com/user/repo.git",
                    "branch": "main", "commit": "abc123"}
        resources = get_resource_map(temp_dir, git_info)

        assert "src" in resources["directories"]
        assert "tests" in resources["directories"]
        assert "README.md" in resources["key_files"]
        assert "package.json" in resources["key_files"]
        assert resources["git_remote"] == "https://github.com/user/repo.git"

    def test_no_matching_resources(self, temp_dir):
        """无匹配目录/文件"""
        (temp_dir / "other").mkdir()
        git_info = {"remote": "", "branch": "", "commit": ""}

        resources = get_resource_map(temp_dir, git_info)
        assert resources["directories"] == []
        assert resources["key_files"] == []
        assert resources["git_remote"] == ""


class TestAnalyzeProjectFindFallback:
    """find 回退路径的文件名处理（回归 docs/ISSUES.md ISSUE-14）"""

    def test_dotfile_name_preserved(self, temp_dir):
        """./.gitignore 不应被 lstrip 误改为 gitignore"""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "./.gitignore\n./main.py\n"
            mock_result.returncode = 0
            mock_run.return_value = mock_result

            result = analyze_project(temp_dir)

        assert ".gitignore" in result
        assert "main.py" in result
