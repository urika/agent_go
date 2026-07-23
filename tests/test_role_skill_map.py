import sys
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_go.role_skill_map import (
    _match_rule, match_rules, apply_rules, load_role_skill_map, DEFAULT_MAP
)


class TestMatchRule:
    def test_agent_type_match(self):
        rule = {"match": {"agent_type": "tester"}}
        assert _match_rule(rule, {"agent_type": "tester"})
        assert not _match_rule(rule, {"agent_type": "developer"})
        assert not _match_rule(rule, {})

    def test_keywords_match(self):
        rule = {"match": {"keywords": ["安全", "security"]}}
        assert _match_rule(rule, {"title": "安全审查", "description": ""})
        assert _match_rule(rule, {"title": "", "description": "security audit"})
        assert not _match_rule(rule, {"title": "普通任务", "description": "nothing"})

    def test_keywords_case_insensitive(self):
        rule = {"match": {"keywords": ["Auth", "REVIEW"]}}
        assert _match_rule(rule, {"title": "auth module", "description": ""})
        assert _match_rule(rule, {"title": "", "description": "code review process"})

    def test_file_patterns_match(self):
        rule = {"match": {"file_patterns": ["*.md", "*.rst"]}}
        assert _match_rule(rule, {"files": ["README.md", "docs/spec.rst"]})
        assert not _match_rule(rule, {"files": ["src/main.py"]})
        assert not _match_rule(rule, {"files": []})
        assert not _match_rule(rule, {})

    def test_combined_conditions_and(self):
        rule = {"match": {"agent_type": "tester", "keywords": ["unit"]}}
        assert _match_rule(rule, {"agent_type": "tester", "title": "unit tests", "description": ""})
        assert not _match_rule(rule, {"agent_type": "tester", "title": "integration", "description": ""})
        assert not _match_rule(rule, {"agent_type": "developer", "title": "unit tests", "description": ""})

    def test_empty_match_matches_anything(self):
        rule = {}
        assert _match_rule(rule, {"title": "anything"})


class TestMatchRules:
    def test_multiple_matches(self):
        role_map = {
            "rules": [
                {"match": {"keywords": ["test"]}, "skills": {"required": ["tdd"]}},
                {"match": {"keywords": ["安全"]}, "skills": {"required": ["security"]}},
            ]
        }
        step = {"title": "test 安全模块", "description": ""}
        matched = match_rules(step, role_map)
        assert len(matched) == 2

    def test_no_match(self):
        role_map = {"rules": [{"match": {"keywords": ["安全"]}}]}
        step = {"title": "普通任务", "description": ""}
        assert match_rules(step, role_map) == []


class TestApplyRules:
    def test_required_skills_always_injected(self):
        role_map = {
            "rules": [{"match": {"keywords": ["安全"]}, "skills": {"required": ["security-review"]}}]
        }
        installed = [{"name": "security-review", "description": "", "path": "/tmp"}]
        result = apply_rules(
            {"title": "安全审查", "skills": [], "description": ""},
            role_map, installed
        )
        assert "security-review" in result["skills"]

    def test_required_skills_not_overridden(self):
        role_map = {
            "rules": [{"match": {"keywords": ["安全"]}, "skills": {"required": ["security-review"]}}]
        }
        installed = [{"name": "security-review", "description": "", "path": "/tmp"}]
        result = apply_rules(
            {"title": "安全审查", "skills": ["security-review", "extra-skill"], "description": ""},
            role_map, installed
        )
        assert "security-review" in result["skills"]
        assert "extra-skill" in result["skills"]

    def test_recommended_skills_only_when_llm_unspecified(self):
        role_map = {
            "rules": [{"match": {"keywords": ["test"]}, "skills": {"recommended": ["tdd-workflow"]}}]
        }
        installed = [{"name": "tdd-workflow", "description": "", "path": "/tmp"}]

        # LLM 未指定 skills（空数组）→ 推荐注入
        result = apply_rules({"title": "write unit test", "skills": [], "description": ""}, role_map, installed)
        assert "tdd-workflow" in result["skills"]

        # LLM 已指定 skills → 不重复注入推荐
        result2 = apply_rules({"title": "write unit test", "skills": ["custom-skill"], "description": ""}, role_map, installed)
        assert "tdd-workflow" not in result2["skills"]

    def test_missing_installed_skill_skipped(self):
        role_map = {
            "rules": [{"match": {"keywords": ["安全"]}, "skills": {"required": ["security-review"]}}]
        }
        result = apply_rules({"title": "安全审查", "skills": [], "description": ""}, role_map, [])
        assert "security-review" not in result["skills"]

    def test_agent_type_from_rule(self):
        role_map = {
            "rules": [{"match": {"keywords": ["架构"]}, "agent_type": "architect"}]
        }
        result = apply_rules({"title": "架构设计", "skills": [], "description": ""}, role_map)
        assert result["agent_type"] == "architect"

    def test_llm_agent_type_priority(self):
        role_map = {
            "rules": [{"match": {"keywords": ["架构"]}, "agent_type": "architect"}]
        }
        result = apply_rules({"title": "架构设计", "agent_type": "reviewer", "skills": [], "description": ""}, role_map)
        assert result["agent_type"] == "reviewer"

    def test_default_agent_type(self):
        role_map = {"rules": [], "default_agent_type": "developer"}
        result = apply_rules({"title": "普通任务", "skills": [], "description": ""}, role_map)
        assert result["agent_type"] == "developer"

    def test_file_pattern_architect_rule(self):
        role_map = {
            "rules": [{"match": {"file_patterns": ["*.md"]}, "agent_type": "architect"}]
        }
        result = apply_rules({"title": "文档更新", "files": ["README.md"], "skills": [], "description": ""}, role_map)
        assert result["agent_type"] == "architect"


class TestLoadRoleSkillMap:
    def test_default_map(self):
        role_map = load_role_skill_map(None)
        assert "rules" in role_map
        assert len(role_map["rules"]) == 5
        assert role_map["default_agent_type"] == "developer"

    def test_default_map_has_recommended_fields(self):
        role_map = load_role_skill_map(None)
        assert "recommended_agents" in role_map
        assert "recommended_skills" in role_map
        assert len(role_map["recommended_agents"]) == 4


class TestLoadRoleSkillMapMerge:
    """全局/项目级规则的三层合并加载（回归 docs/ISSUES.md ISSUE-11）"""

    def test_global_and_project_rules_merged(self, tmp_path):
        """全局与项目级规则与默认规则合并，项目规则排在最前"""
        global_file = tmp_path / "global.json"
        global_file.write_text(json.dumps({
            "rules": [{"match": {"keywords": ["global-kw"]},
                       "skills": {"required": [], "recommended": []}}],
        }), encoding="utf-8")
        project_file = tmp_path / "project.json"
        project_file.write_text(json.dumps({
            "rules": [{"match": {"keywords": ["proj-kw"]},
                       "skills": {"required": [], "recommended": []}}],
            "default_agent_type": "reviewer",
        }), encoding="utf-8")

        with patch("agent_go.role_skill_map._global_map_path", return_value=global_file), \
             patch("agent_go.role_skill_map._project_map_path", return_value=project_file):
            role_map = load_role_skill_map(tmp_path)

        # 三层规则合并：默认规则不丢失
        assert len(role_map["rules"]) == len(DEFAULT_MAP["rules"]) + 2
        # 项目规则在最前（apply_rules 中先匹配的 agent_type 优先）
        assert role_map["rules"][0]["match"]["keywords"] == ["proj-kw"]
        assert role_map["rules"][1]["match"]["keywords"] == ["global-kw"]
        # 标量键由项目级覆盖
        assert role_map["default_agent_type"] == "reviewer"

    def test_no_files_returns_default(self, tmp_path):
        """无全局/项目文件时行为与 DEFAULT_MAP 一致"""
        with patch("agent_go.role_skill_map._global_map_path",
                   return_value=tmp_path / "nonexistent.json"), \
             patch("agent_go.role_skill_map._project_map_path",
                   return_value=tmp_path / "also-nonexistent.json"):
            role_map = load_role_skill_map(tmp_path)
        assert role_map["rules"] == DEFAULT_MAP["rules"]
        assert role_map["default_agent_type"] == DEFAULT_MAP["default_agent_type"]
