import sys, os, subprocess, json, re, time, threading, shlex, signal, logging, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import log_event
from .utils import _format_commit, _safe_append_to_file, _is_safe_verification_command, _log_rejected_command, _slugify
from .subtask import _git_merge_upstream, _run_headless
from .agents import load_agent_type, get_claude_command, get_agent_env
from .git_utils import _worktree_create
from .metrics import collect_timing, collect_change_stats, collect_merge_result

__all__ = ["run_subtask"]


def _apply_resource_limits():
    """子进程 preexec_fn: 设置 ulimit 资源限制，防止验证命令滥用系统资源。"""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))                      # CPU 60s
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024,) * 2)     # 文件 50MB
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))                  # fd 256
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))                     # 子进程 64
    except (ValueError, OSError, ImportError):
        pass  # 限制设置失败（或不支持 resource 模块）不阻塞执行


def _build_sandbox_env():
    """构建验证命令的沙箱环境，移除敏感环境变量。"""
    env = os.environ.copy()
    _sensitive_keywords = ["API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY"]
    sensitive_keys = [k for k in env if any(s in k.upper() for s in _sensitive_keywords)]
    for k in sensitive_keys:
        env.pop(k, None)
    return env


def _create_worktree(task_id, sub_id, repo, task_dir, logger):
    """Create worktree for a subtask. Returns (worktree_path, create_time_ms)."""
    sub_dir = task_dir / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    worktree = sub_dir / "work"

    worktree_create_ms = 0
    if (worktree / ".git").exists():
        logger.info(f"worktree 已存在，跳过创建")
    elif (repo / ".git").exists():
        branch = f"agent_go/{task_id}/{sub_id}"
        wt_start = time.time()
        ok, err_msg = _worktree_create(repo, branch, worktree)
        worktree_create_ms = (time.time() - wt_start) * 1000
        if ok:
            logger.info(f"worktree 创建: 分支={branch}")
        else:
            logger.warning(f"worktree add 失败 ({err_msg})，回退到 git clone")
            worktree.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", str(repo), str(worktree)], capture_output=True, check=True)
            checkout_result = subprocess.run(["git", "checkout", "-b", branch], cwd=str(worktree), capture_output=True)
            if checkout_result.returncode != 0:
                logger.warning(f"分支创建失败: {checkout_result.stderr.strip()}")
    else:
        worktree.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(repo), str(worktree), dirs_exist_ok=True)

    return worktree, worktree_create_ms


def _build_task_md(subtask, repo, task_dir, worktree, logger, headless, merge_conflicts=None):
    """Build TASK.md content. Returns (task_md, verification, skill_names, unresolved_skills)."""
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

    # 注入直接上游子任务的共享上下文（仅依赖图中的直接上游）
    upstream_ids = subtask.get("depends_on", [])
    if upstream_ids:
        ctx_parts = []
        for up_id in upstream_ids:
            ctx_file = task_dir / up_id / "context.md"
            if ctx_file.exists():
                ctx = ctx_file.read_text(encoding="utf-8")
                if ctx.strip():
                    ctx_parts.append(ctx)
        if ctx_parts:
            task_md_parts.extend([
                "## 上游子任务上下文（仅直接依赖）",
                "以下是直接上游子任务的关键信息：",
                "\n".join(ctx_parts),
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
    task_md_text = "\n".join(task_md_parts)
    _boundary_chars = r'\s"\'\(\):/：，。、'
    _before = rf'(?<![^{_boundary_chars}])'
    _after = rf'(?![^{_boundary_chars}])'
    task_md = re.sub(
        rf'{_before}{re.escape(str(repo))}{_after}',
        str(worktree),
        task_md_text
    )

    return task_md, verification, skill_names, unresolved_skills


def _run_claude(task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger):
    """Run Claude in headless or interactive mode. Returns (result, sandbox_type, claude_time)."""
    claude_start = time.time()

    if headless:
        sandbox_type = "headless"
        result = _run_headless(task_md, worktree, env, logger, sub_id, active_pids=active_pids, active_pids_lock=active_pids_lock)
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

    return result, sandbox_type, claude_time


def _verify_changes(task_id, subtask, worktree, headless, task_md, env, tag_name,
                    active_pids, active_pids_lock, logger, issue_ref=""):
    """Verify changes, commit if needed, run verification commands. Returns verification dict."""
    # 记录变更摘要（使用 git status --porcelain 检测所有变更，包括新文件）
    status_result = subprocess.run(["git", "status", "--porcelain"], cwd=str(worktree), capture_output=True, text=True)
    has_changes = bool(status_result.stdout.strip())
    if has_changes:
        diff_result = subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=str(worktree), capture_output=True, text=True)
        tracked = diff_result.stdout.strip()
        new_files = [line[3:] for line in status_result.stdout.strip().split("\n") if line.startswith("??")]
        if tracked and new_files:
            summary = f"{tracked}\n 新增: {', '.join(new_files)}"
        elif tracked:
            summary = tracked
        elif new_files:
            summary = f"新增: {', '.join(new_files)}"
        else:
            summary = f"变更: {len(status_result.stdout.strip().split(chr(10)))} 个文件"
    else:
        summary = "无文件变更"

    # 采集结构化变更统计（在 git commit 之前）
    metrics_changes = collect_change_stats(worktree) if has_changes else {
        "files_changed": 0, "insertions": 0, "deletions": 0,
        "new_files": 0, "modified_files": 0, "actual_files": [],
    }

    # Git 提交 + tag（Conventional Commits 格式），供下游子任务 merge
    # Tag 包含 task_id 前缀，避免跨任务冲突
    git_start = time.time()
    if has_changes:
        commit_msg = _format_commit(subtask['title'], issue_ref, subtask["id"])
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

    git_commit_ms = (time.time() - git_start) * 1000

    # 验证执行（支持单条命令或命令数组）
    verification = subtask.get("verification", "")
    verify_ok = True
    retry_count = 0
    verification_results = []
    verification_ms = 0
    if verification and has_changes:
        # 统一为数组
        if isinstance(verification, str):
            cmds = [verification]
        else:
            cmds = verification
        for vi, vcmd in enumerate(cmds):
            logger.info(f"执行验证 [{vi+1}/{len(cmds)}]: {vcmd}")
            # ── 安全门禁：验证命令必须通过参数级白名单校验 ──
            safe, reason = _is_safe_verification_command(vcmd)
            if not safe:
                _log_rejected_command(vcmd, reason, logger, task_id, sub_id)
                verification_results.append({
                    "command": vcmd[:200], "exit_code": -1,
                    "duration_ms": 0, "attempt": 1,
                    "rejected": True, "reject_reason": reason,
                })
                continue
            v_start = time.time()
            vr = None
            try:
                vr = subprocess.run(shlex.split(vcmd), cwd=str(worktree),
                                    capture_output=True, text=True, timeout=60,
                                    preexec_fn=_apply_resource_limits,
                                    env=_build_sandbox_env())
            except (FileNotFoundError, OSError, ValueError):
                # shlex.split 失败时不降级到 shell=True（安全策略），记录并跳过
                logger.warning(f"验证命令无法解析为 argv (跳过): {vcmd[:100]}")
                verification_results.append({
                    "command": vcmd[:200], "exit_code": -1,
                    "duration_ms": 0, "attempt": 1,
                })
                continue
            v_duration_ms = round((time.time() - v_start) * 1000)
            verification_ms += v_duration_ms
            verification_results.append({
                "command": vcmd[:200], "exit_code": vr.returncode if vr else -1,
                "duration_ms": v_duration_ms, "attempt": 1,
            })
            if vr is not None and vr.returncode != 0 and vr.returncode != 127:
                logger.warning(f"验证失败 (rc={vr.returncode}): {vr.stderr[-300:]}")
                verify_ok = False
                if headless:
                    retry_count += 1
                    logger.info("自动重试: 注入修复指令")
                    failed_cmds = "\n".join(f"  {c}" for c in cmds)
                    fix_prompt = task_md + (
"\n\n【验证失败】以下验证命令执行失败:\n"
f"{failed_cmds}\n\n"
f"最后失败命令: {vcmd}\n"
f"错误输出:\n{vr.stderr[-500:]}\n"
"请修复上述问题，确保所有验证命令通过。直接修改文件，不要询问。"
                    )
                    _run_headless(fix_prompt, worktree, env, logger, f"{subtask['id']}-fix", active_pids=active_pids, active_pids_lock=active_pids_lock)
                    subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
                    subprocess.run(["git", "commit", "-m",
                                    f"{subtask['id']} (fix): 验证修复"], cwd=str(worktree),
                                   capture_output=True)
                    subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree),
                                   capture_output=True)
                    # 重新验证所有命令
                    retry_verify_ok = True
                    for vj, vcmd2 in enumerate(cmds):
                        # ── 安全门禁（重试路径） ──
                        safe2, reason2 = _is_safe_verification_command(vcmd2)
                        if not safe2:
                            _log_rejected_command(vcmd2, reason2, logger, task_id, sub_id)
                            verification_results.append({
                                "command": vcmd2[:200], "exit_code": -1,
                                "duration_ms": 0, "attempt": 2,
                                "rejected": True, "reject_reason": reason2,
                            })
                            continue
                        v2_start = time.time()
                        try:
                            vr2 = subprocess.run(shlex.split(vcmd2), cwd=str(worktree),
                                                 capture_output=True, text=True, timeout=60,
                                                 preexec_fn=_apply_resource_limits,
                                                 env=_build_sandbox_env())
                        except (FileNotFoundError, OSError, ValueError):
                            # shlex.split 失败时不降级到 shell=True（安全策略），记录并跳过
                            logger.warning(f"重试验证命令无法解析 (跳过): {vcmd2[:100]}")
                            verification_results.append({
                                "command": vcmd2[:200], "exit_code": -1,
                                "duration_ms": 0, "attempt": 2,
                            })
                            continue
                        v2_ms = round((time.time() - v2_start) * 1000)
                        verification_ms += v2_ms
                        verification_results.append({
                            "command": vcmd2[:200], "exit_code": vr2.returncode,
                            "duration_ms": v2_ms, "attempt": 2,
                        })
                        if vr2.returncode != 0 and vr2.returncode != 127:
                            verify_ok = False
                            retry_verify_ok = False
                            break
                        verify_ok = True
                    logger.info(f"重试验证: {'通过' if verify_ok else '仍失败'}")
                    diff2 = subprocess.run(["git", "diff", "--stat", "HEAD~1"], cwd=str(worktree),
                                           capture_output=True, text=True)
                    summary = diff2.stdout.strip() or summary
                    break  # 重试后跳出循环
                else:
                    break  # 交互模式，遇到失败即停止
            else:
                logger.info(f"验证 [{vi+1}/{len(cmds)}] 通过")

    return {
        "has_changes": has_changes,
        "summary": summary,
        "metrics_changes": metrics_changes,
        "git_commit_ms": git_commit_ms,
        "verification_ms": verification_ms,
        "verification": verification,
        "verify_ok": verify_ok,
        "retry_count": retry_count,
        "verification_results": verification_results,
    }


def _generate_context(subtask, task_dir, sub_id, logger, headless, result, verify_ok, summary, verification):
    """Generate shared context file for downstream subtasks. Writes to context.md."""
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
    # 写入独立上下文文件（仅被直接下游子任务读取）
    ctx_file = task_dir / sub_id / "context.md"
    ctx_file.write_text("\n".join(ctx_parts) + "\n", encoding="utf-8")
    line_count = len("\n".join(ctx_parts).splitlines())
    logger.info(f"上下文已写入: {line_count} 行")


def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref="", active_pids=None, active_pids_lock=None):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"─── {sub_id} START: {subtask['title']} ───")
    log_event(logger, "subtask_start", {"id": sub_id, "title": subtask["title"],
                "depends_on": subtask.get("depends_on", []), "headless": headless, "issue": issue_ref})

    clone_start = time.time()

    # 1. Create worktree
    worktree, worktree_create_ms = _create_worktree(task_id, sub_id, repo, task_dir, logger)

    # 2. Upstream merge (artifact passing)
    merge_conflicts = {}
    merge_results = []
    merge_upstream_ms = 0
    if upstream_worktrees:
        for up_id, up_path in upstream_worktrees.items():
            if up_path.exists():
                upstream_tag = f"{task_id}/{up_id}"
                logger.info(f"产物传递 (git merge): {up_id} → {sub_id} (tag={upstream_tag})")
                m_start = time.time()
                _git_merge_upstream(up_path, worktree, upstream_tag, logger, headless=headless)
                merge_upstream_ms += (time.time() - m_start) * 1000
                # 检测上游 merge 是否产生冲突
                conflict_file = worktree / ".MERGE_CONFLICT"
                has_conflict = conflict_file.exists()
                if has_conflict:
                    merge_conflicts[up_id] = conflict_file.read_text(encoding="utf-8")
                    conflict_file.unlink()
                merge_results.append(collect_merge_result(up_id, not has_conflict,
                    merge_conflicts.get(up_id, "").split("\n") if has_conflict else None))
    clone_time = time.time() - clone_start

    # 3. Build TASK.md
    task_md, verification, skill_names, unresolved_skills = _build_task_md(
        subtask, repo, task_dir, worktree, logger, headless,
        merge_conflicts=merge_conflicts
    )

    # Write TASK.md to disk
    (sub_dir / "TASK.md").write_text(task_md, encoding="utf-8")

    # Save original verification before path rewriting (for context.md)
    original_verification = verification
    # Rewrite verification command paths
    if verification and str(repo) in verification:
        _boundary_chars = r'\s"\'\(\):/：，。、'
        _before = rf'(?<![^{_boundary_chars}])'
        _after = rf'(?![^{_boundary_chars}])'
        verification = re.sub(
            rf'{_before}{re.escape(str(repo))}{_after}',
            str(worktree),
            verification
        )

    print(f"\n🚀 {sub_id}: {subtask['title']}")
    env = os.environ.copy()
    loaded_skill_names = [sn for sn in skill_names if sn not in unresolved_skills]
    env.update({"AGENT_GO_TASK_ID": task_id, "AGENT_GO_SUBTASK_ID": sub_id, "AGENT_GO_WORKTREE": str(worktree), "AGENT_GO_SKILLS": ",".join(loaded_skill_names)})

    # 4. Agent type configuration
    agent_type_name = subtask.get("agent_type", "developer")
    agent = load_agent_type(agent_type_name, repo)
    if agent:
        env.update(get_agent_env(agent))
        logger.info(f"Agent: {agent.type_name}")
    else:
        from .agents import list_agent_types
        available = [a["type"] for a in list_agent_types()]
        logger.warning(f"Agent 类型 \"{agent_type_name}\" 未注册，降级为 developer。可用: {available}")

    # 5. Run Claude
    result, sandbox_type, claude_time = _run_claude(
        task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger
    )

    # 6. Verify changes
    tag_name = f"{task_id}/{sub_id}"
    verify_results = _verify_changes(
        task_id, subtask, worktree, headless, task_md, env, tag_name,
        active_pids, active_pids_lock, logger, issue_ref=issue_ref
    )
    has_changes = verify_results["has_changes"]
    summary = verify_results["summary"]
    metrics_changes = verify_results["metrics_changes"]
    git_commit_ms = verify_results["git_commit_ms"]
    verification_ms = verify_results["verification_ms"]
    verify_ok = verify_results["verify_ok"]
    retry_count = verify_results["retry_count"]
    verification_results = verify_results["verification_results"]

    # 7. Generate context (use original verification, not path-rewritten)
    _generate_context(subtask, task_dir, sub_id, logger, headless, result, verify_ok, summary, original_verification)

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

    metrics_timing = collect_timing(worktree_create_ms, merge_upstream_ms,
                                     round(claude_time * 1000), verification_ms, git_commit_ms)

    return {"subtask_id": sub_id, "status": status, "exit_code": result.returncode,
            "summary": summary, "worktree": str(worktree), "sandbox_type": sandbox_type,
            "verify_ok": verify_ok, "duration_sec": round(claude_time, 2),
            "agent_type_source": subtask.get("_agent_type_source", "default"),
            "skills_unresolved": unresolved_skills,
            "retry_count": retry_count,
            "timing": metrics_timing,
            "change_stats": metrics_changes,
            "merge_results": merge_results,
            "verification_results": verification_results}
