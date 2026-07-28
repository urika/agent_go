import os, subprocess, re, time, shlex, shutil, logging, json, threading, signal
from pathlib import Path
from typing import Optional, Any

from .console import _LazyConsole
from .config import log_event, safe_input
from .utils import _format_commit, _is_safe_verification_command, _log_rejected_command, _safe_optional_call
from .subtask import _git_merge_upstream, _run_headless
from .agents import load_agent_type, get_claude_command, get_agent_env
from .git_utils import _worktree_create
from .metrics import collect_timing, collect_change_stats, collect_merge_result
# 解耦原则：evaluator 是可选增强，不静态 import（避免核心模块强绑增强模块的传递依赖）。
# 改为调用点（_verify_changes 内 evaluator_enabled 守卫后）动态 import。
from .config import get_api_key

console = _LazyConsole()
__all__ = ["run_subtask"]


def _resolve_env_value(value: str) -> str:
    """解析 config 字符串中的 ${VAR} 占位符为环境变量值。

    用例：config.json 中写 "api_key": "${DEEPSEEK_API_KEY}" 时，
    这个函数会从 os.environ 读取 DEEPSEEK_API_KEY 的真实值并返回。
    未匹配到 ${VAR} 占位符则原样返回。
    """
    if not isinstance(value, str) or "${" not in value:
        return value
    import re as _re
    pattern = _re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
    def _replace(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))  # 未设置时保留原样
    return pattern.sub(_replace, value)

# 模块级常量：路径替换时的边界字符集（在 _build_task_md 和 run_subtask 中共享）
_BOUNDARY_CHARS = r'\s"\'\(\):/：，。、'
_BOUNDARY_BEFORE = rf'(?<![^{_BOUNDARY_CHARS}])'
_BOUNDARY_AFTER = rf'(?![^{_BOUNDARY_CHARS}])'

_mod_logger = logging.getLogger(__name__)


def _effective_config(config: Optional[dict]) -> dict:
    """优先使用调用方传入的运行时 config（含 CLI 覆盖，如 --max-retries/--no-goal），
    否则回退磁盘配置。此前各函数一律 load_config() 读磁盘，导致 CLI 覆盖不生效。"""
    if config:
        return config
    try:
        from .config import load_config
        return load_config()
    except Exception:
        return {}


def _run_verification_cmd(vcmd: str, worktree: Path, attempt: int, env: dict, logger: logging.Logger,
                          task_id: str = "", sub_id: str = "") -> dict:
    """执行单条验证命令，返回结果 dict。避免 shlex.split 和安全门禁逻辑重复。"""
    # 剥离冗余的 cd <dir> && / cd <dir>; 前缀（agent_go 已用 cwd=worktree 执行）
    import re as _re
    vcmd = _re.sub(r'^cd\s+\S+\s*(&&|;|&)\s*', '', vcmd.strip()).strip()

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
        # S2 全量失败反馈：保留输出尾部供修复 prompt 注入
        result_entry["stdout_tail"] = (vr.stdout or "")[-1500:]
        result_entry["stderr_tail"] = (vr.stderr or "")[-1500:]
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


def _build_task_md(subtask, repo, task_dir, worktree, logger, headless, merge_conflicts=None, config=None):
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

    # Phase 2: GoalInjector — 注入目标导向指令（默认关闭，--goal 开启）
    goal_enabled = _effective_config(config).get("goal", {}).get("enabled", False)
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
        # 字面 /goal condition（设计稿 §3.4）：Claude Code 原生 goal 循环的判定条件
        # 解耦：使用 _safe_optional_call helper（utils.py）替代散落的 try/except 样板。
        try:
            from .goal_injector import GoalInjector as _GI
            _vcmds = [verification] if isinstance(verification, str) else verification
            _condition = _GI.build_goal_condition(_vcmds)
            task_md_parts.extend(["", f'/goal "{_condition}"'])
        except Exception as _goal_err:
            _mod_logger.warning(f"goal_injector 加载/调用失败，跳过 /goal 注入（不中断任务）: {_goal_err}")

    # ── Skill 知识注入 ──
    skill_names = subtask.get("skills", [])
    unresolved_skills = []
    if skill_names:
        # 解耦：动态 import + try/except——Skills 是可选增强，加载失败不中断。
        # 关键修复（ISSUE #6）：每个 skill 独立 try/except，单点失败不吞后续 skill。
        installed_names: list[str] = []
        try:
            from .skills import load_skill, render_skill_for_execution, list_skills as _list_skills
            installed_names = [s["name"] for s in _list_skills(repo)]
            task_md_parts.append("")
            for sn in skill_names:
                # 单 skill try/except：单点失败仅记警告 + 标 unresolved，不影响其他 skill
                try:
                    sk = load_skill(sn, repo)
                    if sk:
                        task_md_parts.append(render_skill_for_execution(sk))
                        task_md_parts.append("")
                        logger.info(f"Skill 注入: {sn} → TASK.md")
                    else:
                        unresolved_skills.append(sn)
                except Exception as _one_skill_err:
                    logger.warning(f"Skill 加载失败（已跳过该 skill）: {sn} — {_one_skill_err}")
                    unresolved_skills.append(sn)
        except Exception as _skill_err:
            _mod_logger.warning(f"Skills 模块加载/调用失败，跳过知识注入（不中断任务）: {_skill_err}")
        # 未解析的 skill 警告（与 try/except 平级，不在 except 块内）
        for sn in unresolved_skills:
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


    if headless:
        sandbox_type = "headless"
        allowed_tools = agent.claude_config.get("allowed_tools", []) if agent else []
        shared_activity = [None]
        _progress_stop = threading.Event()
        _last_activity_emit = [None]

        def _tick():
            start = time.time()
            while not _progress_stop.is_set():
                elapsed = int(time.time() - start)
                act = shared_activity[0]
                if act and act.get("target"):
                    console.print(f"\r➜ {sub_id}: {act['tool']} {act['target']}  ({elapsed}s)", end="")
                elif act:
                    console.print(f"\r➜ {sub_id}: {act['tool']}  ({elapsed}s)", end="")
                else:
                    console.print(f"\r➜ {sub_id}: 运行中 ({elapsed}s)", end="")
                # Bridge shared_activity to event stream (only on change)
                if act != _last_activity_emit[0]:
                    _last_activity_emit[0] = act
                    if act and act.get("target"):
                        console.emit("subtask_activity", {
                            "sub_id": sub_id,
                            "activity": f"{act['tool']} {act['target']}",
                        })
                    elif act:
                        console.emit("subtask_activity", {
                            "sub_id": sub_id,
                            "activity": f"{act['tool']}",
                        })
                _progress_stop.wait(5)

        t = threading.Thread(target=_tick, daemon=True)
        t.start()

        try:
            result = _run_headless(task_md, worktree, env, logger, sub_id, active_pids=active_pids,
                                   active_pids_lock=active_pids_lock, allowed_tools=allowed_tools,
                                   shared_activity=shared_activity)
        finally:
            _progress_stop.set()
            t.join(timeout=2)

        elapsed = int(time.time() - claude_start)
        act = shared_activity[0]
        _activity_note = f" → {act['tool']} {act['target']}" if act and act.get("target") else ""
        console.print(f"\r➜ {sub_id}: ✓ {elapsed}s{_activity_note}" + " " * 20)
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
            console.warning("Greywall 未安装，降级原生")
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
    semantic_feedback: Optional[dict] = None,
) -> str:
    """构建增强的修复提示词，注入完整失败上下文（Phase 1 验证循环）。

    包含：
    - 失败命令及其 stdout/stderr 输出
    - 当前 git diff（让 Claude 看到自己改了什么）
    - 历史修复尝试摘要（避免重复同样错误）
    - LLM 语义评估反馈（Phase 3）
    - 剩余机会提示
    """
    parts = [task_md, "", "---", ""]

    # 失败标题
    if attempt >= max_retries:
        parts.append(f"## ⚠️ 验证失败 - 第 {attempt}/{max_retries} 次修复重试（最后一次）")
    else:
        parts.append(f"## ⚠️ 验证失败 - 第 {attempt}/{max_retries} 次修复重试")
    parts.append("")

    # LLM 语义评估反馈（Phase 3）
    if semantic_feedback and not semantic_feedback.get("passed", True):
        parts.append("### LLM 语义评估反馈")
        parts.append(f"**评估结果:** 未通过")
        if semantic_feedback.get("reason"):
            parts.append(f"**原因:** {semantic_feedback['reason']}")
        if semantic_feedback.get("suggestions"):
            parts.append(f"**修复建议:** {semantic_feedback['suggestions']}")
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
        "typecheck", "analyze", "audit", "style",
    ]

    # 词边界匹配：避免子串误判（如 "echo latest" 含 "test"）
    is_deterministic = any(
        re.search(r"\b" + re.escape(kw) + r"\b", v_lower)
        for kw in DETERMINISTIC_KEYWORDS)
    is_heuristic = any(
        re.search(r"\b" + re.escape(kw) + r"\b", v_lower)
        for kw in HEURISTIC_KEYWORDS)

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


def _verify_state_path(task_dir: Path, sub_id: str) -> Path:
    """返回 verify_state.json 路径。"""
    return task_dir / sub_id / "verify_state.json"


def _load_verify_state(task_dir: Path, sub_id: str) -> Optional[dict]:
    """读取已有的验证状态（用于 resume）。"""
    path = _verify_state_path(task_dir, sub_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 简单校验字段
        if isinstance(data, dict) and data.get("subtask_id") == sub_id:
            return data
    except (json.JSONDecodeError, OSError) as e:
        _mod_logger.debug(f"读取 verify_state.json 失败: {e}")
    return None


def _persist_verify_state(
    task_dir: Path,
    sub_id: str,
    verification: str,
    retry_count: int,
    max_retries: int,
    history: list[dict],
    results: list[dict],
) -> None:
    """持久化验证状态到 verify_state.json。"""
    from datetime import datetime
    path = _verify_state_path(task_dir, sub_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "subtask_id": sub_id,
        "verification": verification,
        "attempts": retry_count + 1,
        "max_retries": max_retries,
        "history": history,
        "verification_results": results,
        "last_updated": datetime.now().isoformat(),
    }
    try:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        _mod_logger.debug(f"写入 verify_state.json 失败: {e}")


def _install_subtask_sigterm_handler(task_dir: Path, sub_id: str) -> None:
    """P2 Layer 3：subtask SIGTERM handler — 收到信号时写 verify_state.json + interrupted 标记。

    handler 必须 async-signal-safe（不能做文件 I/O），所以这里只设置一个标志。
    实际写入由 _run_headless 循环检查标志后调用 _persist_verify_state 完成。
    """
    _SUBTASK_INTERRUPTED.set()
    # 立即写一个最小化的 interrupted checkpoint（async-signal-safe write）
    try:
        path = _verify_state_path(task_dir, sub_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"interrupted": true, "subtask_id": "' + sub_id + '"}\n')
    except (OSError, IOError):
        pass


_SUBTASK_INTERRUPTED = threading.Event()  # P2 Layer 3 用的全局中断标志


def _verify_changes(task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
                    active_pids, active_pids_lock, logger, issue_ref="", allowed_tools=None,
                    task_dir=None, config=None):
    """Verify changes, commit if needed, run verification commands. Returns verification dict."""
    # Phase 1: 从运行时 config 读取 max_retries（默认 3，CLI --max-retries 可覆盖）
    _cfg = _effective_config(config)
    max_retries = _cfg.get("verification", {}).get("max_retries", 3)

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
    if has_changes:
        console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Committing changes"})

    # Phase 1 验证循环：可配置的多轮修复重试
    verification = subtask.get("verification", "")
    verify_ok = True
    retry_count = 0
    verification_results = []
    verification_ms = 0
    verification_history: list[dict] = []

	    # Phase 3: 读取 LLM 语义评估配置（运行时 config 优先，CLI --semantic-eval 可覆盖）
	    _full_cfg: dict = _effective_config(config)
	    evaluator_cfg = _full_cfg.get("evaluator", {})
	    evaluator_enabled = bool(evaluator_cfg.get("enabled", False))

	    # L1: 自动启用语义评估 — 对 heuristic/manual 验证即使未配置也强制开启
	    auto_triggered = False
	    if not evaluator_enabled and headless and has_changes and verification:
	        _l1_level = _assess_verification_confidence(verification, True).get("level", "")
	        if _l1_level in ("heuristic", "manual"):
	            evaluator_enabled = True
	            auto_triggered = True
	            logger.info(f"L1 auto: 验证置信度={_l1_level}，自动启用语义评估")

	    semantic_feedback: Optional[dict] = None
    if verification and has_changes:
        cmds = [verification] if isinstance(verification, str) else verification

        # Phase 4: 恢复已有验证状态（resume 场景）
        if task_dir:
            saved_state = _load_verify_state(task_dir, sub_id)
            if saved_state:
                saved_attempts = saved_state.get("attempts", 1)
                saved_history = saved_state.get("history", [])
                saved_results = saved_state.get("verification_results", [])
                retry_count = min(saved_attempts - 1, max_retries)
                verification_history = saved_history[-max(0, retry_count):]
                verification_results = saved_results
                logger.info(f"从 verify_state.json 恢复: 已尝试 {saved_attempts} 次")

        console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Verifying changes"})

        while retry_count <= max_retries:
            # 1. 执行所有验证命令
            all_pass = True
            failed_cmds: list[str] = []
            failed_outputs: list[str] = []
            attempt_label = retry_count + 1
            if retry_count > 0:
                console.emit("subtask_activity", {"sub_id": sub_id,
                    "activity": f"Retrying verification ({attempt_label}/{max_retries + 1})"})

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
                    # S2 全量失败反馈：exit code + stdout/stderr 尾部注入修复 prompt
                    out_parts = [f"exit_code={vr_entry['exit_code']}"]
                    if vr_entry.get("stdout_tail"):
                        out_parts.append(f"stdout:\n{vr_entry['stdout_tail']}")
                    if vr_entry.get("stderr_tail"):
                        out_parts.append(f"stderr:\n{vr_entry['stderr_tail']}")
                    failed_outputs.append("\n".join(out_parts))

            # Phase 4: 持久化验证状态
            if task_dir:
                _persist_verify_state(
                    task_dir, sub_id, verification,
                    retry_count, max_retries,
                    verification_history, verification_results)

            # 2. shell 验证全部通过 → 可选 LLM 语义评估（Phase 3）
            if all_pass and evaluator_enabled and headless:
                logger.info("shell 验证通过，执行 LLM 语义评估...")
                # 解耦：使用 _safe_optional_call helper 统一封装动态 import + try/except。
                # 评估加载/调用异常绝不中断核心流程（降级为"评估跳过"，按架构原则 fail-open）。
                semantic_feedback = _safe_optional_call(
                    ".evaluator", "evaluate_semantic", logger,
                    subtask, worktree, verification,
                    verification_history, _full_cfg, logger,
                    fallback={
                        "passed": True,
                        "reason": "语义评估失败（已跳过）",
                        "cost_usd": 0.0,
                        "latency_ms": 0.0,
                    },
                    label="evaluator.evaluate_semantic",
                )
                verification_results.append({
                    "type": "semantic",
                    "passed": semantic_feedback.get("passed", True),
                    "reason": semantic_feedback.get("reason", "")[:200],
                    "cost_usd": semantic_feedback.get("cost_usd", 0.0),
                    "latency_ms": semantic_feedback.get("latency_ms", 0.0),
                })
                if not semantic_feedback.get("passed", True):
                    logger.warning(f"LLM 语义评估未通过: {semantic_feedback.get('reason', '')[:100]}")
                    all_pass = False
                    failed_cmds = ["<semantic_eval>"]
                    failed_outputs = [f"LLM 语义评估未通过: {semantic_feedback.get('reason', '')}"]

                # Phase 4: 持久化语义评估后的状态
                if task_dir:
                    _persist_verify_state(
                        task_dir, sub_id, verification,
                        retry_count, max_retries,
                        verification_history, verification_results)

            # 3. 全部通过 → 退出
            if all_pass:
                verify_ok = True
                logger.info(f"验证全部通过 (attempt={attempt_label})")
                break

            # 4. 交互模式：显示验证卡片 + 用户决策
            if not headless:
                console.print("")  # ensures clean line position
                console.sep("━", 58)
                console.error(f"{sub_id}: 验证失败")
                for _i, _cmd in enumerate(failed_cmds):
                    console.print(f"   📋 验证命令: {_cmd[:80]}")
                if failed_outputs:
                    _tail = failed_outputs[-1].split("\n")[-6:]
                    console.warning(f"失败输出（尾部 {len(_tail)} 行）:")
                    for _l in _tail:
                        console.print(f"      {_l[:76]}")
                if summary:
                    console.print(f"   📁 文件变更: {summary[:76]}")
                console.sep("━", 58)
                console.print(f"重试: {retry_count}/{max_retries}")
                _user_skip = False
                while True:
                    _c = safe_input("[R]重试  [C]跳过  [A]中止\n> ").strip().upper()
                    if _c in ("R", "RETRY"):
                        _user_skip = False
                        break
                    elif _c in ("C", "CONTINUE"):
                        _user_skip = True
                        break
                    elif _c in ("A", "ABORT"):
                        console.error("任务已中止")
                        sys.exit(0)
                    console.print("无效输入（R=重试, C=跳过, A=中止）")
                if _user_skip:
                    verify_ok = False
                    break  # break retry loop, go to result
                # else fall through to retry logic at #6

            # 5. 已达最大重试次数 → 退出
            if retry_count >= max_retries:
                verify_ok = False
                logger.warning(f"验证失败，已达最大重试次数 ({max_retries})")
                break

            # 6. 构建修复 prompt 并执行修复
            retry_count += 1
            logger.info(f"验证失败，第 {retry_count}/{max_retries} 次修复重试")
            # S2 可观测性：每次修复重试落结构化事件，供 eval 分析
            log_event(logger, "verify_retry", {
                "sub_id": sub_id, "attempt": retry_count, "max_retries": max_retries,
                "failed_cmds": [c[:100] for c in failed_cmds],
                "exit_codes": [vr.get("exit_code") for vr in verification_results[-len(cmds):]],
                "duration_ms": round(verification_ms),
            })

            # diff --stat 在 commit 前已计算（summary）；commit 后工作区干净，git diff 必为空
            git_diff = summary

            fix_prompt = _build_repair_prompt(
                task_md, failed_cmds, failed_outputs,
                git_diff, retry_count, max_retries, verification_history,
                semantic_feedback=semantic_feedback)

            # 修复执行带硬超时（verification.retry_timeout，此前是无人读取的死配置）
            retry_timeout = _cfg.get("verification", {}).get("retry_timeout", 300)
            _run_headless(fix_prompt, worktree, env, logger, f"{subtask['id']}-fix-{retry_count}",
                          active_pids=active_pids, active_pids_lock=active_pids_lock,
                          allowed_tools=allowed_tools, hard_timeout=retry_timeout)

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

            # 重置语义反馈，下次重新评估
            semantic_feedback = None

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


def _is_simple_task(subtask: dict) -> bool:
    """判断子任务是否适合直接 API 执行（vs claude -p）。

    判定策略：
    1. architect/reviewer agent_type → 复杂（探索性任务）
    2. 关键词检测（仅保留高频误伤低的词）
    3. files_hint 通配符过多 → 涉及文件多 → 复杂
    4. 上游依赖 > 2 → 复杂
    """
    # 1. Agent type
    agent_type = subtask.get("agent_type", "developer")
    if agent_type in ("architect", "reviewer"):
        return False

    # 2. 关键词检测（谨慎选择，避免误伤）
    desc = ((subtask.get("description", "") or "") + " " +
            (subtask.get("agent_prompt", "") or "")).lower()
    exploration_keywords = [
        "探索", "调研", "重构", "迁移",
        "refactor", "migrate", "explore",
    ]
    for kw in exploration_keywords:
        if kw in desc:
            return False

    # 3. files_hint 包含大量通配符 → 涉及文件多
    files_hint = subtask.get("files_hint", "") or ""
    if files_hint.count("**") > 1:
        return False

    # 4. 依赖过多
    depends = subtask.get("depends_on", [])
    if len(depends) > 2:
        return False

    return True


def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref="", active_pids=None, active_pids_lock=None, metering_path="", config=None):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)

    # P2 Layer 3：注册 SIGTERM handler 写 verify_state.json interrupted 标记
    # 当 bench 触发 cooperative timeout 时，pipeline 会 SIGTERM claude，
    # handler 写 interrupted checkpoint，executor 后续可检测到
    try:
        import signal as _sig
        _sig.signal(_sig.SIGTERM,
                    lambda s, f: _install_subtask_sigterm_handler(task_dir, sub_id))
    except (ValueError, OSError):
        # SIGTERM handler 在子线程或某些环境下可能无法设置
        pass

    logger.info(f"─── {sub_id} START: {subtask['title']} ───")
    log_event(logger, "subtask_start", {"id": sub_id, "title": subtask["title"],
                "depends_on": subtask.get("depends_on", []), "headless": headless, "issue": issue_ref})

    clone_start = time.time()

    # 1. Create worktree
    worktree, worktree_create_ms = _create_worktree(task_id, sub_id, repo, task_dir, logger)
    console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Creating worktree"})

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
    console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Merging upstream"})
    clone_time = time.time() - clone_start

    # P4-2: 检查点快照 — 在 Claude 执行前保存 worktree 文件快照
    # （catch 所有异常，不影响主线流程）
    try:
        from .checkpoint import take_snapshot as _take_snapshot
        _take_snapshot(task_dir, sub_id, worktree, subtask.get("files_hint", ""))
    except Exception:
        logger.debug(f"[checkpoint] snapshot 失败（非关键）: {sub_id}")

    # 3. Build TASK.md
    task_md, verification, skill_names, unresolved_skills = _build_task_md(
        subtask, repo, task_dir, worktree, logger, headless,
        merge_conflicts=merge_conflicts, config=config,
    )

    # Write TASK.md to disk
    (sub_dir / "TASK.md").write_text(task_md, encoding="utf-8")
    console.emit("subtask_activity", {"sub_id": sub_id, "activity": "TASK.md ready"})

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
    console.emit("subtask_start", {
        "sub_id": sub_id,
        "title": subtask["title"],
        "depends_on": subtask.get("depends_on", []),
        "files_hint": subtask.get("files_hint", ""),
    })
    # Phase 2: Stop Hook 注入（goal.enable_goal_hook，默认关；--goal-hook 开启）
    if verification and _effective_config(config).get("goal", {}).get("enable_goal_hook", False):
        # 解耦：动态 import + try/except——Stop Hook 是可选增强，加载失败不中断。
        try:
            from .goal_injector import GoalInjector
            _vcmds = [verification] if isinstance(verification, str) else verification
            GoalInjector.inject(worktree, _vcmds)
        except Exception as _hook_err:
            logger.warning(f"GoalInjector Stop Hook 注入失败，跳过（不中断任务）: {_hook_err}")

    env = os.environ.copy()
    loaded_skill_names = [sn for sn in skill_names if sn not in unresolved_skills]
    env.update({"AGENT_GO_TASK_ID": task_id, "AGENT_GO_SUBTASK_ID": sub_id, "AGENT_GO_WORKTREE": str(worktree), "AGENT_GO_SKILLS": ",".join(loaded_skill_names)})
    # Phase 1 配套：把计量路径传给 _run_headless，让它记录 Claude 执行成本。
    # 注意：_metering_path 是 cmd_run/cmd_resume 运行时注入 config 的，磁盘上的
    # config.json 没有此键，必须用参数传入，不能 load_config() 重读。
    if metering_path:
        env["AGENT_GO_METERING_PATH"] = str(metering_path)
    # 运行时 config 的 goal 设置经 env 传给 subtask.py 的 watchdog（CLI --no-goal 等覆盖才能生效）
    if config:
        goal_cfg = config.get("goal", {})
        env["AGENT_GO_GOAL_ENABLED"] = "1" if goal_cfg.get("enabled", True) else "0"
        env["AGENT_GO_GOAL_MAX_TURNS"] = str(goal_cfg.get("max_turns", 20))
        env["AGENT_GO_GOAL_TIMEOUT"] = str(goal_cfg.get("timeout_seconds", 600))

        # 关键修复（用户需求）：把 plan_api 的 API 信息通过环境变量传给 claude 子进程。
        # 这样配置 plan_api=deepseek 后，claude -p 会用 deepseek 的 base_url + api_key（只要该端点是 Anthropic 兼容的，如 kimi-coding / 自建网关）。
        # 注意：deepseek 官方 API 是 OpenAI 格式，与 Anthropic 不兼容，无法通过此方式直接使用；
        #       但支持任意 ${VAR} 占位符（resolve_env_value 做 env 变量展开）。
        #       另外可用 worker_base_url 单独指定 worker 的 ANTHROPIC_BASE_URL（proxy/网关场景必用）。
        _plan_api = config.get("plan_api", {})
        _base_url = _resolve_env_value(_plan_api.get("base_url", ""))
        _api_key = _resolve_env_value(_plan_api.get("api_key", ""))
        if _api_key:
            env["ANTHROPIC_API_KEY"] = _api_key
        # worker_base_url：显式设置时传给 ANTHROPIC_BASE_URL（Claude Code 追加 /v1/messages）。
        # 仅适用于 Anthropic 兼容的 proxy/网关（如 LiteLLM）。空值 = 不覆盖，Claude Code 用默认
        # Anthropic API endpoint（api.anthropic.com），与 DeepSeek 原生 API 无关。
        _worker_url = _resolve_env_value(_plan_api.get("worker_base_url", ""))
        if _worker_url:
            env["ANTHROPIC_BASE_URL"] = _worker_url
        # Worker max_tokens 透传到 claude -p 子进程（Claude Code 用 CLAUDE_CODE_MAX_OUTPUT_TOKENS）
        # opus-4-7 上限 128K，256K 会被 API 自动截断
        _worker_max_tokens = _plan_api.get("worker_max_tokens", "")
        if _worker_max_tokens:
            env["AGENT_GO_MAX_TOKENS"] = str(_worker_max_tokens)
        # local_models：经过 proxy 路由到本地模型（cost=0）的模型名列表
        _local_models = _plan_api.get("local_models", [])
        if _local_models:
            env["AGENT_GO_LOCAL_MODELS"] = ",".join(str(m) for m in _local_models)

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

    # S4 复杂度双通道：按 difficulty 路由 claude 模型（空值 = CLI 默认模型）
    difficulty = subtask.get("difficulty", "medium")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    worker_models = _effective_config(config).get("worker_models", {})
    routed_model = worker_models.get(difficulty, "")
    env["AGENT_GO_DIFFICULTY"] = difficulty
    if routed_model:
        env["AGENT_GO_CLAUDE_MODEL"] = routed_model
        logger.info(f"[S4] {sub_id} difficulty={difficulty} → model={routed_model}")
        log_event(logger, "model_routing", {"sub_id": sub_id, "difficulty": difficulty, "model": routed_model})
    # worker_backends：按模型名映射 ANTHROPIC_BASE_URL（覆盖 worker_base_url 的统一值）
    _worker_backends = _effective_config(config).get("worker_backends", {})
    if routed_model and _worker_backends and routed_model in _worker_backends:
        _backend_url = _resolve_env_value(_worker_backends[routed_model])
        if _backend_url:
            env["ANTHROPIC_BASE_URL"] = _backend_url
            logger.info(f"[worker_backend] {routed_model} → {_backend_url}")

    # 5. Run Claude（含混合策略分支：简单任务 → 直接 API）
    _agent_loop_enabled = _effective_config(config).get("agent_loop", {}).get("enabled", False)
    _is_simple = _is_simple_task(subtask)
    if _agent_loop_enabled and _is_simple and headless:
        # 解耦：动态 import + try/except——AgentLoop 是可选增强（方案 C 混合策略），
        # 模块加载或执行失败时回退到传统 claude -p 路径，不中断任务。
        try:
            from .router import resolve_provider, ProviderConfig
            from .agent_loop import AgentLoop
            route = resolve_provider(subtask.get("agent_type", "developer"), config)
            if route:
                pc = route.primary
                _route_info = f"{route.role}:{pc.provider}/{pc.model}"
            else:
                _plan_api = config.get("plan_api", {})
                pc = ProviderConfig(
                    provider=_plan_api.get("provider", "anthropic"),
                    base_url=_plan_api.get("base_url", ""),
                    model=_plan_api.get("model", ""),
                )
                _route_info = f"plan_api:{pc.provider}/{pc.model}"
            # S4 复杂度双通道：按 difficulty 路由模型（非空时覆盖）
            if routed_model:
                pc.model = routed_model
                _route_info += f" → {routed_model}"
                logger.info(f"[S4] AgentLoop {sub_id} difficulty={difficulty} → model={routed_model}")
            api_key = get_api_key(config)
            loop = AgentLoop(logger=logger)
            console.print(f"  🤖 直接 API 模式 ({_route_info})")
            result = loop.run(
                prompt=task_md,
                worktree=worktree,
                pc=pc,
                api_key=api_key,
                config=config,
                tag_name=f"{task_id}/{sub_id}",
                sub_id=sub_id,
                task_id=task_id,
            )
            sandbox_type = "agent_loop"
            claude_time = 0.0
        except Exception as _loop_err:
            logger.warning(f"AgentLoop 加载/执行失败，回退到 claude -p（不中断任务）: {_loop_err}")
            # 关键修复（ISSUE #2）：AgentLoop 可能已部分修改 worktree（未 commit），
            # 必须先 git reset 清空，避免 claude -p 看到/继续 AgentLoop 的脏状态造成假阳性验证。
            try:
                import subprocess as _sp
                _sp.run(["git", "checkout", "--", "."], cwd=worktree, capture_output=True, timeout=10)
                _sp.run(["git", "clean", "-fd"], cwd=worktree, capture_output=True, timeout=10)
                logger.info(f"AgentLoop fallback: 已清空 worktree 残留改动 ({worktree})")
            except Exception as _reset_err:
                logger.warning(f"AgentLoop fallback: worktree reset 失败 ({_reset_err})，继续 fallback（claude -p 可能在脏状态上运行）")
            result, sandbox_type, claude_time = _run_claude(
                task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger
            )
    else:
        result, sandbox_type, claude_time = _run_claude(
            task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger
        )

    # 6. Verify changes
    tag_name = f"{task_id}/{sub_id}"
    verify_results = _verify_changes(
        task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
        active_pids, active_pids_lock, logger, issue_ref=issue_ref,
        allowed_tools=agent.claude_config.get("allowed_tools", []) if agent else None,
        task_dir=task_dir, config=config,
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
            # 语义评估记录无 command/exit_code 字段，单独收集
            semantic_fails = [vr for vr in verification_results
                              if vr.get("type") == "semantic" and not vr.get("passed", True)]
            if semantic_fails:
                reasons.append(f"LLM 语义评估未通过: {semantic_fails[-1].get('reason', '')[:80]}")
            if not failed_cmds and not rejected_cmds and not semantic_fails:
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
    change_stats = verify_results.get("change_stats", {})

    console.emit("subtask_complete", {
        "sub_id": sub_id,
        "status": status,
        "duration_sec": round(claude_time, 2),
        "verify_ok": verify_ok,
        "retry_count": retry_count,
        "summary": summary[:120],
        "files_changed": change_stats.get("files_changed", 0) if change_stats else 0,
        "insertions": change_stats.get("insertions", 0) if change_stats else 0,
        "deletions": change_stats.get("deletions", 0) if change_stats else 0,
    })

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
