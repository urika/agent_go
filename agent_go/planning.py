"""规划阶段辅助：任务耗时预估（M4）。

从 eval.py 迁移而来——estimate_task_duration 逻辑上是「预执行估算」（在线、嵌入 cmd_run），
不是「离线评估」，放在 eval.py 会让核心流程（cli.cmd_run）反向依赖评估模块，违背解耦原则
（核心不依赖增强/评估）。本模块自包含，不 import eval.py，避免循环依赖。

依赖契约：读取 ~/.agent_go/task-*/meta.json 的 results[].duration_sec（核心管线写入）。
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

__all__ = ["estimate_task_duration", "check_under_decomposition",
           "check_over_decomposition", "check_difficulty_mismatch", "difficulty_hint",
           "check_agent_prompt_functions", "validate_plan_quality",
           "check_subtask_file_overlap", "check_parallel_import_relations",
           "_subtask_file_scope"]

logger = logging.getLogger(__name__)

# 无历史数据时的默认子任务耗时（秒），与 eval.py 保持一致
_DEFAULT_SUBTASK_SEC = 240

# S12-P2 G5 V1：hard 任务的最小合理子任务数阈值（硬编码，V2 从 verify_state.json 历史学习）
DIFFICULTY_BASE_SUBTASKS = {"easy": 1, "medium": 2, "hard": 3}


def _subtask_file_scope(st: dict) -> set[str]:
    """提取子任务的文件作用域（files 字段 ∪ files_hint 逗号拆分，`*` 忽略）。

    与 scope_conflict 检查不同，跨子任务文件重叠检测必须同时看 files 与
    files_hint（否则 tester 等仅用 files_hint 的场景检测不到真实重叠）。
    """
    files: set[str] = set()
    for f in st.get("files", []) or []:
        if isinstance(f, str) and f:
            files.add(f)
    hint = str(st.get("files_hint", "") or "")
    if hint and hint != "*":
        files.update(part.strip() for part in hint.split(",") if part.strip())
    return files


def _has_dependency_path(graph: dict[str, list[str]], start: str, target: str) -> bool:
    """判断 start 是否可沿 depends_on 传递闭包到达 target（含 start == target）。"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node == target:
            return True
        stack.extend(graph.get(node, []))
    return False


def check_subtask_file_overlap(subtasks: list[dict]) -> dict[str, Any]:
    """G7 跨子任务文件重叠检测：无依赖路径的子任务共享同一文件 → 交叉污染高风险。

    bench 实证：task-20260809-123021-784-042c 拆 2 个后 sub-2 越界改
    cli.py+storage.py+models.py，与 sub-1 的 storage.py 重叠 → 交叉污染 →
    VERIFICATION_FAILED。opencode / Claude Code 官方最佳实践均为
    「分解工作使每个队友负责不同的文件集」。

    判定：
    - 某文件被 ≥2 个子任务引用，且其中至少一对子任务之间无依赖路径
      （无法保证顺序执行）→ blocking issue（file_overlap_without_dependency）。
    - 所有共享该文件的子任务都在同一条依赖链上（顺序执行，upstream 会 merge）
      → warning（file_overlap_with_dependency），有集成风险但不阻断。

    Returns:
        {"issues": [...], "warnings": [...]}
    """
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    graph: dict[str, list[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        graph[sid] = [str(dep) for dep in st.get("depends_on", [])]

    file_owners: dict[str, list[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        for f in _subtask_file_scope(st):
            file_owners.setdefault(f, []).append(sid)

    for file, owners in sorted(file_owners.items()):
        if len(owners) < 2:
            continue
        # 判断所有 owner 是否都在同一条依赖链上（任意一对双向可达即顺序保证）
        all_chained = all(
            _has_dependency_path(graph, a, b) or _has_dependency_path(graph, b, a)
            for i, a in enumerate(owners)
            for b in owners[i + 1:]
        )
        entry = {
            "type": "file_overlap_with_dependency" if all_chained else "file_overlap_without_dependency",
            "file": file,
            "subtask_ids": owners,
            "reason": (
                f"文件 {file} 被子任务 {'/'.join(owners)} 共同引用" +
                ("，且无依赖顺序——并行修改同一文件必然交叉污染，建议合并或添加依赖。"
                 if not all_chained else
                 "，子任务在同一依赖链上（顺序执行）。仍有集成风险，建议确认改动区域不重叠。")
            ),
        }
        if all_chained:
            warnings.append(entry)
        else:
            issues.append(entry)

    return {"issues": issues, "warnings": warnings}


def _parse_imports(file_text: str) -> set[str]:
    """从源码文本提取 import 的模块路径（正则，零 AST 依赖，容错更强）。

    覆盖：import a.b.c / from a.b import c / import a.b as x / from a import (b, c)。
    返回模块路径集合（a.b.c 的顶级 a.b 前缀也计入，便于模糊匹配）。
    """
    mods: set[str] = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([\w.]+)", file_text, re.MULTILINE):
        mod = m.group(1).strip()
        if mod:
            mods.add(mod)
            # 记录顶级前缀（from a.b.c import d → a.b.c 与 a.b 与 a）
            parts = mod.split(".")
            for i in range(1, len(parts)):
                mods.add(".".join(parts[:i]))
    return mods


def check_parallel_import_relations(subtasks: list[dict], repo: Path) -> list[dict]:
    """改进 C（轻量版）：并行 wave 内的跨文件 import 关系 warning。

    背景（workflow-vs-subagent-review.md）：完整合约冻结依赖 LLM 标记
    contracts_provided/consumed，标记错了更糟。降级为机械规则：对**无依赖路径**
    的子任务对（并行执行，无法保证顺序），若 A 修改的文件被 B 修改的文件 import
    （或反之），说明存在跨 worktree 的接口耦合——各自验证在自己 worktree 中通过，
    但合并后可能因接口不一致而冲突。

    纯 AST/regex，零 LLM 依赖。只在 repo 存在时生效（需读源码）。返回 warning 列表。

    Returns:
        list[dict]: {"type": "parallel_import_relation", "from_subtask", "to_subtask",
                     "imported_file", "importing_file", "reason"}
    """
    if not repo or not subtasks:
        return []

    graph: dict[str, list[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        graph[sid] = [str(dep) for dep in st.get("depends_on", [])]

    # 收集每个子任务修改文件的 import 集（缓存，避免重复读盘）
    file_imports: dict[str, set[str]] = {}
    subtask_files: dict[str, set[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        files = _subtask_file_scope(st)
        subtask_files[sid] = files
        for f in files:
            if f in file_imports:
                continue
            fpath = repo / f
            try:
                if fpath.is_file():
                    file_imports[f] = _parse_imports(fpath.read_text(encoding="utf-8", errors="replace"))
                else:
                    file_imports[f] = set()
            except OSError:
                file_imports[f] = set()

    warnings: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for a in subtasks:
        for b in subtasks:
            sid_a, sid_b = str(a.get("id", "")), str(b.get("id", ""))
            if sid_a >= sid_b or (sid_a, sid_b) in seen_pairs:
                continue
            # 只在无依赖路径的并行对之间检查（同链顺序执行 → 上游 merge 已保证一致性）
            if _has_dependency_path(graph, sid_a, sid_b) or _has_dependency_path(graph, sid_b, sid_a):
                continue
            seen_pairs.add((sid_a, sid_b))

            for fa in subtask_files.get(sid_a, set()):
                for fb in subtask_files.get(sid_b, set()):
                    if fa == fb:
                        continue
                    # A 修改的文件被 B 修改的文件 import
                    if file_imports.get(fb) and _file_imports_module(fa, file_imports[fb]):
                        warnings.append({
                            "type": "parallel_import_relation",
                            "from_subtask": sid_a,
                            "to_subtask": sid_b,
                            "imported_file": fa,
                            "importing_file": fb,
                            "reason": (f"子任务 {sid_a} 修改 {fa}，而 {sid_b} 修改的 {fb} 引用它——"
                                       f"两者并行执行无依赖顺序，若接口不一致合并后可能冲突。"
                                       f"建议确认接口契约，或添加依赖串行化。"),
                        })
                        break
                else:
                    continue
                break
    return warnings


def _file_imports_module(target_file: str, import_mods: set[str]) -> bool:
    """判断目标文件路径是否被 import 模块集合引用。

    匹配规则（模糊但低误报）：
    - target='src/blog/views.py' ↔ import 'src.blog.views' / 'src.blog' / 'blog.views'
    - target='src/models.py' ↔ import 'src.models' / 'models'
    归一化：文件路径 '/' → '.'，去 .py 后缀，与 import 模块路径比对。
    """
    # 归一化文件路径为模块形式：src/blog/views.py → src.blog.views；去掉 __init__
    target_mod = target_file.replace("\\", "/").lstrip("./").replace("/", ".").rstrip(".py")
    if target_mod.endswith(".__init__"):
        target_mod = target_mod[: -len(".__init__")]
    for mod in import_mods:
        mod_norm = mod.strip().replace("/", ".")
        if mod_norm == target_mod:
            return True
        # import 是目标的前缀（src.blog → src.blog.views）或目标的子模块
        if target_mod.startswith(mod_norm + "."):
            return True
        # import 是目标的非顶级子模块（views → src.blog.views）
        if target_mod.endswith("." + mod_norm):
            return True
    return False


def validate_plan_quality(
    subtasks: list[dict],
    requirements: Optional[list[str]] = None,
    repo: Optional[Path] = None,
) -> dict[str, Any]:
    """Run deterministic pre-execution checks on the generated Plan.

    Args:
        subtasks: Plan 拆解后的子任务列表
        requirements: Task Spec 的需求 ID 列表
        repo: 可选的项目根目录（启用 P2 agent_prompt 函数引用检查）
    """
    from .utils import _is_safe_verification_command

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    graph: dict[str, list[str]] = {}
    covered: set[str] = set()
    for st in subtasks:
        sid = str(st.get("id", ""))
        seen_ids.add(sid)
        graph[sid] = [str(dep) for dep in st.get("depends_on", [])]
        command = str(st.get("verification", "") or "")
        if command:
            safe, reason = _is_safe_verification_command(command)
            if not safe:
                warnings.append({"type": "verification_command_rejected", "subtask_id": sid, "reason": reason})
        else:
            warnings.append({"type": "missing_verification", "subtask_id": sid})
        files = set(st.get("files", []) or [])
        forbidden = set(st.get("do_not_touch", []) or [])
        overlap = sorted(files & forbidden)
        if overlap:
            issues.append({"type": "scope_conflict", "subtask_id": sid, "files": overlap})
        covered.update(str(value) for value in st.get("acceptance_criteria_ids", []) or [])
        covered.update(str(value) for value in st.get("requirement_ids", []) or [])

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            issues.append({"type": "dependency_cycle", "subtask_id": node})
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep in seen_ids:
                visit(dep)
        visiting.remove(node)
        visited.add(node)

    for sid in graph:
        visit(sid)

    requirements = [str(value) for value in (requirements or []) if str(value)]
    unique_requirements = set(requirements)
    coverage = None if not unique_requirements else round(len(unique_requirements & covered) / len(unique_requirements), 6)
    if unique_requirements and coverage < 1.0:
        issues.append({"type": "requirement_coverage_incomplete", "missing": sorted(unique_requirements - covered)})

    # G8: 独立可验证性检查（Split Design Benchmark 实证：claude/opencode 均以
    # 「能否独立验证」作为拆分/合并判据，如 storage 新方法脱离 cmd_done 无法验证 → 必合）
    # 检查 1：被依赖的子任务若无验证命令，其产物无法独立验证 → 下游依赖不可信
    for st in subtasks:
        sid = str(st.get("id", ""))
        if str(st.get("verification", "") or "").strip():
            continue
        dependents = [other.get("id") for other in subtasks
                      if sid in {str(d) for d in other.get("depends_on", [])}]
        if dependents:
            issues.append({
                "type": "unverifiable_upstream",
                "subtask_id": sid,
                "depended_by": dependents,
                "reason": (f"子任务 {sid} 无验证命令，但被 {'/'.join(dependents)} 依赖——"
                           f"上游产物未经验证即被下游消费，建议为 {sid} 补充验证或与下游合并。"),
            })

    # G7: 跨子任务文件重叠检测（bench 实证的交叉污染根因）
    overlap = check_subtask_file_overlap(subtasks)
    issues.extend(overlap["issues"])
    warnings.extend(overlap["warnings"])

    # G6 升级：小改动（有效文件数 ≤2）但 ≥3 子任务 → 过度分解 → blocking
    # （bench 实证：fix-missing-default 5 行改动拆 3 个 → 成本翻倍且失败）
    if len(subtasks) >= 3:
        all_files = _subtask_file_scope_all(subtasks)
        if 0 < len(all_files) <= 2:
            issues.append({
                "type": "over_decomposition",
                "subtask_ids": [str(st.get("id", "")) for st in subtasks],
                "file_count": len(all_files),
                "reason": (f"{len(subtasks)} 个子任务仅涉及 {len(all_files)} 个文件——"
                           f"小改动不应过度拆分，串行会叠加延迟且成本翻倍。建议合并为 1 个子任务。"),
            })

    # G8 扩展：verification 与自身改动文件匹配校验
    # Split Design Benchmark 观察到：step1 改 cli.py 却验证 tests/test_storage.py
    # （其他 step 的专属文件）→ verification 与改动不匹配。校验 verification 引用的
    # 测试文件是否属于本 step 的文件作用域；若验证了其他 step 专属文件 → warning。
    _verification_file_re = re.compile(r"(?:^|\s)(tests?[\w./\-]*\.py)")
    _all_owners: dict[str, list[str]] = {}
    for st in subtasks:
        sid = str(st.get("id", ""))
        for f in _subtask_file_scope(st):
            _all_owners.setdefault(f, []).append(sid)
    for st in subtasks:
        sid = str(st.get("id", ""))
        command = str(st.get("verification", "") or "")
        if not command:
            continue
        _v_files = set(m.group(1) for m in _verification_file_re.finditer(command))
        if not _v_files:
            continue
        own_scope = _subtask_file_scope(st)
        for vf in sorted(_v_files):
            # 规范化：去掉 ./ 前缀、test 目录归属
            norm = vf[2:] if vf.startswith("./") else vf
            if norm in own_scope:
                continue
            owners = _all_owners.get(norm, [])
            # 验证了其他 step 专属文件（且本 step 未声明该文件）→ 依赖缺失
            if owners and sid not in owners:
                warnings.append({
                    "type": "verification_file_mismatch",
                    "subtask_id": sid,
                    "verified_file": norm,
                    "owned_by": owners,
                    "reason": (f"子任务 {sid} 的验证命令引用了 {norm}，但该文件属于 "
                               f"{'/'.join(owners)}——若依赖其产物，请补充 depends_on 或将该文件加入 "
                               f"本步骤的 files；否则请改用本步骤相关的验证文件。"),
                })

    # P2: agent_prompt 函数引用检查（需要 repo 路径）
    if repo:
        agent_prompt_warnings = check_agent_prompt_functions(subtasks, repo)
        warnings.extend(agent_prompt_warnings)

    # 改进 C（轻量版）：并行 wave 内跨文件 import 关系 warning（零 LLM 依赖）
    if repo:
        warnings.extend(check_parallel_import_relations(subtasks, repo))

    return {
        "status": "blocked" if issues else ("warning" if warnings else "passed"),
        "blocking_issues": issues,
        "warnings": warnings,
        "plan_conflict_count": sum(1 for issue in issues if issue["type"] in {
            "scope_conflict", "dependency_cycle", "file_overlap_without_dependency",
            "unverifiable_upstream",
        }),
        "plan_warning_count": len(warnings),
        "plan_requirement_coverage": coverage,
        "plan_acceptance_coverage": coverage,
    }


def _subtask_file_scope_all(subtasks: list[dict]) -> set[str]:
    """G6 升级用：所有子任务文件作用域并集（供过度分解判定）。"""
    union: set[str] = set()
    for st in subtasks:
        union.update(_subtask_file_scope(st))
    return union


def check_under_decomposition(subtasks: list[dict], logger: Optional[logging.Logger] = None) -> bool:
    """G5 规划期欠分解检测：hard 子任务 + 总子任务数过少 → 提示可能撞超时/单子任务过长。

    高难度任务被欠分解成 1-2 个长子任务 → 单子任务耗时长 → 撞 retry_timeout。
    根因在规划期，不在执行期（见 docs/design/timeout-kill-strategy-2026-08-06.md G5）。

    V1：硬编码阈值 difficulty_base_subtasks（hard=3）。仅告警提示，不强改 Plan
    （不覆盖 LLM 的分解决定——「确需少量子任务的 hard 任务」是合法场景）。

    Returns:
        True 若检测到欠分解（触发告警）；False 正常。
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return False
    total = len(subtasks)
    has_hard = any(
        isinstance(st, dict) and st.get("difficulty") == "hard"
        for st in subtasks
    )
    if not has_hard:
        return False
    threshold = DIFFICULTY_BASE_SUBTASKS.get("hard", 3)
    if total < threshold:
        _hard_ids = [
            st.get("id", "?") for st in subtasks
            if isinstance(st, dict) and st.get("difficulty") == "hard"
        ]
        _lg.warning(
            f"[G5] 欠分解告警: hard 子任务（{', '.join(_hard_ids)}）仅 {total} 个总子任务 "
            f"< 建议 {threshold} 个——单子任务可能过长并撞 retry_timeout。"
            f"建议再分解或为 hard 子任务配置强模型（worker_models.hard）。"
        )
        return True
    return False


def check_over_decomposition(subtasks: list[dict], total_files: int = 0,
                            logger: Optional[logging.Logger] = None) -> bool:
    """G6 规划期过度分解检测：简单任务拆成过多串行步骤 → 拓扑波次叠加延迟。

    M0 smoke 发现的 fix-missing-default（5 行改动，1 个文件）被拆成 3 个串行子任务
    → 3-wave 拓扑 = 总耗时 sum(各步) → 触达 420s 超时线。

    启发式：
    - 收集所有子任务的文件作用域（files + files_hint）
    - 若实际改动面积极小（≤2 个文件）但子任务数 ≥3 → 过度分解
    - 若 difficulty 全线 easy 但子任务数 ≥3 → 过度分解

    V1：硬编码阈值，warning-only（不覆盖 LLM 的分解决定）。

    Returns:
        True 若检测到过度分解（触发告警）；False 正常。
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return False
    total = len(subtasks)
    if total < 3:
        return False

    # 收集所有子任务涉及的文件
    all_files: set[str] = set()
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        files = set(st.get("files", []) or [])
        files_hint = str(st.get("files_hint", "") or "")
        if files_hint and files_hint != "*":
            files.update(f.strip() for f in files_hint.split(",") if f.strip())
        all_files.update(f for f in files if f)

    # 合并显式传入的文件数
    effective_file_count = max(len(all_files), total_files)

    # 检查：文件数少但子任务多
    if effective_file_count <= 2 and len(all_files) > 0 and total >= 3:
        _lg.warning(
            f"[G6] 过度分解告警: {total} 个子任务仅涉及 {effective_file_count} 个文件 "
            f"({', '.join(sorted(all_files)[:5])})——这可能是不必要的过度分解，"
            f"串行步骤会叠加延迟。建议合并为 1-2 个子任务。"
        )
        return True

    # 检查：全线 easy 但子任务多
    difficulties = [st.get("difficulty", "") for st in subtasks if isinstance(st, dict)]
    if difficulties and all(d == "easy" for d in difficulties) and total >= 3:
        _lg.warning(
            f"[G6] 过度分解告警: {total} 个 easy 子任务——简单改动无需多个步骤。"
            f"建议合并为 1-2 个子任务以减少拓扑波次延迟。"
        )
        return True

    return False


# P2: agent_prompt 中引用的 Python 内置/标准库函数名（不需要在项目中存在）
_BUILTIN_CALLABLES: set[str] = {
    # Python 内置
    "abs", "all", "any", "ascii", "bin", "bool", "breakpoint", "bytearray", "bytes",
    "callable", "chr", "classmethod", "compile", "complex", "delattr", "dict", "dir",
    "divmod", "enumerate", "eval", "exec", "filter", "float", "format", "frozenset",
    "getattr", "globals", "hasattr", "hash", "hex", "id", "input", "int", "isinstance",
    "issubclass", "iter", "len", "list", "locals", "map", "max", "memoryview", "min",
    "next", "object", "oct", "open", "ord", "pow", "print", "property", "range",
    "repr", "reversed", "round", "set", "setattr", "slice", "sorted", "staticmethod",
    "str", "sum", "super", "tuple", "type", "vars", "zip", "__import__",
    # 常见 stdlib 函数（Planner 可能引用）
    "json.load", "json.loads", "json.dump", "json.dumps",
    "os.path.join", "os.path.exists", "os.path.isfile", "os.path.isdir",
    "os.listdir", "os.getenv", "os.environ.get",
    "pathlib.Path",
    "re.compile", "re.search", "re.match", "re.sub",
    "logging.getLogger", "logging.info", "logging.debug", "logging.warning", "logging.error",
    "datetime.now", "datetime.utcnow",
    "time.time", "time.sleep",
    "subprocess.run", "subprocess.check_output",
    "sys.exit", "sys.argv",
    "argparse.ArgumentParser",
    "unittest.TestCase", "pytest.fixture",
    "collections.defaultdict", "collections.OrderedDict", "collections.Counter",
    "itertools.chain", "itertools.product", "itertools.combinations",
    "functools.partial", "functools.lru_cache",
    "typing.List", "typing.Dict", "typing.Optional", "typing.Union", "typing.Any",
    "dataclasses.dataclass",
    "tempfile.NamedTemporaryFile",
    "shutil.copy", "shutil.move", "shutil.rmtree",
    "glob.glob",
    "csv.reader", "csv.DictReader", "csv.writer", "csv.DictWriter",
    "pandas.read_csv", "pandas.DataFrame",
}
# P2: agent_prompt 中函数调用的提取正则
_FUNC_CALL_RE = re.compile(r'\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\s*\(')


def check_agent_prompt_functions(
    subtasks: list[dict],
    repo: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> list[dict[str, Any]]:
    """P2：扫描 agent_prompt 中引用的函数名，检查是否在项目源码中存在。

    如果 Planner 在 agent_prompt 中引用了不存在的函数（如 M0 的 load_filtered），
    worker 可能浪费时间寻找不存在的 API 或编造实现。此检测在预执行阶段
    发出 warning，供人工复核。

    Args:
        subtasks: Plan 拆解后的子任务列表
        repo: 项目根目录（用于扫描 .py 文件中的函数定义）
        logger: 可选的 logger

    Returns:
        list of warning dicts: [{"type": "unknown_function", "subtask_id": sid,
         "function": func_name, "agent_prompt_snippet": "..."}]
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return []

    # 收集项目源码中定义的函数名
    project_funcs: set[str] = set()
    if repo and Path(repo).exists():
        try:
            for py_file in Path(repo).rglob("*.py"):
                # 跳过虚拟环境和缓存
                parts = py_file.parts
                if any(p in parts for p in (".git", "__pycache__", ".venv", "venv", "node_modules",
                                              ".tox", "env", ".env", "site-packages")):
                    continue
                try:
                    content = py_file.read_text(encoding="utf-8", errors="ignore")
                except (OSError, UnicodeDecodeError):
                    continue
                for match in re.finditer(r'^\s*def\s+([a-zA-Z_]\w*)', content, re.MULTILINE):
                    project_funcs.add(match.group(1))
                # 也收集类名（Planner 可能引用类而非函数）
                for match in re.finditer(r'^\s*class\s+([a-zA-Z_]\w*)', content, re.MULTILINE):
                    project_funcs.add(match.group(1))
        except (OSError, PermissionError) as e:
            _lg.debug("check_agent_prompt_functions: 扫描项目文件失败: %s", e)

    warnings: list[dict[str, Any]] = []
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id", "?"))
        prompt = str(st.get("agent_prompt", "") or "")
        if not prompt:
            continue

        # 提取 agent_prompt 中引用的函数/方法名
        referenced = set()
        for match in _FUNC_CALL_RE.finditer(prompt):
            func_name = match.group(1)
            # 跳过单字母变量（如 f(x)）、内置函数、已知 stdlib
            if len(func_name) <= 1:
                continue
            if func_name in _BUILTIN_CALLABLES:
                continue
            # 跳过明显是 shell 命令或路径的
            if func_name.startswith(("./", "/", "~/")):
                continue
            referenced.add(func_name)

        # 对比：引用了但项目中不存在
        if not repo or not project_funcs:
            continue  # 无项目源码可对比 → 跳过此子任务
        unknown = referenced - _BUILTIN_CALLABLES - project_funcs
        # 对于 method-like 调用（如 obj.method()），提取类名部分检查
        truly_unknown = set()
        for func in unknown:
            parts = func.rsplit(".", 1)
            if len(parts) == 2:
                obj, method = parts
                # 如果是已知类的方法调用，跳过（如 df.to_csv → 检查 DataFrame）
                if obj in project_funcs or obj in _BUILTIN_CALLABLES:
                    continue
            truly_unknown.add(func)
        unknown = truly_unknown

        for func in sorted(unknown):
            # 提取 agent_prompt 中包含该函数的片段（最多 80 字符上下文）
            snippet = ""
            idx = prompt.lower().find(func.lower())
            if idx >= 0:
                start = max(0, idx - 30)
                end = min(len(prompt), idx + len(func) + 50)
                snippet = prompt[start:end].strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."

            _lg.warning(
                f"[P2] agent_prompt 引用未知函数: subtask={sid} "
                f"函数={func!r}——项目中未找到定义，Planner 可能按臆测描述 API"
            )
            warnings.append({
                "type": "unknown_function_in_agent_prompt",
                "subtask_id": sid,
                "function": func,
                "agent_prompt_snippet": snippet,
            })

    return warnings


# CR-G4 V1：难度启发式信号词（planner 主观难度的交叉核对）
# hard 信号：跨模块/重构/架构/迁移等结构性改动；easy 信号：单点小改/helper/格式化。
_DIFFICULTY_HARD_KW = (
    "重构", "架构", "跨模块", "跨文件", "重写", "迁移", "性能优化", "并发", "线程安全",
    "refactor", "architecture", "migrate", "rewrite", "concurrency", "thread-safe",
)
_DIFFICULTY_EASY_KW = (
    "helper", "格式化", "添加一个", "单点", "小改", "typo", "重命名", "改个",
    "format", "rename", "add a function", "add a helper", "one-liner",
)
# 文件路径启发式：从描述/agent_prompt 提及的源码路径数估计涉及面
_PATH_RE = re.compile(r"[\w./\-]+\.(?:py|js|ts|tsx|go|rs|java|rb|c|cpp|h|md|yaml|yml|json|toml)")


def difficulty_hint(subtask: dict) -> Optional[str]:
    """CR-G4 V1：从子任务元数据（title/description/agent_prompt）启发式估一档难度。

    信号：hard 关键词 +2/个；easy 关键词 -1/个；提及 ≥3 个不同源码路径 +2（多文件）。
    返回 'easy'/'medium'/'hard'；中性（无强信号）返回 None（不与 planner 唱反调）。
    纯启发式、warning-only——不覆盖 planner 的 difficulty（见设计稿 G4）。
    """
    if not isinstance(subtask, dict):
        return None
    text = " ".join(str(subtask.get(k, "")) for k in ("title", "description", "agent_prompt", "task")).lower()
    if not text.strip():
        return None
    score = 0
    for kw in _DIFFICULTY_HARD_KW:
        if kw.lower() in text:
            score += 2
    for kw in _DIFFICULTY_EASY_KW:
        if kw.lower() in text:
            score -= 1
    _paths = set(m.group(0).lower() for m in _PATH_RE.finditer(text))
    if len(_paths) >= 3:
        score += 2
    if score >= 2:
        return "hard"
    if score <= -1:
        return "easy"
    return None


# 难度档位序号（算跨档距离用）
_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def check_difficulty_mismatch(subtasks: list[dict], logger: Optional[logging.Logger] = None) -> int:
    """CR-G4：planner 标的 difficulty 与启发式 hint 跨两档不一致时告警（不覆盖 LLM）。

    如 planner 标 easy 但信号强烈倾向 hard（跨文件重构）→ 可能用错档模型（easy 槽能力不足）。
    仅"跨两档"（easy↔hard）才报——单档差异（easy↔medium）噪声大不报。
    与 check_under_decomposition 同风格：warning-only，不改 Plan。

    Returns: 告警条数。
    """
    _lg = logger or logging.getLogger(__name__)
    if not subtasks:
        return 0
    _n = 0
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        _planned = st.get("difficulty", "medium")
        if _planned not in _DIFFICULTY_ORDER:
            continue
        _hinted = difficulty_hint(st)
        if _hinted is None:
            continue
        if abs(_DIFFICULTY_ORDER[_hinted] - _DIFFICULTY_ORDER[_planned]) >= 2:
            _n += 1
            _lg.warning(
                f"[G4] 难度交叉核对告警: {st.get('id', '?')} difficulty={_planned} "
                f"但描述信号倾向 {_hinted}（跨文件/重构/多文件启发式）——"
                f"可能用错档模型。建议人工复核 difficulty 或 worker_models 配置。"
            )
    return _n


def _read_meta(task_dir: Path) -> Optional[dict[str, Any]]:
    """读 meta.json（与 eval._read_meta 同构，自包含避免 import eval）。"""
    path = Path(task_dir) / "meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read meta.json from %s: %s", path, e)
        return None


def _scan_task_dirs(base_dir: Path) -> list[Path]:
    """扫描 task-* 目录（与 eval._scan_task_dirs 同构）。"""
    return sorted(Path(base_dir).glob("task-*"), reverse=True)


def estimate_task_duration(subtasks: list[dict], parallel: int, tasks_dir: Path) -> dict[str, Any]:
    """M4 时间预估：历史子任务耗时中位数 × 拓扑波次（考虑并行度）。

    Returns:
        {
            "subtasks": int, "waves": int, "parallel": int,
            "median_subtask_sec": float, "estimated_sec": int,
            "sample_size": int, "confidence": "high|medium|low|none",
        }
    """
    # 1. 拓扑波次数（依赖链深度，带环保护）
    ids = {st["id"] for st in subtasks}
    depth: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset) -> int:
        if sid in depth:
            return depth[sid]
        if sid in seen:
            return 0
        st = next((s for s in subtasks if s["id"] == sid), None)
        if not st:
            return 0
        deps = [d for d in st.get("depends_on", []) if d in ids]
        d = 1 + max((_depth(dep, seen | {sid}) for dep in deps), default=0)
        depth[sid] = d
        return d

    waves = max((_depth(st["id"], frozenset()) for st in subtasks), default=1)

    # 2. 历史子任务耗时中位数
    durations = sorted(
        r["duration_sec"]
        for td in _scan_task_dirs(tasks_dir)
        if (meta := _read_meta(td))
        for r in meta.get("results", [])
        if r.get("status") in ("completed", "no_changes") and r.get("duration_sec")
    )
    sample_size = len(durations)
    median = durations[sample_size // 2] if durations else _DEFAULT_SUBTASK_SEC

    # 3. 估算：受限于「关键路径（波次）」和「并行吞吐」两者较大者
    total_work = median * len(subtasks)
    est_sec = max(waves * median, total_work / max(1, parallel))

    confidence = ("high" if sample_size >= 20 else
                  "medium" if sample_size >= 5 else
                  "low" if sample_size > 0 else "none")
    return {
        "subtasks": len(subtasks),
        "waves": waves,
        "parallel": parallel,
        "median_subtask_sec": median,
        "estimated_sec": round(est_sec),
        "sample_size": sample_size,
        "confidence": confidence,
    }
