import os, subprocess, re, time, shlex, shutil, logging
from pathlib import Path

from .console import get_default_console
from .config import log_event
from .utils import _format_commit, _is_safe_verification_command, _log_rejected_command
from .subtask import _git_merge_upstream, _run_headless
from .agents import load_agent_type, get_claude_command, get_agent_env
from .git_utils import _worktree_create
from .metrics import collect_timing, collect_change_stats, collect_merge_result

__all__ = ["run_subtask"]

# 模块级常量：路径替换时的边界字符集（在 _build_task_md 和 run_subtask 中共享）
_BOUNDARY_CHARS = r'\s"\'\(\):/：，。、'
_BOUNDARY_BEFORE = rf'(?<![^{_BOUNDARY_CHARS}])'
_BOUNDARY_AFTER = rf'(?![^{_BOUNDARY_CHARS}])'


def _run_verification_cmd(vcmd: str, worktree: Path, attempt: int, env: dict, logger: logging.Logger,
                          task_id: str = "", sub_id: str = "") -> dict:
    """执行单条验证命令，返回结果 dict。避免 shlex.split 和安全门禁逻辑重复。"""
    result_entry = {"command": vcmd[:200], "exit_code": -1, "duration_ms": 0, "attempt": attempt}

    # ── 安全门禁 ──
    safe, reason = _is_safe_verification_command(vcmd)
    if not safe:
        _log_rejected_command(vcmd, reason, logger, task_id, sub_id)
        result_entry["rejected"] = True
        result_entry["reject_reason"] = reason
        return result_entry

    # ── 执行命令 ──
    try:
        v_start = time.time()
        vr = subprocess.run(shlex.split(vcmd), cwd=str(worktree),
                            capture_output=True, text=True, timeout=120,
                            preexec_fn=_apply_resource_limits,
                            env=_build_sandbox_env())
        result_entry["exit_code"] = vr.returncode
        result_entry["duration_ms"] = round((time.time() - v_start) * 1000)
    except (FileNotFoundError, OSError, ValueError):
        logger.warning(f"验证命令无法解析为 argv (跳过): {vcmd[:100]}")
        # 不降级到 shell=True（安全策略）
    except subprocess.TimeoutExpired:
        logger.warning(f"验证命令超时 (120s): {vcmd[:100]}")
        result_entry["exit_code"] = -1

    return result_entry


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
    """构建验证命令的沙箱环境，移除敏感环境变量（保留 AGENT_GO_ 前缀变量）。"""
    env = os.environ.copy()
    _sensitive_keywords = ["API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY"]
    sensitive_keys = [k for k in env if any(s in k.upper() for s in _sensitive_keywords)
                      and not k.upper().startswith("AGENT_GO_")]
    for k in sensitive_keys:
        env.pop(k, None)
    # AGENT_GO_API_KEY 例外剔除：验证命令会执行 worktree 中 LLM 生成的代码，不得接触密钥
    env.pop("AGENT_GO_API_KEY", None)
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

    # Phase 2: GoalInjector — 注入目标导向指令
    goal_enabled = True
    try:
        from .config import load_config
        _cfg = load_config()
        goal_enabled = _cfg.get("goal", {}).get("enabled", True)
    except Exception:
        pass
    if verification and goal_enabled:
        task_md_parts.extend([
            "",
            "## /goal: 自主验证-修复循环",
            "",
            "你必须在退出前确保以下验证命令全部通过。",
            "",
            "**验证命令:**",
            f"```bash\n{verification}\n```",
            "",
            "**循环规则:**",
            "1. 完成代码修改后，运行上述验证命令",
            "2. 如果验证失败，仔细阅读错误输出，分析根本原因",
            "3. 修复代码中的问题，再次运行验证",
            "4. 重复直到所有验证命令通过",
            "5. 全部通过后才能退出（/exit 或 Ctrl+D）",
            "",
            "**注意:**",
            "- 不要跳过验证直接退出",
            "- 每次修复后必须重新运行全部验证命令",
            "- 如果连续 3 次修复仍失败，请在输出中说明原因后退出",
        ])

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
    task_md = re.sub(
        rf'{_BOUNDARY_BEFORE}{re.escape(str(repo))}{_BOUNDARY_AFTER}',
        str(worktree),
        task_md_text
    )

    return task_md, verification, skill_names, unresolved_skills


def _run_claude(task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger):
    """Run Claude in headless or interactive mode. Returns (result, sandbox_type, claude_time)."""
    claude_start = time.time()

    console = get_default_console()

    if headless:
        sandbox_type = "headless"
        # Agent 工具白名单（如 architect 只读）在 headless 下也必须强制生效
        allowed_tools = agent.claude_config.get("allowed_tools", []) if agent else []
        result = _run_headless(task_md, worktree, env, logger, sub_id, active_pids=active_pids,
                               active_pids_lock=active_pids_lock, allowed_tools=allowed_tools)
    else:
        # greywall 包装单点完成：agent 路径由 get_claude_command 内部处理，禁止重复包装
        greywall_bin = shutil.which("greywall")
        if agent:
            claude_cmd = get_claude_command(agent, worktree, headless=False)
        else:
            claude_cmd = (["greywall", "--"] if greywall_bin else []) + ["claude", str(worktree)]

        try:
            result = subprocess.run(claude_cmd, env=env, cwd=str(worktree))
            sandbox_type = "greywall" if greywall_bin else "native"
        except FileNotFoundError:
            console.print("   ⚠️ Greywall 未安装，降级原生")
            result = subprocess.run(["claude", str(worktree)], env=env, cwd=str(worktree))
            sandbox_type = "native"

    claude_time = time.time() - claude_start

    return result, sandbox_type, claude_time


def _build_repair_prompt(
    task_md: str,
    failed_cmds: list[str],
    failed_outputs: list[str],
    git_diff: str,
    attempt: int,
    max_retries: int,
    history: list[dict],
) -> str:
    """构建增强的修复提示词，注入完整失败上下文（Phase 1 验证循环）。

    包含：
    - 失败命令及其 stdout/stderr 输出
    - 当前 git diff（让 Claude 看到自己改了什么）
    - 历史修复尝试摘要（避免重复同样错误）
    - 剩余机会提示
    """
    parts = [task_md, "", "---", ""]

    # 失败标题
    if attempt >= max_retries:
        parts.append(f"## ⚠️ 验证失败 - 第 {attempt}/{max_retries} 次修复重试（最后一次）")
    else:
        parts.append(f"## ⚠️ 验证失败 - 第 {attempt}/{max_retries} 次修复重试")
    parts.append("")

    # 失败命令及输出
    parts.append("### 失败命令及输出")
    for cmd, output in zip(failed_cmds, failed_outputs):
        parts.append(f"```")
        parts.append(f"$ {cmd}")
        if output:
            # 截断过长输出
            output_trimmed = output[:2000] + "\n... (输出过长，已截断)" if len(output) > 2000 else output
            parts.append(output_trimmed)
        parts.append("```")
    parts.append("")

    # 当前变更
    if git_diff.strip():
        diff_trimmed = git_diff[:3000] + "\n... (diff 过长，已截断)" if len(git_diff) > 3000 else git_diff
        parts.append("### 当前变更")
        parts.append("```diff")
        parts.append(diff_trimmed)
        parts.append("```")
        parts.append("")

    # 历史修复尝试
    if history:
        parts.append("### 历史修复尝试")
        for h in history:
            parts.append(f"- 第 {h['attempt']} 次: {h.get('fix_summary', '未知')} → 验证仍失败: {h.get('failure_summary', '未知')}")
        parts.append("")

    # 修复指令
    parts.append("### 修复指令")
    parts.append("请仔细分析上述失败原因（特别是 stdout/stderr 输出），修复代码确保所有验证命令通过。")
    parts.append("直接修改文件，不要询问。")
    if attempt >= max_retries:
        parts.append(f"**这是最后一次修复机会。** 如果仍失败，此子任务将被标记为失败。")

    return "\n".join(parts)


def _assess_verification_confidence(verification: str, has_changes: bool) -> dict:
    """评估验证命令的可信度（M5）— 区分确定性测试 vs 启发式检查。

    返回 confidence dict:
    - level: "deterministic" | "heuristic" | "manual" | "none"
    - reason: 人类可读的评估理由
    - warning: 如果置信度低，给出警告信息

    分类逻辑：
    - deterministic: 包含测试框架关键字（test, pytest, spec, cover, assert, unittest, prove, verify）
    - heuristic: 仅包含 lint/check/format/build/compile 等静态检查
    - manual: 无验证命令，依赖用户手动确认
    - none: 无变更，无需验证
    """
    if not has_changes:
        return {"level": "none", "reason": "无变更，无需验证", "warning": ""}

    if not verification or not verification.strip():
        return {
            "level": "manual",
            "reason": "未配置验证命令",
            "warning": "⚠️ 无验证命令 — 结果仅经人工确认，可能存在假阳性",
        }

    v_lower = verification.lower()

    # 确定性测试关键字
    DETERMINISTIC_KEYWORDS = [
        "test", "pytest", "unittest", "spec", "assert",
        "cover", "coverage", "prove", "verify", "mocha",
        "jest", "rspec", "junit", "benchmark",
    ]
    # 启发式检查关键字
    HEURISTIC_KEYWORDS = [
        "lint", "check", "fmt", "format", "build", "compile",
        "typecheck", "analyze", "audit", "style", "audit",
    ]

    is_deterministic = any(kw in v_lower for kw in DETERMINISTIC_KEYWORDS)
    is_heuristic = any(kw in v_lower for kw in HEURISTIC_KEYWORDS)

    if is_deterministic:
        return {
            "level": "deterministic",
            "reason": f"验证命令包含测试/断言关键字: {verification[:80]}",
            "warning": "",
        }

    if is_heuristic:
        return {
            "level": "heuristic",
            "reason": f"验证命令仅做静态检查: {verification[:80]}",
            "warning": "⚠️ 仅静态检查 — 未运行测试，可能存在功能假阳性",
        }

    # 有命令但无法归类
    return {
        "level": "heuristic",
        "reason": f"验证命令无法归类: {verification[:80]}",
        "warning": "⚠️ 验证命令类型未知 — 建议使用测试框架（pytest/jest 等）",
    }


def _verify_changes(task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
                    active_pids, active_pids_lock, logger, issue_ref="", allowed_tools=None):
    """Verify changes, commit if needed, run verification commands. Returns verification dict."""
    # Phase 1: 从 config 读取 max_retries（默认 3）
    max_retries = 3
    try:
        from .config import load_config
        _cfg = load_config()
        max_retries = _cfg.get("verification", {}).get("max_retries", 3)
    except Exception:
        pass  # 测试环境 config 不可用时使用默认值

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

    # Phase 1 验证循环：可配置的多轮修复重试
    verification = subtask.get("verification", "")
    verify_ok = True
    retry_count = 0
    verification_results = []
    verification_ms = 0
    verification_history: list[dict] = []
    if verification and has_changes:
        cmds = [verification] if isinstance(verification, str) else verification

        while retry_count <= max_retries:
            # 1. 执行所有验证命令
            all_pass = True
            failed_cmds: list[str] = []
            failed_outputs: list[str] = []
            attempt_label = retry_count + 1

            for vcmd in cmds:
                logger.info(f"执行验证 [{attempt_label}/{max_retries + 1}]: {vcmd}")
                vr_entry = _run_verification_cmd(
                    vcmd, worktree, attempt_label, env, logger, task_id, sub_id)
                verification_results.append(vr_entry)
                verification_ms += vr_entry.get("duration_ms", 0)

                if vr_entry.get("rejected"):
                    all_pass = False
                    failed_cmds.append(vcmd)
                    failed_outputs.append(f"[拒绝] {vr_entry.get('reject_reason', '')}")
                    continue

                if vr_entry["exit_code"] not in (0, 127):
                    all_pass = False
                    failed_cmds.append(vcmd)
                    # 尝试获取失败命令的输出
                    cmd_output = ""
                    if vr_entry.get("exit_code", -1) > 0:
                        cmd_output = f"exit_code={vr_entry['exit_code']}"
                    failed_outputs.append(cmd_output)

            # 2. 全部通过 → 退出
            if all_pass:
                verify_ok = True
                logger.info(f"验证全部通过 (attempt={attempt_label})")
                break

            # 3. 交互模式：遇到失败即停止
            if not headless:
                verify_ok = False
                break

            # 4. 已达最大重试次数 → 退出
            if retry_count >= max_retries:
                verify_ok = False
                logger.warning(f"验证失败，已达最大重试次数 ({max_retries})")
                break

            # 5. 构建修复 prompt 并执行修复
            retry_count += 1
            logger.info(f"验证失败，第 {retry_count}/{max_retries} 次修复重试")

            # 获取 git diff
            diff_result = subprocess.run(
                ["git", "diff"], cwd=str(worktree),
                capture_output=True, text=True)
            git_diff = diff_result.stdout

            fix_prompt = _build_repair_prompt(
                task_md, failed_cmds, failed_outputs,
                git_diff, retry_count, max_retries, verification_history)

            _run_headless(fix_prompt, worktree, env, logger, f"{subtask['id']}-fix-{retry_count}",
                          active_pids=active_pids, active_pids_lock=active_pids_lock,
                          allowed_tools=allowed_tools)

            # git add + commit + tag
            subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
            subprocess.run(["git", "commit", "-m",
                            f"{subtask['id']} (fix-{retry_count}): 验证修复"],
                           cwd=str(worktree), capture_output=True)
            subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree), capture_output=True)

            # 记录历史
            failed_summary = "; ".join(f"{cmd[:60]}" for cmd in failed_cmds)
            verification_history.append({
                "attempt": retry_count,
                "failed_cmds": failed_cmds,
                "failure_summary": failed_summary,
                "fix_summary": f"已执行修复并重新验证",
            })

        # 更新 summary（如有修复）
        if retry_count > 0 and has_changes:
            diff2 = subprocess.run(
                ["git", "diff", "--stat", f"HEAD~{retry_count}"],
                cwd=str(worktree), capture_output=True, text=True)
            if diff2.stdout.strip():
                summary = diff2.stdout.strip()

    # 构建验证状态摘要
    verification_state = {
        "attempts": retry_count + 1,
        "max_retries": max_retries,
        "final_pass": verify_ok,
        "history": verification_history,
    }

    # M5: 评估验证质量（区分确定性测试 vs 启发式检查）
    verification_confidence = _assess_verification_confidence(verification, has_changes)

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
        "verification_confidence": verification_confidence,
        "verification_state": verification_state,
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
    console = get_default_console()
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
        verification = re.sub(
            rf'{_BOUNDARY_BEFORE}{re.escape(str(repo))}{_BOUNDARY_AFTER}',
            str(worktree),
            verification
        )

    console.print(f"\n🚀 {sub_id}: {subtask['title']}")
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
        task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
        active_pids, active_pids_lock, logger, issue_ref=issue_ref,
        allowed_tools=agent.claude_config.get("allowed_tools", []) if agent else None,
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
        failure_reason = ""
    else:
        status = "failed"
        # 收集失败原因
        reasons = []
        if result.returncode != 0:
            if sandbox_type == "headless":
                reasons.append("Claude 进程异常退出（headless 模式）")
            else:
                reasons.append("Claude 交互未正常完成")
        if not verify_ok:
            failed_cmds = [vr.get("command", "") for vr in verification_results
                           if vr.get("exit_code", 0) not in (0, -1) and not vr.get("rejected")]
            if failed_cmds:
                reasons.append(f"验证失败: {failed_cmds[0][:80]}")
            rejected_cmds = [vr.get("command", "") for vr in verification_results if vr.get("rejected")]
            if rejected_cmds:
                reasons.append(f"验证命令被拒绝: {rejected_cmds[0][:80]}")
            if not failed_cmds and not rejected_cmds:
                reasons.append("验证未通过（无变更或未知原因）")
        if merge_conflicts:
            conflicts = list(merge_conflicts.keys())
            reasons.append(f"上游合并冲突: {', '.join(conflicts)}")
        failure_reason = "; ".join(reasons) if reasons else "未知错误"
    logger.info(f"─── {sub_id} DONE: {subtask['title']} [{status}] ───")
    log_event(logger, "subtask_complete", {
        "id": sub_id, "status": status, "sandbox_type": sandbox_type,
        "clone_sec": round(clone_time, 2), "claude_sec": round(claude_time, 2),
        "summary": summary, "verify_ok": verify_ok,
    })

    metrics_timing = collect_timing(worktree_create_ms, merge_upstream_ms,
                                     round(claude_time * 1000), verification_ms, git_commit_ms)

    verification_confidence = verify_results.get("verification_confidence", {})

    return {"subtask_id": sub_id, "status": status, "exit_code": result.returncode,
            "summary": summary, "failure_reason": failure_reason,
            "worktree": str(worktree), "sandbox_type": sandbox_type,
            "verify_ok": verify_ok, "duration_sec": round(claude_time, 2),
            "agent_type_source": subtask.get("_agent_type_source", "default"),
            "skills_unresolved": unresolved_skills,
            "retry_count": retry_count,
            "verification_confidence": verification_confidence,
            "timing": metrics_timing,
            "change_stats": metrics_changes,
            "merge_results": merge_results,
            "verification_results": verification_results}
