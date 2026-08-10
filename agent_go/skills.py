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

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

__all__ = [
    "load_skill", "load_skills", "discover_skills", "list_skills",
    "render_skill_for_plan", "render_skill_for_execution",
    "get_skill_full", "resolve_skill_chain",
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


def resolve_skill_chain(name: str, project_root: Optional[Path] = None) -> Optional[dict]:
    """追踪 Skill 的 symlink 解析链（诊断多级 skill 目录）。

    多级目录模型（见 docs/design/skill-injection-strategy.md）：
      入口可能是 symlink 目录（如 ~/.agent_go/skills/<name> -> ~/.claude/skills/<name>
      -> ~/.cc-switch/skills/<name>），SKILL.md 文件本身通常不是 symlink。
    本函数从**入口目录**逐跳展开 symlink，输出完整解析链与最终 SKILL.md。

    返回：
        {
          "name": str,
          "entry": 入口 SKILL.md 路径,
          "dir_chain": [入口目录 → 每跳 symlink 目标],
          "resolved": 最终真实 SKILL.md 绝对路径,
          "exists": bool（resolved 文件是否存在）,
          "is_symlink": bool,
        }
        未找到时返回 None。
    """
    from pathlib import Path as _P
    s = load_skill(name, project_root)
    if not s:
        return None
    entry_md = _P(s.path)                       # ~/.agent_go/skills/<name>/SKILL.md
    entry_dir = entry_md.parent                 # ~/.agent_go/skills/<name>
    chain = [str(entry_dir)]
    resolved_dir = entry_dir
    seen = set()
    while resolved_dir.is_symlink():
        real = resolved_dir.readlink()
        if not real.is_absolute():
            real = resolved_dir.parent / real
        # 规范化（normpath 折叠 .. 段），但保留中间 symlink 跳不做完全折叠，
        # 以展示每一跳（如 ~/.claude/skills/x 再指到 ~/.cc-switch/skills/x）
        norm = _P(os.path.normpath(str(real)))
        chain.append(str(norm))
        if str(norm) in seen:
            chain.append(f"⚠️ 循环 symlink: {norm}")
            break
        seen.add(str(norm))
        resolved_dir = norm
    final = resolved_dir / "SKILL.md"
    return {
        "name": name,
        "entry": str(entry_md),
        "dir_chain": chain,
        "resolved": str(final),
        "exists": final.exists(),
        "is_symlink": len(chain) > 1,
    }


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
    """分词：英文按单词提取，连续 CJK 按 bigram（相邻两字符对）拆分。

    中英文混排无空格分词：单字符会丢失语义，导致高频字（管理/请求/组件/系统）
    与任何中文任务都发生重叠，误配无关 skill（ISSUE-33，dogfooding 实测 Python
    修复任务误配 frontend-react）。bigram 保留部分语义（"状态管理"→"状态"+"态管"
    +"管理"），既避免单字符过度命中，也保留整串 CJK 的匹配能力。
    """
    tokens: set[str] = set()
    for m in re.finditer(r"[a-z0-9]+|[^\W\d_]+", text.lower()):
        w = m.group()
        if re.fullmatch(r"[a-z0-9]+", w):
            tokens.add(w)
        else:
            # CJK 串：按相邻字符对（bigram）拆分；长度 1 时保留单字符
            if len(w) >= 2:
                for i in range(len(w) - 1):
                    tokens.add(w[i:i + 2])
            else:
                tokens.add(w)
    return tokens


def discover_skills(task: str, project_root: Optional[Path] = None, max_skills: int = 3) -> list[Skill]:
    """根据任务描述自动匹配 Skill（关键词命中 description）。

    匹配策略（IDF 加权，消除泛词误配）：
    1. 计算每个词在**当前已安装 skill 集合**中的文档频率 df（出现在几个 skill 描述中）。
    2. 匹配分 = Σ 1/df：只出现在一个 skill 的专属词（如「风控」「银行」）得高分，
       出现在大量 skill 的泛词（如「生成」「整理」「报告」）得分趋近 0。
    3. 硬门槛：共同词 ≥2 且 IDF 分 ≥1.0（相当于至少一个专属词）。
       —— 避免「银行业智能风控报告」任务误配 lark-workflow-meeting-summary
       （报告/整理/生成 均为泛词，IDF 分 0.75 <1.0）或 byted-ark-seedance-skill。
    4. 按 IDF 分降序取前 max_skills 个。
    """
    all_skills = list_skills(project_root)
    if not all_skills:
        return []

    task_words = _tokenize_words(task.lower())
    _skip = {
        "verification", "validate", "valid", "list", "read", "write",
        "create", "file", "files", "string", "function", "functions",
        "input", "output", "task", "return", "returns", "add", "using",
        "and", "the", "a", "an", "of", "for", "in", "with", "to",
        "py", "str", "txt", "all", "any", "its", "it", "are", "is",
        "be", "by", "on", "at", "as", "or", "not", "no", "each",
    }

    # 文档频率：词 → 出现在多少个 skill description 中（本集合内计算，测试隔离）
    doc_freq: dict[str, int] = {}
    for info in all_skills:
        desc_words = _tokenize_words(info["description"].lower())
        for w in desc_words - _skip:
            if not w.isdigit():
                doc_freq[w] = doc_freq.get(w, 0) + 1

    matched = []
    for info in all_skills:
        desc_words = _tokenize_words(info["description"].lower())
        overlap = (desc_words & task_words) - _skip
        # 排除纯数字（任务编号/规则序号，无语义），防数字误配
        overlap = {w for w in overlap if not w.isdigit()}
        if len(overlap) < 2:
            continue
        # IDF 加权分：专属词（df 小）主导，泛词（df 大）趋零
        score = sum(1.0 / doc_freq[w] for w in overlap)
        if score < 1.0:
            continue
        s = load_skill(info["name"], project_root)
        if s:
            matched.append((round(score, 4), s))

    # 按 IDF 加权分降序取前 N 个
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


def _rewrite_relative_paths(body: str, skill_path: Optional[Path]) -> str:
    """将 skill body 中的相对路径引用重写为绝对路径。

    问题（symlink 实测）：lark 系列 skill 正文引用 `../lark-shared/SKILL.md` 等
    相对路径（在 claude 的 skill 目录体系中是有效的）。agent_go 把 body 注入
    TASK.md 后，这些相对路径在 worktree 中解析错误（指向仓库内不存在的目录），
    导致 claude 读不到依赖 skill。此处将 `../xxx/SKILL.md` / `./xxx.md` 形式
    的相对引用解析为基于 skill 文件真实位置（resolve 后）的绝对路径。

    仅当存在引用且能解析到真实文件时重写；否则保持原样（fail-open）。
    """
    if not body or not skill_path:
        return body
    import re as _re
    try:
        base_dir = skill_path.resolve().parent
    except OSError:
        return body

    def _replace(m: _re.Match) -> str:
        ref = m.group(0)
        rel = m.group(1)
        target = (base_dir / rel).resolve()
        if target.exists():
            return ref.replace(rel, str(target), 1)
        return ref

    # 匹配 markdown 链接 [text](../path) / 反引号 `../path` / 裸相对路径 ../path
    pattern = _re.compile(r"(?<![\w./-])((?:\.\./)+[A-Za-z0-9_\-./]+(?:\.md|/SKILL\.md))")
    return pattern.sub(_replace, body)


def render_skill_for_execution(skill: Skill, mode: str = "full") -> str:
    """将 Skill 渲染为 TASK.md 执行指令格式。

    两种注入模式（决策见 docs/design/skill-injection-strategy.md）：
    - mode="full"（默认）：注入完整 body + 相对路径重写。用于**无原生 skill 机制的
      后端**（agent_loop / 未来非 claude worker），必须由编排层提供全部知识。
    - mode="guide"：轻量指引——只给 skill 名、领域、**绝对路径**，让 claude 自主
      读取 ~/.claude/skills/<name>/SKILL.md 并按触发条件判断。避免编排层的关键词
      匹配（弱于 claude 语义理解）误导 worker，也避免完整 body 塞入 TASK.md 造成
      上下文浪费与相对路径失效。用于 **claude worker**（claude 原生可读 skill）。
    """
    if mode == "guide":
        lines = [
            f"## Skill 知识注入: {skill.name}（轻量指引）",
        ]
        if skill.description:
            lines.append(f"**领域**: {skill.description}")
        if skill.allowed_tools:
            lines.append(f"**推荐工具**: {', '.join(skill.allowed_tools)}")
        # 绝对路径：skill.path 可能是 symlink 目录（~/.agent_go/skills/<name> →
        # ~/.claude/skills/<name>），resolve() 得到真实目录；再拼 SKILL.md 供 claude Read。
        real_dir = skill.path.resolve() if skill.path else Path("")
        real_file = real_dir / "SKILL.md" if real_dir.suffix != ".md" else real_dir
        lines.extend([
            "",
            f"**Skill 文件**: `{real_file}`",
            "请先使用 Read 工具读取上述 Skill 文件，按其指令执行本子任务。",
            "若文件无法读取或与本任务无关，可忽略并继续。",
        ])
        return "\n".join(lines)

    lines = [
        f"## Skill 知识注入: {skill.name}",
    ]
    if skill.description:
        lines.append(f"**领域**: {skill.description}")
    if skill.allowed_tools:
        lines.append(f"**推荐工具**: {', '.join(skill.allowed_tools)}")
    lines.append("")
    body = _rewrite_relative_paths(skill.body, skill.path)
    lines.append(body)
    return "\n".join(lines)
