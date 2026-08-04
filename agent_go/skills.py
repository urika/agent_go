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


def get_skill_full(name: str, project_root: Optional[Path] = None) -> Optional[dict]:
    """获取 Skill 完整内容（frontmatter + body + 原始 SKILL.md 文本）。

    供 `agent_go skills show <name>` 使用——AI Agent 可直接读取
    SKILL.md 原始内容获得完整使用说明（R-2 SKILL.md 自描述）。
    """
    s = load_skill(name, project_root)
    if not s:
        return None
    raw = ""
    if s.path.exists():
        try:
            raw = s.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
    return {
        "name": s.name,
        "description": s.description,
        "path": str(s.path),
        "frontmatter": s.frontmatter,
        "body": s.body,
        "raw": raw,
        "allowed_tools": s.allowed_tools,
    }


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


def _tokenize_words(text: str) -> set[str]:
    """分词：英文按单词提取，连续 CJK 按字符拆分。

    中文无空格分词，`\\w+` 会把「组件开发与测试」整段合并为单个 token，
    导致 CJK 关键词重叠判定失真。此函数把 CJK 连续串拆成单字符 token
    （接近按字分词），英文保持单词粒度，使 overlap 统计可靠。
    """
    tokens: set[str] = set()
    for m in re.finditer(r"[a-z0-9]+|[^\W\d_]+", text.lower()):
        w = m.group()
        if re.fullmatch(r"[a-z0-9]+", w):
            tokens.add(w)
        else:
            # CJK 串：按字符拆分
            for ch in w:
                tokens.add(ch)
    return tokens


def discover_skills(task: str, project_root: Optional[Path] = None, max_skills: int = 3) -> list[Skill]:
    """根据任务描述自动匹配 Skill（关键词命中 description）。"""
    all_skills = list_skills(project_root)
    matched = []
    task_lower = task.lower()

    for info in all_skills:
        desc_lower = info["description"].lower()
        # 检查 description 中是否有任何词出现在 task 中
        desc_words = _tokenize_words(desc_lower)
        task_words = _tokenize_words(task_lower)
        # 排除通用结构词（任务模板字段名、弱语义词），避免单词误配
        _skip = {
            "verification", "validate", "valid", "list", "read", "write",
            "create", "file", "files", "string", "function", "functions",
            "input", "output", "task", "return", "returns", "add", "using",
            "and", "the", "a", "an", "of", "for", "in", "with", "to",
            "py", "str", "txt", "all", "any", "its", "it", "are", "is",
            "be", "by", "on", "at", "as", "or", "not", "no", "each",
        }
        overlap = (desc_words & task_words) - _skip
        # 排除纯数字（任务编号/规则序号，无语义），防数字误配
        overlap = {w for w in overlap if not w.isdigit()}
        # 至少 2 个实质词重叠才匹配，防止单泛词误配（如 orm-optimizer 的
        # "verification" 与任务模板字段重叠）。CJK 按字符分词后能产出
        # 正常的多字重叠，故不再需要中文单词特例。
        if len(overlap) >= 2:
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
