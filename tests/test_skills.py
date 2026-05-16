"""测试 skills.py — Skill 加载、解析、渲染"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.skills import (
    Skill, _parse_frontmatter, _find_skill_file,
    load_skill, load_skills, discover_skills, list_skills,
    render_skill_for_plan, render_skill_for_execution,
    AGENT_GO_SKILLS_DIR,
)


class TestFrontmatterParsing:
    """YAML frontmatter 解析测试"""

    def test_basic_parsing(self):
        text = """---
name: test-skill
description: 测试 Skill
allowed-tools: Read, Write
---
# 正文内容
这是测试正文。"""
        fm, body = _parse_frontmatter(text)
        assert fm["name"] == "test-skill"
        assert fm["description"] == "测试 Skill"
        assert fm["allowed-tools"] == "Read, Write"
        assert "正文内容" in body

    def test_no_frontmatter(self):
        text = "# 普通 Markdown\n没有 frontmatter"
        fm, body = _parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_boolean_value(self):
        text = """---
name: test
auto: true
---
body"""
        fm, body = _parse_frontmatter(text)
        assert fm["auto"] is True

    def test_list_value(self):
        text = """---
name: test
tools: ["Read", "Write"]
---
body"""
        fm, body = _parse_frontmatter(text)
        assert fm["tools"] == ["Read", "Write"]

    def test_empty_frontmatter(self):
        text = """---
---
body"""
        fm, body = _parse_frontmatter(text)
        assert fm == {}


class TestSkillProperties:
    """Skill 对象属性测试"""

    def test_allowed_tools_string(self):
        skill = Skill(name="test", description="desc", path=Path("/tmp"),
                      frontmatter={"allowed-tools": "Read, Write"})
        assert skill.allowed_tools == ["Read", "Write"]

    def test_allowed_tools_list(self):
        skill = Skill(name="test", description="desc", path=Path("/tmp"),
                      frontmatter={"allowed-tools": ["Read", "Write"]})
        assert skill.allowed_tools == ["Read", "Write"]

    def test_allowed_tools_empty(self):
        skill = Skill(name="test", description="desc", path=Path("/tmp"))
        assert skill.allowed_tools == []


class TestRenderSkill:
    """Skill 渲染测试"""

    def test_render_for_plan(self):
        skill = Skill(
            name="security", description="安全审查",
            path=Path("/tmp"),
            frontmatter={"allowed-tools": "Read"},
            body="## 规则\n1. 检查 JWT"
        )
        result = render_skill_for_plan(skill)
        assert "Skill: security" in result
        assert "安全审查" in result
        assert "检查 JWT" in result

    def test_render_for_execution(self):
        skill = Skill(
            name="security", description="安全审查",
            path=Path("/tmp"),
            frontmatter={"allowed-tools": "Read"},
            body="## 规则\n1. 检查 JWT"
        )
        result = render_skill_for_execution(skill)
        assert "Skill 知识注入: security" in result
        assert "安全审查" in result
        assert "检查 JWT" in result

    def test_render_for_plan_truncates_long_body(self):
        long_body = "x" * 600
        skill = Skill(name="test", description="", path=Path("/tmp"),
                      body=long_body)
        result = render_skill_for_plan(skill)
        assert "截断" in result  # 超过 500 字符截断
        assert len(result) < len(long_body) + 500


class TestLoadSkills:
    """Skill 加载测试"""

    def test_load_builtin_skill(self):
        """加载 ~/.agent_go/skills/security-review/SKILL.md"""
        skill = load_skill("security-review")
        if skill:
            assert skill.name == "security-review"
            assert "JWT" in skill.body or len(skill.body) > 0

    def test_load_skills_multi(self):
        skills = load_skills(["security-review", "frontend-react"])
        assert len(skills) > 0

    def test_load_nonexistent(self):
        skill = load_skill("nonexistent-skill-xyz")
        assert skill is None

    def test_list_skills(self):
        skills = list_skills()
        assert len(skills) > 0
        names = [s["name"] for s in skills]
        assert "security-review" in names
