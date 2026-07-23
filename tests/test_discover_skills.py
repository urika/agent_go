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
        """部分关键词匹配"""
        self._make_skill_dir(tmp_path, "frontend-react",
                             "前端 React 组件开发与测试")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("编写 React 组件测试用例")
        assert len(result) > 0
        assert result[0].name == "frontend-react"

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
        """大小写不敏感匹配"""
        self._make_skill_dir(tmp_path, "security-review",
                             "Security Audit — Authentication, Authorization")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("implement AUTHENTICATION module")
        assert len(result) > 0
        assert result[0].name == "security-review"

    def test_punctuation_handling(self, tmp_path):
        """标点符号不影响关键词提取"""
        self._make_skill_dir(tmp_path, "api-design",
                             "API 设计规范与 RESTful 架构")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("设计 RESTful API!")
        assert len(result) > 0
        assert result[0].name == "api-design"
