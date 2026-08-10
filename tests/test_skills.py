"""测试 skills.py — Skill 加载、解析、渲染"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.skills import (
    Skill, _parse_frontmatter, _find_skill_file,
    load_skill, load_skills, discover_skills, list_skills,
    render_skill_for_plan, render_skill_for_execution,
    resolve_skill_chain,
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

    def test_render_for_execution_guide_mode(self):
        """guide 模式（claude worker）：只给 name + 绝对路径，不注入完整 body。"""
        skill = Skill(
            name="security", description="安全审查",
            path=Path("/tmp/security"),
            frontmatter={"allowed-tools": "Read"},
            body="## 规则\n1. 检查 JWT 签名算法\n2. 禁止硬编码密钥",
        )
        result = render_skill_for_execution(skill, mode="guide")
        assert "轻量指引" in result
        assert "安全审查" in result
        assert "Skill 文件" in result
        assert "/tmp/security/SKILL.md" in result
        # 不应包含完整 body
        assert "检查 JWT" not in result
        assert "硬编码" not in result

    def test_render_for_execution_full_is_default(self):
        """默认模式仍为 full：向后兼容，完整 body 注入（agent_loop 用）。"""
        skill = Skill(
            name="security", description="安全审查",
            path=Path("/tmp/security"),
            body="## 规则\n1. 检查 JWT",
        )
        result = render_skill_for_execution(skill)
        assert "检查 JWT" in result
        assert "轻量指引" not in result

    def test_render_for_execution_guide_symlink_abs_path(self, tmp_path):
        """guide 模式绝对路径应 resolve 掉 symlink（claude 可按真实位置 Read）。"""
        real_dir = tmp_path / "real-skill"
        real_dir.mkdir()
        (real_dir / "SKILL.md").write_text("# real", encoding="utf-8")
        link_dir = tmp_path / "link-skill"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        skill = Skill(
            name="linked", description="链接 skill",
            path=link_dir / "SKILL.md", body="body",
        )
        result = render_skill_for_execution(skill, mode="guide")
        assert str(real_dir / "SKILL.md") in result
        assert str(link_dir / "SKILL.md") not in result


class TestResolveSkillChain:
    """resolve_skill_chain — symlink 解析链诊断。"""

    def test_resolve_real_dir(self, tmp_path):
        """原生目录（非 symlink）：链只有 1 跳。"""
        from agent_go.skills import resolve_skill_chain
        skill_dir = tmp_path / "skills" / "native"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: native\ndescription: d\n---\nbody",
                                            encoding="utf-8")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path / "skills"):
            info = resolve_skill_chain("native")
        assert info is not None
        assert info["is_symlink"] is False
        assert len(info["dir_chain"]) == 1
        assert info["exists"] is True

    def test_resolve_symlink_chain(self, tmp_path):
        """多级 symlink：完整展开解析链。"""
        from agent_go.skills import resolve_skill_chain
        real = tmp_path / "real"
        real.mkdir()
        (real / "SKILL.md").write_text("---\nname: s1\ndescription: d\n---\nbody",
                                       encoding="utf-8")
        mid = tmp_path / "mid"
        mid.symlink_to(real, target_is_directory=True)
        entry = tmp_path / "skills"
        entry.mkdir()
        (entry / "s1").symlink_to(mid, target_is_directory=True)
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", entry):
            info = resolve_skill_chain("s1")
        assert info is not None
        assert info["is_symlink"] is True
        assert len(info["dir_chain"]) == 3, info["dir_chain"]
        assert str(real) in info["dir_chain"]
        assert info["exists"] is True
        assert str(real / "SKILL.md") == info["resolved"]

    def test_resolve_missing_skill(self, tmp_path):
        """不存在的 skill → None。"""
        from agent_go.skills import resolve_skill_chain
        entry = tmp_path / "empty"
        entry.mkdir()
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", entry):
            assert resolve_skill_chain("nope") is None

    def test_resolve_broken_symlink(self, tmp_path):
        """断裂 symlink（目标不存在）→ exists=False。"""
        from agent_go.skills import resolve_skill_chain
        entry = tmp_path / "skills"
        entry.mkdir()
        (entry / "broken").symlink_to(tmp_path / "ghost", target_is_directory=True)
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", entry):
            info = resolve_skill_chain("broken")
        # 断裂时 load_skill 返回 None（文件读不到），resolve 返回 None
        assert info is None


class TestLoadSkills:
    """Skill 加载测试（使用临时 skill 目录，不依赖全局安装）"""

    @staticmethod
    def _make_skill_dir(tmp_path, name, description, body=""):
        """在 tmp_path 下创建一个模拟的 skill 目录。"""
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{body}",
            encoding="utf-8",
        )
        return skill_dir

    def test_load_builtin_skill(self):
        """加载 ~/.agent_go/skills/security-review/SKILL.md（仅本地有则验证）"""
        skill = load_skill("security-review")
        if skill:
            assert skill.name == "security-review"
            assert "JWT" in skill.body or len(skill.body) > 0

    def test_load_skills_multi(self, tmp_path):
        """批量加载多个 Skill（使用临时目录）"""
        self._make_skill_dir(tmp_path, "security-review", "安全审查", "# 规则\n检查 JWT")
        self._make_skill_dir(tmp_path, "frontend-react", "前端 React", "# 规则\n组件规范")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            skills = load_skills(["security-review", "frontend-react"])
        assert len(skills) == 2
        assert skills[0].name == "security-review"
        assert skills[1].name == "frontend-react"

    def test_load_skills_missing_skipped(self, tmp_path):
        """不存在的 Skill 被跳过，不报错"""
        self._make_skill_dir(tmp_path, "security-review", "安全审查")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            skills = load_skills(["security-review", "nonexistent-skill"])
        assert len(skills) == 1
        assert skills[0].name == "security-review"

    def test_load_nonexistent(self):
        skill = load_skill("nonexistent-skill-xyz")
        assert skill is None

    def test_list_skills(self, tmp_path):
        """列出所有已安装 Skill（使用临时目录）"""
        self._make_skill_dir(tmp_path, "security-review", "安全审查 — 涉及认证、权限、加密")
        self._make_skill_dir(tmp_path, "code-review", "代码审查 — 质量与规范")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            skills = list_skills()
        assert len(skills) == 2
        names = [s["name"] for s in skills]
        assert "security-review" in names
        assert "code-review" in names

    def test_list_skills_empty(self, tmp_path):
        """无 skill 文件时返回空列表"""
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", empty_dir):
            skills = list_skills()
        assert skills == []
