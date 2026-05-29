"""测试 _format_commit — Conventional Commits 格式生成"""

from agent_go.utils import _format_commit, _detect_commit_prefix, _detect_commit_scope


class TestFormatCommitChinese:
    """中文关键词测试"""

    def test_feat_add(self):
        msg = _format_commit("新增用户登录功能")
        assert msg.startswith("feat:"), f"期望 feat: 开头，实际: {msg}"

    def test_feat_implement(self):
        msg = _format_commit("实现 OAuth2 流程")
        assert msg.startswith("feat:"), f"期望 feat: 开头，实际: {msg}"

    def test_fix_fix(self):
        msg = _format_commit("修复空指针异常")
        assert msg.startswith("fix:"), f"期望 fix: 开头，实际: {msg}"

    def test_fix_correct(self):
        msg = _format_commit("修正拼写错误")
        assert msg.startswith("fix:"), f"期望 fix: 开头，实际: {msg}"

    def test_refactor(self):
        msg = _format_commit("重构用户模块")
        assert msg.startswith("refactor:"), f"期望 refactor: 开头，实际: {msg}"

    def test_docs(self):
        msg = _format_commit("更新 API 文档")
        assert msg.startswith("docs:"), f"期望 docs: 开头，实际: {msg}"

    def test_test(self):
        msg = _format_commit("补充单元测试")
        assert msg.startswith("test:"), f"期望 test: 开头，实际: {msg}"

    def test_chore_upgrade(self):
        msg = _format_commit("升级依赖版本")
        assert msg.startswith("chore:"), f"期望 chore: 开头，实际: {msg}"

    def test_chore_config(self):
        msg = _format_commit("配置 CI 流程")
        assert msg.startswith("chore:"), f"期望 chore: 开头，实际: {msg}"


class TestFormatCommitEnglish:
    """英文关键词测试"""

    def test_feat_add(self):
        msg = _format_commit("Add user login feature")
        assert msg.startswith("feat:"), f"期望 feat: 开头，实际: {msg}"

    def test_feat_implement(self):
        msg = _format_commit("implement OAuth2 flow")
        assert msg.startswith("feat:"), f"期望 feat: 开头，实际: {msg}"

    def test_fix_bug(self):
        msg = _format_commit("fix null pointer exception")
        assert msg.startswith("fix:"), f"期望 fix: 开头，实际: {msg}"

    def test_refactor(self):
        msg = _format_commit("refactor authentication module")
        assert msg.startswith("refactor:"), f"期望 refactor: 开头，实际: {msg}"

    def test_docs(self):
        msg = _format_commit("update readme")
        assert msg.startswith("docs:"), f"期望 docs: 开头，实际: {msg}"

    def test_test(self):
        msg = _format_commit("write unit tests for auth module")
        assert msg.startswith("test:"), f"期望 test: 开头，实际: {msg}"

    def test_chore_bump(self):
        msg = _format_commit("bump dependencies")
        assert msg.startswith("chore:"), f"期望 chore: 开头，实际: {msg}"


class TestFormatCommitEdgeCases:
    """边界情况测试"""

    def test_fallback_to_chore(self):
        """无法识别关键词时降级为 chore"""
        msg = _format_commit("做些杂事")
        assert msg.startswith("chore:"), f"期望 chore: 开头，实际: {msg}"

    def test_with_issue_ref(self):
        msg = _format_commit("实现登录", issue_ref="42")
        assert "Refs: #42" in msg, f"应包含 Refs: #42，实际: {msg}"

    def test_with_sub_id(self):
        msg = _format_commit("实现登录", sub_id="sub-1")
        assert "agent_go: sub-1" in msg, f"应包含 agent_go: sub-1，实际: {msg}"

    def test_full_format(self):
        msg = _format_commit("修复登录 bug", issue_ref="42", sub_id="sub-1")
        assert msg.startswith("fix:")
        assert "Refs: #42" in msg
        assert "agent_go: sub-1" in msg

    def test_empty_title(self):
        """空标题"""
        msg = _format_commit("")
        assert msg.startswith("chore:")

    def test_title_priority(self):
        """feat 优先级高于 fix"""
        msg = _format_commit("实现新功能并修复bug")
        assert msg.startswith("feat:"), "feat 应优先于 fix"


class TestDetectCommitPrefix:
    """_detect_commit_prefix 功能测试"""

    def test_feat(self):
        assert _detect_commit_prefix("新增登录") == "feat"
        assert _detect_commit_prefix("add auth") == "feat"

    def test_fix(self):
        assert _detect_commit_prefix("修复bug") == "fix"
        assert _detect_commit_prefix("fix null pointer") == "fix"

    def test_refactor(self):
        assert _detect_commit_prefix("重构模块") == "refactor"
        assert _detect_commit_prefix("refactor auth") == "refactor"

    def test_docs(self):
        assert _detect_commit_prefix("更新文档") == "docs"
        assert _detect_commit_prefix("update readme") == "docs"

    def test_test(self):
        assert _detect_commit_prefix("补充测试") == "test"
        assert _detect_commit_prefix("write spec") == "test"

    def test_chore(self):
        assert _detect_commit_prefix("升级依赖") == "chore"
        assert _detect_commit_prefix("bump version") == "chore"

    def test_fallback(self):
        assert _detect_commit_prefix("做些杂事") == "chore"
        assert _detect_commit_prefix("random text") == "chore"


class TestDetectCommitScope:
    """_detect_commit_scope 功能测试"""

    def test_explicit_scope(self):
        """显式的 (scope) 格式"""
        assert _detect_commit_scope("feat(auth): add login") == "auth"

    def test_common_module(self):
        """常见模块名自动检测"""
        # 英文空格分隔的模块名可检测
        assert _detect_commit_scope("implement auth module") == "auth"
        assert _detect_commit_scope("fix api endpoint") == "api"
        # 中文语境中模块名嵌入无法用 \b 检测，需显式 scope

    def test_no_scope(self):
        """无匹配时返回空字符串"""
        assert _detect_commit_scope("完成各种改进") == ""


class TestFormatCommitWithScope:
    """带 scope 的 _format_commit 测试"""

    def test_explicit_scope_param(self):
        msg = _format_commit("add login", scope="auth")
        assert msg.startswith("feat(auth):"), f"期望 feat(auth): 开头，实际: {msg}"

    def test_auto_scope_from_title(self):
        """英文空格分隔的模块名自动检测为 scope"""
        msg = _format_commit("implement auth module")
        assert "auth" in msg.split("\n")[0], f"应包含 auth scope，实际: {msg.split(chr(10))[0]}"

    def test_scope_with_issue_ref(self):
        msg = _format_commit("修复api接口", issue_ref="42", scope="api")
        assert msg.startswith("fix(api):")
        assert "Refs: #42" in msg

    def test_scope_with_sub_id(self):
        msg = _format_commit("更新文档", sub_id="sub-2", scope="docs")
        assert msg.startswith("docs(docs):")
        assert "agent_go: sub-2" in msg
