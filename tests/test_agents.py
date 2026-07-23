"""测试 agents.py — Agent 类型加载、CLI 命令构建、环境变量

全覆盖:
  - load_agent_type / list_agent_types（已有）
  - get_claude_command（headless / 交互 / greywall / 权限模式）
  - get_agent_env
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.agents import (
    AgentType, load_agent_type, list_agent_types,
    get_claude_command, get_agent_env,
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


class TestGetClaudeCommand:
    """get_claude_command — 根据 Agent 类型构建 claude CLI 参数"""

    def test_headless_default_permission(self):
        """headless 模式默认 bypassPermissions"""
        agent = AgentType(type_name="developer", claude_config={
            "permission_mode": "default",
        })
        cmd = get_claude_command(agent, Path("/tmp/work"), headless=True)
        assert "claude" in cmd
        assert "-p" in cmd
        assert "--permission-mode" in cmd
        # default → headless 会强制改为 bypassPermissions
        assert "bypassPermissions" in cmd
        assert "--no-session-persistence" in cmd
        assert "--output-format" in cmd

    def test_headless_with_allowed_tools(self):
        """headless 模式带 allowed_tools"""
        agent = AgentType(type_name="tester", claude_config={
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read", "Write", "Bash"],
        })
        cmd = get_claude_command(agent, Path("/tmp/work"), headless=True)
        assert "--allowedTools" in cmd
        tools_idx = cmd.index("--allowedTools")
        assert cmd[tools_idx + 1] == "Read,Write,Bash"

    def test_headless_bypass_permission(self):
        """bypassPermissions → --permission-mode bypassPermissions"""
        agent = AgentType(type_name="tester", claude_config={
            "permission_mode": "bypassPermissions",
        })
        cmd = get_claude_command(agent, Path("/tmp/work"), headless=True)
        perm_idx = cmd.index("--permission-mode")
        assert cmd[perm_idx + 1] == "bypassPermissions"

    def test_interactive_no_greywall(self):
        """交互模式，无 greywall"""
        agent = AgentType(type_name="developer", claude_config={
            "permission_mode": "default",
        })
        with patch("shutil.which", return_value=None):
            cmd = get_claude_command(agent, Path("/tmp/work"), headless=False)
        assert cmd == ["claude", "/tmp/work"]

    def test_interactive_with_greywall(self):
        """交互模式，有 greywall 包装"""
        agent = AgentType(type_name="developer")
        with patch("shutil.which", return_value="/usr/local/bin/greywall"):
            cmd = get_claude_command(agent, Path("/tmp/work"), headless=False)
        assert cmd[0] == "greywall"
        assert cmd[1] == "--"
        assert cmd[2] == "claude"

    def test_interactive_bypass_permission(self):
        """交互模式 bypassPermissions"""
        agent = AgentType(type_name="tester", claude_config={
            "permission_mode": "bypassPermissions",
        })
        with patch("shutil.which", return_value=None):
            cmd = get_claude_command(agent, Path("/tmp/work"), headless=False)
        assert "--permission-mode" in cmd
        perm_idx = cmd.index("--permission-mode")
        assert cmd[perm_idx + 1] == "bypassPermissions"

    def test_interactive_accept_edits(self):
        """交互模式 acceptEdits"""
        agent = AgentType(type_name="reviewer", claude_config={
            "permission_mode": "acceptEdits",
        })
        with patch("shutil.which", return_value=None):
            cmd = get_claude_command(agent, Path("/tmp/work"), headless=False)
        perm_idx = cmd.index("--permission-mode")
        assert cmd[perm_idx + 1] == "acceptEdits"


class TestGetAgentEnv:
    """get_agent_env — Agent 环境变量"""

    def test_agent_type_env(self):
        agent = AgentType(type_name="reviewer")
        env = get_agent_env(agent)
        assert env["AGENT_GO_AGENT_TYPE"] == "reviewer"

    def test_default_agent(self):
        agent = AgentType(type_name="developer")
        env = get_agent_env(agent)
        assert env["AGENT_GO_AGENT_TYPE"] == "developer"

    def test_returns_dict(self):
        agent = AgentType(type_name="custom")
        env = get_agent_env(agent)
        assert isinstance(env, dict)

    def test_env_isolation(self):
        """返回的 env 不引用外部对象"""
        agent = AgentType(type_name="tester")
        env = get_agent_env(agent)
        env["EXTRA"] = "x"
        env2 = get_agent_env(agent)
        assert "EXTRA" not in env2


class TestListAgentTypesOverride:
    """用户同名覆盖内置类型的可见性（回归 docs/ISSUES.md ISSUE-13）"""

    def test_user_override_visible(self, tmp_path):
        """用户定义与内置同名时，列表显示用户版本并标注 overrides"""
        (tmp_path / "developer.json").write_text(
            '{"description": "my custom developer"}', encoding="utf-8")
        with patch("agent_go.agents.AGENT_GO_AGENTS_DIR", tmp_path):
            agents = list_agent_types()
        dev = [a for a in agents if a["type"] == "developer"]
        # 同名条目只出现一次，且为用户版本
        assert len(dev) == 1
        assert dev[0]["source"] == "user (overrides builtin)"
        assert dev[0]["description"] == "my custom developer"

    def test_user_only_type_listed(self, tmp_path):
        """纯用户自定义类型正常列出"""
        (tmp_path / "security-expert.json").write_text(
            '{"description": "custom role"}', encoding="utf-8")
        with patch("agent_go.agents.AGENT_GO_AGENTS_DIR", tmp_path):
            agents = list_agent_types()
        custom = [a for a in agents if a["type"] == "security-expert"]
        assert len(custom) == 1
        assert custom[0]["source"] == "user"
        # 内置类型仍然全部可见
        assert any(a["type"] == "developer" and a["source"] == "builtin" for a in agents)
