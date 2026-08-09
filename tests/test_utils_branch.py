"""测试 slugify_branch_name — 标题转合法 git 分支名"""

from agent_go.utils import slugify_branch_name


def test_slugify_normal_title():
    """普通标题：转小写，空白替换为横杠"""
    assert slugify_branch_name("Hello World") == "hello-world"


def test_slugify_uppercase_and_spaces():
    """含大写和连续空白：小写并合并空白为单个横杠，保留点号"""
    assert slugify_branch_name("API  v2.0") == "api-v2.0"


def test_slugify_special_characters():
    """含特殊字符：移除非法字符（斜杠/感叹号/@），仅保留合法分支名字符"""
    assert slugify_branch_name("Bug/fix! @login") == "bugfix-login"


def test_slugify_long_title_truncated():
    """超长标题：超过 40 字符时截断到 40 字符以内"""
    result = slugify_branch_name("x" * 100)
    assert result == "x" * 40
    assert len(result) <= 40


def test_slugify_empty_or_whitespace_input():
    """空输入或纯空白输入：返回 'unnamed'"""
    assert slugify_branch_name("") == "unnamed"
    assert slugify_branch_name("   ") == "unnamed"
    assert slugify_branch_name("\t  ") == "unnamed"
