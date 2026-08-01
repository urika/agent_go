"""Task Spec 解析与准入审查（S11-P0）。

Task Spec 是 agent_go 的结构化输入契约（SDD 的工程实现）。一个 Markdown 文件，
按 7 个章节组织，被解析后注入 Plan prompt 的不同位置。

章节规范（带 * 为 L1 硬门禁必填）：
    ## 1. 目标（做什么）          *
    ## 2. 动机（为什么）          *
    ## 3. 范围（动哪里，不动哪里）  *
    ## 4. 约束
    ## 5. 验收标准（怎么算做完）    *
    ## 6. 参考资料
    ## 7. 已知风险

L1 硬门禁（确定性检查，0 误判，阻断执行）：
    1. 必填章节完整性（§1/§2/§3/§5 必须存在且非空）
    2. 文件路径有效性（Spec 引用的路径在 repo 中存在，或最近似匹配）
    3. 验证命令白名单（Spec 中的验证命令遵循 SAFE_VERIFICATION_PREFIXES）
    4. 章节长度下限（防敷衍：「修 bug」之类过短目标）

L1 通过后返回结构化 TaskSpec，由 generate_plan 注入 prompt。
"""

import re
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import SAFE_VERIFICATION_PREFIXES

__all__ = [
    "TaskSpec",
    "SpecViolation",
    "parse_spec",
    "validate_spec_l1",
    "render_spec_template",
    "extract_file_paths",
]

logger = logging.getLogger(__name__)

# 7 个章节的匹配模式：## N. 名称（容许中英文标点、可选星号标记）
# 章节以「## 」开头的标题行界定，内容延续到下一个「## 」标题或文件末尾。
_SECTION_KEYS = {
    "goal": "1",
    "motivation": "2",
    "scope": "3",
    "constraint": "4",
    "acceptance": "5",
    "reference": "6",
    "risk": "7",
}

# 每个章节可接受的中英文标题别名
_SECTION_ALIASES = {
    "1": ["目标", "goal", "做什么"],
    "2": ["动机", "motivation", "为什么", "背景", "why"],
    "3": ["范围", "scope", "动哪里"],
    "4": ["约束", "constraint", "设计约束"],
    "5": ["验收", "acceptance", "验收标准", "怎么算做完", "完成标准"],
    "6": ["参考", "reference", "参考资料", "references"],
    "7": ["风险", "risk", "已知风险"],
}

# L1 必填章节
_REQUIRED_SECTIONS = ["1", "2", "3", "5"]

# 章节长度下限（字符数，去空白后）
_MIN_LENGTH = {
    "1": 20,  # 目标
    "2": 15,  # 动机
    "3": 30,  # 范围
    "5": 20,  # 验收标准
}


@dataclass
class TaskSpec:
    """解析后的 Task Spec。"""

    title: str = ""
    # 7 章节内容（原始 Markdown 文本，已 strip）
    goal: str = ""           # §1 目标 *
    motivation: str = ""     # §2 动机 *
    scope: str = ""          # §3 范围 * （含「需要改动」和「明确不动」）
    constraint: str = ""     # §4 约束
    acceptance: str = ""     # §5 验收标准 *
    reference: str = ""      # §6 参考资料
    risk: str = ""           # §7 已知风险
    source_path: Optional[Path] = None

    @property
    def is_complete(self) -> bool:
        """L1 必填章节是否都非空。"""
        return all(getattr(self, k) for k in ("goal", "motivation", "scope", "acceptance"))


@dataclass
class SpecViolation:
    """L1 硬门禁的单项违规。"""

    check: str          # 检查项标识：required / path / whitelist / length
    section: str        # 相关章节号（如 "1"）或 ""
    message: str        # 人可读说明
    suggestion: str = ""  # 修复建议（如最近似文件名）


# ─── 解析 ────────────────────────────────────────────────────────────

def _split_sections(text: str) -> dict[str, str]:
    """把 Markdown 按「## 」二级标题切成 {章节号: 内容}。

    容错：标题行可能是「## 1. 目标」「## 1 目标」「## 目标」「## 1.目标」。
    通过遍历别名表把标题映射回章节号 1-7。
    """
    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    current_key: Optional[str] = None
    title_line = ""

    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            heading = m.group(1).strip().lower()
            current_key = _match_section_key(heading)
            if current_key:
                sections.setdefault(current_key, [])
                # 标题行本身不纳入内容
                continue
            # 不是已知章节标题 → 当作正文归入当前章节（容错）
            if current_key:
                sections[current_key].append(line)
        else:
            # 一级标题 # Title 作为整个 Spec 的 title
            tm = re.match(r"^#\s+(.+?)\s*$", line)
            if tm and current_key is None:
                title_line = tm.group(1).strip()
                continue
            if current_key:
                sections[current_key].append(line)

    result = {k: "\n".join(v).strip() for k, v in sections.items()}
    result["__title__"] = title_line
    return result


def _match_section_key(heading: str) -> Optional[str]:
    """把标题文本映射回章节号 1-7。先匹配数字前缀，再匹配别名。"""
    # 「1.」「1、」「1 」等数字前缀
    num_m = re.match(r"^(\d)\s*[.、:：\s]?\s*(.*)$", heading)
    if num_m:
        num = num_m.group(1)
        if num in _SECTION_ALIASES:
            return num
    # 别名匹配
    for num, aliases in _SECTION_ALIASES.items():
        for alias in aliases:
            if alias in heading:
                return num
    return None


def parse_spec(text_or_path) -> Optional[TaskSpec]:
    """解析 Task Spec 文本或文件路径，返回 TaskSpec。

    Args:
        text_or_path: str（Markdown 文本）或 Path/str 路径（读取文件）

    Returns:
        TaskSpec，或 None（文件不存在/读取失败）
    """
    source_path = None
    if isinstance(text_or_path, Path):
        source_path = text_or_path
        if not text_or_path.exists():
            logger.warning("Spec 文件不存在: %s", text_or_path)
            return None
        text = text_or_path.read_text(encoding="utf-8", errors="replace")
    elif isinstance(text_or_path, str) and (
        "\n" not in text_or_path and len(text_or_path) < 500 and "/" in text_or_path or text_or_path.endswith(".md")
    ):
        # 看起来像路径（短、含 / 或 .md 后缀、无换行）
        p = Path(text_or_path)
        if p.exists():
            source_path = p
            text = p.read_text(encoding="utf-8", errors="replace")
        else:
            text = text_or_path
    else:
        text = str(text_or_path)

    sections = _split_sections(text)
    title = sections.pop("__title__", "")

    spec = TaskSpec(
        title=title,
        goal=sections.get("1", ""),
        motivation=sections.get("2", ""),
        scope=sections.get("3", ""),
        constraint=sections.get("4", ""),
        acceptance=sections.get("5", ""),
        reference=sections.get("6", ""),
        risk=sections.get("7", ""),
        source_path=source_path,
    )
    return spec


# ─── 文件路径提取 ─────────────────────────────────────────────────────

# 反引号或引号包裹的路径，或常见代码路径模式（src/...、tests/...）
_PATH_PATTERN = re.compile(
    r"`([^`]+\.\w+)`"              # `path/to/file.py`
    r"|(?<![\w/])(\.?[\w\-./]*?/[\w\-./]+\.\w{1,6})(?![\w])"  # 含 / 的文件路径
)


def extract_file_paths(text: str) -> list[str]:
    """从文本中提取看起来像文件路径的字符串（去重，保序）。"""
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_PATTERN.finditer(text):
        path = (m.group(1) or m.group(2) or "").strip().strip("`").strip("'").strip('"')
        # 过滤明显不是路径的（无扩展名或太短）
        if not path or "." not in path or len(path) < 3:
            continue
        # 排除 URL 和绝对路径：相对文件路径不以 / 开头，且不含 URL scheme
        if path.startswith("/") or "://" in path or "@" in path:
            continue
        # 排除 URL 的一部分：检查匹配位置前后的窗口是否含 URL scheme
        window = text[max(0, m.start() - 10):m.start() + 5]
        if "://" in window:
            continue
        if path not in seen:
            seen.add(path)
            found.append(path)
    return found


def _repo_file_set(repo: Optional[Path]) -> set[str]:
    """获取 repo 中已跟踪的文件集合（git ls-files），用于路径校验。"""
    if repo is None or not repo.exists():
        return set()
    try:
        if (repo / ".git").exists():
            r = subprocess.run(
                ["git", "ls-files"], cwd=str(repo),
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return {f.strip() for f in r.stdout.splitlines() if f.strip()}
        return set()
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("git ls-files 失败: %s", e)
        return set()


def _fuzzy_match(path: str, file_set: set[str]) -> Optional[str]:
    """路径不在 file_set 中时，找最近似的一个（文件名匹配）。"""
    if not file_set:
        return None
    basename = Path(path).name
    if not basename:
        return None
    # 精确文件名匹配
    candidates = [f for f in file_set if Path(f).name == basename]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 取路径最相似的
        return min(candidates, key=lambda f: _edit_distance(path, f))
    # 包含文件名的
    contains = [f for f in file_set if basename in f]
    if contains:
        return min(contains, key=lambda f: _edit_distance(path, f))
    # 编辑距离回退：在 basename 级别找最近的（如 usr.py → user.py）
    same_ext = [f for f in file_set if Path(f).suffix == Path(basename).suffix]
    pool = same_ext or list(file_set)
    best = None
    best_d = None
    for f in pool:
        d = _edit_distance(basename, Path(f).name)
        # 阈值：编辑距离不超过 basename 长度的 40%（避免无关匹配）
        if d <= max(2, len(basename) * 0.4):
            if best_d is None or d < best_d:
                best_d, best = d, f
    return best


def _edit_distance(a: str, b: str) -> int:
    """简单 Levenshtein 距离（路径较短，O(n*m) 可接受）。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = cur
    return prev[-1]


# ─── 验证命令白名单检查 ───────────────────────────────────────────────

# 验证命令的常见出现形式：代码块里的 shell 命令、行内的反引号命令
_CMD_BLOCK = re.compile(r"```(?:bash|shell|sh)?\s*\n(.*?)```", re.DOTALL)
_CMD_INLINE = re.compile(r"`((?:python|pytest|npm|yarn|cargo|go|make|ruby|rspec|rubocop)\s[^`]+)`")


def _extract_verification_commands(text: str) -> list[str]:
    """从验收标准/约束文本中提取候选验证命令。"""
    cmds: list[str] = []
    for block in _CMD_BLOCK.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
    cmds.extend(_CMD_INLINE.findall(text))
    return cmds


def _cmd_matches_whitelist(cmd: str) -> bool:
    """检查单条命令是否匹配安全白名单前缀。"""
    cmd = cmd.strip()
    if not cmd:
        return True
    # 去掉前导 env 赋值（如 FOO=bar python ...）
    cmd = re.sub(r"^[A-Z_][A-Z0-9_]*=\S+\s+", "", cmd)
    for prefix in SAFE_VERIFICATION_PREFIXES:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True
    return False


# ─── L1 硬门禁 ────────────────────────────────────────────────────────

def validate_spec_l1(spec: TaskSpec, repo: Optional[Path] = None) -> list[SpecViolation]:
    """L1 硬门禁：4 项确定性检查。返回违规列表（空列表 = 通过）。

    Args:
        spec: 解析后的 TaskSpec
        repo: 仓库路径（用于文件路径校验；None 时跳过路径检查）

    Returns:
        SpecViolation 列表。空列表表示全部通过。
    """
    violations: list[SpecViolation] = []

    # 检查 1：必填章节完整性
    section_values = {
        "1": spec.goal, "2": spec.motivation, "3": spec.scope, "5": spec.acceptance,
    }
    section_names = {"1": "目标", "2": "动机", "3": "范围", "5": "验收标准"}
    for num, val in section_values.items():
        if not val.strip():
            violations.append(SpecViolation(
                check="required",
                section=num,
                message=f"必填章节缺失或为空：§{num} {section_names[num]}",
            ))

    # 检查 2：章节长度下限（仅对已填的必填章节检查，避免与检查 1 重复报错）
    field_map = {"1": spec.goal, "2": spec.motivation, "3": spec.scope, "5": spec.acceptance}
    for num, val in field_map.items():
        if val.strip() and len(val.strip()) < _MIN_LENGTH[num]:
            violations.append(SpecViolation(
                check="length",
                section=num,
                message=f"§{num} {section_names[num]} 内容过短（{len(val.strip())} < {_MIN_LENGTH[num]} 字符），疑似敷衍",
            ))

    # 检查 3：文件路径有效性（§3 范围 + §4 约束中引用的路径）
    if repo is not None:
        file_set = _repo_file_set(repo)
        if file_set:
            scope_text = f"{spec.scope}\n{spec.constraint}"
            for path in extract_file_paths(scope_text):
                if path not in file_set:
                    suggestion = _fuzzy_match(path, file_set) or ""
                    violations.append(SpecViolation(
                        check="path",
                        section="3",
                        message=f"Spec 引用的文件路径在仓库中不存在：{path}",
                        suggestion=f"最接近的匹配：{suggestion}" if suggestion else "请确认路径或仓库范围",
                    ))

    # 检查 4：验证命令白名单（§5 验收标准 + §4 约束中的命令）
    cmd_text = f"{spec.acceptance}\n{spec.constraint}"
    for cmd in _extract_verification_commands(cmd_text):
        if not _cmd_matches_whitelist(cmd):
            violations.append(SpecViolation(
                check="whitelist",
                section="5",
                message=f"验证命令不在安全白名单内：{cmd}",
                suggestion=f"允许的前缀包括：{', '.join(SAFE_VERIFICATION_PREFIXES[:8])}{' ...' if len(SAFE_VERIFICATION_PREFIXES) > 8 else ''}",
            ))

    return violations


# ─── Spec 模板生成 ────────────────────────────────────────────────────

_SPEC_TEMPLATE = """# Task Spec: <任务名称>

> 生成于 {timestamp} | 仓库: {repo}
> agent_go 输入契约（SDD）。带 * 的章节为必填（L1 准入审查）。

## 1. 目标（做什么）*

<一段话描述这个任务要达成的最终效果>

## 2. 动机（为什么）*

<为什么要做。关联 Issue / PRD 章节在此引用。>

## 3. 范围（动哪里，不动哪里）*

### 需要改动的文件/模块
{scope_change_hint}

### 明确不动的区域
<- 明确列出禁止改动的模块，阻止 Planner 猜测 ->

## 4. 约束

<技术约束、设计约束、兼容性要求。如：迁移必须可回滚、不新增依赖、保持某 API 签名不变。>

## 5. 醇收标准（怎么算做完）*

<可自动化判定的验收条件，尽量能写成验证命令。例：
- [ ] `pytest tests/test_xxx.py -v` 全绿
- [ ] 新字段默认值为 False>

## 6. 参考资料

<设计文档链接、类似实现的 commit hash、相关 Issue 编号。>

## 7. 已知风险

<用户已知的风险点。如：大表迁移锁表、兼容性问题。Planner 会在分解时考虑。>
"""


def render_spec_template(repo: Optional[Path] = None) -> str:
    """生成空白 Task Spec 模板。

    如果提供 repo，预填「需要改动」区域的模块/文件列表提示。
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d")
    repo_str = str(repo) if repo else "<repo-path>"

    if repo is not None and repo.exists():
        try:
            if (repo / ".git").exists():
                r = subprocess.run(
                    ["git", "ls-files"], cwd=str(repo),
                    capture_output=True, text=True, timeout=5,
                )
                files = [f for f in r.stdout.splitlines() if f.strip()][:30] if r.returncode == 0 else []
            else:
                files = []
            if files:
                # 按顶层目录分组，取最常见的前几个
                top_dirs: dict[str, int] = {}
                for f in files:
                    top = f.split("/")[0] if "/" in f else "."
                    top_dirs[top] = top_dirs.get(top, 0) + 1
                dirs_sorted = sorted(top_dirs.items(), key=lambda x: -x[1])[:8]
                hint_lines = ["# 仓库主要目录（参考，按需修改）："]
                for d, n in dirs_sorted:
                    hint_lines.append(f"#   {d}/  ({n} 文件)")
                hint_lines.append("# ")
                hint_lines.append("<- 在此列出本次需要改动的具体文件，如：src/models/user.py ->")
                scope_hint = "\n".join(hint_lines)
            else:
                scope_hint = "<- 在此列出本次需要改动的文件 ->"
        except (FileNotFoundError, subprocess.SubprocessError):
            scope_hint = "<- 在此列出本次需要改动的文件 ->"
    else:
        scope_hint = "<- 在此列出本次需要改动的文件 ->"

    # 修正模板中的笔误（醇收 -> 验收）
    template = _SPEC_TEMPLATE.replace("醇收标准", "验收标准")
    return template.format(timestamp=timestamp, repo=repo_str, scope_change_hint=scope_hint)
