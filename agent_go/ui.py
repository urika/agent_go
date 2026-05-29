from .common_imports import *
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import safe_input, log_event
from .utils import read_reference_docs
from .api import generate_plan

def plan_to_md(plan):
    """将 Plan 转为 Markdown 文档。"""
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
            lines.append(f"- 文件: {', '.join(step['files'])}")
        if step.get("verification"):
            lines.append(f"- 验证: `{step['verification']}`")
        if step.get("risks"):
            lines.append(f"- 风险: {'; '.join(step['risks'])}")
        lines.append("")
    deps = plan.get("dependencies", {})
    if deps:
        lines.append("## 依赖关系\n")
        for sid, prereqs in deps.items():
            lines.append(f"- 步骤 {sid} 依赖: {prereqs}")
    return "\n".join(lines)

def print_plan(plan, config):
    """展示 Plan，包含 Agent Prompt 和资源清单。"""
    behavior = config.get("behavior", {})

    print("\n" + "=" * 70)
    print("📋 执行方案（Plan Mode）")
    print("=" * 70)
    print(f"\n📝 概述: {plan.get('overview', 'N/A')}")
    print(f"⏱️  预估: {plan.get('estimated_effort', 'N/A')}")

    # 共享资源清单
    sr = plan.get("shared_resources", {})
    if sr and behavior.get("show_resource_map", True):
        print(f"\n📦 共享资源清单:")
        if sr.get("git_remote"):
            print(f"   🔗 Git 远程: {sr['git_remote']}")
        if sr.get("git_branch"):
            print(f"   🌿 当前分支: {sr['git_branch']}")
        if sr.get("directories"):
            print(f"   📁 关键目录: {', '.join(sr['directories'])}")
        if sr.get("config_files"):
            print(f"   ⚙️  配置文件: {', '.join(sr['config_files'])}")
        if sr.get("env_vars"):
            print(f"   🔐 环境变量: {', '.join(sr['env_vars'])}")

    print(f"\n📌 执行步骤:")
    for step in plan.get("steps", []):
        print(f"\n[{step['id']}] {step['title']}")
        print(f"      {step['description']}")
        print(f"      📁 文件: {', '.join(step.get('files', []))}")
        print(f"      ✅ 验证: {step.get('verification', 'N/A')}")
        if step.get("risks"):
            print(f"      ⚠️  风险: {', '.join(step['risks'])}")

        # Agent Prompt
        if behavior.get("show_agent_prompt", True) and step.get("agent_prompt"):
            prompt_preview = step["agent_prompt"][:200] + "..." if len(step["agent_prompt"]) > 200 else step["agent_prompt"]
            print(f"      🤖 Agent Prompt: {prompt_preview}")

    deps = plan.get("dependencies", {})
    if deps:
        print(f"\n🔗 依赖关系:")
        for sid, prereqs in deps.items():
            print(f"      步骤 {sid} 依赖: {prereqs}")
    print("=" * 70)

def _prompt_fallback(logger):
    """交互式询问用户是否降级到规则拆解。返回 True=降级, False=重试。"""
    print("\n⚠️ API 重新生成失败。请选择:")
    print("  [F] 降级到本地规则拆解（不依赖 API）")
    print("  [R] 重试（再次调用 API）")
    print("  [N] 取消任务")
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
            print("❌ 已取消")
            sys.exit(0)
        print("无效输入（F=降级, R=重试, N=取消）")

def confirm_plan(plan, config, repo, logger, iteration=1, task="") -> tuple:
    """
    用户确认 Plan。支持默认同意模式。
    返回: (plan, doc_paths) 或 (None, None) 或 ("__FALLBACK__", None)
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
        print_plan(plan, config)

        # 默认同意模式
        if auto_confirm and iteration == 1:
            print(f"\n⚡ 默认同意模式已开启（来自配置 behavior.auto_confirm_plan）")
            print(f"   按 Enter 直接确认，或输入任意键进入交互模式...")
            quick = safe_input("\n> ").strip()
            if not quick:
                logger.info("默认同意模式：自动确认 Plan")
                log_event(logger, "plan_auto_confirmed", {"iteration": iteration})
                return plan, reference_doc_paths
            # 用户输入了内容，进入交互模式
            auto_confirm = False

        print("\n请选择操作:")
        print("  [Y] 确认方案，拆解为子任务并执行")
        print("  [S] 补充输入/修正需求（重新生成）")
        print("  [D] 挂载参考文档（重新生成）")
        print("  [E] 编辑某个步骤")
        print("  [R] 重新生成方案")
        print("  [N] 取消任务")

        choice = safe_input("\n> ").strip().upper()
        log_event(logger, "user_plan_choice", {"choice": choice, "iteration": iteration, "auto_confirm": auto_confirm})

        if choice == "Y" or (choice == "" and auto_confirm):
            logger.info("用户确认 Plan")
            return plan, reference_doc_paths
        elif choice == "N":
            logger.info("用户取消")
            print("❌ 已取消")
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
        elif choice == "S":
            print("\n✏️  请输入补充内容（支持多行，空行结束）：")
            lines = []
            while True:
                line = safe_input()
                if line.strip() == "" and lines and lines[-1].strip() == "":
                    break
                lines.append(line)
            supplement = "\n".join(lines).strip()
            if not supplement:
                print("补充为空，未重新生成")
                continue
            logger.info(f"用户补充: {supplement[:200]}...")
            existing_docs = read_reference_docs(reference_doc_paths, repo, logger) if reference_doc_paths else ""
            iteration += 1
            try:
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, supplement, existing_docs, iteration)
                plan["_original_task"] = original
                plan_api_failure_count = 0
                print(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                print(f"⚠️ 失败: {e}")
                plan_api_failure_count += 1
                if plan_api_failure_count >= max_plan_api_failures:
                    fallback_choice = _prompt_fallback(logger)
                    if fallback_choice == "fallback":
                        return ("__FALLBACK__", None)
                    plan_api_failure_count = 0  # 用户选择重试，重置计数
        elif choice == "D":
            print("\n📎 输入参考文档路径（多个逗号分隔，目录自动读 .md）：")
            doc_input = safe_input("\n> ").strip()
            if not doc_input:
                continue
            new_paths = [p.strip() for p in doc_input.split(",")]
            reference_doc_paths.extend(new_paths)
            reference_doc_paths = list(dict.fromkeys(reference_doc_paths))
            docs_content = read_reference_docs(reference_doc_paths, repo, logger)
            if not docs_content:
                print("⚠️ 未读取到有效文档")
                continue
            logger.info(f"挂载 {len(reference_doc_paths)} 个文档，重新生成")
            iteration += 1
            try:
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, "", docs_content, iteration)
                plan["_original_task"] = original
                plan_api_failure_count = 0
                print(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                print(f"⚠️ 失败: {e}")
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
                    print("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
            print("无效输入")

def plan_to_subtasks(plan, logger, repo=None):
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

        subtasks.append({
            "id": f"sub-{step['id']}",
            "title": step.get("title", f"步骤 {step['id']}"),
            "description": desc,
            "files_hint": files_hint,
            "agent_prompt": step.get("agent_prompt", ""),
            "verification": step.get("verification", ""),
            "risks": step.get("risks", []),
            "depends_on": depends_on,
            "skills": rule_result["skills"],
            "agent_type": rule_result["agent_type"],
            "_agent_type_source": "llm" if step.get("agent_type") else ("rule" if rule_result.get("matched_rules") else "default"),
        })

    log_event(logger, "plan_decomposed", {"count": len(subtasks)})
    return subtasks

def print_subtasks(subtasks, config):
    behavior = config.get("behavior", {})
    print("\n" + "─" * 60)
    print("📋 子任务列表")
    print("─" * 60)
    for st in subtasks:
        print(f"\n[{st['id']}] {st['title']}")
        # 标注 Agent 角色来源
        agent_type = st.get("agent_type", "developer")
        source = st.get("_agent_type_source", "default")
        source_tag = {"llm": "", "rule": " [规则匹配]", "default": "", "inferred": " [自动推断]"}.get(source, "")
        print(f"      \U0001f464 Agent: {agent_type}{source_tag}")
        skills = st.get("skills", [])
        if skills:
            print(f"      \U0001f9e0 Skill: {', '.join(skills)}")
        # 只展示描述前200字符，避免太长
        desc = st.get("description", "")
        preview = desc[:200] + "..." if len(desc) > 200 else desc
        print(f"      {preview}")
        if st.get("files_hint"):
            print(f"      \U0001f4c1 涉及文件: {st['files_hint']}")
        if behavior.get("show_agent_prompt", True) and st.get("agent_prompt"):
            prompt_preview = st["agent_prompt"][:150] + "..." if len(st["agent_prompt"]) > 150 else st["agent_prompt"]
            print(f"      \U0001f916 Agent Prompt: {prompt_preview}")
    print("\n" + "─" * 60)

def confirm_subtasks(subtasks, config, logger):
    behavior = config.get("behavior", {})
    auto_confirm = behavior.get("auto_confirm_subtasks", False)

    # 环境变量强制交互
    if os.environ.get("AGENT_GO_INTERACTIVE", "").lower() == "1":
        auto_confirm = False

    print_subtasks(subtasks, config)

    if auto_confirm:
        print(f"\n⚡ 默认同意模式已开启（behavior.auto_confirm_subtasks）")
        print(f"   按 Enter 直接执行，或输入任意键进入交互...")
        quick = safe_input("\n> ").strip()
        if not quick:
            logger.info("默认同意模式：自动确认子任务")
            log_event(logger, "subtasks_auto_confirmed", {"count": len(subtasks)})
            return subtasks
        auto_confirm = False

    print("\n请选择操作:")
    print("  [Y] 全部确认并执行")
    print("  [N] 取消任务")
    print("  [E] 编辑某个子任务")
    print("  [A] 添加新子任务")
    print("  [D] 删除某个子任务")

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
                    print("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
            print("无效输入")

def verify_subtask(current, total, summary, logger, config=None):
    print(f"\n{'='*60}\n✅ {current}/{total} 完成\n{'='*60}")
    print(f"📊 {summary}\n[C]继续 [R]重试 [M]修改 [A]中止")
    auto_verify = config.get("behavior", {}).get("auto_verify_subtask", False) if config else False
    while True:
        c = safe_input("\n> ").strip().upper()
        log_event(logger, "user_verify", {"current": current, "choice": c})
        if c in ("C", "CONTINUE") or (c == "" and auto_verify): return "next"
        elif c in ("R", "RETRY"): return "retry"
        elif c in ("M", "MODIFY"): return "modify"
        elif c in ("A", "ABORT"): return "abort"
        else: print("无效输入")
