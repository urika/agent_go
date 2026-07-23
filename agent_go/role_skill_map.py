import json
import logging
from pathlib import Path
from fnmatch import fnmatch
from typing import Any, Optional


from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

__all__ = ["load_role_skill_map", "apply_rules"]

DEFAULT_MAP = {
    "rules": [
        {
            "match": {"agent_type": "tester"},
            "skills": {"required": [], "recommended": ["tdd-workflow", "test-coverage"]}
        },
        {
            "match": {"keywords": ["安全", "security", "auth", "认证", "权限", "加密"]},
            "skills": {"required": ["security-review"], "recommended": []}
        },
        {
            "match": {"keywords": ["审查", "review", "audit"]},
            "skills": {"required": [], "recommended": ["code-review"]},
            "agent_type": "reviewer"
        },
        {
            "match": {"keywords": ["架构", "设计", "architect", "design", "分析"]},
            "skills": {"required": [], "recommended": []},
            "agent_type": "architect"
        },
        {
            "match": {"file_patterns": ["*.md", "*.rst", "*.txt"]},
            "skills": {"required": [], "recommended": []},
            "agent_type": "architect"
        }
    ],
    "default_agent_type": "developer",
    "recommended_agents": ["developer", "architect", "reviewer", "tester"],
    "recommended_skills": []
}


def _global_map_path() -> Path:
    return AGENT_GO_DIR / "role_skill_map.json"


def _project_map_path(project_root: Path) -> Path:
    if project_root is None:
        return None
    return Path(project_root) / ".agent_go" / "role_skill_map.json"


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to load JSON from %s: %s", path, e)
    return None


def load_role_skill_map(project_root: Optional[Path] = None) -> dict[str, Any]:
    """加载角色-Skill 映射规则，三层合并：项目级 > 全局 > 内置默认。

    - 项目级：`<project_root>/.agent_go/role_skill_map.json`
    - 全局：`~/.agent_go/role_skill_map.json`
    - 内置：`DEFAULT_MAP`

    规则列表按 项目 → 全局 → 默认 顺序拼接（apply_rules 中先匹配到的
    agent_type 优先，因此更具体的层级天然优先）；标量键
    （default_agent_type / recommended_agents / recommended_skills）
    由更具体的层级整体覆盖。
    """
    project_map = _load_json(_project_map_path(project_root))
    global_map = _load_json(_global_map_path())

    merged: dict[str, Any] = dict(DEFAULT_MAP)
    rules: list[dict[str, Any]] = []
    for layer in (project_map, global_map, DEFAULT_MAP):
        if layer:
            rules.extend(layer.get("rules", []))
    merged["rules"] = rules
    for layer in (global_map, project_map):
        if layer:
            for key in ("default_agent_type", "recommended_agents", "recommended_skills"):
                if key in layer:
                    merged[key] = layer[key]
    return merged


def _match_rule(rule: dict[str, Any], step: dict[str, Any]) -> bool:
    cond = rule.get("match", {})

    if "agent_type" in cond:
        if step.get("agent_type", "").lower() != cond["agent_type"].lower():
            return False

    if "keywords" in cond:
        title = step.get("title", "")
        desc = step.get("description", "")
        combined = f"{title} {desc}".lower()
        if not any(kw.lower() in combined for kw in cond["keywords"]):
            return False

    if "file_patterns" in cond:
        files = step.get("files", [])
        if not files:
            return False
        if not any(any(fnmatch(f, pat) for pat in cond["file_patterns"]) for f in files):
            return False

    return True


def match_rules(step: dict[str, Any], role_map: dict[str, Any]) -> list[dict[str, Any]]:
    rules = role_map.get("rules", [])
    return [r for r in rules if _match_rule(r, step)]


def apply_rules(step: dict[str, Any], role_map: dict[str, Any], installed_skills: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    installed_names = {s["name"] for s in (installed_skills or [])}
    matched = match_rules(step, role_map)

    required_skills = []
    recommended_skills = []
    matched_agent_type = None

    for rule in matched:
        skills = rule.get("skills", {})
        for sk in skills.get("required", []):
            if sk in installed_names and sk not in required_skills:
                required_skills.append(sk)
        for sk in skills.get("recommended", []):
            if sk in installed_names and sk not in recommended_skills:
                recommended_skills.append(sk)
        if rule.get("agent_type") and not matched_agent_type:
            matched_agent_type = rule["agent_type"]

    llm_skills = step.get("skills", [])
    merged_skills = list(llm_skills)
    for sk in required_skills:
        if sk not in merged_skills:
            merged_skills.append(sk)

    has_llm_specified = bool(llm_skills)
    for sk in recommended_skills:
        if sk not in merged_skills and not has_llm_specified:
            if len(merged_skills) < 2:
                merged_skills.append(sk)

    agent_type = step.get("agent_type") or matched_agent_type or role_map.get("default_agent_type", "developer")

    return {
        "skills": merged_skills,
        "agent_type": agent_type,
        "required_skills": required_skills,
        "matched_rules": [r.get("match", {}) for r in matched],
    }
