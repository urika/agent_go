import sys, os, logging, subprocess, tempfile
from pathlib import Path
from typing import Any, Optional

from .config import safe_input, log_event
from .utils import read_reference_docs
from .api import generate_plan
from .console import _LazyConsole

console = _LazyConsole()

__all__ = [
    "plan_to_md", "print_plan", "confirm_plan",
    "plan_to_subtasks", "print_subtasks", "confirm_subtasks",
    "verify_subtask",
]

def plan_to_md(plan: dict[str, Any]) -> str:
    """将 Plan 转为 Markdown 文档（IDS §4.2.4 格式：英文 key，自由文本值中英均可）。"""
    lines = [
        f"# 执行方案\n",
        f"## 概述\n{plan.get('overview', 'N/A')}\n",
        f"## 预估工作量\n{plan.get('estimated_effort', 'N/A')}\n",
        f"## 共享资源清单\n",
    ]
    sr = plan.get("shared_resources", {})
    if sr.get("git_remote"): lines.append(f"- Git 远程: {sr['git_remote']}")
    if sr.get("git_branch"): lines.append(f"- 当前分支: {sr['git_branch']}")
    if sr.get("directories"): lines.append(f"- 关键目录: {', '.join(sr['directories'])}")
    if sr.get("config_files"): lines.append(f"- 配置文件: {', '.join(sr['config_files'])}")
    if sr.get("env_vars"): lines.append(f"- 环境变量: {', '.join(sr['env_vars'])}")
    lines.append(f"\n## 执行步骤 ({len(plan.get('steps', []))} 步)\n")
    for step in plan.get("steps", []):
        lines.append(f"### [{step['id']}] {step['title']}\n")
        lines.append(f"{step.get('description', '')}\n")
        if step.get("files"):
            lines.append(f"- files: {', '.join(step['files'])}")
        if step.get("verification"):
            lines.append(f"- verification: `{step['verification']}`")
        if step.get("risks"):
            r_text = '; '.join(step['risks'])
            lines.append(f"- Risks: {r_text}")
        diff = step.get("difficulty", "medium")
        if diff:
            lines.append(f"- difficulty: {diff}")
        agent = step.get("agent_type", "")
        if agent:
            lines.append(f"- agent: {agent}")
        skill_list = step.get("skills", [])
        if skill_list:
            lines.append(f"- skill: {', '.join(skill_list)}")
        lines.append("")
    deps = plan.get("dependencies", {})
    if deps:
        lines.append("## 依赖关系\n")
        for sid, prereqs in deps.items():
            lines.append(f"- step {sid} depends_on: {', '.join(str(p) for p in prereqs)}")
    return "\n".join(lines)


def _parse_plan_md(text: str) -> dict:
    """将 PLAN.md Markdown 解析回 Plan dict（plan_to_md 的逆操作）。

    IDS §4.2.4 格式：英文 key（description/files/verification/risks/…），
    自由文本值中英均可。未知字段忽略（向前兼容）。
    支持边界：描述含 ###、缺失字段取默认、多值字段逗号分隔。
    """
    import re as _re
    plan: dict = {"overview": "", "estimated_effort": "", "shared_resources": {},
                  "steps": [], "dependencies": {}}
    current_section = None
    current_step = None

    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("## 概述"):
            current_section = "overview"; current_step = None; continue
        elif s.startswith("## 预估工作量"):
            current_section = "effort"; current_step = None; continue
        elif s.startswith("## 共享资源清单"):
            current_section = "resources"; current_step = None; continue
        elif s.startswith("## 执行步骤"):
            current_section = "steps"; current_step = None; continue
        elif s.startswith("## 依赖关系"):
            current_section = "deps"; current_step = None; continue

        if current_section == "overview" and s:
            plan["overview"] = (plan["overview"] + " " + s).strip()
        elif current_section == "effort" and s:
            plan["estimated_effort"] = (plan["estimated_effort"] + " " + s).strip()
        elif current_section == "resources":
            m = _re.match(r'^- (.+): (.+)$', line)
            if m:
                plan["shared_resources"][m.group(1).strip()] = m.group(2).strip()
        elif current_section == "steps":
            m = _re.match(r'^### \[(\d+)\] (.+)$', line)
            if m:
                if current_step:
                    plan["steps"].append(current_step)
                current_step = {
                    "id": int(m.group(1)), "title": m.group(2),
                    "description": "", "files": [], "verification": "",
                    "risks": [], "difficulty": "medium",
                    "agent_type": "developer", "skills": [],
                }
                continue
            if current_step:
                m2 = _re.match(r'^- ([a-zA-Z]+): (.+)$', line)
                if m2:
                    key, val = m2.group(1).lower(), m2.group(2).strip().strip("` ")
                    if key == "files":
                        current_step["files"] = [
                            x.strip() for x in _re.split(r'[,，]', val) if x.strip()]
                    elif key == "skill":
                        current_step["skills"] = [
                            x.strip() for x in _re.split(r'[,，]', val) if x.strip()]
                    elif key == "risks":
                        current_step["risks"] = [
                            x.strip("- ").strip() for x in val.split(";") if x.strip()]
                    elif key == "difficulty":
                        if val in ("easy", "medium", "hard"):
                            current_step["difficulty"] = val
                    elif key in ("agent", "agent_type"):
                        current_step["agent_type"] = val
                    elif key == "verification":
                        current_step["verification"] = val
                elif s and not line.startswith("-") and not line.startswith("#"):
                    current_step["description"] += " " + s
        elif current_section == "deps":
            m = _re.match(r'^- step (\d+) depends_on: (.+)$', line)
            if m:
                plan["dependencies"][m.group(1)] = [
                    p.strip() for p in m.group(2).split(",") if p.strip()]

    if current_step:
        plan["steps"].append(current_step)
    # Normalize dependency values to str
    plan["dependencies"] = {str(k): [str(p) for p in v] for k, v in plan.get("dependencies", {}).items()}
    return plan

def _estimate_duration(plan: dict[str, Any], parallel: int = 1) -> str:
    """根据 Plan 的步骤数和依赖关系估算执行时间（M4）。

    计算逻辑：
    - 每个步骤：~180s 执行 + ~60s 验证 = ~240s
    - 串行（parallel=1）：步骤数 × 240s
    - 并行：依赖图的拓扑层数（waves）× 240s
    - 返回人类可读的估算字符串，如 "约 8-12 分钟"
    """
    steps = plan.get("steps", [])
    n = len(steps)
    if n == 0:
        return "N/A"

    BASE_PER_STEP = 240  # 秒（180s 执行 + 60s 验证）

    if parallel <= 1:
        total_sec = n * BASE_PER_STEP
    else:
        deps = plan.get("dependencies", {})
        step_ids = {str(s["id"]) for s in steps}

        in_degree: dict[str, int] = {sid: 0 for sid in step_ids}
        children: dict[str, list[str]] = {sid: [] for sid in step_ids}
        for sid, prereqs in deps.items():
            sid = str(sid)
            if sid in step_ids:
                for p in prereqs:
                    p = str(p)
                    if p in step_ids:
                        in_degree[sid] += 1
                        children.setdefault(p, []).append(sid)

        waves = 0
        remaining = set(step_ids)
        while remaining:
            ready = {sid for sid in remaining if in_degree.get(sid, 0) == 0}
            if not ready:
                waves += len(remaining)
                break
            waves += 1
            for sid in ready:
                remaining.discard(sid)
                for child in children.get(sid, []):
                    if child in in_degree:
                        in_degree[child] -= 1

        total_sec = waves * BASE_PER_STEP

    # 统一按 0.8/1.2 区间估算（steps 非空时最少 240s，无秒级场景）
    minutes = total_sec / 60
    low = int(minutes * 0.8)
    high = int(minutes * 1.2)
    return f"约 {low}-{high} 分钟"


def _console_force_title(msg: str) -> None:
    console.force(f"\n{'=' * 60}")
    console.force(f"  {msg}")
    console.force(f"{'=' * 60}")

def _console_force_subtitle(msg: str) -> None:
    console.force(f"\n── {msg} ──")

def print_plan(plan: dict[str, Any], config: dict[str, Any], force: bool = False) -> None:
    """紧凑展示 Plan（P0-5）。低信息密度字段折叠，水平布局减少行数。"""
    behavior = config.get("behavior", {})
    verbose = behavior.get("show_agent_prompt", True)
    parallel = config.get("_parallel", 1)
    duration = _estimate_duration(plan, parallel)

    _out = console.force if force else console.print
    _sep = lambda c, w: console.force(c * w) if force else console.sep(c, w)
    _title = lambda m: _console_force_title(m) if force else console.title(m)
    _subtitle = lambda m: _console_force_subtitle(m) if force else console.subtitle(m)

    _sep("=", 70)
    _title("📋 执行方案")
    _out(f"概述: {plan.get('overview', 'N/A')}")
    _out(f"工作量: {plan.get('estimated_effort', 'N/A')}  |  预计耗时: {duration}")
    if parallel > 1:
        _out(f"并行度: {parallel}")

    # 共享资源（紧凑单行）
    sr = plan.get("shared_resources", {})
    if sr and behavior.get("show_resource_map", True):
        _parts = []
        if sr.get("git_remote"): _parts.append(f"🔗 {sr['git_remote']}")
        if sr.get("git_branch"): _parts.append(f"🌿 {sr['git_branch']}")
        if sr.get("directories"): _parts.append(f"📁 {', '.join(sr['directories'])}")
        if _parts:
            _out(" | ".join(_parts))

    # 步骤（紧凑 2-3 行）
    _subtitle("步骤")
    steps = plan.get("steps", [])
    deps = plan.get("dependencies", {})
    _max_id_width = max(len(str(s["id"])) for s in steps) if steps else 2

    for step in steps:
        _sid = f"[{step['id']:>{_max_id_width}}]"
        _tag = f" · {step.get('difficulty', 'medium')}" if step.get('difficulty') else ""
        _tag += f" · {step.get('agent_type', 'developer')}" if step.get('agent_type') else ""
        _files = step.get("files", [])
        _file_hint = f" · {len(_files)} 文件" if _files else ""
        _ver = step.get("verification", "")
        _v_hint = f"  ✅ {_ver}" if _ver else ""
        _out(f"{_sid} {step['title']}{_tag}{_file_hint}")
        _out(f"    {step.get('description', '')}{_v_hint}")
        if verbose and step.get("risks"):
            _out(f"    ⚠️  {'; '.join(step['risks'][:3])}")
        if verbose and step.get("agent_prompt"):
            _ap = step["agent_prompt"][:120] + "..." if len(step["agent_prompt"]) > 120 else step["agent_prompt"]
            _out(f"    🤖 {_ap}")

    # 依赖（紧凑行内）
    if deps:
        _dep_lines = []
        for sid, prereqs in deps.items():
            _dep_lines.append(f"  step {sid} → {' → '.join(str(p) for p in prereqs)}")
        _subtitle("依赖")
        _out("\n".join(_dep_lines))
    _sep("=", 70)


def _edit_plan_via_editor(plan: dict[str, Any], logger: logging.Logger) -> None:
    """用 $EDITOR 编辑完整 Plan。编辑后原地修改 plan dict。"""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(plan_to_md(plan))
        tmp_path = tf.name
    try:
        logger.info(f"$EDITOR ({editor}) 打开: {tmp_path}")
        console.force(f"✏️  正在用 {editor} 打开 Plan（{tmp_path}）...")
        console.force("   保存后退出编辑器即可生效")
        ret = subprocess.run([editor, tmp_path], check=False)
        if ret.returncode != 0:
            console.error(f"编辑器退出码 {ret.returncode}，放弃本版修改")
            return
        edited = Path(tmp_path).read_text(encoding="utf-8").strip()
        if not edited:
            console.error("编辑后文件为空，放弃修改")
            return
        new_plan = _parse_plan_md(edited)
        if not new_plan.get("steps"):
            console.error("编辑后 Plan 不包含任何步骤，放弃修改")
            return
        plan.clear()
        plan.update(new_plan)
        console.success("Plan 已更新")
        logger.info("$EDITOR 编辑成功: Plan 已更新")
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _prompt_fallback(logger: logging.Logger) -> str:
    """交互式询问用户是否降级到规则拆解。返回 True=降级, False=重试。"""
    console.force("\n⚠️ API 重新生成失败。请选择:")
    console.force("  [F] 降级到本地规则拆解（不依赖 API）")
    console.force("  [R] 重试（再次调用 API）")
    console.force("  [N] 取消任务")
    while True:
        c = safe_input("\n> ").strip().upper()
        if c == "F":
            logger.info("用户选择降级到规则拆解")
            return "fallback"
        elif c == "R":
            logger.info("用户选择重试")
            return "retry"
        elif c == "N":
            logger.info("用户取消")
            console.error("已取消")
            sys.exit(0)
        console.print("无效输入（F=降级, R=重试, N=取消）")

def confirm_plan(plan: dict[str, Any], config: dict[str, Any], repo: Path, logger: logging.Logger, iteration: int = 1, task: str = "") -> tuple[Optional[dict[str, Any]], Optional[list[str]]]:
    """
    用户确认 Plan。支持默认同意模式。
    返回: (plan, doc_paths) 或 (None, doc_paths)（R 重新生成）或 ("__FALLBACK__", None)
    """
    behavior = config.get("behavior", {})
    auto_confirm = behavior.get("auto_confirm_plan", False)
    reference_doc_paths = []
    plan_api_failure_count = 0
    max_plan_api_failures = 2

    # 检查环境变量强制交互
    if os.environ.get("AGENT_GO_INTERACTIVE", "").lower() == "1":
        auto_confirm = False

    empty_count = 0
    while True:
        print_plan(plan, config, force=console.quiet)

        # 默认同意模式
        if auto_confirm and iteration == 1:
            if not sys.stdin.isatty() or console.json_mode:
                logger.info("默认同意模式：自动确认 Plan")
                log_event(logger, "plan_auto_confirmed", {"iteration": iteration})
                return plan, reference_doc_paths
            console.force(f"\n⚡ 默认同意模式已开启（来自配置 behavior.auto_confirm_plan）")
            console.force(f"   按 Enter 直接确认，或输入任意键进入交互模式...")
            quick = safe_input("\n> ").strip()
            if not quick:
                logger.info("默认同意模式：自动确认 Plan")
                log_event(logger, "plan_auto_confirmed", {"iteration": iteration})
                return plan, reference_doc_paths
            # 用户输入了内容，进入交互模式
            auto_confirm = False

        console.force("\n请选择操作:")
        console.force("  [Y] 确认方案，拆解为子任务并执行")
        console.force("  [S] 补充输入/修正需求（重新生成）")
        console.force("  [D] 挂载参考文档（重新生成）")
        console.force("  [E] 编辑某个步骤")
        console.force("  [M] 用 $EDITOR 编辑完整方案")
        console.force("  [R] 重新生成方案")
        console.force("  [N] 取消任务")

        choice = safe_input("\n> ").strip().upper()
        log_event(logger, "user_plan_choice", {"choice": choice, "iteration": iteration, "auto_confirm": auto_confirm})

        if choice == "Y" or (choice == "" and auto_confirm):
            logger.info("用户确认 Plan")
            return plan, reference_doc_paths
        elif choice == "N":
            logger.info("用户取消")
            console.force("❌ 已取消")
            sys.exit(0)
        elif choice == "R":
            logger.info("用户请求重新生成")
            return None, reference_doc_paths
        elif choice == "E":
            idx_str = safe_input(f"编辑第几个步骤 (1-{len(plan['steps'])}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(plan["steps"]):
                idx = int(idx_str) - 1
                step = plan["steps"][idx]
                new_title = safe_input(f"  标题 [{step['title']}]: ").strip()
                new_desc = safe_input(f"  描述 [{step['description']}]: ").strip()
                new_files = safe_input(f"  文件 [{', '.join(step.get('files',[]))}]: ").strip()
                new_prompt = safe_input(f"  Agent Prompt [{step.get('agent_prompt','')[:50]}...]: ").strip()
                if new_title: step["title"] = new_title
                if new_desc: step["description"] = new_desc
                if new_files: step["files"] = [f.strip() for f in new_files.split(",")]
                if new_prompt: step["agent_prompt"] = new_prompt
                logger.info(f"用户编辑步骤 {step['id']}")
        elif choice == "M":
            logger.info("用户选择 $EDITOR 编辑完整方案")
            _edit_plan_via_editor(plan, logger)
        elif choice == "S":
            console.force("\n✏️  请输入补充内容（支持多行，空行结束）：")
            lines = []
            while True:
                line = safe_input()
                if line.strip() == "" and lines and lines[-1].strip() == "":
                    break
                lines.append(line)
            supplement = "\n".join(lines).strip()
            if not supplement:
                console.force("补充为空，未重新生成")
                continue
            logger.info(f"用户补充: {supplement[:200]}...")
            existing_docs = read_reference_docs(reference_doc_paths, repo, logger) if reference_doc_paths else ""
            iteration += 1
            try:
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, supplement, existing_docs, iteration)
                plan["_original_task"] = original
                plan_api_failure_count = 0
                console.force(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                console.force(f"⚠️ 失败: {e}")
                plan_api_failure_count += 1
                if plan_api_failure_count >= max_plan_api_failures:
                    fallback_choice = _prompt_fallback(logger)
                    if fallback_choice == "fallback":
                        return ("__FALLBACK__", None)
                    plan_api_failure_count = 0  # 用户选择重试，重置计数
        elif choice == "D":
            console.force("\n📎 输入参考文档路径（多个逗号分隔，目录自动读 .md）：")
            doc_input = safe_input("\n> ").strip()
            if not doc_input:
                continue
            new_paths = [p.strip() for p in doc_input.split(",")]
            reference_doc_paths.extend(new_paths)
            reference_doc_paths = list(dict.fromkeys(reference_doc_paths))
            docs_content = read_reference_docs(reference_doc_paths, repo, logger)
            if not docs_content:
                console.force("⚠️ 未读取到有效文档")
                continue
            logger.info(f"挂载 {len(reference_doc_paths)} 个文档，重新生成")
            iteration += 1
            try:
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, "", docs_content, iteration)
                plan["_original_task"] = original
                plan_api_failure_count = 0
                console.force(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                console.force(f"⚠️ 失败: {e}")
                plan_api_failure_count += 1
                if plan_api_failure_count >= max_plan_api_failures:
                    fallback_choice = _prompt_fallback(logger)
                    if fallback_choice == "fallback":
                        return ("__FALLBACK__", None)
                    plan_api_failure_count = 0
        else:
            if choice == "":
                empty_count += 1
                if empty_count > 5:
                    console.force("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
            console.force("无效输入")

def plan_to_subtasks(plan: dict[str, Any], logger: logging.Logger, repo: Optional[Path] = None) -> list[dict[str, Any]]:
    """Plan → 子任务，注入 Agent Prompt、资源清单、依赖关系。
    同时应用角色-Skill 映射规则进行兜底匹配。"""
    subtasks = []
    shared = plan.get("shared_resources", {})
    deps = plan.get("dependencies", {})

    for step in plan.get("steps", []):
        files = step.get("files", [])
        files_hint = ", ".join(files) if files else "*"

        desc_parts = [step.get("description", "")]
        if step.get("agent_prompt"):
            desc_parts.append(f"\n【Agent 执行指令】\n{step['agent_prompt']}")
        if shared:
            resource_text = "\n".join([
                f"Git 远程: {shared.get('git_remote', 'N/A')}" if shared.get('git_remote') else "",
                f"当前分支: {shared.get('git_branch', 'N/A')}" if shared.get('git_branch') else "",
                f"关键目录: {', '.join(shared.get('directories', []))}" if shared.get('directories') else "",
                f"配置文件: {', '.join(shared.get('config_files', []))}" if shared.get('config_files') else "",
                f"环境变量: {', '.join(shared.get('env_vars', []))}" if shared.get('env_vars') else "",
            ])
            resource_text = "\n".join(line for line in resource_text.split("\n") if line)
            if resource_text:
                desc_parts.append(f"\n【共享资源清单】\n{resource_text}")
        if step.get("verification"):
            desc_parts.append(f"\n【验证命令】\n{step['verification']}")
        if step.get("risks"):
            desc_parts.append(f"\n【风险提示】\n{'; '.join(step['risks'])}")

        desc = "\n".join(desc_parts)

        step_id = str(step["id"])
        upstream_ids = deps.get(step_id, [])
        depends_on = [f"sub-{d}" for d in upstream_ids]

        # 应用角色-Skill 映射规则兜底
        from .role_skill_map import load_role_skill_map, apply_rules
        from .skills import list_skills
        role_map = load_role_skill_map(repo)
        installed = list_skills(repo)
        rule_result = apply_rules(step, role_map, installed)

        # 自动检测缓存相关步骤，追加测试隔离提示
        _risks = list(step.get("risks", []))
        _step_text = (step.get("description", "") + " " + step.get("agent_prompt", "") + " " + step.get("verification", "")).lower()
        if "cache_page" in _step_text or "@cache_page" in _step_text:
            _cache_note = "@cache_page 需要 conftest.py 中添加 cache.clear() fixture，否则 pytest 跨测试共享缓存导致 data=[]"
            if _cache_note not in _risks:
                _risks.append(_cache_note)

        subtask_id = f"sub-{step['id']}"

        # S4 复杂度双通道：LLM 标注的 difficulty 透传到执行阶段（非法值归一为 medium）
        _step_difficulty = step.get("difficulty", "medium")
        if _step_difficulty not in ("easy", "medium", "hard"):
            _step_difficulty = "medium"
        # 自动提升：orm-optimizer skill + 多文件 → 复杂度至少 medium
        if _step_difficulty == "easy" and "orm-optimizer" in rule_result["skills"] and len(files) >= 2:
            _step_difficulty = "medium"
            logger.info(f"[difficulty_bump] {subtask_id}: orm-optimizer + {len(files)} files → easy→medium")

        subtasks.append({
            "id": subtask_id,
            "title": step.get("title", f"步骤 {step['id']}"),
            "description": desc,
            "files_hint": files_hint,
            "agent_prompt": step.get("agent_prompt", ""),
            "verification": step.get("verification", ""),
            "risks": _risks,
            "depends_on": depends_on,
            "skills": rule_result["skills"],
            "agent_type": rule_result["agent_type"],
            "difficulty": _step_difficulty,
            "_agent_type_source": "llm" if step.get("agent_type") else ("rule" if rule_result.get("matched_rules") else "default"),
        })

    log_event(logger, "plan_decomposed", {"count": len(subtasks)})
    return subtasks

def print_subtasks(subtasks: list[dict[str, Any]], config: dict[str, Any], force: bool = False) -> None:
    behavior = config.get("behavior", {})
    _out = console.force if force else console.print
    _out("\n" + "─" * 60)
    _out("📋 子任务列表")
    if force:
        console.force("─" * 60)
    else:
        console.sep("─", 60)
    for st in subtasks:
        _out(f"\n[{st['id']}] {st['title']}")
        # 标注 Agent 角色来源
        agent_type = st.get("agent_type", "developer")
        source = st.get("_agent_type_source", "default")
        source_tag = {"llm": "", "rule": " [规则匹配]", "default": "", "inferred": " [自动推断]"}.get(source, "")
        _out(f"\U0001f464 Agent: {agent_type}{source_tag}")
        skills = st.get("skills", [])
        if skills:
            _out(f"\U0001f9e0 Skill: {', '.join(skills)}")
        # 只展示描述前200字符，避免太长
        desc = st.get("description", "")
        preview = desc[:200] + "..." if len(desc) > 200 else desc
        _out(f"{preview}")
        if st.get("files_hint"):
            _out(f"\U0001f4c1 涉及文件: {st['files_hint']}")
        if behavior.get("show_agent_prompt", True) and st.get("agent_prompt"):
            prompt_preview = st["agent_prompt"][:150] + "..." if len(st["agent_prompt"]) > 150 else st["agent_prompt"]
            _out(f"\U0001f916 Agent Prompt: {prompt_preview}")
    _out("\n" + "─" * 60)

def confirm_subtasks(subtasks: list[dict[str, Any]], config: dict[str, Any], logger: logging.Logger) -> list[dict[str, Any]]:
    behavior = config.get("behavior", {})
    auto_confirm = behavior.get("auto_confirm_subtasks", False)

    # 环境变量强制交互
    if os.environ.get("AGENT_GO_INTERACTIVE", "").lower() == "1":
        auto_confirm = False

    print_subtasks(subtasks, config, force=console.quiet)

    if auto_confirm:
        if not sys.stdin.isatty() or console.json_mode:
            logger.info("默认同意模式：自动确认子任务")
            log_event(logger, "subtasks_auto_confirmed", {"count": len(subtasks)})
            return subtasks
        console.force(f"\n⚡ 默认同意模式已开启（behavior.auto_confirm_subtasks）")
        console.force(f"   按 Enter 直接执行，或输入任意键进入交互...")
        quick = safe_input("\n> ").strip()
        if not quick:
            logger.info("默认同意模式：自动确认子任务")
            log_event(logger, "subtasks_auto_confirmed", {"count": len(subtasks)})
            return subtasks
        auto_confirm = False

    console.force("\n请选择操作:")
    console.force("  [Y] 全部确认并执行")
    console.force("  [N] 取消任务")
    console.force("  [E] 编辑某个子任务")
    console.force("  [A] 添加新子任务")
    console.force("  [D] 删除某个子任务")

    empty_count = 0
    while True:
        choice = safe_input("\n> ").strip().upper()
        log_event(logger, "user_subtask_choice", {"choice": choice})
        if choice == "Y":
            return subtasks
        elif choice == "N":
            sys.exit(0)
        elif choice == "E":
            idx_str = safe_input(f"编辑第几个 (1-{len(subtasks)}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(subtasks):
                idx = int(idx_str) - 1
                st = subtasks[idx]
                t = safe_input(f"标题 [{st['title']}]: ").strip()
                d = safe_input(f"描述 [{st['description'][:100]}...]: ").strip()
                f = safe_input(f"文件 [{st.get('files_hint','')}]: ").strip()
                p = safe_input(f"Agent Prompt [{st.get('agent_prompt','')[:50]}...]: ").strip()
                if t: st["title"] = t
                if d: st["description"] = d
                if f: st["files_hint"] = f
                if p: st["agent_prompt"] = p
            print_subtasks(subtasks, config)
        elif choice == "A":
            title = safe_input("新标题: ").strip()
            desc = safe_input("描述: ").strip()
            files = safe_input("文件: ").strip()
            prompt = safe_input("Agent Prompt: ").strip()
            subtasks.append({"id": f"sub-{len(subtasks)+1}", "title": title, "description": desc, "files_hint": files, "agent_prompt": prompt})
            print_subtasks(subtasks, config)
        elif choice == "D":
            idx_str = safe_input(f"删除第几个 (1-{len(subtasks)}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(subtasks):
                del subtasks[int(idx_str)-1]
                for i, st in enumerate(subtasks):
                    st["id"] = f"sub-{i+1}"
            print_subtasks(subtasks, config)
        else:
            if choice == "":
                empty_count += 1
                if empty_count > 5:
                    console.force("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
            console.force("无效输入")

def verify_subtask(current: int, total: int, summary: str, logger: logging.Logger, config: Optional[dict[str, Any]] = None) -> str:
    console.force(f"\n{'='*60}\n✅ {current}/{total} 完成\n{'='*60}")
    console.force(f"📊 {summary}\n[C]继续 [R]重试 [M]修改 [A]中止")
    auto_verify = config.get("behavior", {}).get("auto_verify_subtask", False) if config else False
    while True:
        c = safe_input("\n> ").strip().upper()
        log_event(logger, "user_verify", {"current": current, "choice": c})
        if c in ("C", "CONTINUE") or (c == "" and auto_verify): return "next"
        elif c in ("R", "RETRY"): return "retry"
        elif c in ("M", "MODIFY"): return "modify"
        elif c in ("A", "ABORT"): return "abort"
        else: print("无效输入")
