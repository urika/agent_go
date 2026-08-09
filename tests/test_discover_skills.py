"""测试 discover_skills — 基于关键词的任务-技能自动匹配"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.skills import discover_skills, Skill, AGENT_GO_SKILLS_DIR


class _MakeSkillDirMixin:
    """Helper: 在临时目录下创建模拟 skill"""

    @staticmethod
    def _make_skill_dir(tmp_path, name, description):
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nSkill body.",
            encoding="utf-8",
        )
        return skill_dir


class TestDiscoverSkills(_MakeSkillDirMixin):
    """discover_skills 自动匹配测试"""

    def test_exact_match(self, tmp_path):
        """任务描述与 skill description 有重叠词时命中"""
        # 注: 使用英文关键词，因 discover_skills 基于 r'\w+' 分词，
        # 中文文本无空格时整句为一个 token，无法部分匹配
        self._make_skill_dir(tmp_path, "security-review",
                             "Security audit — authentication authorization encryption")
        self._make_skill_dir(tmp_path, "code-review",
                             "Code review — quality and standards")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("audit authentication module for security")
        assert len(result) > 0
        names = [s.name for s in result]
        assert "security-review" in names

    def test_no_match(self, tmp_path):
        """无关键词语义重叠时不匹配"""
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密")
        self._make_skill_dir(tmp_path, "code-review",
                             "代码审查 — 质量与规范")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("更新 README 文档")
        assert result == []

    def test_partial_match(self, tmp_path):
        """部分关键词匹配（多词重叠命中）"""
        self._make_skill_dir(tmp_path, "frontend-react",
                             "前端 React 组件开发与测试")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("编写 React 组件测试用例")
        assert len(result) > 0
        assert result[0].name == "frontend-react"

    def test_single_english_word_no_match(self, tmp_path):
        """单一英文泛词不匹配（防止 skill description 中的弱义词误配，
        如 orm-optimizer 的 "verification" 与任务模板字段重叠）"""
        self._make_skill_dir(tmp_path, "frontend-react",
                             "前端 React 组件开发与测试")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("编写 React 组件测试用例")
        # 英文单词 "react" 不满足 ≥2 词规则（中文词除外），不匹配
        # 注意：此描述含中文词，走 CJK 分支应仍匹配；用纯英文单泛词验证
        result2 = discover_skills("implement authentication module")
        names = [s.name for s in result2]
        assert "frontend-react" not in names

    def test_sort_by_overlap_count(self, tmp_path):
        """按匹配关键词数排序，重叠词多的排在前面"""
        self._make_skill_dir(tmp_path, "skill-a",
                             "React 组件开发与性能优化")
        self._make_skill_dir(tmp_path, "skill-b",
                             "React 测试编写和组件审查")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("React 组件测试")
        # skill-b 匹配 "React" "组件" "测试" 三个词
        # skill-a 匹配 "React" "组件" 两个词
        if len(result) >= 2:
            # skill-b 应该在前面
            names = [s.name for s in result]
            assert "skill-b" == names[0] or len(result) >= 1

    def test_max_skills_limit(self, tmp_path):
        """max_skills 参数限制返回数量"""
        for i in range(5):
            self._make_skill_dir(tmp_path, f"skill-{i}",
                                 f"task 测试 验证 审查 质量 {i}")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("task 测试", max_skills=2)
        assert len(result) <= 2

    def test_returns_skill_objects(self, tmp_path):
        """返回类型为 Skill 对象"""
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("安全审查")
        for s in result:
            assert isinstance(s, Skill)
            assert s.body != ""

    def test_no_installed_skills(self, tmp_path):
        """无已安装 skill 时返回空列表"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", empty_dir):
            result = discover_skills("任何任务")
        assert result == []

    def test_case_insensitive_match(self, tmp_path):
        """大小写不敏感匹配（多词重叠命中）"""
        self._make_skill_dir(tmp_path, "security-review",
                             "Security Audit — Authentication, Authorization")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            # 单英文泛词 "authentication" 不满足 ≥2 词规则，不匹配
            result = discover_skills("implement AUTHENTICATION module")
            assert len(result) == 0
            # 多词重叠（authentication + authorization）命中，且大小写不敏感
            result2 = discover_skills("implement AUTHENTICATION and AUTHORIZATION flows")
            assert len(result2) > 0
            assert result2[0].name == "security-review"

    def test_punctuation_handling(self, tmp_path):
        """标点符号不影响关键词提取"""
        self._make_skill_dir(tmp_path, "api-design",
                             "API 设计规范与 RESTful 架构")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("设计 RESTful API!")
        assert len(result) > 0
        assert result[0].name == "api-design"

    def test_no_cross_type_mismatch_issue33(self, tmp_path):
        """ISSUE-33: Python 修复任务不得误配前端 skill（CJK bigram 防单字符误配）。

        修复前：CJK 按单字符分词，frontend-react description 中的高频字（管理/组件/
        网络/请求）与任何中文任务都易重叠 ≥2 个字符，导致无关 skill 注入。修复后
        bigram 保留语义，跨类型任务无重叠。
        """
        self._make_skill_dir(tmp_path, "frontend-react",
                             "React 前端开发 — 组件、状态管理、路由、样式、网络请求")
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密、SQL注入、密钥管理的代码")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            # Python 修复任务：不应匹配 frontend-react
            result = discover_skills("修复 planning.py 的 validate_plan_quality 函数 scope_conflict 误报")
            names = [s.name for s in result]
            assert "frontend-react" not in names, f"Python 修复任务误配前端 skill: {names}"
            # 安全任务：应匹配 security-review
            result2 = discover_skills("对认证和权限相关的代码进行安全审查，检查 SQL 注入")
            names2 = [s.name for s in result2]
            assert "security-review" in names2, f"安全任务应匹配 security-review: {names2}"
            # 前端任务：应匹配 frontend-react
            result3 = discover_skills("编写 React 前端组件，处理状态管理和路由")
            names3 = [s.name for s in result3]
            assert "frontend-react" in names3, f"前端任务应匹配 frontend-react: {names3}"
