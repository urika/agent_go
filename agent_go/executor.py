import sys, os, subprocess, json, re, time, threading, shlex, signal, logging, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import log_event
from .utils import _format_commit, _safe_append_to_file, _is_safe_verification_command, _slugify
from .subtask import _git_merge_upstream, _run_headless
from .agents import load_agent_type, get_claude_command, get_agent_env
from .git_utils import _worktree_create

def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref=""):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    worktree = sub_dir / "work"

    logger.info(f"─── {sub_id} START: {subtask['title']} ───")
    log_event(logger, "subtask_start", {"id": sub_id, "title": subtask["title"],
                "depends_on": subtask.get("depends_on", []), "headless": headless, "issue": issue_ref})

    clone_start = time.time()
    if (worktree / ".git").exists():
        logger.info(f"worktree 已存在，跳过创建")
    elif (repo / ".git").exists():
        branch = f"agent_go/{task_id}/{sub_id}"
        ok = _worktree_create(repo, branch, worktree)
        if ok:
            logger.info(f"worktree 创建: 分支={branch}")
        else:
            logger.warning(f"worktree add 失败，回退到 git clone")
            worktree.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", str(repo), str(worktree)], capture_output=True, check=True)
            checkout_result = subprocess.run(["git", "checkout", "-b", branch], cwd=str(worktree), capture_output=True)
            if checkout_result.returncode != 0:
                logger.warning(f"分支创建失败: {checkout_result.stderr.strip()}")
    else:
        worktree.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(repo), str(worktree), dirs_exist_ok=True)

    # 产物传递：通过 git merge 将上游代码合并到当前 worktree
    # Tag 使用完整路径 task_id/sub_id 避免跨任务冲突
    merge_conflicts = {}
    if upstream_worktrees:
        for up_id, up_path in upstream_worktrees.items():
            if up_path.exists():
                upstream_tag = f"{task_id}/{up_id}"
                logger.info(f"产物传递 (git merge): {up_id} → {sub_id} (tag={upstream_tag})")
                _git_merge_upstream(up_path, worktree, upstream_tag, logger, headless=headless)
                # 检测上游 merge 是否产生冲突
                conflict_file = worktree / ".MERGE_CONFLICT"
                if conflict_file.exists():
                    merge_conflicts[up_id] = conflict_file.read_text(encoding="utf-8")
                    conflict_file.unlink()  # 读取后删除标记文件
    clone_time = time.time() - clone_start

    # 构建 TASK.md：包含完整 Agent Prompt、资源清单、上游上下文
    task_md_parts = [f"# 子任务: {subtask['title']}", ""]

    # 注入 git merge 冲突信息（如有）
    if merge_conflicts:
        task_md_parts.extend([
            "## ⚠️ 上游合并冲突（需手动解决）",
            "以下文件在合并上游代码时产生了冲突，请先解决这些冲突再进行修改：",
        ])
        for up_id, info in merge_conflicts.items():
            task_md_parts.append(f"来源: {up_id}")
            task_md_parts.append(info)
        task_md_parts.extend([
            "",
            "解决冲突后请执行: `git add . && git commit -m 'resolve merge conflicts'`",
            "",
        ])

    # 注入上游子任务的共享上下文
    shared_ctx = (task_dir / "SHARED_CONTEXT.md")
    if shared_ctx.exists():
        ctx = shared_ctx.read_text(encoding="utf-8")
        if ctx.strip():
            task_md_parts.extend([
                "## 上游子任务上下文",
                "以下是前面子任务的关键信息，请先理解再开始操作：",
                ctx,
                "",
            ])

    task_md_parts.append(f"## 描述\n{subtask['description']}")
    if subtask.get("agent_prompt"):
        task_md_parts.extend(["", "## 执行指令（Agent Prompt）", subtask["agent_prompt"]])

    # 验证要求
    verification = subtask.get("verification", "")
    exec_requirements = [
        "",
        "## 执行要求",
        "- 在此隔离 worktree 中完成修改",
        "- 变更保留在此目录",
    ]
    if verification:
        exec_requirements.append(f"- **必须执行验证**: `{verification}`，确保通过后再完成")
        exec_requirements.append("- 如验证失败，请修复问题后重新验证，直到通过")
    if not headless:
        exec_requirements.append("- 完成后退出 Claude Code（/exit 或 Ctrl+D）")
    task_md_parts.extend(exec_requirements)

    # ── Skill 知识注入 ──
    skill_names = subtask.get("skills", [])
    unresolved_skills = []
    if skill_names:
        from .skills import load_skill, render_skill_for_execution, list_skills as _list_skills
        installed_names = [s["name"] for s in _list_skills(repo)]
        task_md_parts.append("")
        for sn in skill_names:
            sk = load_skill(sn, repo)
            if sk:
                task_md_parts.append(render_skill_for_execution(sk))
                task_md_parts.append("")
                logger.info(f"Skill 注入: {sn} → TASK.md")
            else:
                unresolved_skills.append(sn)
                logger.warning(f"Skill 未找到: \"{sn}\"，已跳过。已安装: {installed_names[:10]}")

    # 将 Agent Prompt 中的源项目路径替换为 worktree 路径，确保隔离
    # 边界字符包含: 空白、引号、括号、冒号(含全角)、路径分隔符、中文标点
    task_md_text = "\n".join(task_md_parts)
    _boundary_chars = r'\s"\'\(\):/：，。、'
    _before = rf'(?<![^{_boundary_chars}])'
    _after = rf'(?![^{_boundary_chars}])'
    task_md = re.sub(
        rf'{_before}{re.escape(str(repo))}{_after}',
        str(worktree),
        task_md_text
    )
    (sub_dir / "TASK.md").write_text(task_md, encoding="utf-8")

    print(f"\n🚀 {sub_id}: {subtask['title']}")
    env = os.environ.copy()
    env.update({"AGENT_GO_TASK_ID": task_id, "AGENT_GO_SUBTASK_ID": sub_id, "AGENT_GO_WORKTREE": str(worktree)})

    # ── Agent 类型配置 ──
    agent_type_name = subtask.get("agent_type", "developer")
    agent = load_agent_type(agent_type_name, repo)
    if agent:
        env.update(get_agent_env(agent))
        logger.info(f"Agent: {agent.type_name}")
    else:
        from .agents import list_agent_types
        available = [a["type"] for a in list_agent_types()]
        logger.warning(f"Agent 类型 \"{agent_type_name}\" 未注册，降级为 developer。可用: {available}")

    claude_start = time.time()

    if headless:
        sandbox_type = "headless"
        result = _run_headless(task_md, worktree, env, logger, sub_id)
    else:
        # 根据 Agent 类型构建 Claude 命令
        if agent:
            claude_cmd = get_claude_command(agent, worktree, headless=False)
        else:
            claude_cmd = ["claude", str(worktree)]

        try:
            # 尝试先匹配 greywall 包装
            greywall_bin = shutil.which("greywall")
            if greywall_bin:
                result = subprocess.run([greywall_bin, "--"] + claude_cmd, env=env, cwd=str(worktree))
                sandbox_type = "greywall"
            else:
                result = subprocess.run(claude_cmd, env=env, cwd=str(worktree))
                sandbox_type = "native"
        except FileNotFoundError:
            print("   ⚠️ Greywall 未安装，降级原生")
            result = subprocess.run(["claude", str(worktree)], env=env, cwd=str(worktree))
            sandbox_type = "native"

    claude_time = time.time() - claude_start

    # 记录变更摘要
    diff = subprocess.run(["git", "diff", "--stat"], cwd=str(worktree), capture_output=True, text=True)
    summary = diff.stdout.strip() or "无文件变更"

    # Git 提交 + tag（Conventional Commits 格式），供下游子任务 merge
    # Tag 包含 task_id 前缀，避免跨任务冲突
    tag_name = f"{task_id}/{sub_id}"
    has_changes = summary != "无文件变更"
    if has_changes:
        commit_msg = _format_commit(subtask['title'], issue_ref, sub_id)
        add_result = subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        if add_result.returncode != 0:
            logger.warning(f"git add 失败: {add_result.stderr.strip()}")
        commit_result = subprocess.run(["git", "commit", "-m", commit_msg],
                                       cwd=str(worktree), capture_output=True)
        if commit_result.returncode != 0:
            logger.warning(f"git commit 失败: {commit_result.stderr.strip()[:200]}")
    tag_result = subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree), capture_output=True)
    if tag_result.returncode != 0:
        logger.warning(f"git tag 失败: {tag_result.stderr.strip()[:200]}")
    if has_changes:
        logger.info(f"已提交并打 tag: {tag_name}")
    else:
        logger.info(f"已打 tag (无新增变更): {tag_name}")

    # 验证执行
    verification = subtask.get("verification", "")
    verify_ok = True
    if verification and has_changes:
        logger.info(f"执行验证: {verification}")
        try:
            vr = subprocess.run(shlex.split(verification), cwd=str(worktree),
                                capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, OSError):
            if _is_safe_verification_command(verification):
                vr = subprocess.run(verification, shell=True, cwd=str(worktree),
                                    capture_output=True, text=True, timeout=120)
            else:
                logger.warning(f"验证命令不安全 (shell=True 拒绝): {verification[:100]}")
                verify_ok = True  # 跳过不安全命令的验证
                vr = None
        if vr is not None and vr.returncode != 0 and vr.returncode != 127:  # 127 = command not found
            logger.warning(f"验证失败 (rc={vr.returncode}): {vr.stderr[-300:]}")
            if headless:
                logger.info("自动重试: 注入修复指令")
                fix_prompt = task_md + (
"\n\n【验证失败】以下命令执行失败:\n"
f"  {verification}\n"
f"错误输出:\n{vr.stderr[-500:]}\n"
"请修复上述问题，确保验证命令通过。直接修改文件，不要询问。"
                )
                _run_headless(fix_prompt, worktree, env, logger, f"{sub_id}-fix")
                # 重新提交
                subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
                subprocess.run(["git", "commit", "-m",
                                f"{sub_id} (fix): 验证修复"], cwd=str(worktree),
                               capture_output=True)
                subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree),
                               capture_output=True)
                # 重新验证
                try:
                    vr2 = subprocess.run(shlex.split(verification), cwd=str(worktree),
                                         capture_output=True, text=True, timeout=120)
                except (FileNotFoundError, OSError):
                    if _is_safe_verification_command(verification):
                        vr2 = subprocess.run(verification, shell=True, cwd=str(worktree),
                                             capture_output=True, text=True, timeout=120)
                    else:
                        logger.warning(f"修复重试验证命令不安全 (跳过): {verification[:100]}")
                        verify_ok = True
                        vr2 = None
                verify_ok = vr2.returncode == 0 if vr2 is not None else verify_ok
                logger.info(f"重试验证: {'通过' if verify_ok else '仍失败'}")
                # 更新 diff
                diff2 = subprocess.run(["git", "diff", "--stat", "HEAD~1"], cwd=str(worktree),
                                       capture_output=True, text=True)
                summary = diff2.stdout.strip() or summary
            else:
                verify_ok = False
        else:
            logger.info("验证通过")

    # 生成共享上下文（供下游子任务使用）
    ctx_parts = [
        f"### {sub_id}: {subtask['title']}",
        f"- 状态: {'通过' if verify_ok else '需关注'}",
        f"- 变更: {summary}",
    ]
    if verification:
        ctx_parts.append(f"- 验证: `{verification}` — {'✅' if verify_ok else '❌'}")
    if subtask.get("risks"):
        ctx_parts.append(f"- 风险: {'; '.join(subtask['risks'])}")
    # 尝试从 Claude 输出中提取关键决策
    if headless and hasattr(result, 'stdout') and result.stdout:
        decisions = re.findall(r'(?:决策|选择|采用|改[用为]|降级|fallback)\S*[：:]\s*(.+)',
                               result.stdout, re.IGNORECASE)
        if decisions:
            ctx_parts.append(f"- 关键决策: {'; '.join(decisions[:3])}")
    ctx_parts.append("")
    # 线程安全地追加共享上下文
    shared_ctx_file = (task_dir / "SHARED_CONTEXT.md")
    _safe_append_to_file(shared_ctx_file, "\n".join(ctx_parts) + "\n", logger)
    line_count = len(shared_ctx_file.read_text(encoding="utf-8").splitlines()) if shared_ctx_file.exists() else 0
    logger.info(f"共享上下文已更新: {line_count} 行")

    # 状态判定: completed(有变更) / no_changes(完成但无变更) / failed(异常)
    if result.returncode == 0 and verify_ok:
        status = "no_changes" if summary == "无文件变更" else "completed"
    else:
        status = "failed"
    logger.info(f"─── {sub_id} DONE: {subtask['title']} [{status}] ───")
    log_event(logger, "subtask_complete", {
        "id": sub_id, "status": status, "sandbox_type": sandbox_type,
        "clone_sec": round(clone_time, 2), "claude_sec": round(claude_time, 2),
        "summary": summary, "verify_ok": verify_ok,
    })

    return {"subtask_id": sub_id, "status": status, "exit_code": result.returncode,
            "summary": summary, "worktree": str(worktree), "sandbox_type": sandbox_type,
            "verify_ok": verify_ok, "duration_sec": round(claude_time, 2),
            "agent_type_source": subtask.get("_agent_type_source", "default"),
            "skills_unresolved": unresolved_skills}
