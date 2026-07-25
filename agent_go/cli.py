import sys, os, subprocess, json, time, logging, argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Optional


from .config import load_config, safe_input, setup_logger, AGENT_GO_DIR, log_event
from .console import Console, set_default_console
from .api import generate_plan, decompose_fallback
from .ui import confirm_plan, plan_to_md, plan_to_subtasks, confirm_subtasks
from .utils import read_reference_docs, _detect_tool_versions
from .pipeline import _run_pipeline
from .skills import load_skills, discover_skills, render_skill_for_plan, list_skills
from .agents import load_agent_type, list_agent_types
from .eval import cmd_eval
from .tui import cmd_status_tui
from .workflow_gen import cmd_ci

logger = logging.getLogger(__name__)

__all__ = [
    "main", "cmd_run", "cmd_resume", "cmd_list", "cmd_show",
    "cmd_status", "cmd_config", "cmd_clean", "cmd_pr", "cmd_review",
    "cmd_router",
]

def _build_parser():
    """构建 argparse parser"""
    parser = argparse.ArgumentParser(
        prog="agent_go",
        description="Plan Mode orchestration tool - wraps Claude Code with structured Plan -> Decompose -> Execute workflow",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="Plan, decompose and execute a task")
    run_parser.add_argument("repo", help="Path to the repository")
    run_parser.add_argument("task", nargs="?", default="请根据项目情况完成改进", help="Task description")
    run_parser.add_argument("--docs", help="Comma-separated list of reference document paths")
    run_parser.add_argument("--skill", help="Comma-separated list of skill names to load")
    run_parser.add_argument("--agent-type", dest="agent_type", help="Default agent type for all subtasks")
    run_parser.add_argument("--yes", "-y", action="store_true", help="Skip all confirmations (headless mode)")
    run_parser.add_argument("--headless", action="store_true", help="Run subtasks in headless mode")
    run_parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-error output")
    run_parser.add_argument("--verbose", action="store_true", help="Show debug/diagnostic output")
    run_parser.add_argument("--issue", type=int, dest="issue_ref", help="GitHub issue number to link")
    run_parser.add_argument("--parallel", type=int, default=1, help="Max concurrent subtasks (default: 1)")
    run_parser.add_argument("--remote", help="Push worktree branches to remote URL")
    run_parser.add_argument("--no-cache", action="store_true", help="Skip plan cache lookup")
    run_parser.add_argument("--max-retries", type=int, default=None,
                            help="验证失败后最大修复重试次数（默认 3）")
    run_parser.add_argument("--no-goal", action="store_true",
                            help="禁用 goal 指令注入（TASK.md 不追加 /goal）")
    run_parser.add_argument("--semantic-eval", action="store_true",
                            help="启用 LLM 语义评估（覆盖 config）")
    run_parser.add_argument("--no-semantic-eval", action="store_true",
                            help="禁用 LLM 语义评估")
    run_parser.add_argument("--preserve-worktrees", action="store_true", dest="preserve_worktrees",
                            help="保留全部 worktree 不清除（默认仅保留 failed/blocked）")
    run_parser.add_argument("--no-preserve", action="store_true", dest="no_preserve",
                            help="强制清理所有 worktree，包括失败/阻断的")
    run_parser.add_argument("--no-verify-block", action="store_true", dest="no_verify_block",
                            help="验证失败不阻断下游依赖（默认阻断）")
    run_parser.add_argument("--goal", action="store_true",
                            help="启用 goal 指令注入（TASK.md 追加 /goal 循环，默认关闭）")
    run_parser.add_argument("--goal-hook", action="store_true", dest="goal_hook",
                            help="注入 Stop Hook（.claude/settings.json + verify-goal.sh，默认关闭）")
    run_parser.add_argument("--agent-loop", action="store_true",
                            help="启用混合策略：简单任务走直接 API，复杂任务保留 claude -p（默认关闭）")

    # resume 子命令
    resume_parser = subparsers.add_parser("resume", help="Resume a paused/interrupted task")
    resume_parser.add_argument("task_id", help="Task ID to resume")
    resume_parser.add_argument("--yes", "-y", action="store_true", help="Skip all confirmations")
    resume_parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    resume_parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-error output")
    resume_parser.add_argument("--parallel", type=int, default=1, help="Max concurrent subtasks")
    resume_parser.add_argument("--remote", help="Push worktree branches to remote URL")
    resume_parser.add_argument("--max-retries", type=int, default=None,
                               help="验证失败后最大修复重试次数（默认 3）")
    resume_parser.add_argument("--preserve-worktrees", action="store_true", dest="preserve_worktrees",
                               help="保留全部 worktree 不清除")
    resume_parser.add_argument("--no-preserve", action="store_true", dest="no_preserve",
                               help="强制清理所有 worktree")
    resume_parser.add_argument("--no-verify-block", action="store_true", dest="no_verify_block",
                               help="验证失败不阻断下游依赖（默认阻断）")

    # list 子命令
    subparsers.add_parser("list", help="List all historical tasks")

    # show 子命令
    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("task_id", help="Task ID to show")

    # status 子命令
    status_parser = subparsers.add_parser("status", help="Live status monitoring")
    status_parser.add_argument("--watch", "-w", action="store_true", help="Auto-refresh status")
    status_parser.add_argument("--no-tui", action="store_true", help="Text mode instead of TUI")
    status_parser.add_argument("--verbose", "-v", action="store_true", help="Show Claude events")

    # clean 子命令
    subparsers.add_parser("clean", help="Remove all task data")

    # config 子命令
    subparsers.add_parser("config", help="View current configuration")

    # skills 子命令
    subparsers.add_parser("skills", help="List available Skills")

    # agents 子命令
    subparsers.add_parser("agents", help="List available Agent types")

    # pr 子命令
    pr_parser = subparsers.add_parser("pr", help="Generate and create PR")
    pr_parser.add_argument("task_id", help="Task ID to create PR from")
    pr_parser.add_argument("--offline", action="store_true", help="Only generate PR.md, do not create PR")

    # ci 子命令
    ci_parser = subparsers.add_parser("ci", help="Generate GitHub Actions workflow")
    ci_parser.add_argument("repo", nargs="?", help="Path to the repository (default: current dir)")
    ci_parser.add_argument("--dry-run", action="store_true", help="Print workflow without writing file")

    # review 子命令
    review_parser = subparsers.add_parser("review", help="Review task results or code")
    review_parser.add_argument("repo", nargs="?", help="Path to the repository to review")
    review_parser.add_argument("--task", dest="task_id", help="Task ID to review results (M7)")
    review_parser.add_argument("--pr", dest="pr_ref", help="PR number to review")
    review_parser.add_argument("--yes", "-y", action="store_true", help="Run in headless mode")
    review_parser.add_argument("--comment", action="store_true", help="Post findings as inline PR comments")
    review_parser.add_argument("--fix", action="store_true", help="Apply fixes to working tree")
    review_parser.add_argument("--approve", action="store_true", help="Approve the review")
    review_parser.add_argument("--reject", action="store_true", help="Reject the review")

    # cache 子命令
    cache_parser = subparsers.add_parser("cache", help="Plan cache management")
    cache_parser.add_argument("subcommand", nargs="?", choices=["list", "clean", "clear", "stats"],
                              help="Cache operation: list|clean|clear|stats")

    # eval 子命令
    eval_parser = subparsers.add_parser("eval", help="Quality/performance/cost evaluation")
    eval_parser.add_argument("subcommand", choices=["quality", "perf", "cost", "reliability", "ux", "all"],
                             help="Evaluation type")
    eval_parser.add_argument("task_id", nargs="?", help="Task ID to evaluate")
    eval_parser.add_argument("--all", dest="eval_all", action="store_true", help="Evaluate all tasks")

    # inspect 子命令
    inspect_parser = subparsers.add_parser("inspect", help="查看保留的 worktree 现场")
    inspect_parser.add_argument("task_id", help="Task ID to inspect")
    inspect_parser.add_argument("--json", action="store_true", help="Output as JSON")
    inspect_parser.add_argument("--all", action="store_true", help="Show all subtasks, not just preserved ones")

    # router 子命令
    router_parser = subparsers.add_parser("router", help="Role-aware model routing configuration")
    router_sub = router_parser.add_subparsers(dest="router_subcommand", help="Router operation")
    router_sub.add_parser("show", help="Show current router configuration")
    router_sub.add_parser("enable", help="Enable role-aware routing")
    router_sub.add_parser("disable", help="Disable role-aware routing")
    set_role_parser = router_sub.add_parser("set-role", help="Configure a role's provider")
    set_role_parser.add_argument("role", choices=["planner", "worker", "reviewer"],
                                 help="Role to configure")
    set_role_parser.add_argument("--provider", required=True, help="Provider: anthropic|openai|deepseek|custom")
    set_role_parser.add_argument("--model", required=True, help="Model name")
    set_role_parser.add_argument("--base-url", required=True, help="API base URL")
    set_role_parser.add_argument("--fallback-provider", help="Fallback provider")
    set_role_parser.add_argument("--fallback-model", help="Fallback model name")
    set_role_parser.add_argument("--fallback-base-url", help="Fallback API base URL")

    return parser


def cmd_run(args=None):
    if args is None:
        parser = _build_parser()
        args = parser.parse_args()
        if args.command != "run":
            # Fallback: dispatch non-run commands
            main()
            return

    # 从 argparse 结果提取参数
    repo = Path(args.repo).resolve()
    task = args.task
    doc_paths = [p.strip() for p in args.docs.split(",")] if args.docs else []
    skill_names = [s.strip() for s in args.skill.split(",")] if args.skill else []
    agent_type_name = args.agent_type or ""
    issue_ref = str(args.issue_ref) if args.issue_ref else ""
    remote_url = args.remote or ""
    no_cache = args.no_cache
    max_retries = args.max_retries
    no_goal = args.no_goal
    semantic_eval = args.semantic_eval
    no_semantic_eval = args.no_semantic_eval
    preserve_worktrees = args.preserve_worktrees or None
    if args.no_preserve:
        preserve_worktrees = False
    auto_yes = args.yes
    headless = auto_yes or args.headless
    parallel = args.parallel
    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)

    # 初始化 Console 实例（headless / --yes 隐含 quiet 模式）
    console = Console(quiet=quiet or headless, verbose=verbose)
    set_default_console(console)

    # 并发模式要求 headless（避免同时打开多个交互式 Claude Code 终端）
    if parallel > 1 and not headless:
        console.warning("并发模式 (--parallel) 需要无头模式 (--headless / --yes)，已自动切换到串行执行。")
        parallel = 1

    if not repo.exists():
        console.error(f"路径不存在: {repo}")
        sys.exit(1)

    config = load_config()
    config["_parallel"] = parallel  # M4: 时间预估用
    if max_retries is not None:
        config.setdefault("verification", {})["max_retries"] = max_retries
    if no_goal:
        config.setdefault("goal", {})["enabled"] = False
    if semantic_eval:
        config.setdefault("evaluator", {})["enabled"] = True
    if no_semantic_eval:
        config.setdefault("evaluator", {})["enabled"] = False
    if getattr(args, "no_verify_block", False):
        config.setdefault("verification", {})["block_on_failure"] = False
    if getattr(args, "goal", False):
        config.setdefault("goal", {})["enabled"] = True
    if getattr(args, "goal_hook", False):
        config.setdefault("goal", {})["enable_goal_hook"] = True
    if getattr(args, "agent_loop", False):
        config.setdefault("agent_loop", {})["enabled"] = True

    if auto_yes:
        config["behavior"]["auto_confirm_plan"] = True
        config["behavior"]["auto_confirm_subtasks"] = True
        config["behavior"]["auto_verify_subtask"] = True

    # 生成唯一任务 ID：时间戳(毫秒精度) + 随机后缀，防止碰撞
    for _ in range(5):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{datetime.now().microsecond // 1000:03d}"
        suffix = os.urandom(2).hex()
        task_id = f"task-{ts}-{suffix}"
        task_dir = AGENT_GO_DIR / task_id
        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            # 任务 ID 碰撞，重试下一轮
            time.sleep(0.01)
    else:
        task_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1 配套：结构化计量日志路径
    config["_metering_path"] = str(task_dir / "metering.jsonl")
    config["_task_id"] = task_id

    logger = setup_logger(task_id, task_dir)
    logger.info("=" * 60)
    logger.info("任务启动")
    logger.info(f"ID: {task_id}, 任务: {task}, 项目: {repo}")
    if doc_paths:
        logger.info(f"参考文档: {doc_paths}")

    tool_versions = _detect_tool_versions(logger)
    if tool_versions:
        logger.info(f"工具版本: {tool_versions}")

    # ── Skill 加载 ──
    skills = []
    if skill_names:
        skills = load_skills(skill_names, repo)
        if skills:
            logger.info(f"已加载 Skill: {[s.name for s in skills]}")
        else:
            console.print(f"⚠️  未找到 Skill: {skill_names}")
    elif config.get("skills", {}).get("auto_discover", False):
        max_auto = config.get("skills", {}).get("max_auto_skills", 3)
        skills = discover_skills(task, repo, max_auto)
        if skills:
            logger.info(f"自动匹配 Skill: {[s.name for s in skills]}")

    # ── Agent 类型加载 ──
    agent_type = None
    agent_type_name = agent_type_name or config.get("agents", {}).get("default", "developer")
    agent_type = load_agent_type(agent_type_name, repo)
    if agent_type:
        logger.info(f"Agent 类型: {agent_type.type_name}")

    # 将 Skill 注入 Plan prompt（如果有）
    skill_plan_context = ""
    if skills:
        skill_plan_context = "\n\n".join(render_skill_for_plan(s) for s in skills)

    console.print(f"\n🔧 主任务: {task}")
    console.print(f"📁 项目: {repo}")
    console.print(f"🆔 任务ID: {task_id}")
    if doc_paths:
        console.print(f"📎 参考文档: {', '.join(doc_paths)}")

    # Plan Mode
    console.print("\n🤖 进入 Plan Mode...")
    initial_docs = read_reference_docs(doc_paths, repo, logger) if doc_paths else ""

    plan = None
    max_iter = config.get("behavior", {}).get("max_plan_iterations", 5)
    iteration = 1
    last_error = None

    for attempt in range(3):
        try:
            plan = generate_plan(task, repo, config, logger, "", initial_docs, iteration, skill_plan_context, no_cache=no_cache)
            plan["_original_task"] = task
            break
        except Exception as e:
            last_error = e
            logger.error(f"Plan 失败 (尝试 {attempt+1}): {e}")

    if plan is not None:
        # API 成功 → Plan 确认流程
        confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration=1, task=task)
        # 检查降级信号
        if confirmed_plan == "__FALLBACK__":
            console.print(f"\n⚠️ 降级到本地规则拆解...")
            subtasks = decompose_fallback(task, repo, config, logger)
            doc_paths = []
            confirmed_plan = None  # 跳过下方 subtasks 赋值
        else:
            while confirmed_plan is None and iteration < max_iter:
                iteration += 1
                # 重生成时重新读取 D 挂载的参考文档，避免确认环节挂载的内容丢失
                regen_docs = read_reference_docs(final_doc_paths, repo, logger) if final_doc_paths else ""
                try:
                    plan = generate_plan(task, repo, config, logger, "", regen_docs, iteration, skill_plan_context, no_cache=no_cache)
                except Exception as e:
                    logger.error(f"重试生成 Plan 失败: {e}")
                    console.print(f"\n⚠️ 重试失败: {e}")
                    console.print("\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break
                plan["_original_task"] = task
                confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration, task=task)
                if confirmed_plan == "__FALLBACK__":
                    console.print(f"\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break

        if confirmed_plan is None and 'subtasks' not in locals():
            console.print(f"⚠️ 达到最大迭代次数 {max_iter}，使用最后版本")
            confirmed_plan = plan

        if confirmed_plan is not None:
            # 正常 Plan 路径：拆解子任务并保存 PLAN.md
            # （降级路径已在上方得到 subtasks，confirmed_plan 为 None，跳过本块）
            subtasks = plan_to_subtasks(confirmed_plan, logger, repo=repo)
            doc_paths = final_doc_paths
            (task_dir / "PLAN.md").write_text(plan_to_md(confirmed_plan), encoding="utf-8")
            logger.info("[PLAN] PLAN.md 已保存")
    else:
        # 降级拆解
        console.print(f"\n⚠️ Plan Mode 失败: {last_error}")
        subtasks = decompose_fallback(task, repo, config, logger)

    # 子任务确认
    confirmed = confirm_subtasks(subtasks, config, logger)

    meta = {
        "task_id": task_id, "task": task, "repo": str(repo),
        "created": ts, "status": "running",
        "reference_docs": doc_paths, "issue": issue_ref,
        "subtasks": confirmed, "results": [],
        "tool_versions": tool_versions,
        "skills": [s.name for s in skills],
        "agent_type": agent_type.type_name if agent_type else "developer",
        "remote_url": remote_url,
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # M4: 时间预估（历史子任务耗时中位数 × 拓扑波次，回答「走之前能跑完吗」）
    from .eval import estimate_task_duration
    est = estimate_task_duration(confirmed, parallel, AGENT_GO_DIR)
    conf_tag = {"high": "", "medium": "（样本较少）", "low": "（样本很少，仅供参考）",
                "none": "（无历史数据，经验值）"}[est["confidence"]]
    console.print(f"\n⏱️ 预计耗时: ~{est['estimated_sec'] / 60:.0f} 分钟 "
                  f"— {est['subtasks']} 个子任务 / {est['waves']} 个波次 / 并行 {parallel}{conf_tag}")
    logger.info(f"[M4] 时间预估: {est['estimated_sec']}s "
                f"(waves={est['waves']}, median={est['median_subtask_sec']}s, samples={est['sample_size']})")
    log_event(logger, "time_estimate", est)

    _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta, remote_url=remote_url,
                  preserve_worktrees=preserve_worktrees)

def cmd_resume(args=None):
    """恢复被中断的任务。"""
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
        auto_yes = getattr(args, 'yes', False)
        headless = auto_yes or getattr(args, 'headless', False)
        parallel = getattr(args, 'parallel', 1)
        remote_url = getattr(args, 'remote', "")
        preserve_worktrees = getattr(args, 'preserve_worktrees', False) or None
        if getattr(args, 'no_preserve', False):
            preserve_worktrees = False
    elif len(sys.argv) < 3:
        print("Usage: agent_go resume <task-id> [--yes] [--headless] [--parallel N] [--remote <url>]")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print(f"任务不存在: {task_id}")
        sys.exit(1)
    # logger 需在 result.json 恢复循环之前初始化，否则损坏文件触发 UnboundLocalError
    logger = setup_logger(task_id, task_dir)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("status") not in ("running", "paused"):
        print(f"任务状态为 {meta['status']}，无法恢复。仅 running/paused 状态可恢复")
        sys.exit(1)

    confirmed = meta.get("subtasks", [])
    results = meta.get("results", [])
    # 如果 meta.json 中 results 为空，尝试从独立 result.json 文件恢复
    if not results:
        for st in confirmed:
            result_file = task_dir / st["id"] / "result.json"
            if result_file.exists():
                try:
                    r = json.loads(result_file.read_text(encoding="utf-8"))
                    results.append(r)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read result for %s: %s", st["id"], e)
    worktree_map = {}
    results_map = {}
    completed_ids = set()
    for r in results:
        wid = r["subtask_id"]
        wt = task_dir / wid / "work"
        if wt.exists() and (wt / ".git").exists():
            worktree_map[wid] = wt
        results_map[wid] = r
        if r.get("status") in ("completed", "no_changes", "degraded"):
            completed_ids.add(wid)

    repo = Path(meta["repo"])
    config = load_config()
    # 与 cmd_run 一致：注入计量路径与任务 ID，否则 resume 后 planner/worker 计量丢失
    config["_metering_path"] = str(task_dir / "metering.jsonl")
    config["_task_id"] = task_id

    # CLI 覆盖：--max-retries / --no-verify-block（args 模式）
    if args and getattr(args, 'max_retries', None) is not None:
        config.setdefault("verification", {})["max_retries"] = args.max_retries
    if args and getattr(args, 'no_verify_block', False):
        config.setdefault("verification", {})["block_on_failure"] = False

    auto_yes = "--yes" in sys.argv or "-y" in sys.argv
    headless = auto_yes or "--headless" in sys.argv
    parallel = 1
    remote_url = ""
    # 如果从 sys.argv 解析（非 args 模式）
    if "--parallel" in sys.argv:
        try:
            pi = sys.argv.index("--parallel")
            parallel = max(1, int(sys.argv[pi + 1]))
        except (IndexError, ValueError):
            logger.debug("Invalid --parallel value, defaulting to 3")
            parallel = 3
    if "--remote" in sys.argv:
        try:
            ri = sys.argv.index("--remote")
            remote_url = sys.argv[ri + 1]
        except (IndexError, ValueError):
            logger.debug("Invalid --remote flag value, ignoring")
    # sys.argv 模式的 --max-retries / --no-verify-block（非 args 模式）
    if not (args and hasattr(args, 'task_id')):
        if "--max-retries" in sys.argv:
            try:
                mi = sys.argv.index("--max-retries")
                config.setdefault("verification", {})["max_retries"] = int(sys.argv[mi + 1])
            except (IndexError, ValueError):
                logger.debug("Invalid --max-retries value, ignoring")
        if "--no-verify-block" in sys.argv:
            config.setdefault("verification", {})["block_on_failure"] = False
    # 如果从 sys.argv 解析（非 args 模式），解析 preserve 标志
    if not (args and hasattr(args, 'task_id')):
        preserve_worktrees = "--preserve-worktrees" in sys.argv
        if "--no-preserve" in sys.argv:
            preserve_worktrees = False
        else:
            preserve_worktrees = preserve_worktrees or None

    issue_ref = meta.get("issue", "")

    if auto_yes:
        config["behavior"]["auto_confirm_plan"] = True
        config["behavior"]["auto_confirm_subtasks"] = True
        config["behavior"]["auto_verify_subtask"] = True

    # 恢复时优先使用命令行 --remote，其次 meta.json 中记录的
    remote_url = remote_url or meta.get("remote_url", "")
    meta["remote_url"] = remote_url

    logger.info(f"═══ 恢复任务 {task_id} ═══")
    logger.info(f"已完成: {len(completed_ids)}/{len(confirmed)}, 剩余: {len(confirmed) - len(completed_ids)}")
    if remote_url:
        logger.info(f"远程推送: {remote_url}")
    meta["status"] = "running"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta,
                  worktree_map, results_map, completed_ids, remote_url=remote_url,
                  preserve_worktrees=preserve_worktrees)

def cmd_inspect(args) -> None:
    """查看保留的 worktree 现场。"""
    task_id = args.task_id
    as_json = getattr(args, 'json', False)
    show_all = getattr(args, 'all', False)
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print(f"任务不存在: {task_id}")
        return

    meta_path = task_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    subtasks = meta.get("subtasks", [])

    entries = []
    for st in subtasks:
        sid = st["id"]
        sub_dir = task_dir / sid
        wt_path = sub_dir / "work"
        result_file = sub_dir / "result.json"
        preserved_file = sub_dir / ".preserved"
        task_file = sub_dir / "TASK.md"

        # 读取 result
        result = {}
        if result_file.exists():
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        status = result.get("status", "unknown")
        worktree_exists = wt_path.exists() and (wt_path / ".git").exists()
        is_preserved = preserved_file.exists()

        # 过滤：默认只显示保留的，--all 显示全部
        if not show_all and not is_preserved and status not in ("failed", "blocked"):
            continue
        if not show_all and not is_preserved and not worktree_exists:
            continue

        preserved_data = {}
        if is_preserved:
            try:
                preserved_data = json.loads(preserved_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        branch = preserved_data.get("branch", f"agent_go/{task_id}/{sid}")
        failure_reason = result.get("failure_reason", preserved_data.get("failure_reason", ""))
        summary = result.get("summary", "")
        verify_ok = result.get("verify_ok", None)

        entries.append({
            "id": sid,
            "title": st.get("title", ""),
            "status": status,
            "worktree_exists": worktree_exists,
            "is_preserved": is_preserved,
            "worktree_path": str(wt_path) if worktree_exists else "",
            "branch": branch,
            "failure_reason": failure_reason,
            "summary": summary,
            "verify_ok": verify_ok,
            "has_task_md": task_file.exists(),
        })

    if as_json:
        import json as _json
        print(_json.dumps({"task_id": task_id, "entries": entries}, indent=2, ensure_ascii=False))
        return

    if not entries:
        print(f"任务 {task_id} 中没有保留的 worktree（--all 可查看全部）")
        return

    print(f"\n🔍 保留现场: {task_id}")
    print(f"📁 任务目录: {task_dir}")
    print("─" * 70)
    for e in entries:
        icon_map = {"failed": "❌", "blocked": "🔗", "completed": "✅", "no_changes": "⏭️", "running": "🔄"}
        icon = icon_map.get(e["status"], "❓")
        preserved_tag = " [保留]" if e["is_preserved"] else ""
        print(f"\n{icon} {e['id']}{preserved_tag}: {e['title']}")
        print(f"   状态: {e['status']}")
        if e["failure_reason"]:
            print(f"   原因: {e['failure_reason']}")
        if e["summary"]:
            print(f"   摘要: {e['summary']}")
        if e["verify_ok"] is not None:
            print(f"   验证: {'通过' if e['verify_ok'] else '失败'}")
        if e["worktree_exists"]:
            print(f"   📁 {e['worktree_path']}")
            print(f"   🔗 git branch: {e['branch']}")
            if e["has_task_md"]:
                print(f"   📝 TASK.md | result.json")
        else:
            print(f"   (worktree 不存在 — 已清理或未创建)")
    print("─" * 70)
    print(f"提示: cd 到 worktree 路径查看完整文件状态")


def cmd_list() -> None:
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        print("暂无任务")
        return
    print(f"{'任务ID':<26} {'状态':<12} {'子任务':<8} {'参考文档':<12} {'描述'}")
    print("─" * 90)
    for t in tasks:
        meta_path = t / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = meta.get("status", "unknown")
        icon = {"completed": "🟢", "aborted": "🟡", "failed": "🔴"}.get(status, "⚪")
        docs = ",".join(meta.get("reference_docs", []))[:15]
        print(f"{t.name:<25} {icon} {status:<10} {len(meta.get('subtasks',[])):<8} {docs:<12} {meta.get('task','')[:30]}")

def cmd_show(args=None):
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
    elif len(sys.argv) < 3:
        print("Usage: agent_go show <task-id>")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print("任务不存在")
        sys.exit(1)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    print(f"\n🆔 {task_id}")
    print(f"📝 {meta['task']}")
    print(f"📁 {meta['repo']}")
    print(f"📊 {meta.get('status','unknown')}")
    if meta.get("reference_docs"):
        print(f"📎 参考文档: {', '.join(meta['reference_docs'])}")
    results = meta.get("results", [])
    for i, st in enumerate(meta.get("subtasks", [])):
        r = results[i] if i < len(results) else None
        icon = "✅" if r and r["status"] == "completed" else "❌" if r else "⏳"
        print(f"\n{icon} [{st['id']}] {st['title']}")
        if st.get("agent_prompt"):
            print(f"       🤖 Agent Prompt: {st['agent_prompt'][:100]}...")
        # Agent 角色和 Skill 可观测性
        agent_type = st.get("agent_type", "developer")
        source = r.get("agent_type_source", "default") if r else st.get("_agent_type_source", "default")
        source_label = {"llm": "LLM", "rule": "规则", "default": "默认", "inferred": "推断"}.get(source, source)
        print(f"       👤 Agent: {agent_type} (来源: {source_label})")
        skills = st.get("skills", [])
        if skills:
            print(f"       🧠 Skill: {', '.join(skills)}")
        unresolved = r.get("skills_unresolved", []) if r else []
        if unresolved:
            print(f"       ⚠️  Skill 未找到: {', '.join(unresolved)}")
        if r:
            print(f"       📊 {r['summary']}")

def cmd_review(args=None):
    """审查任务结果或代码变更。"""
    task_id = None
    repo = None
    headless = False
    pr_ref = ""

    if args:
        task_id = getattr(args, 'task_id', None)
        repo_path = getattr(args, 'repo', None)
        headless = getattr(args, 'yes', False)
        pr_ref = getattr(args, 'pr_ref', "") or ""
        approve = getattr(args, 'approve', False)
        reject = getattr(args, 'reject', False)
    else:
        approve = "--approve" in sys.argv
        reject = "--reject" in sys.argv
        if "--task" in sys.argv:
            try:
                task_id = sys.argv[sys.argv.index("--task") + 1]
            except (IndexError, ValueError):
                pass
        if len(sys.argv) >= 3 and not task_id:
            repo_path = sys.argv[2]
        else:
            repo_path = None

    # M7: Task results review
    if task_id:
        _show_task_review(task_id, approve=approve, reject=reject)
        return

    # 代码审查（原有逻辑）
    if not repo_path:
        print("Usage: agent_go review <repo-path> [--pr <N>] [--yes] | --task <task-id>")
        return
    repo = Path(repo_path).resolve()
    if not repo.exists():
        print(f"路径不存在: {repo}")
        return

    prompt = "请审查当前项目的代码变更，输出审查报告。重点检查：安全性、错误处理、代码质量、潜在bug。"
    if pr_ref:
        prompt = f"请审查 PR #{pr_ref} 的代码变更，输出审查报告。重点检查：安全性、错误处理、代码质量、潜在bug、API设计。"

    if headless:
        import subprocess
        result = subprocess.run(
            ["claude", "-p", prompt, "--permission-mode", "bypassPermissions", "--no-session-persistence"],
            cwd=str(repo))
        print(f"\n审查完成 (exit: {result.returncode})")
    else:
        import subprocess
        subprocess.run(["claude", str(repo)])


def _show_task_review(task_id: str, approve: bool = False, reject: bool = False) -> None:
    """显示任务结果审查（M7）— 按文件分组展示变更摘要。"""
    from .console import get_default_console
    console = get_default_console()
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print(f"❌ 任务不存在: {task_id}")
        return

    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        console.print(f"❌ 任务元数据不存在: {meta_path}")
        return

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"❌ 无法读取任务元数据: {e}")
        return

    subtasks = meta.get("subtasks", [])
    results = meta.get("results", [])

    # 读取每个子任务的结果
    result_map = {r["subtask_id"]: r for r in results}
    subtask_map = {s["id"]: s for s in subtasks}

    # 收集文件变更：按文件路径分组
    file_changes: dict[str, list[dict]] = {}
    for r in results:
        sid = r.get("subtask_id", "")
        summary = r.get("summary", "")
        change_stats = r.get("change_stats", {})
        actual_files = change_stats.get("actual_files", [])
        for f in actual_files:
            if f not in file_changes:
                file_changes[f] = []
            file_changes[f].append({
                "subtask_id": sid,
                "title": subtask_map.get(sid, {}).get("title", ""),
                "insertions": change_stats.get("insertions", 0),
                "deletions": change_stats.get("deletions", 0),
                "verify_ok": r.get("verify_ok", False),
                "agent_type": subtask_map.get(sid, {}).get("agent_type", ""),
            })

    # 输出审查仪表
    task_title = meta.get("task", "")
    created = meta.get("created", "")
    status = meta.get("status", "")

    lines = [
        f"# 📋 任务审查: {task_id}",
        f"",
        f"**任务**: {task_title}",
        f"**创建时间**: {created}",
        f"**状态**: {status}",
        f"**子任务数**: {len(subtasks)}",
        f"",
    ]

    # 文件变更摘要
    if file_changes:
        lines.append("## 📁 文件变更汇总")
        lines.append("")
        lines.append(f"| 文件 | 涉及子任务 | 变更量 | 验证 |")
        lines.append(f"|------|-----------|--------|------|")
        for file_path, changes in sorted(file_changes.items()):
            sub_ids = ", ".join(c["subtask_id"] for c in changes)
            total_ins = sum(c["insertions"] for c in changes)
            total_del = sum(c["deletions"] for c in changes)
            all_verified = all(c["verify_ok"] for c in changes)
            verify_icon = "✅" if all_verified else "❌"
            lines.append(f"| `{file_path}` | {sub_ids} | +{total_ins}/-{total_del} | {verify_icon} |")
        lines.append("")

    # 子任务详情
    lines.append("## 🔍 子任务详情")
    lines.append("")
    lines.append("| 子任务 | 标题 | Agent | 状态 | 验证 | 耗时 |")
    lines.append("|--------|------|-------|------|------|------|")
    for r in results:
        sid = r.get("subtask_id", "")
        st = subtask_map.get(sid, {})
        title = st.get("title", "")[:40]
        agent_type = st.get("agent_type", "")
        status_icon = {"completed": "✅", "no_changes": "⏭️", "failed": "❌", "blocked": "🔗"}.get(r.get("status", ""), "❓")
        verify_icon = "✅" if r.get("verify_ok") else "❌"
        dur = f"{r.get('duration_sec', 0):.0f}s"
        failure = f" — {r.get('failure_reason', '')}" if r.get("failure_reason") else ""
        lines.append(f"| {sid} | {title} | {agent_type} | {status_icon} | {verify_icon} | {dur}{failure} |")
    lines.append("")

    # 质量仪表
    quality = _build_quality_dashboard(meta)
    if quality:
        lines.append(quality)

    # 审查结论
    if approve:
        _review_conclusion_path = task_dir / "review.json"
        _review_conclusion_path.write_text(json.dumps({
            "task_id": task_id,
            "reviewed_at": datetime.now().isoformat(),
            "decision": "approved",
            "summary": "审查通过",
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        lines.append("")
        lines.append("✅ **审查通过** — 已写入 review.json")
    elif reject:
        _review_conclusion_path = task_dir / "review.json"
        _review_conclusion_path.write_text(json.dumps({
            "task_id": task_id,
            "reviewed_at": datetime.now().isoformat(),
            "decision": "rejected",
            "summary": "审查未通过",
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        lines.append("")
        lines.append("❌ **审查未通过** — 已写入 review.json")

    console.print("\n".join(lines))


def _build_quality_dashboard(meta: dict) -> str:
    """构建 PR 质量仪表（M3）— 回答「我该不该 merge？」。

    返回 Markdown 格式的质量评估段，包含：
    - 通过率统计（completed / total / degraded）
    - 每子任务验证状态（verify_ok, duration, failure_reason）
    - 合并就绪指示器（🟢 ready / 🟡 caution / 🔴 blocked）
    """
    results = meta.get("results", [])
    subtasks = meta.get("subtasks", [])
    total = len(subtasks)
    if total == 0:
        return ""

    # 分类统计
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    degraded = sum(1 for r in results if r.get("status") == "degraded")
    no_changes = sum(1 for r in results if r.get("status") == "no_changes")
    pass_rate = round((completed / total) * 100) if total > 0 else 0

    # 验证统计
    verified = sum(1 for r in results if r.get("verify_ok"))
    verify_rate = round((verified / total) * 100) if total > 0 else 0

    # 总耗时
    total_duration = sum(r.get("duration_sec", 0) for r in results)
    duration_str = f"{int(total_duration // 60)}m{int(total_duration % 60)}s"

    # 合并就绪判定
    if failed > 0:
        readiness = "🔴 **不建议合并** — 有失败子任务"
    elif degraded > 0:
        readiness = "🟡 **谨慎合并** — 有降级完成的子任务，需人工 Review"
    elif completed + no_changes == total and verify_rate >= 100:
        readiness = "🟢 **可以合并** — 全部通过验证"
    elif completed + no_changes == total:
        readiness = "🟡 **谨慎合并** — 全部完成但部分验证未通过"
    else:
        readiness = "🟡 **谨慎合并** — 部分子任务未完成"

    lines = [
        "## 📊 Quality Dashboard",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 通过率 | {pass_rate}% ({completed}/{total}) |",
        f"| 验证通过率 | {verify_rate}% ({verified}/{total}) |",
        f"| 总耗时 | {duration_str} |",
        f"| 失败 | {failed} | 降级 | {degraded} | 无变更 | {no_changes} |",
        "",
        f"**合并就绪**: {readiness}",
        "",
    ]

    # 每子任务详情
    if results:
        lines.append("### 子任务详情")
        lines.append("")
        lines.append("| 子任务 | 状态 | 验证 | 耗时 | 摘要 |")
        lines.append("|--------|------|------|------|------|")
        for r in results:
            sid = r.get("subtask_id", "?")
            status = r.get("status", "?")
            status_icon = {"completed": "✅", "no_changes": "⏭️", "degraded": "⚠️", "failed": "❌", "blocked": "🔗"}.get(status, "❓")
            verify_icon = "✅" if r.get("verify_ok") else "❌"
            dur = f"{r.get('duration_sec', 0):.0f}s"
            summary = (r.get("summary", "") or "")[:60]
            failure = f" — {r.get('failure_reason', '')}" if r.get("failure_reason") else ""
            lines.append(f"| {sid} | {status_icon} {status} | {verify_icon} | {dur} | {summary}{failure} |")
        lines.append("")

    # M5: 启发式验证警告（可能假阳性）
    weak = [r for r in results if r.get("verification_confidence", {}).get("warning")]
    if weak:
        lines.append(f"> ⚠️ {len(weak)} 个子任务的验证为启发式检查（可能假阳性），建议人工抽查: "
                     f"{', '.join(r['subtask_id'] for r in weak)}")
        lines.append("")

    return "\n".join(lines)


def cmd_pr(args=None):
    """根据已完成任务的 meta.json + git log 生成 PR 描述。"""
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
        offline = getattr(args, 'offline', False)
    elif len(sys.argv) < 3:
        print("Usage: agent_go pr <task-id> [--offline]")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
        offline = "--offline" in sys.argv
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print(f"任务不存在: {task_id}")
        sys.exit(1)

    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))

    # 收集变更信息
    subtask_lines = []
    for r in meta.get("results", []):
        icon = "✅" if r.get("status") == "completed" else "❌"
        subtask_lines.append(f"- {icon} **{r['subtask_id']}**: {r.get('summary', 'N/A')} ({r.get('sandbox_type', '?')}, {r.get('duration_sec', 0):.0f}s)")

    # 读取共享上下文
    ctx_file = task_dir / "SHARED_CONTEXT.md"
    context = ctx_file.read_text(encoding="utf-8") if ctx_file.exists() else ""

    # 质量仪表（M3）
    quality_dashboard = _build_quality_dashboard(meta)

    pr_body = f"""## Summary

{meta.get('task', 'N/A')}

{quality_dashboard}
## Subtasks

{chr(10).join(subtask_lines)}

## Verification

{context if context else '_No verification details_'}
"""

    if meta.get("issue"):
        pr_body = f"Fixes #{meta['issue']}\n\n{pr_body}"

    if offline:
        out = task_dir / "PR.md"
        out.write_text(pr_body, encoding="utf-8")
        print(f"PR 描述已写入 {out}")
        print(f"请手动创建 PR 或稍后执行: agent_go pr {task_id}")
    else:
        # 在线模式：通过 gh CLI 创建 PR
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as tf:
            tf.write(pr_body)
            pr_file = tf.name
        title = meta.get("task", "agent_go task")[:72]
        base = meta.get("base_branch", "main")
        result = subprocess.run([
            "gh", "pr", "create", "--title", f"{title}",
            "--body-file", pr_file, "--base", base,
        ], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"❌ gh pr create 失败: {result.stderr.strip()}")
            (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
            print(f"PR 描述已备份到 {task_dir}/PR.md")
        os.unlink(pr_file)

def cmd_status(args=None):
    """实时监控所有任务状态。默认 TUI 模式。--no-tui 回退文本模式。"""
    if args:
        if getattr(args, 'no_tui', False):
            _cmd_status_text(args)
        else:
            cmd_status_tui()
    elif "--no-tui" in sys.argv:
        _cmd_status_text()
    else:
        cmd_status_tui()


def _cmd_status_text(args=None):
    """文本模式（原有实现）。--watch 持续刷新，--verbose 显示 Claude 事件。"""
    if args:
        watch = getattr(args, 'watch', False)
        verbose = getattr(args, 'verbose', False)
    else:
        watch = "--watch" in sys.argv or "-w" in sys.argv
        verbose = "--verbose" in sys.argv or "-v" in sys.argv

    def _get_task_tail_lines(log_path: Path, count: int = 2) -> list[str]:
        """从执行日志尾部提取最后 count 条 Claude 事件。"""
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        # 从最后 50 行中筛选 claude 相关行
        tail = lines[-50:]
        claude_lines = [l for l in tail if "[claude" in l or "[text]" in l
                        or "[Read]" in l or "[Write]" in l or "[Bash]" in l
                        or "[tool_result]" in l or "[result]" in l]
        return claude_lines[-count:]

    def _get_task_status(task_dir: Path) -> Optional[dict[str, Any]]:
        meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = meta.get("status", "unknown")
        zombie = False
        log_path = task_dir / "execution.log"
        ZOMBIE_TIMEOUT = 600  # 10 分钟无日志输出视为僵尸任务

        # 僵尸检测：status=running 但日志已超过 ZOMBIE_TIMEOUT 未更新
        if status == "running" and log_path.exists():
            log_mtime = log_path.stat().st_mtime
            if time.time() - log_mtime > ZOMBIE_TIMEOUT:
                zombie = True
                meta["status"] = "failed"
                meta["_zombie_note"] = f"进程异常退出，日志于 {datetime.fromtimestamp(log_mtime).strftime('%H:%M:%S')} 停止更新"
                # 尝试终止可能残留的 claude 进程
                try:
                    import signal as _signal
                    for proc_file in (task_dir / "meta.json").parent.rglob("*.pid"):
                        try:
                            pid = int(proc_file.read_text().strip())
                            os.kill(pid, _signal.SIGKILL)
                        except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
                            pass
                except Exception:
                    pass
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                status = "failed"

        results = meta.get("results", [])
        completed = sum(1 for r in results if r.get("status") in ("completed", "no_changes", "degraded"))
        total = len(meta.get("subtasks", []))
        current = ""
        if results and status == "running":
            last = results[-1]
            current = f"{last.get('subtask_id', '?')}: {last.get('summary', '?')[:40]}"
        if log_path.exists():
            for line in reversed(log_path.read_text(encoding="utf-8").strip().split("\n")[-10:]):
                if "subtask_start" in line:
                    try:
                        evt = json.loads(line.split(" | ")[-1].strip())
                        current = evt.get("title", current)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        logger.debug("Failed to parse subtask_start event from log")
                    break
        progress = f"{completed}/{total}" if total > 0 else "-"
        icon = {"completed": "✅", "degraded": "⚠️", "running": "🔄", "failed": "❌", "aborted": "⏹️"}.get(status, "❓")
        elapsed = ""
        created = meta.get("created", "")
        if created:
            try:
                # created 格式为 "20260725-030125-545"（带毫秒后缀），剥离后解析
                created_clean = created.rsplit("-", 1)[0] if created.count("-") == 2 else created
                start = datetime.strptime(created_clean, "%Y%m%d-%H%M%S")
                # 运行中=实时，已完成=冻结在最后日志时间
                if status == "running":
                    end = datetime.now()
                elif log_path.exists():
                    end = datetime.fromtimestamp(log_path.stat().st_mtime)
                else:
                    end = datetime.now()
                delta = end - start
                elapsed = f"{int(delta.total_seconds() // 60)}m{int(delta.total_seconds() % 60)}s"
            except ValueError:
                logger.debug("Failed to parse elapsed time from created timestamp")
        tail_lines = _get_task_tail_lines(log_path) if verbose and status == "running" else []
        return {
            "id": task_dir.name, "icon": icon, "status": status,
            "progress": progress, "current": current, "elapsed": elapsed,
            "task": meta.get("task", "?")[:50], "issue": meta.get("issue", ""),
            "tail": tail_lines,
        }

    while True:
        tasks_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        if not tasks_dirs:
            print("暂无任务")
            return

        rows = [_get_task_status(td) for td in tasks_dirs]
        rows = [r for r in rows if r is not None]

        if watch:
            os.system("clear" if os.name == "posix" else "cls")

        print(f"{'任务ID':<24} {'状态':<6} {'进度':<8} {'耗时':<8} {'Issue':<6} {'当前子任务'}")
        print("─" * 110)
        for r in rows:
            issue_str = f"#{r['issue']}" if r['issue'] else "-"
            print(f"{r['id']:<24} {r['icon']} {r['status']:<4} {r['progress']:<8} "
                  f"{r['elapsed']:<8} {issue_str:<6} {r['current'][:50]}")
            if r["tail"]:
                for tl in r["tail"]:
                    line_text = tl.split(" | ")[-1] if " | " in tl else tl
                    print(f"  └ {line_text.strip()[:90]}")
        print("─" * 110)
        flags = " --watch" if watch else ""
        flags += " --verbose" if verbose else ""
        print(f"共 {len(rows)} 个任务 | agent_go status{flags} | Ctrl+C 退出\n")

        if not watch:
            break
        time.sleep(5)

def cmd_config() -> None:
    config = load_config()
    print(json.dumps(config, indent=2, ensure_ascii=False))

def cmd_clean() -> None:
    import shutil as _shutil
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        print("暂无任务")
        return
    print(f"将清理 {len(tasks)} 个任务目录:")
    for t in tasks:
        print(f"  {t.name}")
    confirm = safe_input("\n确认删除? [y/N]: ").strip().lower()
    if confirm == "y":
        # repo → 该仓库关联的 task_id 集合（用于 worktree prune + tag 清理）
        repo_task_ids: dict[str, set] = {}
        for t in tasks:
            meta_path = t / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    repo_str = meta.get("repo", "")
                    task_id = meta.get("task_id", t.name)
                    if repo_str and Path(repo_str).exists():
                        repo_task_ids.setdefault(repo_str, set()).add(task_id)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read meta for %s: %s", t.name, e)
        for t in tasks:
            _shutil.rmtree(t, ignore_errors=True)
        for repo_path, task_ids in repo_task_ids.items():
            subprocess.run(["git", "worktree", "prune"], cwd=repo_path, capture_output=True)
            # 清理该仓库下所有关联任务的 tags
            for tid in task_ids:
                tag_list = subprocess.run(["git", "tag", "-l", f"{tid}/*"], cwd=repo_path, capture_output=True, text=True)
                for tag in tag_list.stdout.strip().split("\n"):
                    if tag:
                        subprocess.run(["git", "tag", "-d", tag], cwd=repo_path, capture_output=True)
        print(f"已清理 {len(tasks)} 个任务")
    else:
        print("已取消")

def cmd_skills() -> None:
    """列出所有可用的 Skill。"""
    skills = list_skills()
    if not skills:
        print("\n暂无可用 Skill。在 ~/.agent_go/skills/<name>/SKILL.md 创建。")
        print("示例 Skill 格式: YAML frontmatter + Markdown body")
        return
    print(f"\n📚 可用 Skill ({len(skills)} 个)")
    print("─" * 55)
    for s in skills:
        desc = s["description"][:45] + "..." if len(s["description"]) > 45 else s["description"]
        print(f"  {s['name']:<30} {desc}")
    print("─" * 55)

def cmd_cache(args=None):
    """Plan 缓存管理。"""
    from .api import list_cache_entries, clean_expired_cache

    if args and hasattr(args, 'subcommand'):
        sub = args.subcommand
    elif len(sys.argv) < 3:
        print("Usage: agent_go cache <list|clean|clear|stats>")
        return
    else:
        sub = sys.argv[2]
    config = load_config()

    if sub == "list":
        entries = list_cache_entries()
        if not entries:
            print("暂无缓存")
            return
        print(f"{'缓存键':<14} {'任务':<30} {'创建':<18} {'命中':<6}")
        print("─" * 70)
        for e in entries:
            m = e.get("meta", {})
            key = e.get("cache_key", "")[:12]
            task = m.get("task", "?")[:30]
            created = m.get("created_at", "?")[:16]
            hits = m.get("hit_count", 0)
            print(f"{key:<14} {task:<30} {created:<18} {hits:<6}")
    elif sub == "clean":
        removed = clean_expired_cache(config)
        print(f"清理 {removed} 条过期缓存")
    elif sub == "clear":
        import shutil
        from .api import _cache_dir
        d = _cache_dir()
        if d.exists():
            shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        print("已清除所有缓存")
    elif sub == "stats":
        entries = list_cache_entries()
        print(f"缓存条目: {len(entries)}")
        if entries:
            total_hits = sum(e.get("meta", {}).get("hit_count", 0) for e in entries)
            print(f"总命中: {total_hits}")
            print(f"磁盘: {_cache_size()}")
    else:
        print(f"未知子命令: {sub}。可用: list, clean, clear, stats")


def _cache_size() -> str:
    from .api import _cache_dir
    d = _cache_dir()
    total = 0
    for f in d.rglob("*.json"):
        total += f.stat().st_size
    if total < 1024:
        return f"{total}B"
    elif total < 1024 * 1024:
        return f"{total / 1024:.1f}KB"
    return f"{total / 1024 / 1024:.1f}MB"


def cmd_router(args=None) -> None:
    """角色感知模型路由配置管理。"""
    from .config import CONFIG_PATH

    config = load_config()
    router_cfg = config.setdefault("router", {})

    subcmd = args.router_subcommand if args else "show"

    if subcmd == "show":
        _print_router_config(router_cfg)
        return

    if subcmd == "enable":
        router_cfg["enabled"] = True
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print("✅ 角色感知路由已启用")
        _print_router_config(router_cfg)
        return

    if subcmd == "disable":
        router_cfg["enabled"] = False
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print("✅ 角色感知路由已禁用（回退到 plan_api）")
        return

    if subcmd == "set-role":
        role = args.role
        router_cfg.setdefault("roles", {})
        role_cfg = router_cfg["roles"].setdefault(role, {})
        role_cfg["provider"] = args.provider
        role_cfg["model"] = args.model
        role_cfg["base_url"] = args.base_url

        if args.fallback_provider and args.fallback_model and args.fallback_base_url:
            role_cfg["fallback"] = {
                "provider": args.fallback_provider,
                "model": args.fallback_model,
                "base_url": args.fallback_base_url,
            }
        elif args.fallback_provider:
            print("⚠️  --fallback-provider 需要同时指定 --fallback-model 和 --fallback-base-url")

        # Planner 铁律：不允许配置降级到弱模型
        if role == "planner" and "fallback" in role_cfg:
            print("⚠️  警告：Planner 角色不应配置降级（规划 token 省小钱，Worker token 数倍膨胀）")
            print("   建议移除 fallback: agent_go router set-role planner --provider ... --model ... --base-url ...")

        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"✅ {role} 角色已配置")
        _print_role_config(role, router_cfg)
        return

    print(f"未知操作: {subcmd}。可用: show | enable | disable | set-role")


def _print_router_config(router_cfg: dict) -> None:
    """打印路由器配置摘要。"""
    enabled = router_cfg.get("enabled", False)
    status = "🟢 启用" if enabled else "⚪ 禁用"
    print(f"\n🔀 角色感知路由: {status}")
    print(f"   熔断: {router_cfg.get('circuit_breaker', {}).get('failure_threshold', 5)} 次失败 → "
          f"{router_cfg.get('circuit_breaker', {}).get('cooldown_seconds', 60)}s 冷却")
    print(f"   Agent 映射: {json.dumps(router_cfg.get('agent_type_mapping', {}), ensure_ascii=False)}")

    roles = router_cfg.get("roles", {})
    if roles:
        print("   角色配置:")
        for role_name in ["planner", "worker", "reviewer"]:
            if role_name in roles:
                _print_role_config(role_name, router_cfg)
    else:
        print("   ⚠️  未配置任何角色，请使用 'agent_go router set-role' 配置")


def _print_role_config(role_name: str, router_cfg: dict) -> None:
    """打印单个角色配置。"""
    roles = router_cfg.get("roles", {})
    rc = roles.get(role_name, {})
    provider = rc.get("provider", "?")
    model = rc.get("model", "?")
    fallback = rc.get("fallback")
    fb_str = f" → fallback: {fallback['provider']}:{fallback['model']}" if fallback else " (不降级)"
    print(f"     {role_name}: {provider}:{model}{fb_str}")


def cmd_agents() -> None:
    """列出所有可用的 Agent 类型。"""
    agents = list_agent_types()
    print(f"\n🤖 Agent 类型 ({len(agents)} 种)")
    print("─" * 55)
    for a in agents:
        src = "内置" if a.get("source") == "builtin" else "用户"
        desc = a["description"][:40] + "..." if len(a["description"]) > 40 else a["description"]
        print(f"  {a['type']:<25} [{src}] {desc}")
    print("─" * 55)

def main() -> None:
    try:
        parser = _build_parser()
        args = parser.parse_args()

        if not args.command:
            parser.print_help()
            return

        if args.command == "run":
            cmd_run(args)
        elif args.command == "resume":
            cmd_resume(args)
        elif args.command == "list":
            cmd_list()
        elif args.command == "show":
            cmd_show(args)
        elif args.command == "status":
            cmd_status(args)
        elif args.command == "config":
            cmd_config()
        elif args.command == "clean":
            cmd_clean()
        elif args.command == "pr":
            cmd_pr(args)
        elif args.command == "skills":
            cmd_skills()
        elif args.command == "agents":
            cmd_agents()
        elif args.command == "cache":
            cmd_cache(args)
        elif args.command == "ci":
            cmd_ci(args)
        elif args.command == "review":
            cmd_review(args)
        elif args.command == "eval":
            cmd_eval(args)
        elif args.command == "router":
            cmd_router(args)
        elif args.command == "inspect":
            cmd_inspect(args)
    except KeyboardInterrupt:
        print("\n\n⏹️  用户中断（Ctrl+C）")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)

