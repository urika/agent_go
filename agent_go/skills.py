"""Skill 加载器 — 解析 YAML frontmatter + Markdown body 的 SKILL.md 文件。

Skill 文件格式：
---
name: security-review
description: 安全审查 — 涉及认证、权限、加密
allowed-tools: Read, Write
---
# Skill 正文内容

加载路径（按优先级）：
1. ~/.agent_go/skills/<name>/SKILL.md
2. <project>/.agent_go/skills/<name>/SKILL.md
"""

import re
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

__all__ = [
    "load_skill", "load_skills", "discover_skills", "list_skills",
    "render_skill_for_plan", "render_skill_for_execution",
]

logger = logging.getLogger(__name__)

AGENT_GO_SKILLS_DIR = Path.home() / ".agent_go" / "skills"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    frontmatter: dict = field(default_factory=dict)
    body: str = ""

    @property
    def allowed_tools(self) -> list[str]:
        raw = self.frontmatter.get("allowed-tools", "")
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        if isinstance(raw, list):
            return raw
        return []


# ── YAML frontmatter 解析（纯 regex，无外部依赖） ──

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """从 Markdown 文本中提取 YAML frontmatter 和正文。"""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_text = match.group(1)
    body = text[match.end():]
    # 简单 YAML key: value 解析（支持单层）
    frontmatter = {}
    for line in fm_text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            # 尝试解析为原生类型
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            elif value.isdigit():
                value = int(value)
            elif value.startswith("[") and value.endswith("]"):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as e:
                    logger.debug("Failed to parse JSON list in frontmatter key '%s': %s", key, e)
            frontmatter[key] = value
    return frontmatter, body.strip()


def _find_skill_file(name: str, project_root: Optional[Path] = None) -> Optional[Path]:
    """查找 Skill 文件（按优先级遍历）。"""
    candidates = []

    # 1. ~/.agent_go/skills/<name>/SKILL.md
    candidates.append(AGENT_GO_SKILLS_DIR / name / "SKILL.md")

    # 2. <project>/.agent_go/skills/<name>/SKILL.md
    if project_root:
        candidates.append(project_root / ".agent_go" / "skills" / name / "SKILL.md")

    for path in candidates:
        if path.exists():
            return path
    return None


def load_skill(name: str, project_root: Optional[Path] = None) -> Optional[Skill]:
    """加载指定名称的 Skill。"""
    path = _find_skill_file(name, project_root)
    if not path:
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _parse_frontmatter(text)

    return Skill(
        name=frontmatter.get("name", name),
        description=frontmatter.get("description", ""),
        path=path,
        frontmatter=frontmatter,
        body=body,
    )


def load_skills(names: list[str], project_root: Optional[Path] = None) -> list[Skill]:
    """批量加载多个 Skill（跳过不存在的）。"""
    skills = []
    for name in names:
        s = load_skill(name, project_root)
        if s:
            skills.append(s)
    return skills


def list_skills(project_root: Optional[Path] = None) -> list[dict]:
    """列出所有已安装的 Skill（名称 + description）。"""
    result = []
    search_dirs = [AGENT_GO_SKILLS_DIR]
    if project_root:
        proj_dir = project_root / ".agent_go" / "skills"
        if proj_dir.exists():
            search_dirs.append(proj_dir)

    for sd in search_dirs:
        if not sd.exists():
            continue
        for skill_dir in sorted(sd.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            s = load_skill(skill_dir.name, project_root)
            if s:
                result.append({
                    "name": s.name,
                    "description": s.description,
                    "path": str(s.path),
                })
    return result


def discover_skills(task: str, project_root: Optional[Path] = None, max_skills: int = 3) -> list[Skill]:
    """根据任务描述自动匹配 Skill（关键词命中 description）。"""
    all_skills = list_skills(project_root)
    matched = []
    task_lower = task.lower()

    for info in all_skills:
        desc_lower = info["description"].lower()
        # 检查 description 中是否有任何词出现在 task 中
        desc_words = set(re.findall(r"\w+", desc_lower))
        task_words = set(re.findall(r"\w+", task_lower))
        overlap = desc_words & task_words
        if overlap:
            s = load_skill(info["name"], project_root)
            if s:
                matched.append((len(overlap), s))

    # 按匹配度排序，取前 N 个
    matched.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in matched[:max_skills]]


# ── 渲染为 Plan / TASK.md 注入格式 ──

def render_skill_for_plan(skill: Skill) -> str:
    """将 Skill 渲染为 Plan prompt 注入格式（轻量摘要）。"""
    lines = [
        f"### Skill: {skill.name}",
        f"描述: {skill.description}",
    ]
    if skill.allowed_tools:
        lines.append(f"推荐工具: {', '.join(skill.allowed_tools)}")
    if skill.body:
        # Plan 注入只取首段摘要（前 500 字符）
        summary = skill.body[:500]
        if len(skill.body) > 500:
            summary += "\n... (截断)"
        lines.append(f"知识摘要:\n{summary}")
    return "\n".join(lines)


def render_skill_for_execution(skill: Skill) -> str:
    """将 Skill 渲染为 TASK.md 执行指令格式（完整内容）。"""
    lines = [
        f"## Skill 知识注入: {skill.name}",
    ]
    if skill.description:
        lines.append(f"**领域**: {skill.description}")
    if skill.allowed_tools:
        lines.append(f"**推荐工具**: {', '.join(skill.allowed_tools)}")
    lines.append("")
    lines.append(skill.body)
    return "\n".join(lines)
