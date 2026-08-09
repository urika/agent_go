import os, subprocess, re, sys, time, shlex, shutil, logging, json, threading, signal
from pathlib import Path
from typing import Optional, Any

from .console import _LazyConsole
from .config import log_event, safe_input, meter_event, write_censored_event
from .utils import _format_commit, _is_safe_verification_command, _log_rejected_command, _safe_optional_call
from .subtask import _git_merge_upstream, _run_headless
from .agents import load_agent_type, get_claude_command, get_agent_env
from .git_utils import _worktree_create
from .metrics import collect_timing, collect_change_stats, collect_merge_result
from .artifacts import ARTIFACT_DIR_NAME
# 解耦原则：evaluator 是可选增强，不静态 import（避免核心模块强绑增强模块的传递依赖）。
# 改为调用点（_verify_changes 内 evaluator_enabled 守卫后）动态 import。
from .config import get_api_key

console = _LazyConsole()
# 中断状态由 pipeline 按 subtask 传入；不在模块级共享，避免并发任务互相影响。
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


# 本地后端真实模型名探测缓存：{(base_url, host, port): model_name}
_local_model_probe_cache: dict = {}


def _probe_local_model(base_url: str, timeout: float = 2.0) -> str:
    """探测本地代理后端的真实模型名。

    本地代理（如 llama.cpp anthropic_proxy）通过 SIGHUP 切换模型时，
    /v1/models 只暴露固定 claude 别名，但 /status 页面暴露当前 MODEL_NAME
    （如 mlx-community/Qwen3.6-27B-4bit）。本函数解析 /status 的第一个
    "Model" 字段来获取真实后端模型名。

    Args:
        base_url: 本地后端 base URL（如 http://127.0.0.1:4000）
        timeout: HTTP 超时秒数

    Returns:
        真实模型名；探测失败时返回空字符串。
    """
    if not base_url:
        return ""
    key = base_url.rstrip("/")
    if key in _local_model_probe_cache:
        return _local_model_probe_cache[key]
    model = ""
    try:
        import urllib.request as _urlreq
        status_url = key + "/status"
        req = _urlreq.Request(status_url, headers={"User-Agent": "agent_go-probe/1.0"})
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        # 解析第一个 "Model" 字段（本地后端真实模型名）。
        # 兼容两种 HTML 结构：llama.cpp 原生 <span class="label">Model</span><span class="value">...
        # 与自定义代理页（如 Local LLM Stack）同结构。仅缓存成功结果——
        # 失败不缓存，代理 SIGHUP 切换/短暂不可达恢复后能重新探测（避免空串永久生效）。
        m = re.search(r'<span class="label">Model</span><span class="value">([^<]+)</span>', body)
        if m:
            model = m.group(1).strip()
    except Exception:
        model = ""
    if model:
        _local_model_probe_cache[key] = model
    return model


# 本地后端验证缓存：base_url → (is_really_local, actual_model)。
# 探测调用有真实 API 成本，缓存避免每子任务重复探测（代理热切换 SIGHUP 时
# 用 _local_model_probe_cache 的失效逻辑兜底——成功结果缓存，失败不缓存可重试）。
_local_verify_cache: dict[str, tuple[bool, str]] = {}


def _verify_local_backend(base_url: str, timeout: float = 45.0) -> tuple[bool, str]:
    """验证"指向本机的后端"是否真的返回本地模型。

    背景：本地代理（4000 端口）可能实际转发到云（如 glm-4.7），此时若按
    URL 判定本地并清零成本，$/pass 会严重失真。本函数做一次轻量 claude 调用，
    对比响应 model 与 /status 声明的本地模型：

      - 响应 model == 本地模型（如 mlx-community/Qwen3.6-27B-4bit）→ 真本地 (True, model)
      - 响应 model 是云模型（如 glm-4.7）且 != 本地声明 → 实际走云 (False, actual_model)
      - 探测失败/无法解析 → 保守不清零 (False, "")，宁多算不乱清

    Returns:
        (is_really_local, actual_model)：actual_model 为响应解析的真实模型；
        探测失败时 ("", 空)。
    """
    if not base_url:
        return (False, "")
    key = base_url.rstrip("/")
    if key in _local_verify_cache:
        return _local_verify_cache[key]

    _local_declared = _probe_local_model(base_url, timeout=5.0)  # /status 声明模型
    _actual = ""
    try:
        import subprocess as _sp
        # 走代理做一次轻量调用，解析真实响应 model
        _cmd = ["claude", "-p", "hi",
                "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages"]
        _env = dict(os.environ)
        _env["ANTHROPIC_BASE_URL"] = key
        # 清掉可能使 claude 走其它后端的变量
        _env.pop("ANTHROPIC_AUTH_TOKEN", None)
        _cp = _sp.run(_cmd, capture_output=True, text=True, timeout=timeout, env=_env,
                      cwd=str(Path(__file__).resolve().parent.parent))
        for _line in (_cp.stdout or "").splitlines():
            try:
                _ev = json.loads(_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(_ev, dict):
                _msg_model = _ev.get("message", {}).get("model", "")
                if not _msg_model:
                    _inner = _ev.get("event", {}) if _ev.get("type") == "stream_event" else {}
                    _msg_model = _inner.get("message", {}).get("model", "")
                if _msg_model:
                    _actual = str(_msg_model).strip()
                    break
    except Exception:
        _actual = ""

    # 判定：响应 model 是本地声明 → 真本地；否则视为走云（不清零）
    _is_local = bool(_local_declared) and bool(_actual) and _actual == _local_declared
    _result = (_is_local, _actual)
    _local_verify_cache[key] = _result
    return _result

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
        # 支持 && 链：拆分为多个子命令逐个执行（与 _is_safe_verification_command 的
        # && 拆分校验一致）。LLM 常生成 "cmd1 && cmd2" 格式验证命令；若整条 shlex.split
        # 后直接执行，&& 会变成前一个命令的普通参数（如 ruff 报 unexpected argument），
        # 导致合法命令误判失败。短路语义：任一子命令非 0 则整体失败。
        _parts = [p.strip() for p in vcmd.split("&&")]
        _exit_code = 0
        _out = ""
        _err = ""
        for _part in _parts:
            if not _part:
                continue
            vr = subprocess.run(shlex.split(_part), cwd=str(worktree),
                                capture_output=True, text=True, timeout=120,
                                preexec_fn=_apply_resource_limits,
                                env=_build_sandbox_env())
            _exit_code = vr.returncode
            _out += vr.stdout or ""
            _err += vr.stderr or ""
            if _exit_code != 0:
                break  # && 短路：前一个失败则整体失败
        result_entry["exit_code"] = _exit_code
        result_entry["duration_ms"] = round((time.time() - v_start) * 1000)
        # S2 全量失败反馈：保留输出尾部供修复 prompt 注入
        result_entry["stdout_tail"] = _out[-3000:]
        result_entry["stderr_tail"] = _err[-3000:]
    except (FileNotFoundError, OSError, ValueError):
        logger.warning(f"验证命令无法解析为 argv (跳过): {vcmd[:100]}")
        # 不降级到 shell=True（安全策略）
    except subprocess.TimeoutExpired:
        logger.warning(f"验证命令超时 (120s): {vcmd[:100]}")
        result_entry["exit_code"] = -1

    return result_entry


def _apply_resource_limits():
    """子进程 preexec_fn: 设置 ulimit 资源限制，防止验证命令滥用系统资源。

    RLIMIT_NPROC 不设置（ISSUE-31）：macOS 上 RLIMIT_NPROC 是 per-user 语义，
    限制的是"该用户所有进程总数"，而非验证命令的子进程树。当前用户已有大量
    进程（agent_go 多任务 + 后台进程累积，实测 455+）时，任何 fork 都会触发
    BlockingIOError[Errno 35]，使正确代码被误判失败。fork 炸弹防护交给
    RLIMIT_CPU（CPU 时间耗尽即杀），而非不精确的 NPROC。
    """
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))                      # CPU 60s
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024,) * 2)     # 文件 50MB
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))                  # fd 256
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


def _build_architecture_context(subtask, task_dir):
    """构建架构上下文段落（SDD 设计意图传递）。

    从 subtask 元数据生成以下信息：
    - 子任务 ID
    - 上游依赖（含 context.md 摘要）
    - 文件修改范围约束（基于 files_hint）

    返回 Markdown 字符串；无有效上下文时返回空字符串。
    """
    parts: list[str] = []

    # 子任务标识
    sub_id = subtask.get("id", "?")
    parts.append(f"- **子任务**: {sub_id}")

    # 上游依赖摘要
    upstream_ids = subtask.get("depends_on", [])
    if upstream_ids:
        upstream_summaries = []
        for up_id in upstream_ids:
            ctx_file = task_dir / up_id / "context.md"
            if ctx_file.exists():
                ctx = ctx_file.read_text(encoding="utf-8").strip()
                if ctx:
                    # 取 context.md 的第一行作为摘要（通常是标题）
                    first_line = ctx.split("\n")[0].lstrip("#").strip()
                    upstream_summaries.append(f"{up_id}（已完成 — {first_line}）")
                else:
                    upstream_summaries.append(f"{up_id}（已完成）")
            else:
                upstream_summaries.append(f"{up_id}")
        parts.append(f"- **依赖的上游**: {', '.join(upstream_summaries)}")

    # 文件范围约束
    files_hint = subtask.get("files_hint", "")
    if files_hint and files_hint.strip() != "*":
        scope_files = [f.strip() for f in files_hint.split(",") if f.strip()]
        if scope_files:
            parts.append("- **范围约束**: 你只能修改以下文件：")
            for sf in scope_files:
                parts.append(f"  - `{sf}`")

    # 禁止修改约束（SDD 边界传递，防止越界改动导致交叉污染）
    do_not_touch = subtask.get("do_not_touch", []) or []
    if do_not_touch:
        parts.append("- **禁止修改（do_not_touch）**: 以下文件/模块不属于本子任务，绝对不要改动：")
        for f in do_not_touch:
            parts.append(f"  - `{f}`")
        parts.append("  若你发现需要修改这些文件才能完成任务，请停止并说明原因（可能存在拆分或依赖设计问题）。")

    # scope_boundary 语义边界（Planner 标注的职责边界）
    scope_boundary = subtask.get("scope_boundary", "")
    if scope_boundary and scope_boundary.strip():
        parts.append(f"- **职责边界**: {scope_boundary.strip()}")

    if len(parts) <= 1:
        # 只有子任务 ID，没有有效上下文
        return ""

    return "## 架构上下文\n\n" + "\n".join(parts) + "\n"


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

    # ── SDD 设计意图传递：架构上下文（子任务位置、上游、范围约束）──
    arch_ctx = _build_architecture_context(subtask, task_dir)
    if arch_ctx:
        task_md_parts.append(arch_ctx)

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
        "- **你必须生成实际的代码变更。仅分析/阅读代码而不做修改将被视为任务失败。**",
        "- **不要仅输出想法或计划 — 直接修改代码文件。**",
        "- **不要自行 git commit** — 修改文件后保留为未提交状态，编排层会在验证通过后统一提交（commit 是完成边界）。你只需确保验证通过。",
    ]
    if verification:
        exec_requirements.append(f"- **必须执行验证**: `{verification}`，确保通过后再完成")
        exec_requirements.append("- 如验证失败，请修复问题后重新验证，直到通过")
    if not headless:
        exec_requirements.append("- 完成后退出 Claude Code（/exit 或 Ctrl+D）")
    task_md_parts.extend(exec_requirements)

    # S9-B 产物导出约定：--artifact-dir 开启时注入 __artifacts__/ 目录约定
    # 声明制——只有写入 __artifacts__/ 的文件才视为交付物，随 worktree 清理不丢失
    if _effective_config(config).get("artifact_dir"):
        task_md_parts.extend([
            "",
            "## 产物输出",
            f"如需生成文档/表格/演示文稿等非代码交付物，写入 `{ARTIFACT_DIR_NAME}/` 目录。",
            "该目录下的文件将在任务完成后导出到指定位置（--artifact-dir），不会随 worktree 清理丢失。",
        ])

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


def _run_claude(task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger, config=None, hard_timeout=0):
    """Run Claude in headless or interactive mode. Returns (result, sandbox_type, claude_time).

    hard_timeout: 首跑硬超时（秒），0=不限制。仅 headless 模式生效，透传给 _run_headless。
    """
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
                                   shared_activity=shared_activity,
                                   hard_timeout=hard_timeout,
                                   config=_effective_config(config))
            # S12-P0 G1：result 自带 kill_reason 属性（_run_headless 写入），
            # 由 run_subtask 读取后传入 _verify_changes 归因。
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
    scope_violation: Optional[dict] = None,
) -> str:
    """构建增强的修复提示词，注入完整失败上下文（Phase 1 验证循环）。

    包含：
    - 范围偏差（L1 Scope Compliance）：越界改动 / 遗漏改动的文件级对比
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

    # ── L1 范围合规：文件级偏差（SDD 设计意图验证）──
    if scope_violation and not scope_violation.get("compliant", True):
        parts.append("### ⚠️ 范围偏差")
        parts.append("")
        expected_files = scope_violation.get("expected", [])
        if expected_files:
            parts.append("你的任务范围只包含以下文件：")
            for ef in expected_files:
                parts.append(f"- `{ef}`")
            parts.append("")
        out_of_scope = scope_violation.get("out_of_scope", [])
        if out_of_scope:
            parts.append("**越界改动（请撤销这些文件的修改）：**")
            for oos in out_of_scope:
                parts.append(f"- `{oos}`")
            parts.append("")
        missing = scope_violation.get("missing", [])
        if missing:
            parts.append("**遗漏改动（这些文件在范围内但未被修改，请补上）：**")
            for ms in missing:
                parts.append(f"- `{ms}`")
            parts.append("")
        if out_of_scope or missing:
            parts.append("**先撤销越界改动，再补上遗漏改动，最后重新运行验证。**")
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


def _meter_cost_for_sub(metering_path: str, sub_id: str) -> float:
    """聚合 metering.jsonl 中某子任务（sub_id）的累计成本。

    用于成本控制 L2：重试循环每次修复前读取该子任务已花费，
    超预算则停止修复。metering 事件挂靠 role=worker/evaluator 且带 sub_id。
    """
    if not metering_path:
        return 0.0
    total = 0.0
    try:
        with open(metering_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("sub_id") == sub_id and ev.get("event") != "cost_censored":
                    total += ev.get("cost_usd", 0.0) or 0.0
    except OSError:
        return 0.0
    return total


def _metering_available(metering_path: str) -> bool:
    if not metering_path:
        return False
    try:
        with open(metering_path, encoding="utf-8"):
            return True
    except OSError:
        return False


def _check_scope_compliance(worktree, files_hint):
    """L1 范围合规检查：比对实际改动文件 vs files_hint 预期范围（确定性，零 LLM 成本）。

    files_hint 为 "*" 或空时跳过检查（全文件范围或无约束）。
    返回 {"compliant": bool, "out_of_scope": [str], "missing": [str], "expected": [str], "actual": [str]}
    """
    # 跳过：无范围约束
    if not files_hint or files_hint.strip() == "*":
        return {"compliant": True, "out_of_scope": [], "missing": [], "expected": [], "actual": []}

    # 解析预期文件范围
    expected = {f.strip() for f in files_hint.split(",") if f.strip()}
    if not expected:
        return {"compliant": True, "out_of_scope": [], "missing": [], "expected": [], "actual": []}

    # 获取实际改动文件：先查未提交变更，再查已提交变更
    actual_files: set[str] = set()
    try:
        # 未提交变更（初始执行后、commit 前）
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"], cwd=str(worktree),
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            actual_files.update(f.strip() for f in r.stdout.strip().split("\n") if f.strip())
        # 已暂存变更（git add 后、commit 前）
        r2 = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "HEAD"], cwd=str(worktree),
            capture_output=True, text=True, timeout=10)
        if r2.returncode == 0 and r2.stdout.strip():
            actual_files.update(f.strip() for f in r2.stdout.strip().split("\n") if f.strip())
        # 已提交变更（commit 后、验证循环中）
        if not actual_files:
            r3 = subprocess.run(
                ["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(worktree),
                capture_output=True, text=True, timeout=10)
            if r3.returncode == 0 and r3.stdout.strip():
                actual_files.update(f.strip() for f in r3.stdout.strip().split("\n") if f.strip())
    except Exception:
        return {"compliant": True, "out_of_scope": [], "missing": [],
                "expected": sorted(expected), "actual": []}

    out_of_scope = sorted(actual_files - expected)
    missing = sorted(expected - actual_files)

    return {
        "compliant": len(out_of_scope) == 0 and len(missing) == 0,
        "out_of_scope": out_of_scope,
        "missing": missing,
        "expected": sorted(expected),
        "actual": sorted(actual_files),
    }


def _defect_fingerprint(reason: str) -> str:
    """从语义评估失败 reason 提取缺陷指纹。

    打地鼠检测用：指纹应捕获「指出的缺陷是什么」，而非文本差异。策略：
    1. 去除非中英文字符、空白、标点
    2. 提取高频主题词（中文 bigram + 英文单词）
    指纹为排序后的主题词集合字符串；不同缺陷 → 主题词集差异大。
    """
    import re as _re
    text = _re.sub(r"[^\w\u4e00-\u9fff]+", "", (reason or "").lower())
    if len(text) < 4:
        return ""
    tokens: set[str] = set()
    # 英文单词（≥3 字符）与英文标识符（如 AttributeError、NoneType）
    tokens.update(_re.findall(r"[a-z][a-z0-9_]{2,}", text))
    # 中文 bigram（相邻两字，捕获中文缺陷描述）
    cjk = _re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update("".join(cjk[i:i + 2]) for i in range(len(cjk) - 1))
    # 去掉过于常见的噪声词（多为表述性动词/副词，不具判别力）
    noise = {"the", "and", "that", "with", "this", "have", "from", "code", "test",
             "代码", "实现", "问题", "需要", "应该", "当前", "没有", "存在", "进行",
             "以及", "由于", "仍然", "还是", "导致", "造成", "出现", "仍然", "缺少",
             "加载", "数据", "函数", "方法", "调用", "返回", "之后", "已经", "是否",
             "无法", "不能", "没有", "报错", "失败", "异常", "错误", "还有", "并且",
             "继续", "再次", "重新", "测试", "检查", "修复", "修改", "添加", "删除"}
    tokens = {t for t in tokens if t not in noise}
    if not tokens:
        return ""
    return " ".join(sorted(tokens))


def _defect_similarity(fp_a: str, fp_b: str) -> float:
    """计算两个缺陷指纹的相似度（Jaccard）。"""
    set_a = set(fp_a.split())
    set_b = set(fp_b.split())
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _verify_changes(task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
                    active_pids, active_pids_lock, logger, issue_ref="", allowed_tools=None,
                    task_dir=None, config=None, interrupt_event=None, initial_kill_reason=None):
    """Verify changes, commit if needed, run verification commands. Returns verification dict."""
    # S12-P0 G1：追踪本子任务最后一次 _run_headless 的 kill_reason（运行时 kill 分类）。
    # initial_kill_reason 来自首次 _run_claude 的 result（_run_headless 在 subprocess 内运行）。
    _latest_kill_reason = [initial_kill_reason]

    # Phase 1: 从运行时 config 读取 max_retries（默认 3，CLI --max-retries 可覆盖）
    _cfg = _effective_config(config)
    max_retries = _cfg.get("verification", {}).get("max_retries", 3)

    # 中断事件由 pipeline 创建；直接调用时使用独立事件，绝不跨 subtask 共享。
    interrupt_event = interrupt_event or threading.Event()

    git_ok = True

    # 按 difficulty 缩减重试预算：easy → 2, medium → 3, hard → 5
    difficulty = subtask.get("difficulty", "medium")
    _difficulty_caps = {"easy": 2, "medium": 3, "hard": 5}
    max_retries = min(max_retries, _difficulty_caps.get(difficulty, 3))
    # S12-P1 G4：budget_mode=degrade 时，降档模型子任务 max_retries 降为 1，
    # 避免在便宜模型上无限烧钱（"延长死亡时间"而非保产出）。
    if (config or {}).get("_degraded"):
        _cap_deg = max_retries
        max_retries = min(max_retries, 1)
        if max_retries != _cap_deg:
            logger.warning(f"[degrade] {sub_id} max_retries {_cap_deg}→{max_retries}（降级模式）")

    # 记录变更摘要（使用 git status --porcelain 检测所有变更，包括新文件）
    status_result = subprocess.run(["git", "status", "--porcelain"], cwd=str(worktree), capture_output=True, text=True)
    has_changes = bool(status_result.stdout.strip())
    _self_committed = False  # worker 自行提交标记：True 时跳过重复 commit，只打 tag
    # 稳健性：worker 可能自行 commit，此时工作区干净但已有变更。
    # 优先基于本次运行记录的 base_commit 判断，避免把近期人工提交误认成 worker 产出。
    if not has_changes:
        try:
            _head_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(worktree),
                capture_output=True, text=True, timeout=10).stdout.strip()
            _head_msg = subprocess.run(
                ["git", "log", "-1", "--format=%s", "HEAD"], cwd=str(worktree),
                capture_output=True, text=True, timeout=10).stdout.strip()
            _base_commit = (_cfg.get("_base_commit", "") if isinstance(_cfg, dict) else "")
            if _base_commit:
                _ancestor = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", _base_commit, _head_hash],
                    cwd=str(worktree), capture_output=True, timeout=10)
                _parents = subprocess.run(
                    ["git", "show", "-s", "--format=%P", "HEAD"], cwd=str(worktree),
                    capture_output=True, text=True, timeout=10).stdout.split()
                _is_worker_commit = (_ancestor.returncode == 0 and _head_hash != _base_commit
                                     and len(_parents) == 1)
            else:
                # 兼容旧任务：没有 base_commit 时保守保留旧时间窗口逻辑。
                _head_date = subprocess.run(
                    ["git", "log", "-1", "--format=%at", "HEAD"], cwd=str(worktree),
                    capture_output=True, text=True, timeout=10).stdout.strip()
                _is_worker_commit = bool(_head_date and _head_date.isdigit()
                                         and time.time() - int(_head_date) < 600)
            if _is_worker_commit:
                _self_commit_stat = subprocess.run(
                    ["git", "show", "--stat", "--format=", "HEAD"], cwd=str(worktree),
                    capture_output=True, text=True, timeout=10).stdout.strip()
                has_changes = True
                _self_committed = True  # worker 已自行提交，跳过重复 commit，只打 tag
                summary = f"worker 自行提交: {_head_msg}\n{_self_commit_stat}" if _self_commit_stat else f"worker 自行提交: {_head_msg}"
                logger.info(f"[self_commit] {subtask['id']}: 识别到 base_commit 之后的 worker commit ({_head_hash[:8]})")
        except Exception as _sc_err:
            logger.debug(f"[self_commit] 检查失败（忽略）: {_sc_err}")
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
    if _self_committed:
        # worker 已自行提交：工作区干净，跳过重复 commit（否则 nothing-to-commit 非零误判失败），只打 tag
        logger.info(f"[self_commit] {sub_id}: 跳过重复 commit，直接打 tag")
    elif has_changes:
        commit_msg = _format_commit(subtask['title'], issue_ref, subtask["id"])
        add_result = subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        if add_result.returncode != 0:
            git_ok = False
            logger.warning(f"git add 失败: {add_result.stderr.strip()}")
        if git_ok:
            commit_result = subprocess.run(["git", "commit", "-m", commit_msg],
                                           cwd=str(worktree), capture_output=True)
            if commit_result.returncode != 0:
                git_ok = False
                logger.warning(f"git commit 失败: {commit_result.stderr.strip()[:200]}")
    if git_ok:
        tag_result = subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree), capture_output=True)
        if tag_result.returncode != 0:
            git_ok = False
            logger.warning(f"git tag 失败: {tag_result.stderr.strip()[:200]}")
    if git_ok and has_changes:
        logger.info(f"已提交并打 tag: {tag_name}")
    elif git_ok and _self_committed:
        logger.info(f"已打 tag (worker 自行提交): {tag_name}")
    elif git_ok:
        logger.info(f"已打 tag (无新增变更): {tag_name}")
    else:
        logger.warning(f"Git 完成边界失败，禁止将 {sub_id} 作为成功结果传递")

    git_commit_ms = (time.time() - git_start) * 1000
    commit_hash = ""
    if git_ok:
        _hash_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(worktree),
            capture_output=True, text=True,
        )
        if _hash_result.returncode == 0:
            commit_hash = _hash_result.stdout.strip()
    if has_changes:
        console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Committing changes"})

    # Phase 1 验证循环：可配置的多轮修复重试
    verification = subtask.get("verification", "")
    # 有验证命令但无变更：不再直接判失败，而是进入验证循环执行验证——
    # 若验证通过则算成功（no_changes），失败才算失败。这修复「任务已满足、
    # 无需变更但被误判失败」的场景（如函数已存在、claude 确认验证通过）。
    agent_type_check = subtask.get("agent_type", "developer")
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
    _l1_confidence_dict: Optional[dict] = None
    if not evaluator_enabled and headless and has_changes and verification:
        _l1_confidence_dict = _assess_verification_confidence(verification, True)
        if _l1_confidence_dict.get("level") in ("heuristic", "manual"):
            evaluator_enabled = True
            auto_triggered = True
            logger.info(f"L1 auto: 验证置信度={_l1_confidence_dict['level']}，自动启用语义评估")

    semantic_feedback: Optional[dict] = None
    # 打地鼠检测（改进方向 4）：记录每次语义评估失败指出的缺陷指纹。
    # 若连续两次语义评估指出的缺陷类型显著不同（相似度 < 阈值）→ agent 在
    # 「修 A 漏 B」打地鼠而非收敛 → 提前终止重试，避免浪费 token。
    _semantic_fail_fingerprints: list[str] = []
    if verification:
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
                # 截断 verification_results：只保留低于当前 attempt 的 shell 结果，
                # 丢弃语义评估结果（循环中会重新评估）。当前 attempt 的 cmd 结果
                # 可能是中断时部分执行的，需丢弃让循环重新生成——避免 resume 时重复累积。
                _cur_attempt = retry_count + 1
                verification_results = [
                    r for r in saved_results
                    if r.get("type") != "semantic"
                    and r.get("attempt", 0) and r.get("attempt", 0) < _cur_attempt
                ]
                logger.info(f"从 verify_state.json 恢复: 已尝试 {saved_attempts} 次, "
                            f"截断 verification_results: {len(saved_results)}→{len(verification_results)}")

        console.emit("subtask_activity", {"sub_id": sub_id, "activity": "Verifying changes"})

        _verify_loop_start = time.time()

        while retry_count <= max_retries:
            # P2 Layer 3b: 中断信号→退出验证循环（由 _install_subtask_sigterm_handler 设置）
            if interrupt_event.is_set():
                logger.warning(f"收到 SIGTERM 信号，退出验证循环")
                break
            # 1. 执行所有验证命令
            all_pass = True
            failed_cmds: list[str] = []
            failed_outputs: list[str] = []
            # 安全门禁拒绝的验证命令 (command, reason)：触发 G8 短路，跳过修复重试
            _rejected_cmd = None
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
                    # S12-P1 G8 模式：安全门禁拒绝的命令无法通过修复重试解决
                    # （修复只改代码，不会让该命令变得可执行），记录后短路退出。
                    all_pass = False
                    failed_cmds.append(vcmd)
                    failed_outputs.append(f"[拒绝] {vr_entry.get('reject_reason', '')}")
                    _rejected_cmd = (vcmd, vr_entry.get("reject_reason", ""))
                    break

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

            # S12-P1 G8 模式：验证命令被安全门禁拒绝 → 直接判定失败并短路，
            # 跳过修复重试（不调用修复逻辑、不增加 retry_count；resume 亦不重复修复）。
            if _rejected_cmd is not None:
                verify_ok = False
                logger.warning(
                    f"[G8] {sub_id} 验证命令被安全门禁拒绝: {_rejected_cmd[0][:100]} — "
                    f"原因: {_rejected_cmd[1]}，跳过修复重试")
                break

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
                        "confidence": 0.5,
                        "reason": "语义评估失败（已跳过）",
                        "cost_usd": 0.0,
                        "latency_ms": 0.0,
                    },
                    label="evaluator.evaluate_semantic",
                    assessment_path=str(task_dir) if task_dir else "",
                    verification_confidence=_l1_confidence_dict if auto_triggered else None,
                )
                verification_results.append({
                    "type": "semantic",
                    "passed": semantic_feedback.get("passed", True),
                    "reason": semantic_feedback.get("reason", "")[:200],
                    "cost_usd": semantic_feedback.get("cost_usd", 0.0),
                    "latency_ms": semantic_feedback.get("latency_ms", 0.0),
                })
                # 截断兜底：若 evaluator 因 diff 截断导致无法判断（reason 含关键词，
                # 或置信度极低），用完整 diff 无截断重跑一次 evaluator，避免浪费修复重试。
                _reason = semantic_feedback.get("reason", "") or ""
                _conf = semantic_feedback.get("confidence", 1.0)
                _inconclusive = any(kw in _reason for kw in ["截断", "无法确认", "inconclusive"])
                _low_confidence = _conf is not None and _conf <= 0.3
                if (_inconclusive or _low_confidence) and not semantic_feedback.get("passed", True):
                    logger.warning(
                        f"语义评估可能因信息不足误判: confidence={_conf} inconclusive={_inconclusive}，"
                        f"用完整 diff 重试 evaluator"
                    )
                    try:
                        _retry_cfg = dict(_full_cfg)
                        _retry_cfg["_no_diff_truncation"] = True  # evaluator 内部禁用 diff 截断
                        _retry_feedback = _safe_optional_call(
                            ".evaluator", "evaluate_semantic", logger,
                            subtask, worktree, verification,
                            verification_history, _retry_cfg, logger,
                            fallback=semantic_feedback,
                            label="evaluator.evaluate_semantic_retry",
                            assessment_path=str(task_dir) if task_dir else "",
                            verification_confidence=_l1_confidence_dict if auto_triggered else None,
                        )
                        if _retry_feedback is not semantic_feedback:
                            # 替换 verification_results 中上次的记录
                            verification_results[-1] = {
                                "type": "semantic",
                                "passed": _retry_feedback.get("passed", True),
                                "reason": _retry_feedback.get("reason", "")[:200],
                                "cost_usd": _retry_feedback.get("cost_usd", 0.0),
                                "latency_ms": _retry_feedback.get("latency_ms", 0.0),
                            }
                            semantic_feedback = _retry_feedback
                            logger.info(f"重试 evaluator 完成: passed={semantic_feedback.get('passed')} conf={semantic_feedback.get('confidence')}")
                    except Exception as _retry_err:
                        logger.debug(f"语义评估截断重试失败（已忽略）: {_retry_err}")
                if semantic_feedback.get("evaluator_skipped"):
                    logger.warning(f"语义评估 API 调用失败（已跳过，结果视为通过）")
                    if _cfg.get("evaluator", {}).get("fail_closed", False):
                        logger.warning(f"fail_closed=True，标记为失败")
                        all_pass = False
                        failed_cmds = ["<semantic_eval>"]
                        failed_outputs = [f"语义评估 API 调用失败（fail_closed）: {semantic_feedback.get('reason', '')}"]
                elif not semantic_feedback.get("passed", True):
                    logger.warning(f"LLM 语义评估未通过: {semantic_feedback.get('reason', '')[:100]}")
                    all_pass = False
                    failed_cmds = ["<semantic_eval>"]
                    failed_outputs = [f"LLM 语义评估未通过: {semantic_feedback.get('reason', '')}"]
                    # 打地鼠检测：记录本次缺陷指纹
                    _reason = str(semantic_feedback.get("reason", "") or "")
                    _fp = _defect_fingerprint(_reason)
                    if _fp:
                        _semantic_fail_fingerprints.append(_fp)

                # Phase 4: 持久化语义评估后的状态
                if task_dir:
                    _persist_verify_state(
                        task_dir, sub_id, verification,
                        retry_count, max_retries,
                        verification_history, verification_results)

            # 3. 全部通过 → 退出（ISSUE-32：验证通过但存在超范围改动时记录审计）
            if all_pass:
                verify_ok = True
                logger.info(f"验证全部通过 (attempt={attempt_label})")
                try:
                    _scope_check = _check_scope_compliance(worktree, subtask.get("files_hint", ""))
                    if _scope_check and not _scope_check.get("compliant", True):
                        _oos = _scope_check.get("out_of_scope", [])
                        _miss = _scope_check.get("missing", [])
                        logger.warning(
                            f"[L1] {sub_id} 验证通过但范围偏差: "
                            f"out_of_scope={_oos[:5]} missing={_miss[:5]}"
                        )
                        verification_results.append({
                            "type": "scope_compliance",
                            "passed": False,
                            "out_of_scope": _oos[:10],
                            "missing": _miss[:10],
                            "reason": "验证通过但存在超范围改动（审计记录）",
                        })
                except Exception:
                    # scope 检查失败（worktree 异常等）不阻断通过
                    pass
                break

            # S12-P1 G8：验证循环 kill_reason 感知（不重试预算熔断）
            # over_budget_l2/l3 → 直接 Failed，不进重试（再花更多钱在已超预算任务上违背约束）
            # stuck/hard_timeout/goal_* → 正常 verify 但重试预算已受限
            # （CR-L1：cleanup_race 仅在 bench 度量层合成，运行时不写入子任务 kill_reason，
            #  此分支为防御性保留——若未来运行时引入该信号可直接视为通过，语义无害）
            _kill_reason_now = _latest_kill_reason[0] or ""
            if isinstance(_kill_reason_now, str) and _kill_reason_now.startswith("over_budget"):
                verify_ok = False
                logger.warning(
                    f"[G8] {sub_id} kill_reason={_kill_reason_now}：预算熔断，跳过修复重试")
                break
            if _kill_reason_now == "cleanup_race":
                verify_ok = True
                logger.info(
                    f"[G8] {sub_id} kill_reason=cleanup_race：任务实际已完成，视为通过")
                break

            # 4. 验证失败展示：交互卡片 / --yes 自动重试倒计时 / headless 静默重试
            _elapsed_sec = int(time.time() - _verify_loop_start)
            _ins = metrics_changes.get("insertions", 0)
            _del = metrics_changes.get("deletions", 0)
            _change_summary = f"+{_ins}/-{_del}" if _ins or _del else summary[:60]

            if not headless:
                # ── 交互模式：结构化验证卡片 ──
                _card_width = 62
                console.force("")
                console.force("┌─ " + f"❌ {sub_id} 验证失败".ljust(_card_width - 4, "─") + "┐")
                console.force(f"│ 重试: {retry_count}/{max_retries}   耗时: {_elapsed_sec}s   变更: {_change_summary}".ljust(_card_width) + "│")
                console.force("├─" + "─" * (_card_width - 2) + "┤")
                for _i, _cmd in enumerate(failed_cmds):
                    console.force(f"│ 📋 验证命令: {_cmd[:60]}".ljust(_card_width) + "│")
                if failed_outputs:
                    _tail = failed_outputs[-1].split("\n")[-8:]
                    console.force(f"│ ⚠️  失败输出（尾部 {len(_tail)} 行）".ljust(_card_width) + "│")
                    for _l in _tail:
                        for _line in _l.split("\n"):
                            _trunc = _line[:60]
                            console.force(f"│   {_trunc}".ljust(_card_width) + "│")
                if summary:
                    console.force(f"│ 📁 文件变更".ljust(_card_width) + "│")
                    for _s in summary.split("\n")[:3]:
                        console.force(f"│   {_s[:60]}".ljust(_card_width) + "│")
                console.force("├─" + "─" * (_card_width - 2) + "┤")
                console.force(f"│ [R] 重试  [C] 跳过  [A] 中止".ljust(_card_width) + "│")
                console.force("└" + "─" * (_card_width - 2) + "┘")
                _user_skip = False
                while True:
                    _c = safe_input("\n> ").strip().upper()
                    if _c in ("R", "RETRY"):
                        _user_skip = False
                        break
                    elif _c in ("C", "CONTINUE"):
                        _user_skip = True
                        break
                    elif _c in ("A", "ABORT"):
                        console.error("任务已中止")
                        sys.exit(0)
                    console.force("无效输入（R=重试, C=跳过, A=中止）")
                if _user_skip:
                    verify_ok = False
                    break  # break retry loop, go to result
                # else fall through to retry logic at #6

            elif sys.stdin.isatty():
                # ── --yes + TTY 模式：自动重试倒计时（允许 Ctrl+C 中止）──
                console.force(f"\r  [R] 自动重试中... (第 {retry_count + 1}/{max_retries + 1} 次)  按 Ctrl+C 中止")
                try:
                    for _i in range(5, 0, -1):
                        console.force(f"\r  [R] 自动重试中... {_i}s  ", end="")
                        time.sleep(1)
                except KeyboardInterrupt:
                    console.force("\n⏸ 用户中断，跳过重试")
                    verify_ok = False
                    break

            # 5. 已达最大重试次数 → 退出
            if retry_count >= max_retries:
                verify_ok = False
                logger.warning(f"验证失败，已达最大重试次数 ({max_retries})")
                break

            # 改进方向 4：打地鼠检测——连续两次语义评估指出不同缺陷 → 提前终止
            # （agent 在「修 A 漏 B」打地鼠而非收敛，继续重试只是浪费 token）
            _fp_len = len(_semantic_fail_fingerprints)
            if _fp_len >= 2:
                _sim = _defect_similarity(
                    _semantic_fail_fingerprints[-2], _semantic_fail_fingerprints[-1])
                _diverge_threshold = float(_cfg.get("verification", {}).get(
                    "diverge_similarity_threshold", 0.3))
                if _sim < _diverge_threshold:
                    verify_ok = False
                    logger.warning(
                        f"[打地鼠检测] 连续两次语义评估指出不同缺陷 "
                        f"(similarity={_sim:.2f} < {_diverge_threshold})，提前终止重试")
                    log_event(logger, "verify_divergence", {
                        "sub_id": sub_id, "attempt": retry_count,
                        "similarity": round(_sim, 4), "max_retries": max_retries,
                        "reasons": _semantic_fail_fingerprints[-2:],
                    })
                    verification_results.append({
                        "type": "divergence",
                        "passed": False,
                        "similarity": round(_sim, 4),
                        "reason": "连续两次语义评估指出不同缺陷（打地鼠），提前终止",
                    })
                    _latest_kill_reason[0] = "verify_divergence"
                    break

            # S10 成本控制 L2：子任务累计成本上限（跨重试，防修复循环烧钱）。
            # 每次修复前读取 metering.jsonl 中该子任务累计 cost，超 per_subtask_budget×系数则
            # 停止修复（final fail）。默认关闭（cost_control.enabled=False 不检查）。
            _cc_cfg = _cfg.get("cost_control") or {}
            if _cc_cfg.get("enabled"):
                _meter_path = (_effective_config(config) or {}).get("_metering_path", "")
                if _meter_path and not _metering_available(_meter_path):
                    verify_ok = False
                    _latest_kill_reason[0] = "metering_unavailable"
                    logger.error("成本计量不可用，停止验证修复重试")
                    break
                _budgets = _cc_cfg.get("per_subtask_budget_usd", {}) or {}
                _sub_budget = (_budgets.get(difficulty, _budgets.get("medium", 0.0))
                               if isinstance(_budgets, dict) else _budgets)
                _sub_mult = _cc_cfg.get("subtask_multiplier", 2.5)
                _sub_limit = float(_sub_budget or 0) * _sub_mult
                if _sub_limit > 0 and _meter_path:
                    _sub_cost = _meter_cost_for_sub(_meter_path, sub_id)
                    if _sub_cost >= _sub_limit:
                        verify_ok = False
                        _latest_kill_reason[0] = "over_budget_l2"
                        logger.warning(
                            f"[cost_control L2] 子任务 {sub_id} 累计成本 ${_sub_cost:.4f} "
                            f"≥ 上限 ${_sub_limit:.4f}（{_sub_budget}×{_sub_mult}），停止修复重试")
                        write_censored_event(_meter_path, level="L2", sub_id=sub_id,
                                             spent=_sub_cost, budget=_sub_limit,
                                             reason=f"子任务累计成本 ${_sub_cost:.4f} ≥ 上限 ${_sub_limit:.4f}")
                        break

            # 6. 构建修复 prompt 并执行修复
            if interrupt_event.is_set():
                logger.warning(f"收到 SIGTERM，跳过第 {retry_count + 1} 次修复")
                break
            retry_count += 1
            # retry 时升级模型：worker_models_fallback 配置了升级目标则切换
            _fb_models = _cfg.get("worker_models_fallback", {})
            _sub_diff = subtask.get("difficulty", "medium")
            _fallback_model = _fb_models.get(_sub_diff, "")
            if _fallback_model and env.get("AGENT_GO_CLAUDE_MODEL", "") != _fallback_model:
                env["AGENT_GO_CLAUDE_MODEL"] = _fallback_model
                logger.info(f"[model_upgrade] retry={retry_count} difficulty={_sub_diff} → {_fallback_model}")
                log_event(logger, "model_upgrade", {"sub_id": sub_id, "retry": retry_count,
                          "difficulty": difficulty, "model": _fallback_model})
            logger.info(f"验证失败，第 {retry_count}/{max_retries} 次修复重试")
            # S2 可观测性：每次修复重试落结构化事件，供 eval 分析
            log_event(logger, "verify_retry", {
                "sub_id": sub_id, "attempt": retry_count, "max_retries": max_retries,
                "failed_cmds": [c[:100] for c in failed_cmds],
                "exit_codes": [vr.get("exit_code") for vr in verification_results[-len(cmds):]],
                "duration_ms": round(verification_ms),
            })
            # 同时写入 metering.jsonl（统一计量审计），挂靠 role=worker
            _cfg_for_meter = _effective_config(config)
            meter_event(_cfg_for_meter.get("_metering_path", "") if _cfg_for_meter else "", {
                "role": "worker",
                "virtual_model": "agentgo-worker",
                "actual_provider": "verification",
                "actual_model": "verify_retry",
                "event": "verify_retry",
                "sub_id": sub_id,
                "attempt": retry_count,
                "max_retries": max_retries,
                "failed_cmds": [c[:100] for c in failed_cmds],
                "exit_codes": [vr.get("exit_code") for vr in verification_results[-len(cmds):]],
                "duration_ms": round(verification_ms),
                "result": "retry",
                "cost_usd": 0.0,
                "task_id": _cfg_for_meter.get("_task_id", "") if _cfg_for_meter else "",
            })

            # diff --stat 在 commit 前已计算（summary）；commit 后工作区干净，git diff 必为空
            git_diff = summary

            # ── L1 范围合规检查：比对实际改动文件 vs files_hint 预期范围 ──
            scope_violation = _check_scope_compliance(worktree, subtask.get("files_hint", ""))

            fix_prompt = _build_repair_prompt(
                task_md, failed_cmds, failed_outputs,
                git_diff, retry_count, max_retries, verification_history,
                semantic_feedback=semantic_feedback,
                scope_violation=scope_violation)

            # 修复执行带硬超时（verification.retry_timeout，按 difficulty 弹性缩放）
            _difficulty = subtask.get("difficulty", "medium")
            _base_timeout = _cfg.get("verification", {}).get("retry_timeout", 300)
            _difficulty_mult = {"easy": 1, "medium": 1.5, "hard": 2.5}.get(_difficulty, 1.5)
            # CR-建议#2：retry_timeout 封顶按难度缩放——hard 任务（db-*/可观测性）修复重试
            # 900s 偏紧（撞墙钟→假 timeout）。easy/medium 保持，hard 放宽到 1500s。
            _retry_caps = {"easy": 600, "medium": 900, "hard": 1500}
            _cap = _retry_caps.get(_difficulty, 900)
            retry_timeout = min(int(_base_timeout * _difficulty_mult), _cap)
            logger.info(f"[retry_timeout] difficulty={_difficulty} base={_base_timeout}s mult={_difficulty_mult} → timeout={retry_timeout}s")
            _fix_result = _run_headless(fix_prompt, worktree, env, logger, f"{subtask['id']}-fix-{retry_count}",
                                        active_pids=active_pids, active_pids_lock=active_pids_lock,
                                        allowed_tools=allowed_tools, hard_timeout=retry_timeout,
                                        config=_cfg)
            # S12-P0 G1：捕获 fix 重试的 kill_reason（修复超时等）
            # 仅接受真实字符串值（防御 MagicMock 等测试替身对象）
            _fix_kr = getattr(_fix_result, "kill_reason", None)
            if isinstance(_fix_kr, str) and _fix_kr:
                _latest_kill_reason[0] = _fix_kr

            # git add + commit + tag
            fix_add = subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
            fix_commit = subprocess.run(["git", "commit", "-m",
                                         f"{subtask['id']} (fix-{retry_count}): 验证修复"],
                                        cwd=str(worktree), capture_output=True) if fix_add.returncode == 0 else None
            fix_tag = subprocess.run(["git", "tag", "-f", tag_name], cwd=str(worktree), capture_output=True) \
                if fix_commit is not None and fix_commit.returncode == 0 else None
            if (fix_add.returncode != 0 or fix_commit is None or fix_commit.returncode != 0 or
                    fix_tag is None or fix_tag.returncode != 0):
                git_ok = False
                verify_ok = False
                logger.warning("验证修复后的 Git 提交/tag 失败，停止重试")
                break

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
            # fix 重试后重新采集变更统计，反映修复合计的变更
            metrics_changes = collect_change_stats(worktree)

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
        "git_ok": git_ok,
        "commit_hash": commit_hash,
        "retry_count": retry_count,
        "verification_results": verification_results,
        "verification_confidence": verification_confidence,
        "verification_state": verification_state,
        # S12-P0 G1：子任务级 kill_reason（运行时 kill 分类，供度量侧归因）
        "kill_reason": _latest_kill_reason[0] or ("none" if verify_ok else None),
    }


def _generate_context(subtask, task_dir, sub_id, logger, headless, result, verify_ok, summary, verification, retry_count=0, verification_state=None):
    """Generate shared context file for downstream subtasks. Writes to context.md."""
    ctx_parts = [
        f"### {sub_id}: {subtask['title']}",
        f"- 状态: {'通过' if verify_ok else '需关注'}",
        f"- 变更: {summary}",
    ]
    if retry_count > 0:
        attempts = (verification_state or {}).get("attempts", retry_count + 1)
        ctx_parts.append(f"- 修复重试: {retry_count}/{max(0, attempts - 1)} 次")
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


def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref="", active_pids=None, active_pids_lock=None, metering_path="", config=None, interrupt_event=None):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    interrupt_event = interrupt_event or threading.Event()

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
    except Exception as _cp_err:
        logger.warning(f"[checkpoint] snapshot 失败（非关键）: {sub_id}: {_cp_err}")

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
    # S12-P1 G4 + S12-P2：budget_mode=degrade 时，剩余子任务按对称降级表
    # worker_models_degrades（config）降档模型（如 hard→medium），并标 degraded=True，
    # 让最终验收人知道这部分由便宜模型产出。
    difficulty = subtask.get("difficulty", "medium")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    worker_models = _effective_config(config).get("worker_models", {})
    routed_model = worker_models.get(difficulty, "")
    # CR-G3：task_type 路由（优先于 difficulty）。task_type 由 Spec `task_type:` 字段或
    # role_skill_map 关键词检测得出（plan_to_subtasks 写入子任务）；配了
    # worker_models_by_type[type] 则覆盖难度路由，未配回退难度（degrade/fallback 仍在其后生效）。
    _task_type = subtask.get("task_type")
    if _task_type:
        _by_type = _effective_config(config).get("worker_models_by_type", {}) or {}
        _type_model = _by_type.get(_task_type, "")
        if _type_model:
            routed_model = _type_model
            logger.info(f"[G3] {sub_id} task_type={_task_type} → model={_type_model}（覆盖 difficulty={difficulty}）")
    env["AGENT_GO_DIFFICULTY"] = difficulty
    _is_degraded = bool(config and config.get("_degraded"))
    if _is_degraded:
        _degrades_cfg = _effective_config(config).get("worker_models_degrades", {}) or {}
        # 兼容旧式数组降档链（worker_models_degrades 未配置时回退到 builtin）
        if _degrades_cfg and isinstance(_degrades_cfg, dict):
            _deg_target = _degrades_cfg.get(difficulty, "")
        else:
            _downgrade_chain = ["hard", "medium", "easy", ""]
            try:
                _di = _downgrade_chain.index(difficulty)
            except ValueError:
                _di = 1
            _deg_target = _downgrade_chain[_di + 1] if _di < len(_downgrade_chain) - 1 else ""
        _deg_model = worker_models.get(_deg_target, "") if _deg_target else ""
        if _deg_model:
            routed_model = _deg_model
            logger.warning(f"[degrade] {sub_id} difficulty={difficulty} → 降档模型 {_deg_target}={_deg_model}")
    if routed_model:
        env["AGENT_GO_CLAUDE_MODEL"] = routed_model
        logger.info(f"[S4] {sub_id} difficulty={difficulty} → model={routed_model}")
        log_event(logger, "model_routing", {"sub_id": sub_id, "difficulty": difficulty,
                                             "task_type": _task_type, "model": routed_model})
    # worker_backends：按模型名映射 ANTHROPIC_BASE_URL（覆盖 worker_base_url 的统一值）
    _worker_backends = _effective_config(config).get("worker_backends", {})
    _backend_url = ""
    if routed_model and _worker_backends and routed_model in _worker_backends:
        _backend_url = _resolve_env_value(_worker_backends[routed_model])
        if _backend_url:
            env["ANTHROPIC_BASE_URL"] = _backend_url
            logger.info(f"[worker_backend] {routed_model} → {_backend_url}")
    # 本地后端检测：worker_backends / worker_base_url 指向本机（127.0.0.1/localhost）
    # 时，标记为本地模型（成本清零），并把真实后端模型名透传给 subtask
    # （claude 会把 claude-haiku-4-5 等路由名硬编码映射成 deepseek-v4-flash
    # 之类的内部名，无法反映真实本地后端）。
    # 真实模型名解析优先级：
    #   1. 探测本地代理 /status（支持模型热切换，如 SIGHUP 后从
    #      Qwen3.6-27B-4bit 切到别的模型也能自动识别）
    #   2. local_model_names 静态映射（routed_model → 真实名）
    #   3. 回退 routed_model 本身
    _plan_api_cfg = config.get("plan_api", {}) if config else {}
    _worker_url_src = _backend_url or _resolve_env_value(_plan_api_cfg.get("worker_base_url", ""))
    _is_local_url = bool(_worker_url_src) and re.search(r"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])", _worker_url_src)
    if _is_local_url:
        # S12 本地判定加固：URL 指向本机 ≠ 一定走本地模型——4000 代理可能实际转发到云
        # （如 glm-4.7）。做一次轻量探测调用验证响应 model：
        #   响应 == /status 声明本地模型 → 真本地（AGENT_GO_IS_LOCAL=1，成本清零）
        #   响应是云模型             → 实际走云（不清零，按实际模型计价）
        #   探测失败                 → 保守不清零（宁多算不乱清）
        _really_local, _actual_model = _verify_local_backend(_worker_url_src)
        if _really_local:
            env["AGENT_GO_IS_LOCAL"] = "1"
            _local_model_name = _actual_model or _probe_local_model(_worker_url_src)
            if not _local_model_name:
                _local_names_cfg = _effective_config(config).get("local_model_names", {})
                if isinstance(_local_names_cfg, dict) and routed_model in _local_names_cfg:
                    _local_model_name = str(_local_names_cfg[routed_model])
            if not _local_model_name:
                _local_model_name = routed_model
            if _local_model_name:
                env["AGENT_GO_LOCAL_MODEL"] = _local_model_name
            logger.info(f"[worker_local] {routed_model} → 本地后端 {_worker_url_src} (model={_local_model_name})")
        else:
            # 实际走云：不强设 AGENT_GO_IS_LOCAL，按响应模型计价（若可解析）
            env.pop("AGENT_GO_IS_LOCAL", None)
            if _actual_model:
                # 透传实际响应模型给 subtask 成本重算（覆盖 claude 报告价）
                env["AGENT_GO_ACTUAL_MODEL"] = _actual_model
            logger.warning(
                f"[worker_local] {routed_model} → {_worker_url_src} 验证为云后端"
                f"(响应 model={_actual_model or '未知'})，成本按实际模型计价（不清零）")

    # 5. Run Claude（含混合策略分支：简单任务 → 直接 API）
    # 首跑硬超时（verification.run_timeout × 难度系数）：父进程存活但子进程失控时的兜底。
    # 与 stuck 检测（事件/文件/CPU 全静默）互补——即使持续活动但永不完成（如死循环），
    # 到点即 kill，kill_reason=hard_timeout。0=禁用。
    _initial_timeout = 0
    _run_tcfg = _effective_config(config).get("verification", {}) or {}
    _run_base = _run_tcfg.get("run_timeout", 0) or 0
    if _run_base > 0:
        _run_mult = {"easy": 1, "medium": 1.5, "hard": 2.5}.get(difficulty, 1.5)
        _initial_timeout = min(int(_run_base * _run_mult), 7200)
        logger.info(f"[run_timeout] {sub_id} difficulty={difficulty} base={_run_base}s mult={_run_mult} → timeout={_initial_timeout}s")
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
                task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger,
                config=config, hard_timeout=_initial_timeout,
            )
    else:
        result, sandbox_type, claude_time = _run_claude(
            task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger,
            config=config, hard_timeout=_initial_timeout,
        )

    # 6. Verify changes
    tag_name = f"{task_id}/{sub_id}"
    verify_results = _verify_changes(
        task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name,
        active_pids, active_pids_lock, logger, issue_ref=issue_ref,
        allowed_tools=agent.claude_config.get("allowed_tools", []) if agent else None,
        task_dir=task_dir, config=config, interrupt_event=interrupt_event,
        initial_kill_reason=getattr(result, "kill_reason", None),
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
    _generate_context(subtask, task_dir, sub_id, logger, headless, result, verify_ok, summary,
                      original_verification, retry_count=retry_count,
                      verification_state=verify_results.get("verification_state"))

    # 状态判定: completed(产出验证通过) / no_changes / failed(产出无效或任务未完成)
    # CR-失败修复：verify_ok（产出有效性）是主信号；claude 进程崩溃(returncode≠0)但**产生了可验证
    # 变更** → 仍计完成（交付有效），仅记 infra 异常标记。rc≠0 且无变更（如 agent_loop 撞
    # max_turns 未产出）→ 仍 failed（任务未完成），不因 verify_ok 空真而误判成功。
    _has_changes = summary != "无文件变更"
    if verify_ok and verify_results.get("git_ok", True) and (result.returncode == 0 or _has_changes):
        status = "no_changes" if not _has_changes else "completed"
        failure_reason = ""
        if result.returncode != 0:
            logger.warning(
                f"[executor] {sub_id} claude 进程异常退出 (rc={result.returncode}) 但产出验证通过，"
                f"按完成计（infra 异常，非能力失败）")
            verify_results["crash_but_verified"] = True
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
        if not verify_results.get("git_ok", True):
            reasons.append("Git 提交或 tag 失败")
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
    from .failure import classify_failure
    failure_class = classify_failure({
        "status": status,
        "verify_ok": verify_ok,
        "exit_code": result.returncode,
        "kill_reason": verify_results.get("kill_reason"),
        "crash_but_verified": verify_results.get("crash_but_verified", False),
        "verification_results": verification_results,
    })

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
             "commit_hash": verify_results.get("commit_hash", ""),
             "failure_class": failure_class,
            "agent_type_source": subtask.get("_agent_type_source", "default"),
            "skills_unresolved": unresolved_skills,
            "retry_count": retry_count,
            "verification_confidence": verification_confidence,
            "timing": metrics_timing,
            "change_stats": metrics_changes,
            "merge_results": merge_results,
            "verification_results": verification_results,
            # S12-P0 G1：子任务级 kill_reason（none/stuck/hard_timeout/over_budget_l2/...）
            "kill_reason": verify_results.get("kill_reason") if isinstance(verify_results, dict) else None,
            # S12-P1 G4：budget_mode=degrade 降档模型产出标记
            "degraded": bool(_is_degraded)}
