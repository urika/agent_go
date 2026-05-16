"""测试 agents.py — Agent 类型加载与配置"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.agents import (
    AgentType, load_agent_type, list_agent_types,
    AGENT_GO_AGENTS_DIR,
)


class TestLoadAgentType:
    """Agent 类型加载测试"""

    def test_load_builtin_developer(self):
        """内置 developer 类型"""
        agent = load_agent_type("developer")
        assert agent is not None
        assert agent.type_name == "developer"
        assert "权限" in agent.description or "permission" in agent.description.lower() or "写" in agent.description

    def test_load_builtin_architect(self):
        """内置 architect 类型"""
        agent = load_agent_type("architect")
        assert agent is not None
        assert agent.type_name == "architect"
        assert "只读" in agent.description or "read" in agent.description.lower()

    def test_load_builtin_reviewer(self):
        """内置 reviewer 类型"""
        agent = load_agent_type("reviewer")
        assert agent is not None
        assert agent.type_name == "reviewer"

    def test_load_builtin_tester(self):
        """内置 tester 类型"""
        agent = load_agent_type("tester")
        assert agent is not None
        assert agent.type_name == "tester"
        assert agent.claude_config.get("permission_mode") == "bypassPermissions"

    def test_load_user_defined(self):
        """加载用户自定义的 security-reviewer 类型"""
        agent = load_agent_type("security-reviewer")
        if agent:
            assert agent.type_name == "security-reviewer"
            assert "security-review" in agent.preload_skills

    def test_load_nonexistent(self):
        """不存在的类型返回 None"""
        agent = load_agent_type("nonexistent-agent-type")
        assert agent is None

    def test_list_agents(self):
        """列出所有 Agent 类型"""
        agents = list_agent_types()
        assert len(agents) >= 4  # 至少 4 个内置 + 可能有用户定义
        builtin_names = [a["type"] for a in agents if a.get("source") == "builtin"]
        assert "developer" in builtin_names
        assert "architect" in builtin_names


class TestAgentTypeProperties:
    """AgentType 属性测试"""

    def test_default_agent(self):
        agent = AgentType(type_name="custom")
        assert agent.type_name == "custom"
        assert agent.description == ""
        assert agent.claude_config == {}
        assert agent.preload_skills == []

    def test_with_skills(self):
        agent = AgentType(
            type_name="tester",
            preload_skills=["security-review", "frontend-react"]
        )
        assert len(agent.preload_skills) == 2
