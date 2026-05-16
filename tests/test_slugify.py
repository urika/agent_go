"""测试 _slugify — 生成分支名适用的短标识"""

from agent_go import _slugify


class TestSlugify:
    """_slugify 基础功能测试"""

    def test_normal_text(self):
        result = _slugify("Add user login")
        assert result == "Add-user-login"

    def test_chinese_text(self):
        result = _slugify("实现用户登录")
        assert "实现" in result
        assert "用户" in result
        assert "登录" in result
        # 中文之间不应有连字符
        assert "--" not in result

    def test_special_chars(self):
        result = _slugify("Hello! @World #2024")
        assert "Hello" in result
        assert "World" in result
        assert result == "Hello-World-2024"

    def test_leading_trailing_dashes(self):
        result = _slugify("---hello-world---")
        # 不应以连字符开头或结尾
        assert not result.startswith("-")
        assert not result.endswith("-")
        assert result == "hello-world"

    def test_max_len_truncation(self):
        long_text = "a" * 100
        result = _slugify(long_text, max_len=30)
        assert len(result) <= 30

    def test_max_len_custom(self):
        result = _slugify("hello world foo bar", max_len=10)
        assert len(result) <= 10

    def test_empty_string(self):
        result = _slugify("")
        assert result == ""

    def test_only_special_chars(self):
        result = _slugify("!!! @@@ ###")
        assert result == ""

    def test_mixed_chinese_english(self):
        result = _slugify("修复 auth bug 导致登录失败")
        assert "修复" in result
        assert "auth" in result
        assert "bug" in result
