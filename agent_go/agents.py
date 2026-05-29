"""Agent 类型定义 — 不同角色的 Claude Code 配置。

Agent 配置文件：~/.agent_go/agents/<type>.json

结构：
{
  "type": "developer",
  "description": "开发者 — 实际编码实现",
  "claude_config": {
    "permission_mode": "default",
    "allowed_tools": ["Read", "Write", "Edit", "Bash"]
  },
  "preload_skills": ["security-review"]
}
"""

import json
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

__all__ = ["load_agent_type", "list_agent_types"]

AGENT_GO_AGENTS_DIR = Path.home() / ".agent_go" / "agents"
AGENT_GO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

# 内置 Agent 类型（无需配置文件）
_BUILTIN_AGENTS = {
    "developer": {
        "type": "developer",
        "description": "开发者 — 默认角色，拥有完整读写和执行权限",
        "claude_config": {
            "permission_mode": "default",
        },
        "preload_skills": [],
    },
    "architect": {
        "type": "architect",
        "description": "架构师 — 只读分析，不修改代码",
        "claude_config": {
            "permission_mode": "default",
            "allowed_tools": ["Read", "Grep", "Glob"],
        },
        "preload_skills": [],
    },
    "reviewer": {
        "type": "reviewer",
        "description": "审查者 — 只读审查，输出审查报告",
        "claude_config": {
            "permission_mode": "default",
            "allowed_tools": ["Read", "Grep", "Glob"],
            "extra_args": [],
        },
        "preload_skills": [],
    },
    "tester": {
        "type": "tester",
        "description": "测试工程师 — 编写和运行测试",
        "claude_config": {
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read", "Write", "Bash"],
        },
        "preload_skills": [],
    },
}


@dataclass
class AgentType:
    type_name: str
    description: str = ""
    claude_config: dict = field(default_factory=dict)
    preload_skills: list[str] = field(default_factory=list)


def load_agent_type(name: str, project_root: Optional[Path] = None) -> Optional[AgentType]:
    """加载指定名称的 Agent 类型定义。"""
    # 优先加载用户定义
    candidates = [AGENT_GO_AGENTS_DIR / f"{name}.json"]
    if project_root:
        candidates.append(project_root / ".agent_go" / "agents" / f"{name}.json")

    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return AgentType(
                    type_name=data.get("type", name),
                    description=data.get("description", ""),
                    claude_config=data.get("claude_config", {}),
                    preload_skills=data.get("preload_skills", []),
                )
            except (json.JSONDecodeError, Exception):
                pass

    # 降级到内置类型
    builtin = _BUILTIN_AGENTS.get(name)
    if builtin:
        return AgentType(
            type_name=builtin["type"],
            description=builtin["description"],
            claude_config=builtin["claude_config"],
            preload_skills=builtin["preload_skills"],
        )

    return None


def list_agent_types() -> list[dict]:
    """列出所有可用的 Agent 类型。"""
    result = []
    seen = set()

    # 内置类型
    for name, cfg in _BUILTIN_AGENTS.items():
        result.append({"type": name, "description": cfg["description"], "source": "builtin"})
        seen.add(name)

    # 用户定义类型
    if AGENT_GO_AGENTS_DIR.exists():
        for f in sorted(AGENT_GO_AGENTS_DIR.glob("*.json")):
            name = f.stem
            if name not in seen:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    result.append({
                        "type": name,
                        "description": data.get("description", ""),
                        "source": "user",
                    })
                    seen.add(name)
                except json.JSONDecodeError:
                    pass

    return result


def get_claude_command(
    agent: AgentType,
    worktree: Path,
    headless: bool = False,
) -> list[str]:
    """根据 Agent 类型构建 claude CLI 参数列表。"""

    import shutil
    greywall = shutil.which("greywall")

    if headless:
        agent_mode = agent.claude_config.get("permission_mode", "")
        mode = agent_mode if agent_mode in ("bypassPermissions", "acceptEdits") else "bypassPermissions"
        cmd = [
            "claude", "-p", "",
            "--permission-mode", mode,
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        allowed = agent.claude_config.get("allowed_tools", [])
        if allowed:
            cmd.extend(["--allowedTools", ",".join(allowed)])
        return cmd

    cmd = (["greywall", "--"] if greywall else [])
    cmd.extend(["claude", str(worktree)])

    permission_mode = agent.claude_config.get("permission_mode", "default")
    if permission_mode == "bypassPermissions":
        cmd.extend(["--permission-mode", "bypassPermissions"])
    elif permission_mode == "acceptEdits":
        cmd.extend(["--permission-mode", "acceptEdits"])

    allowed = agent.claude_config.get("allowed_tools", [])
    if allowed:
        cmd.extend(["--allowedTools", ",".join(allowed)])

    return cmd


def get_agent_env(agent: AgentType) -> dict[str, str]:
    """根据 Agent 类型构建环境变量。"""
    env = {}
    if agent.type_name:
        env["AGENT_GO_AGENT_TYPE"] = agent.type_name
    return env
