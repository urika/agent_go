import sys, os, subprocess, json, time, logging, argparse, shutil
from pathlib import Path
from datetime import datetime
from typing import Any, Optional


from .config import load_config, safe_input, setup_logger, AGENT_GO_DIR, log_event, get_api_key
from .console import Console, set_default_console, _LazyConsole
from .api import generate_plan, decompose_fallback
from .ui import confirm_plan, plan_to_md, plan_to_subtasks, confirm_subtasks
from .utils import read_reference_docs, _detect_tool_versions
from .pipeline import _run_pipeline
from .skills import load_skills, discover_skills, render_skill_for_plan, list_skills
from .spec import parse_spec, validate_spec_l1, render_spec_template, detect_step_conflicts
from .agents import load_agent_type, list_agent_types
from .eval import cmd_eval
from .replay import cmd_replay
from .checkpoint import list_checkpoints, restore_checkpoint, SnapshotManager
from .mcp_server import main as cmd_mcp
from .web_server import main as cmd_web
from .tui import cmd_status_tui
from .workflow_gen import cmd_ci
from .git_utils import init_git_repo

logger = logging.getLogger(__name__)

console = _LazyConsole()

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
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="Output JSON Lines (machine-readable, stderr for interactive prompts)")
    parser.add_argument("--config", default=argparse.SUPPRESS,
                        help="Path to config JSON file (default: ~/.agent_go/config.json)")
    parser.add_argument("--profile", default=argparse.SUPPRESS,
                        help="Configuration profile name: ~/.agent_go/profiles/<name>.json 或 ~/.agent_go/config.<name>.json")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="Plan, decompose and execute a task")
    run_parser.add_argument("repo", help="Path to the repository")
    run_parser.add_argument("task", nargs="?", default="请根据项目情况完成改进", help="Task description")
    run_parser.add_argument("--docs", help="Comma-separated list of reference document paths")
    run_parser.add_argument("--spec", dest="spec_path", default=None,
                            help="Task Spec 文件路径（结构化输入契约，SDD）。读取后按 7 章节解析注入 Plan prompt；通过 L1 准入审查后方可执行")
    run_parser.add_argument("--force", action="store_true",
                            help="跳过 Spec 准入审查（L1+L2 全部跳过）。仅限确信 Spec 正确的场景")
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
    run_parser.add_argument("--interactive", action="store_true",
                            help="启动 TUI 仪表盘实时监控子任务执行")
    run_parser.add_argument("--step-confirm", action="store_true",
                            help="每波执行前暂停确认（适用于交互式非 TUI 场景）")
    run_parser.add_argument("--auto-init", action="store_true",
                            help="目标目录非 git 仓库时自动 git init + 首次 commit（默认关闭）")
    run_parser.add_argument("--artifact-dir", default=None,
                            help="产物导出目录：子任务写入 worktree/__artifacts__/ 的文件在此收集导出（默认不导出）")
    run_parser.add_argument("--max-cost", type=float, default=None, dest="max_cost",
                            help="任务级成本预算（USD）：累计 metering 成本超限即熔断剩余子任务（默认关闭）")
    run_parser.add_argument("--budget", type=float, default=None, dest="budget",
                            help="--max-cost 的别名（per-task 成本预算，S12-P1 G3）；同传时 --budget 生效")
    run_parser.add_argument("--budget-mode", choices=["strict", "degrade", "ignore"], default=None, dest="budget_mode",
                            help="预算策略（S12-P1 G3）：strict=超预算 block；degrade=切便宜模型继续；ignore=关 L3（默认 strict）")
    run_parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to config JSON file (default: ~/.agent_go/config.json)")

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
    resume_parser.add_argument("--artifact-dir", default=None,
                               help="产物导出目录：收集各 worktree/__artifacts__/ 的文件（覆盖 meta 中的记录）")
    resume_parser.add_argument("--max-cost", type=float, default=None, dest="max_cost",
                               help="任务级成本预算（USD）：累计 metering 成本超限即熔断剩余子任务（默认关闭）")
    resume_parser.add_argument("--budget", type=float, default=None, dest="budget",
                               help="--max-cost 的别名（per-task 成本预算，S12-P1 G3）；同传时 --budget 生效")
    resume_parser.add_argument("--budget-mode", choices=["strict", "degrade", "ignore"], default=None, dest="budget_mode",
                               help="预算策略（S12-P1 G3）：strict=超预算 block；degrade=切便宜模型继续；ignore=关 L3（默认 strict）")

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
    _clean_parser = subparsers.add_parser("clean", help="Remove task data")
    _clean_parser.add_argument("--older-than", type=int, default=None,
                               help="只清理早于 N 天前的任务目录（保留期清理，S12 失败清理 #3）")

    # config 子命令
    subparsers.add_parser("config", help="View current configuration")

    # spec 子命令（S11-P0：Task Spec 工具）
    spec_parser = subparsers.add_parser("spec", help="Task Spec 工具（SDD 结构化输入）")
    spec_sub = spec_parser.add_subparsers(dest="spec_subcommand", help="Spec operation")
    spec_template_parser = spec_sub.add_parser("template", help="生成空白 Task Spec 模板")
    spec_template_parser.add_argument("repo", nargs="?", help="仓库路径（预填模块列表提示）")
    spec_template_parser.add_argument("--output", "-o", default=None, help="输出文件路径（默认打印到 stdout）")
    spec_validate_parser = spec_sub.add_parser("validate", help="对 Spec 文件运行 L1 准入审查")
    spec_validate_parser.add_argument("spec_path", help="Task Spec 文件路径")
    spec_validate_parser.add_argument("repo", nargs="?", help="仓库路径（用于文件路径校验）")

    # skills 子命令
    skills_parser = subparsers.add_parser("skills", help="List available Skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_subcommand", help="Skills operation")
    skills_sub.add_parser("list", help="List all available Skills")
    show_skill_parser = skills_sub.add_parser("show", help="Show a Skill's full SKILL.md content (agent-readable)")
    show_skill_parser.add_argument("name", help="Skill name")
    show_skill_parser.add_argument("--json", action="store_true", dest="json_mode",
                                   help="Output as JSON (frontmatter + body + raw)")

    # agents 子命令
    subparsers.add_parser("agents", help="List available Agent types")

    # pr 子命令
    pr_parser = subparsers.add_parser("pr", help="Generate and create PR")
    pr_parser.add_argument("task_id", help="Task ID to create PR from")
    pr_parser.add_argument("--offline", action="store_true", help="Only generate PR.md, do not create PR")
    pr_parser.add_argument("--push", action="store_true", help="Push branch to remote before creating PR")
    pr_parser.add_argument("--remote", default="origin", help="Remote name to push to (default: origin)")

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
    review_parser.add_argument("--changes-requested", action="store_true", help="Request changes (review rejected with feedback)")
    review_parser.add_argument("--comment-text", help="Review comment text (with --changes-requested or --reject)")
    review_parser.add_argument("--deep", action="store_true",
                                help="深层审查：使用独立模型逐子任务分析 diff 并给出评审意见")

    # cache 子命令
    cache_parser = subparsers.add_parser("cache", help="Plan cache management")
    cache_parser.add_argument("subcommand", nargs="?", choices=["list", "clean", "clear", "stats"],
                              help="Cache operation: list|clean|clear|stats")

    # recover 子命令（异常中断后从 worktree 重建 meta.json）
    recover_parser = subparsers.add_parser("recover", help="从 worktree 状态重建被异常中断的任务 meta.json")
    recover_parser.add_argument("task_id", help="Task ID to recover (e.g., task-20260725-224612-955-fd40)")
    recover_parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                                 help="只扫描，不更新 meta.json")

    # eval 子命令
    eval_parser = subparsers.add_parser("eval", help="Quality/performance/cost evaluation")
    eval_parser.add_argument("subcommand", choices=["quality", "perf", "cost", "reliability", "ux", "gate", "bench", "baseline", "cost-baseline", "models", "judge", "all"],
                             help="Evaluation type")
    eval_parser.add_argument("task_id", nargs="?", help="Task ID to evaluate")
    eval_parser.add_argument("--all", dest="eval_all", action="store_true", help="Evaluate all tasks")
    # gate 子命令参数
    eval_parser.add_argument("--baseline", type=float, dest="baseline", default=None,
                             help="$/pass rate 绝对阈值（gate 子命令默认模式，缺省 0.05）")
    eval_parser.add_argument("--check-regression", dest="check_regression", action="store_true",
                             help="gate 改用「不劣化」语义：对比历史基线（劣化 >10%% 即失败）")
    eval_parser.add_argument("--update-baseline", dest="update_baseline", action="store_true",
                             help="gate 强制更新历史基线为当前 rate（模型升级等场景重置基线）")
    # bench / models 子命令参数
    eval_parser.add_argument("--tasks", dest="tasks", default="eval_suite",
                             help="任务集目录（bench 子命令，缺省 eval_suite/）")
    eval_parser.add_argument("--candidate-models", dest="candidate_models",
                             help="被测模型列表，逗号分隔（bench 子命令，如 sonnet-5,deepseek-chat）")
    eval_parser.add_argument("--repeat", dest="repeat", type=int, default=3,
                             help="每任务重复次数（bench 子命令，默认 3）")
    eval_parser.add_argument("--output", dest="output", default="eval_suite/results.jsonl",
                             help="结果输出文件（bench 子命令）")
    eval_parser.add_argument("--no-skills", dest="no_skills", action="store_true",
                             help="禁用 skill 自动发现（bench 子命令，用于 skill on/off 对比）")
    eval_parser.add_argument("--source-batch", dest="source_batch", default="",
                             help="批次标识（bench 子命令，如 baseline / results_v2 / smoke-*，写入每条 record）")
    eval_parser.add_argument("--results", dest="results", default="eval_suite/results.jsonl",
                             help="读取结果文件（models/cost-baseline 子命令，逗号分隔多个文件）")
    eval_parser.add_argument("--tolerance", dest="tolerance", type=float, default=1.5,
                             help="成本基线预算 = P90 × tolerance（cost-baseline 子命令，默认 1.5）")
    # judge 子命令参数
    eval_parser.add_argument("--judge-models", dest="judge_models",
                             help="评判模型列表，逗号分隔（judge 子命令）")
    eval_parser.add_argument("--judge-subcommand", dest="judge_subcommand", default="run",
                             choices=["run", "calibrate"],
                             help="judge 子命令：run=交叉评判 calibrate=人工校准")
    eval_parser.add_argument("--llm-scores", dest="llm_scores",
                             help="LLM 评分文件（judge calibrate 用）")
    eval_parser.add_argument("--human-scores", dest="human_scores",
                             help="人工评分 CSV（judge calibrate 用）")

    # inspect 子命令
    inspect_parser = subparsers.add_parser("inspect", help="查看保留的 worktree 现场")
    inspect_parser.add_argument("task_id", help="Task ID to inspect")
    inspect_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # plan-history / plan-diff 子命令（Plan 版本管理）
    plan_history_parser = subparsers.add_parser("plan-history", help="List Plan version history")
    plan_history_parser.add_argument("task_id", help="Task ID")
    plan_diff_parser = subparsers.add_parser("plan-diff", help="Diff two Plan versions")
    plan_diff_parser.add_argument("task_id", help="Task ID")
    plan_diff_parser.add_argument("--v1", type=int, default=1, help="First version (default: 1)")
    plan_diff_parser.add_argument("--v2", type=int, default=None, help="Second version (default: latest)")
    inspect_parser.add_argument("--all", action="store_true", help="Show all subtasks, not just preserved ones")

    # replay 子命令（P4-1 执行回放）
    replay_parser = subparsers.add_parser("replay", help="Execution timeline replay")
    replay_parser.add_argument("task_id", help="Task ID to replay")
    replay_parser.add_argument("--json", action="store_true", dest="json_mode",
                               help="Output as JSON Lines")

    # checkpoint 子命令（P4-2 检查点快照）
    checkpoint_parser = subparsers.add_parser("checkpoint", help="Manage file snapshots for subtask rollback")
    checkpoint_sub = checkpoint_parser.add_subparsers(dest="checkpoint_command", help="Checkpoint operation")
    chk_list = checkpoint_sub.add_parser("list", help="List checkpoints for a task")
    chk_list.add_argument("task_id", help="Task ID")
    chk_list.add_argument("--json", action="store_true", dest="json_mode", help="Output as JSON")
    chk_restore = checkpoint_sub.add_parser("restore", help="Restore files from a checkpoint")
    chk_restore.add_argument("task_id", help="Task ID")
    chk_restore.add_argument("--name", "-n", required=True, help="Checkpoint name (subtask ID)")
    chk_restore.add_argument("--target", help="Target directory (default: task_dir/sub_id/work)")
    chk_delete = checkpoint_sub.add_parser("delete", help="Delete a checkpoint")
    chk_delete.add_argument("task_id", help="Task ID")
    chk_delete.add_argument("--name", "-n", required=True, help="Checkpoint name to delete")

    # mcp 子命令
    mcp_parser = subparsers.add_parser("mcp", help="Start MCP server (JSON-RPC 2.0 over stdio, or HTTP/SSE)")
    mcp_parser.add_argument("--http", action="store_true",
                            help="以 HTTP/SSE transport 运行（默认 stdio）。POST /mcp 处理请求，GET /mcp 为 SSE 推送")
    mcp_parser.add_argument("--host", default="127.0.0.1", help="HTTP 绑定地址（默认 127.0.0.1，仅本地）")
    mcp_parser.add_argument("--port", type=int, default=8090, help="HTTP 监听端口（默认 8090）")

    # web 子命令
    web_parser = subparsers.add_parser("web", help="只读 Web 观察平台（任务清单/子任务明细/日志/metering/时间线）")
    web_parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1，仅本地）")
    web_parser.add_argument("--port", type=int, default=8091, help="监听端口（默认 8091）")
    web_parser.add_argument("--token", default=None, help="可选 Bearer token 鉴权（默认关闭）")

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


def _build_spec_context(spec_obj) -> str:
    """把 TaskSpec 的范围/约束/验收/风险组装成注入 Plan prompt 的结构化约束文本。

    §3 范围 / §4 约束 / §5 验收 / §7 风险 → system prompt 硬约束
    （§1 目标已在 cmd_run 中替代 task；§2 动机 / §6 参考已注入 user content）
    """
    parts = []
    if spec_obj.scope:
        parts.append(f"【范围（必须遵守）】\n{spec_obj.scope.strip()}")
    if spec_obj.constraint:
        parts.append(f"【设计约束（必须遵守）】\n{spec_obj.constraint.strip()}")
    if spec_obj.acceptance:
        parts.append(f"【验收标准（verification 命令应覆盖这些）】\n{spec_obj.acceptance.strip()}")
    if spec_obj.risk:
        parts.append(f"【已知风险（在 steps[].risks 和 difficulty 中体现）】\n{spec_obj.risk.strip()}")
    return "\n\n".join(parts)


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
    spec_path = Path(args.spec_path).resolve() if getattr(args, "spec_path", None) else None
    force_spec = getattr(args, "force", False)
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
    json_mode = getattr(args, "json_mode", False)

    # 初始化 Console 实例（headless / --yes 隐含 quiet 模式）
    console = Console(quiet=quiet or headless, verbose=verbose, json_mode=json_mode)
    set_default_console(console)

    # 并发模式要求 headless（避免同时打开多个交互式 Claude Code 终端）
    if parallel > 1 and not headless:
        console.warning("并发模式 (--parallel) 需要无头模式 (--headless / --yes)，已自动切换到串行执行。")
        parallel = 1

    if not repo.exists():
        console.error(f"路径不存在: {repo}")
        sys.exit(1)

    # --auto-init：目标目录非 git 仓库时自动 init + 首次 commit，
    # 保证 worktree / commit / tag / merge 机制可用
    if getattr(args, "auto_init", False) and not (repo / ".git").exists():
        console.warning(f"{repo} 不是 git 仓库，自动初始化 (--auto-init)")
        ok, err = init_git_repo(repo)
        if not ok:
            console.error(f"git init 失败: {err}")
            sys.exit(1)
        console.print("✓ git 初始化完成（本地，无 remote）")

    config = load_config(config_path=getattr(args, "config", None))
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
    if getattr(args, "artifact_dir", None):
        config["artifact_dir"] = args.artifact_dir

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

    # S10 成本控制：--max-cost / --budget 开启 L3 任务级熔断（默认关闭）
    # S12-P1 G3：--budget-mode 设置预算策略（strict/degrade/ignore）
    _budget_flag = getattr(args, "budget", None) or getattr(args, "max_cost", None)
    _budget_mode_flag = getattr(args, "budget_mode", None)
    if _budget_flag:
        _cc = dict(config.get("cost_control") or {})
        _cc["enabled"] = True
        _cc["max_budget_usd"] = float(_budget_flag)
        if _budget_mode_flag:
            _cc["budget_mode"] = _budget_mode_flag
        config["cost_control"] = _cc
        logger.info(f"[cost_control] --budget ${_budget_flag} 已启用 L3 任务级熔断 (mode={_cc.get('budget_mode', 'strict')})")
    elif _budget_mode_flag:
        _cc = dict(config.get("cost_control") or {})
        _cc["budget_mode"] = _budget_mode_flag
        config["cost_control"] = _cc
        logger.info(f"[cost_control] --budget-mode {_budget_mode_flag}（未指定预算，仅设策略）")

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
            console.warning(f"未找到 Skill: {skill_names}")
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

    # ── Task Spec 准入审查（S11-P0）──
    spec_obj = None
    spec_context = ""
    if spec_path is not None:
        if not spec_path.exists():
            console.error(f"Spec 文件不存在: {spec_path}")
            sys.exit(1)
        spec_obj = parse_spec(spec_path)
        if spec_obj is None:
            console.error(f"Spec 解析失败: {spec_path}")
            sys.exit(1)
        console.print(f"📋 Task Spec: {spec_obj.title or spec_path.name}")
        # --yes 模式仍跑 L1（确定性检查，0 误判，不跳过）；--force 全跳过
        if not force_spec:
            console.print("🔍 L1 准入审查中...")
            violations = validate_spec_l1(spec_obj, repo)
            if violations:
                console.error(f"❌ L1 准入审查未通过（{len(violations)} 项违规）：")
                for i, v in enumerate(violations, 1):
                    sec = f" §{v.section}" if v.section else ""
                    console.error(f"  {i}. [{v.check}{sec}] {v.message}")
                    if v.suggestion:
                        console.error(f"     💡 {v.suggestion}")
                console.error("\n修正 Spec 后重试，或用 --force 跳过审查（不推荐）。")
                sys.exit(1)
            console.print("✅ L1 准入审查通过")
        else:
            console.print("⚠️ --force 已跳过 Spec 准入审查")
        logger.info(f"Task Spec 加载: {spec_path}（完整={spec_obj.is_complete}, force={force_spec}）")
        # Spec §1 目标作为任务描述的增强（若 Spec 完整，目标替代一句话 task 的模糊性）
        if spec_obj.goal:
            task = spec_obj.goal.strip()
        # 结构化约束注入（由 generate_plan 的 spec_context 参数消费）
        spec_context = _build_spec_context(spec_obj)

    # Plan Mode
    console.print("\n🤖 进入 Plan Mode...")
    initial_docs = read_reference_docs(doc_paths, repo, logger) if doc_paths else ""
    # Spec §6 参考资料并入 initial_docs
    if spec_obj and spec_obj.reference:
        initial_docs = (initial_docs + "\n\n" if initial_docs else "") + f"===== Task Spec §6 参考资料 =====\n{spec_obj.reference}\n===== 结束 ====="

    plan = None
    max_iter = config.get("behavior", {}).get("max_plan_iterations", 5)
    iteration = 1
    last_error = None

    for attempt in range(3):
        try:
            plan = generate_plan(task, repo, config, logger, "", initial_docs, iteration, skill_plan_context, no_cache=no_cache, spec_context=spec_context)
            plan["_original_task"] = task
            break
        except Exception as e:
            last_error = e
            logger.error(f"Plan 失败 (尝试 {attempt+1}): {e}")

    if plan is not None:
        # API 成功 → Plan 确认流程
        confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration=1, task=task, plan_dir=task_dir)
        # 检查降级信号
        if confirmed_plan == "__FALLBACK__":
            console.print(f"\n⚠️ 降级到本地规则拆解...")
            subtasks = decompose_fallback(task, repo, config, logger)
            doc_paths = []
            confirmed_plan = None
        else:
            while confirmed_plan is None and iteration < max_iter:
                iteration += 1
                # 重生成时重新读取 D 挂载的参考文档，避免确认环节挂载的内容丢失
                regen_docs = read_reference_docs(final_doc_paths, repo, logger) if final_doc_paths else ""
                # 保存上一版 Plan 快照后再生
                if plan:
                    _save_plan_snapshot(task_dir, plan, iteration - 1)
                _prev_plan = dict(plan) if plan else None  # R-4: diff 基线
                try:
                    plan = generate_plan(task, repo, config, logger, "", regen_docs, iteration, skill_plan_context, no_cache=no_cache, spec_context=spec_context)
                except Exception as e:
                    logger.error(f"重试生成 Plan 失败: {e}")
                    console.print(f"\n⚠️ 重试失败: {e}")
                    console.print("\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break
                plan["_original_task"] = task
                if _prev_plan:
                    # R-4: 实时 diff——用户知道重新生成改了什么
                    from .ui import show_plan_diff
                    show_plan_diff(_prev_plan, plan)
                confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration, task=task, plan_dir=task_dir)
                if confirmed_plan == "__FALLBACK__":
                    console.print(f"\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break

        if confirmed_plan is None and 'subtasks' not in locals():
            console.warning(f"达到最大迭代次数 {max_iter}，使用最后版本")
            confirmed_plan = plan

        if confirmed_plan is not None:
            # 正常 Plan 路径：拆解子任务并保存 PLAN.md
            # （降级路径已在上方得到 subtasks，confirmed_plan 为 None，跳过本块）
            # L1.5 AST 冲突检测（S11，学术驱动）：Plan 确认后、执行前拦截多 step 同文件/同符号冲突
            try:
                step_conflicts = detect_step_conflicts(confirmed_plan.get("steps") or [], repo)
                symbol_conflicts = [c for c in step_conflicts if c.severity == "symbol"]
                file_conflicts = [c for c in step_conflicts if c.severity == "file"]
                if step_conflicts:
                    console.print(f"\n⚡ L1.5 AST 冲突检测：{len(step_conflicts)} 处")
                    for c in step_conflicts:
                        icon = "🔴" if c.severity == "symbol" else "🟡"
                        console.print(f"  {icon} [{c.severity}] {c.file} (steps {'/'.join(map(str, c.steps))})")
                        if c.symbols:
                            console.print(f"      同名符号: {', '.join(c.symbols)}")
                    # 符号级冲突（高置信）在交互模式询问是否继续，--yes/--force 跳过询问
                    if symbol_conflicts and not auto_yes and not force_spec:
                        resp = safe_input("\n⚠️ 存在符号级冲突，可能集成失败。继续执行? [y/N] ").strip().lower()
                        if resp not in ("y", "yes"):
                            console.error("已取消执行。建议调整 Plan：合并冲突 step 或添加依赖。")
                            sys.exit(1)
            except Exception as _e:
                # 冲突检测是辅助功能，失败不阻断主流程
                logger.warning(f"L1.5 冲突检测失败（跳过）: {_e}")
            subtasks = plan_to_subtasks(
                confirmed_plan, logger, repo=repo,
                default_skills=[s.name for s in skills] if skills else None,
                disable_rule_skills=not config.get("skills", {}).get("auto_discover", False))
            doc_paths = final_doc_paths
            (task_dir / "PLAN.md").write_text(plan_to_md(confirmed_plan), encoding="utf-8")
            _save_plan_snapshot(task_dir, confirmed_plan, iteration)
            logger.info(f"[PLAN] PLAN.md 已保存 (v{iteration})")
            # S12-P2 G5：规划期欠分解检测——hard 子任务 + 总子任务数过少 → 提示可能撞超时
            try:
                from .planning import check_under_decomposition
                check_under_decomposition(subtasks, logger)
            except Exception as _ge:
                logger.debug(f"[G5] 欠分解检测失败（忽略）: {_ge}")
        elif 'subtasks' in locals() and subtasks is not None:
            # 降级路径中已通过 decompose_fallback 生成 subtasks，无需重复调用
            pass
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
    # 解耦：从 planning.py 取（预执行估算，不依赖 eval 评估模块）
    from .planning import estimate_task_duration
    est = estimate_task_duration(confirmed, parallel, AGENT_GO_DIR)
    conf_tag = {"high": "", "medium": "（样本较少）", "low": "（样本很少，仅供参考）",
                "none": "（无历史数据，经验值）"}[est["confidence"]]
    console.print(f"\n⏱️ 预计耗时: ~{est['estimated_sec'] / 60:.0f} 分钟 "
                  f"— {est['subtasks']} 个子任务 / {est['waves']} 个波次 / 并行 {parallel}{conf_tag}")
    logger.info(f"[M4] 时间预估: {est['estimated_sec']}s "
                f"(waves={est['waves']}, median={est['median_subtask_sec']}s, samples={est['sample_size']})")
    log_event(logger, "time_estimate", est)

    _interactive_mode = getattr(args, 'interactive', False)
    _step_confirm = getattr(args, 'step_confirm', False)

    if _interactive_mode:
        import threading as _th, signal as _sig
        from .tui import cmd_status_tui
        _interrupted = _th.Event()

        _pipeline_t = _th.Thread(
            target=_run_pipeline,
            args=(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta),
            kwargs={"remote_url": remote_url, "preserve_worktrees": preserve_worktrees,
                    "interrupted": _interrupted},
            daemon=True)
        _pipeline_t.start()
        cmd_status_tui(task_filter=task_id)
        _interrupted.set()
        _pipeline_t.join(timeout=10)
        _sig.signal(_sig.SIGINT, _prev_int)
        _sig.signal(_sig.SIGTERM, _prev_term)
    else:
        _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta,
                      remote_url=remote_url, preserve_worktrees=preserve_worktrees,
                      step_confirm=_step_confirm)

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
        console.print("Usage: agent_go resume <task-id> [--yes] [--headless] [--parallel N] [--remote <url>]")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print(f"任务不存在: {task_id}")
        sys.exit(1)
    # logger 需在 result.json 恢复循环之前初始化，否则损坏文件触发 UnboundLocalError
    logger = setup_logger(task_id, task_dir)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("status") not in ("running", "paused", "interrupted", "cancelled", "stale_aborted"):
        console.print(f"任务状态为 {meta['status']}，无法恢复。仅 running/paused/interrupted/cancelled/stale_aborted 状态可恢复")
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
        if r.get("status") == "completed":
            completed_ids.add(wid)
        elif r.get("status") in ("no_changes", "degraded") and not r.get("recovered"):
            # 正常 pipeline 的 no_changes/degraded → 当作已完成（pipeline 自己认定的状态）
            # 但 recover 推断的 no_changes（有 recovered=True）→ 不算完成（没有 commit）
            completed_ids.add(wid)

    repo = Path(meta["repo"])
    config = load_config()
    # 与 cmd_run 一致：注入计量路径与任务 ID，否则 resume 后 planner/worker 计量丢失
    config["_metering_path"] = str(task_dir / "metering.jsonl")
    config["_task_id"] = task_id

    # S10 成本控制：--max-cost / --budget 开启 L3 任务级熔断（默认关闭）
    # S12-P1 G3：--budget-mode 设置预算策略（strict/degrade/ignore）
    _budget_flag = getattr(args, "budget", None) or (getattr(args, "max_cost", None) if args else None)
    _budget_mode_flag = getattr(args, "budget_mode", None) if args else None
    if _budget_flag:
        _cc = dict(config.get("cost_control") or {})
        _cc["enabled"] = True
        _cc["max_budget_usd"] = float(_budget_flag)
        if _budget_mode_flag:
            _cc["budget_mode"] = _budget_mode_flag
        config["cost_control"] = _cc
        logger.info(f"[cost_control] --budget ${_budget_flag} 已启用 L3 任务级熔断 (mode={_cc.get('budget_mode', 'strict')})")
    elif _budget_mode_flag:
        _cc = dict(config.get("cost_control") or {})
        _cc["budget_mode"] = _budget_mode_flag
        config["cost_control"] = _cc
        logger.info(f"[cost_control] --budget-mode {_budget_mode_flag}（未指定预算，仅设策略）")

    # CLI 覆盖：--max-retries / --no-verify-block / --artifact-dir（args 模式）
    if args and getattr(args, 'max_retries', None) is not None:
        config.setdefault("verification", {})["max_retries"] = args.max_retries
    if args and getattr(args, 'no_verify_block', False):
        config.setdefault("verification", {})["block_on_failure"] = False
    if args and getattr(args, 'artifact_dir', None):
        config["artifact_dir"] = args.artifact_dir

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
                  preserve_worktrees=preserve_worktrees,
                  step_confirm=getattr(args, 'step_confirm', False) if args else False)

def cmd_inspect(args) -> None:
    """查看保留的 worktree 现场。"""
    task_id = args.task_id
    as_json = getattr(args, 'json', False)
    show_all = getattr(args, 'all', False)
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print(f"任务不存在: {task_id}")
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
        console.print(_json.dumps({"task_id": task_id, "entries": entries}, indent=2, ensure_ascii=False))
        return

    if not entries:
        console.print(f"任务 {task_id} 中没有保留的 worktree（--all 可查看全部）")
        return

    console.print(f"\n🔍 保留现场: {task_id}")
    console.print(f"📁 任务目录: {task_dir}")
    console.sep("─", 70)
    for e in entries:
        icon_map = {"failed": "❌", "blocked": "🔗", "completed": "✅", "no_changes": "⏭️", "running": "🔄"}
        icon = icon_map.get(e["status"], "❓")
        preserved_tag = " [保留]" if e["is_preserved"] else ""
        console.print(f"\n{icon} {e['id']}{preserved_tag}: {e['title']}")
        console.print(f"状态: {e['status']}")
        if e["failure_reason"]:
            console.print(f"原因: {e['failure_reason']}")
        if e["summary"]:
            console.print(f"摘要: {e['summary']}")
        if e["verify_ok"] is not None:
            console.print(f"验证: {'通过' if e['verify_ok'] else '失败'}")
        if e["worktree_exists"]:
            console.print(f"📁 {e['worktree_path']}")
            console.print(f"🔗 git branch: {e['branch']}")
            if e["has_task_md"]:
                console.print(f"📝 TASK.md | result.json")
        else:
            console.print(f"(worktree 不存在 — 已清理或未创建)")
    console.sep("─", 70)
    console.print(f"提示: cd 到 worktree 路径查看完整文件状态")


def cmd_list() -> None:
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        console.print("暂无任务")
        return
    console.print(f"{'任务ID':<26} {'状态':<12} {'子任务':<8} {'参考文档':<12} {'描述'}")
    console.sep("─", 90)
    for t in tasks:
        meta_path = t / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = meta.get("status", "unknown")
        icon = {"completed": "🟢", "aborted": "🟡", "failed": "🔴", "cancelled": "⏹️"}.get(status, "⚪")
        docs = ",".join(meta.get("reference_docs", []))[:15]
        console.print(f"{t.name:<25} {icon} {status:<10} {len(meta.get('subtasks',[])):<8} {docs:<12} {meta.get('task','')[:30]}")

def cmd_show(args=None):
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
    elif len(sys.argv) < 3:
        console.print("Usage: agent_go show <task-id>")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print("任务不存在")
        sys.exit(1)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    console.print(f"\n🆔 {task_id}")
    console.print(f"📝 {meta['task']}")
    console.print(f"📁 {meta['repo']}")
    console.print(f"📊 {meta.get('status','unknown')}")
    if meta.get("reference_docs"):
        console.print(f"📎 参考文档: {', '.join(meta['reference_docs'])}")
    results = meta.get("results", [])
    for i, st in enumerate(meta.get("subtasks", [])):
        r = results[i] if i < len(results) else None
        icon = "✅" if r and r["status"] == "completed" else "❌" if r else "⏳"
        console.print(f"\n{icon} [{st['id']}] {st['title']}")
        if st.get("agent_prompt"):
            console.print(f"🤖 Agent Prompt: {st['agent_prompt'][:100]}...")
        # Agent 角色和 Skill 可观测性
        agent_type = st.get("agent_type", "developer")
        source = r.get("agent_type_source", "default") if r else st.get("_agent_type_source", "default")
        source_label = {"llm": "LLM", "rule": "规则", "default": "默认", "inferred": "推断"}.get(source, source)
        console.print(f"👤 Agent: {agent_type} (来源: {source_label})")
        skills = st.get("skills", [])
        if skills:
            console.print(f"🧠 Skill: {', '.join(skills)}")
        unresolved = r.get("skills_unresolved", []) if r else []
        if unresolved:
            console.print(f"⚠️  Skill 未找到: {', '.join(unresolved)}")
        if r:
            console.print(f"📊 {r['summary']}")

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
        changes_requested = getattr(args, 'changes_requested', False)
        comment_text = getattr(args, 'comment_text', "") or ""
        deep_review = getattr(args, 'deep', False)
    else:
        approve = "--approve" in sys.argv
        reject = "--reject" in sys.argv
        changes_requested = "--changes-requested" in sys.argv
        deep_review = "--deep" in sys.argv
        comment_text = ""
        if "--comment-text" in sys.argv:
            try:
                i = sys.argv.index("--comment-text")
                comment_text = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
            except (IndexError, ValueError):
                pass
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
        _show_task_review(task_id, approve=approve, reject=reject,
                          changes_requested=changes_requested, comment_text=comment_text,
                          deep_review=deep_review)
        return

    # 代码审查（原有逻辑）
    if not repo_path:
        console.print("Usage: agent_go review <repo-path> [--pr <N>] [--yes] | --task <task-id>")
        return
    repo = Path(repo_path).resolve()
    if not repo.exists():
        console.print(f"路径不存在: {repo}")
        return

    prompt = "请审查当前项目的代码变更，输出审查报告。重点检查：安全性、错误处理、代码质量、潜在bug。"
    if pr_ref:
        prompt = f"请审查 PR #{pr_ref} 的代码变更，输出审查报告。重点检查：安全性、错误处理、代码质量、潜在bug、API设计。"

    if headless:
        import subprocess
        result = subprocess.run(
            ["claude", "-p", prompt, "--permission-mode", "bypassPermissions", "--no-session-persistence"],
            cwd=str(repo))
        console.print(f"\n审查完成 (exit: {result.returncode})")
    else:
        import subprocess
        subprocess.run(["claude", str(repo)])


def _show_task_review(task_id: str, approve: bool = False, reject: bool = False,
                      changes_requested: bool = False, comment_text: str = "",
                      deep_review: bool = False) -> None:
    """显示任务结果审查（M7）— 按文件分组展示变更摘要。

    Args:
        deep_review: 是否启用深层审查（独立模型分析每个子任务的 diff）
    """
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.error(f"任务不存在: {task_id}")
        return

    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        console.error(f"任务元数据不存在: {meta_path}")
        return

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        console.error(f"无法读取任务元数据: {e}")
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

    # S7 叠加式审查流水线：深层审查（独立模型分析 diff）
    if deep_review:
        lines.append("## 🔬 深层审查（独立模型）")
        lines.append("")
        for r in results:
            sid = r.get("subtask_id", "")
            st = subtask_map.get(sid, {})
            title = st.get("title", "")
            r_status = r.get("status", "")
            if r_status not in ("completed", "failed"):
                continue
            wt_path = r.get("worktree", "")
            if not wt_path or not Path(wt_path).exists():
                continue
            try:
                # 获取 git diff
                diff = subprocess.run(
                    ["git", "diff", "HEAD"],
                    cwd=str(wt_path), capture_output=True, text=True, timeout=15,
                ).stdout
                if not diff.strip():
                    continue
                # 获取 repo 路径构建审查 prompt
                review_repo = meta.get("repo", "")
                review_prompt = (
                    f"你是一位资深代码审查者。请审查以下 git diff 变更。\n\n"
                    f"### 子任务: {title} ({sid})\n"
                    f"### 任务描述: {st.get('description', '')[:200]}\n\n"
                    f"### git diff:\n```diff\n{diff[:3000]}\n```\n\n"
                    f"请评估：\n"
                    f"1. 代码是否正确实现了需求？\n"
                    f"2. 是否有潜在的 bug、安全问题或性能问题？\n"
                    f"3. 代码风格是否与现有代码一致？\n"
                    f"4. 是否有更好的实现方式？\n\n"
                    f"对每个问题分别回答「通过」或「需改进」，并附上具体说明。"
                )
                from .router import resolve_provider, ProviderConfig, call_with_role
                _route = resolve_provider("reviewer", config)
                if _route:
                    _review_pc = _route.primary
                    _review_key = (_review_pc.api_key or
                                   os.environ.get("AGENT_GO_API_KEY", "") or
                                   config.get("plan_api", {}).get("api_key", ""))
                else:
                    _plan_api = config.get("plan_api", {})
                    _review_pc = ProviderConfig(
                        provider=_plan_api.get("provider", "anthropic"),
                        base_url=_plan_api.get("base_url", ""),
                        model=_plan_api.get("model", ""),
                    )
                    _review_key = get_api_key(config)
                _review_content, _review_metering = call_with_role(
                    type('_Route', (), {'role': 'reviewer', 'primary': _review_pc,
                                         'fallback': None})(),
                    [{"role": "user", "content": review_prompt}],
                    _review_key, logger, task_id=task_id, subtask_id=sid,
                )
                lines.append(f"### {sid}: {title}")
                lines.append("")
                lines.append(f"```\n{_review_content[:2000]}\n```")
                lines.append("")
            except Exception as e:
                lines.append(f"### {sid}: {title}")
                lines.append(f"> ⚠️ 审查失败: {e}")
                lines.append("")

    # 质量仪表
    quality = _build_quality_dashboard(meta, task_dir=task_dir)
    if quality:
        lines.append(quality)

    # 审查结论（三态：changes-requested > reject > approve）
    if changes_requested:
        _conclusion = {
            "task_id": task_id,
            "reviewed_at": datetime.now().isoformat(),
            "decision": "changes-requested",
            "summary": comment_text or "需要修改后重新审查",
        }
        (task_dir / "review.json").write_text(
            json.dumps(_conclusion, indent=2, ensure_ascii=False), encoding="utf-8")
        lines.append("")
        lines.append(f"📝 **需要修改** — 已写入 review.json")
        if comment_text:
            lines.append(f"  审查意见: {comment_text}")
        lines.append("")
        lines.append("**建议**: 修复问题后执行 `agent_go review --task <id> --approve` 重新审查")
    elif reject:
        _conclusion = {
            "task_id": task_id,
            "reviewed_at": datetime.now().isoformat(),
            "decision": "rejected",
            "summary": comment_text or "审查未通过",
        }
        (task_dir / "review.json").write_text(
            json.dumps(_conclusion, indent=2, ensure_ascii=False), encoding="utf-8")
        lines.append("")
        lines.append("❌ **审查未通过** — 已写入 review.json")
        if comment_text:
            lines.append(f"  审查意见: {comment_text}")
    elif approve:
        _conclusion = {
            "task_id": task_id,
            "reviewed_at": datetime.now().isoformat(),
            "decision": "approved",
            "summary": comment_text or "审查通过",
        }
        (task_dir / "review.json").write_text(
            json.dumps(_conclusion, indent=2, ensure_ascii=False), encoding="utf-8")
        lines.append("")
        lines.append("✅ **审查通过** — 已写入 review.json")

    console.print("\n".join(lines))


def _build_quality_dashboard(meta: dict, task_dir: Optional[Path] = None) -> str:
    """构建 PR 质量仪表（M3）— 回答「我该不该 merge？」。

    返回 Markdown 格式的质量评估段，包含：
    - 通过率统计（completed / total / degraded）
    - 每子任务验证状态（verify_ok, duration, failure_reason）
    - 审查结论（从 review.json 读取）
    - Plan 版本信息（从 plans/ 目录读取）
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

    # 审查结论
    if task_dir:
        review_path = task_dir / "review.json"
        if review_path.exists():
            try:
                review = json.loads(review_path.read_text(encoding="utf-8"))
                decision = review.get("decision", "?")
                decision_icon = {"approved": "✅", "rejected": "❌", "changes-requested": "📝"}.get(decision, "❓")
                reviewed_at = review.get("reviewed_at", "")[:19]
                review_summary = review.get("summary", "")
                lines.append(f"| **审查结论** | {decision_icon} {decision} ({reviewed_at}) |")
                lines.append(f"| **审查摘要** | {review_summary} |")
                lines.append("")
            except (json.JSONDecodeError, OSError):
                pass

        # Plan 版本信息
        plans_dir = task_dir / "plans"
        if plans_dir.exists():
            versions = sorted([f.stem for f in plans_dir.glob("v*.json")], key=lambda x: int(x[1:]))
            if versions:
                lines.append(f"| **Plan 版本** | {len(versions)} 个版本 (v{versions[0][1:]}–v{versions[-1][1:]}) |")
                lines.append("")

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


def _save_plan_snapshot(task_dir: Path, plan: dict, version: int) -> None:
    """保存 Plan 版本快照到 task_dir/plans/v{version}.json。"""
    plans_dir = task_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "version": version,
        "saved_at": datetime.now().isoformat(),
        "plan": plan,
    }
    path = plans_dir / f"v{version}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


def _show_plan_history(task_dir: Path) -> None:
    """显示 Plan 版本历史。"""
    plans_dir = task_dir / "plans"
    if not plans_dir.exists():
        console.print("📋 Plan 版本历史")
        console.print("  (无历史版本)")
        return

    versions = sorted(
        [f.stem for f in plans_dir.glob("v*.json")],
        key=lambda x: int(x[1:]),
    )
    if not versions:
        console.print("📋 Plan 版本历史")
        console.print("  (无历史版本)")
        return

    lines = ["## 📋 Plan 版本历史", ""]
    for v in versions:
        try:
            data = json.loads((plans_dir / f"{v}.json").read_text(encoding="utf-8"))
            saved_at = data.get("saved_at", "?")[:19]
            plan = data.get("plan", {})
            steps = plan.get("steps", plan.get("subtasks", []))
            step_count = len(steps)
            step_titles = "; ".join(s.get("title", "?")[:30] for s in steps[:5])
            lines.append(f"| `{v}` | {saved_at} | {step_count} 步骤 | {step_titles}... |")
        except Exception as e:
            lines.append(f"| `{v}` | ❌ 读取失败: {e} | |")

    console.print("\n".join(lines))


def _show_plan_diff(task_dir: Path, v1: int, v2: Optional[int] = None) -> None:
    """对比两个 Plan 版本的差异（P2-2：增强对比）。"""
    plans_dir = task_dir / "plans"
    if not plans_dir.exists():
        console.error("无 Plan 版本历史")
        return

    versions = sorted(
        [int(f.stem[1:]) for f in plans_dir.glob("v*.json")],
    )
    if not versions:
        console.error("无 Plan 版本历史")
        return

    if v2 is None:
        v2 = versions[-1]
    if v1 not in versions or v2 not in versions:
        console.error(f"版本不存在 (可用: {versions})")
        return

    data1 = json.loads((plans_dir / f"v{v1}.json").read_text(encoding="utf-8"))
    data2 = json.loads((plans_dir / f"v{v2}.json").read_text(encoding="utf-8"))
    plan1 = data1.get("plan", {})
    plan2 = data2.get("plan", {})
    steps1 = plan1.get("steps", plan1.get("subtasks", []))
    steps2 = plan2.get("steps", plan2.get("subtasks", []))

    console.sep("=", 68)
    console.title(f"🔍 Plan Diff: v{v1} → v{v2}")
    console.print(f"保存: {data1.get('saved_at', '?')[:19]} → {data2.get('saved_at', '?')[:19]}")

    # 概览统计
    _s1_ids = {s["id"] for s in steps1}
    _s2_ids = {s["id"] for s in steps2}
    _added = _s2_ids - _s1_ids
    _removed = _s1_ids - _s2_ids
    _matched = _s1_ids & _s2_ids
    console.subtitle("概览")
    console.print(f"  步骤: {len(steps1)} → {len(steps2)}  ({'+' if _added else ''}{len(_added)}/-{len(_removed)}/={len(_matched)})")
    if plan1.get("overview") != plan2.get("overview"):
        console.print(f"  📝 概述: ✏️ 已修改")

    # 全局字段对比
    _global_keys = ["estimated_effort"]
    _global_diffs = [(k, plan1.get(k, ""), plan2.get(k, "")) for k in _global_keys if plan1.get(k) != plan2.get(k)]
    if _global_diffs:
        console.subtitle("全局变更")
        for _k, _v1, _v2 in _global_diffs:
            console.print(f"  {_k}: \"{str(_v1)[:60]}\" → \"{str(_v2)[:60]}\"")

    # 步骤对比详情
    console.subtitle("步骤详情")
    _TITLE = 0; _DESC = 1; _FILES = 2; _VER = 3; _DIFF = 4; _AGENT = 5; _SKILL = 6
    _headers = ["#", "标题", "变更"]
    _rows: list[list[str]] = []
    _all_ids = sorted(_s1_ids | _s2_ids)
    for sid in _all_ids:
        s1 = next((s for s in steps1 if s["id"] == sid), None)
        s2 = next((s for s in steps2 if s["id"] == sid), None)
        if not s1:
            _rows.append([str(sid), s2["title"][:50], "🆕 新增"])
            continue
        if not s2:
            _rows.append([str(sid), s1["title"][:50], "🗑️ 删除"])
            continue
        _changes = []
        # Compare each field
        for _field, _label in [("description", "描述"), ("files", "文件"), ("verification", "验证"),
                               ("difficulty", "难度"), ("agent_type", "代理"), ("skills", "技能"), ("risks", "风险")]:
            _v1 = s1.get(_field)
            _v2 = s2.get(_field)
            if _field in ("files", "skills"):
                _v1_set = set(_v1) if _v1 else set()
                _v2_set = set(_v2) if _v2 else set()
                if _v1_set != _v2_set:
                    _changes.append(_label)
            elif _field == "risks":
                if (_v1 or []) != (_v2 or []):
                    _changes.append(_label)
            elif str(_v1) != str(_v2):
                _changes.append(_label)
        if not _changes and s1.get("title") != s2.get("title"):
            _changes.append("标题")
        _tag = ", ".join(_changes) if _changes else "—"
        _row_tag = "✏️  修改" if _changes else "✓"
        _rows.append([str(sid), s1["title"][:50], _row_tag if not _changes else f"✏️  {_tag}"])
    if _rows:
        console.table(_headers, _rows)

    # 依赖对比
    deps1 = plan1.get("dependencies", {})
    deps2 = plan2.get("dependencies", {})
    if deps1 != deps2:
        console.subtitle("依赖变更")
        _all_sids = sorted(set(deps1.keys()) | set(deps2.keys()))
        for sid in _all_sids:
            _d1 = deps1.get(sid, [])
            _d2 = deps2.get(sid, [])
            if _d1 != _d2:
                console.print(f"  步骤 {sid}: {_d1} → {_d2}")
    console.sep("=", 68)


def cmd_pr(args=None):
    """根据已完成任务的 meta.json + git log 生成并推送 PR。"""
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
        offline = getattr(args, 'offline', False)
        do_push = getattr(args, 'push', False)
        remote = getattr(args, 'remote', "origin")
    elif len(sys.argv) < 3:
        console.print("Usage: agent_go pr <task-id> [--offline] [--push] [--remote <name>]")
        sys.exit(1)
    else:
        task_id = sys.argv[2]
        offline = "--offline" in sys.argv
        do_push = "--push" in sys.argv
        remote = "origin"
        if "--remote" in sys.argv:
            try:
                remote = sys.argv[sys.argv.index("--remote") + 1]
            except (IndexError, ValueError):
                pass
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print(f"任务不存在: {task_id}")
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

    # 审查结论
    review_section = ""
    review_path = task_dir / "review.json"
    if review_path.exists():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
            decision = review.get("decision", "?")
            decision_icon = {"approved": "✅", "rejected": "❌"}.get(decision, "❓")
            reviewed_at = review.get("reviewed_at", "")[:19]
            review_summary = review.get("summary", "")
            review_section = f"## Review\n\n- **结论**: {decision_icon} {decision} ({reviewed_at})\n- **摘要**: {review_summary}\n\n"
        except (json.JSONDecodeError, OSError):
            pass

    # 质量仪表（M3）
    quality_dashboard = _build_quality_dashboard(meta, task_dir=task_dir)

    pr_body = f"""## Summary

{meta.get('task', 'N/A')}

{quality_dashboard}
{review_section}## Subtasks

{chr(10).join(subtask_lines)}

## Verification

{context if context else '_No verification details_'}
"""

    if meta.get("issue"):
        pr_body = f"Fixes #{meta['issue']}\n\n{pr_body}"

    # S7: --push 先推送分支到远程
    if do_push and not offline:
        repo = meta.get("repo", "")
        if repo and Path(repo).exists():
            branch = meta.get("base_branch", "main")
            push_result = subprocess.run(
                ["git", "push", remote, f"HEAD:{branch}"],
                cwd=str(Path(repo)), capture_output=True, text=True,
            )
            if push_result.returncode == 0:
                console.success(f"分支已推送到 {remote}/{branch}")
            else:
                console.print(f"⚠ ️ 推送失败: {push_result.stderr.strip()[:200]}")

    if offline:
        out = task_dir / "PR.md"
        out.write_text(pr_body, encoding="utf-8")
        console.print(f"PR 描述已写入 {out}")
        push_hint = f" --push" if not do_push else ""
        console.print(f"请手动创建 PR 或稍后执行: agent_go pr {task_id}{push_hint}")
    else:
        # 在线模式：通过 gh CLI 创建 PR
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as tf:
            tf.write(pr_body)
            pr_file = tf.name
        title = meta.get("task", "agent_go task")[:72]
        base = meta.get("base_branch", "main")
        try:
            if not shutil.which("gh"):
                console.error("未安装 gh CLI。请先安装: brew install gh")
                (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
                console.print(f"PR 描述已备份到 {task_dir}/PR.md")
                return
            result = subprocess.run([
                "gh", "pr", "create", "--title", f"{title}",
                "--body-file", pr_file, "--base", base,
            ], capture_output=True, text=True)
            if result.returncode == 0:
                console.print(result.stdout.strip())
            else:
                console.error(f"gh pr create 失败: {result.stderr.strip()}")
                (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
                console.print(f"PR 描述已备份到 {task_dir}/PR.md")
        finally:
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
            console.print("暂无任务")
            return

        rows = [_get_task_status(td) for td in tasks_dirs]
        rows = [r for r in rows if r is not None]

        if watch:
            subprocess.run(["clear" if os.name == "posix" else "cls"], capture_output=True)

        console.print(f"{'任务ID':<24} {'状态':<6} {'进度':<8} {'耗时':<8} {'Issue':<6} {'当前子任务'}")
        console.sep("─", 110)
        for r in rows:
            issue_str = f"#{r['issue']}" if r['issue'] else "-"
            console.print(f"{r['id']:<24} {r['icon']} {r['status']:<4} {r['progress']:<8} "
                  f"{r['elapsed']:<8} {issue_str:<6} {r['current'][:50]}")
            if r["tail"]:
                for tl in r["tail"]:
                    line_text = tl.split(" | ")[-1] if " | " in tl else tl
                    console.print(f"└ {line_text.strip()[:90]}")
        console.sep("─", 110)
        flags = " --watch" if watch else ""
        flags += " --verbose" if verbose else ""
        console.print(f"共 {len(rows)} 个任务 | agent_go status{flags} | Ctrl+C 退出\n")

        if not watch:
            break
        time.sleep(5)

def cmd_config() -> None:
    config = load_config()
    console.print(json.dumps(config, indent=2, ensure_ascii=False))

def cmd_spec(args) -> None:
    """Task Spec 工具：template（生成模板）/ validate（L1 准入审查）。"""
    sub = getattr(args, "spec_subcommand", None)
    if sub == "template":
        repo = Path(args.repo).resolve() if args.repo else None
        if repo and not repo.exists():
            console.error(f"路径不存在: {repo}")
            sys.exit(1)
        content = render_spec_template(repo)
        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            console.print(f"✅ Task Spec 模板已生成: {out}")
        else:
            console.print(content)
    elif sub == "validate":
        spec_path = Path(args.spec_path)
        if not spec_path.exists():
            console.error(f"Spec 文件不存在: {spec_path}")
            sys.exit(1)
        spec = parse_spec(spec_path)
        if spec is None:
            console.error(f"Spec 解析失败: {spec_path}")
            sys.exit(1)
        repo = Path(args.repo).resolve() if args.repo else None
        violations = validate_spec_l1(spec, repo)
        console.print(f"\n📋 Task Spec: {spec.title or spec_path.name}")
        console.print(f"   完整性: {'✅ 全部必填章节就绪' if spec.is_complete else '❌ 缺失必填章节'}")
        if spec.source_path:
            console.print(f"   来源: {spec.source_path}")
        if not violations:
            console.print("\n✅ L1 准入审查通过（0 项违规）")
        else:
            console.print(f"\n❌ L1 准入审查未通过（{len(violations)} 项违规）：")
            for i, v in enumerate(violations, 1):
                sec = f" §{v.section}" if v.section else ""
                console.print(f"  {i}. [{v.check}{sec}] {v.message}")
                if v.suggestion:
                    console.print(f"     💡 {v.suggestion}")
            sys.exit(1)
    else:
        console.print("Usage: agent_go spec <template|validate> [args]")
        console.print("  template [repo] [--output PATH]  生成空白 Task Spec 模板")
        console.print("  validate <spec_path> [repo]       对 Spec 文件运行 L1 准入审查")

def cmd_clean(args=None) -> None:
    import shutil as _shutil
    import time as _time
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        console.print("暂无任务")
        return
    # S12 失败清理 #3：--older-than N 天 → 只清理早于 N 天前未修改的任务目录（保留期）
    older_than = getattr(args, "older_than", None) if args else None
    if older_than:
        _cutoff = _time.time() - float(older_than) * 86400
        _before = len(tasks)
        tasks = [t for t in tasks
                 if (t.stat().st_mtime if t.exists() else 0) < _cutoff]
        _filtered = _before - len(tasks)
        if _filtered:
            console.print(f"跳过 {_filtered} 个近期任务（--older-than {older_than} 天保留）")
    if not tasks:
        console.print("无符合条件的任务")
        return
    console.print(f"将清理 {len(tasks)} 个任务目录:")
    for t in tasks:
        console.print(f"{t.name}")
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
        console.print(f"已清理 {len(tasks)} 个任务")
    else:
        console.print("已取消")

def cmd_skills(args=None) -> None:
    """列出或查看 Skill。agent_go skills [list | show <name>]。"""
    from .skills import get_skill_full

    sub = getattr(args, "skills_subcommand", None) if args else None

    # show <name>：输出完整 SKILL.md（人类可读 / --json 结构化，供 Agent 自描述读取）
    if sub == "show":
        name = args.name
        info = get_skill_full(name)
        if not info:
            console.error(f"Skill 不存在: {name}。可用: agent_go skills list")
            return
        if getattr(args, "json_mode", False):
            console.print(json.dumps({
                "name": info["name"], "description": info["description"],
                "path": info["path"], "frontmatter": info["frontmatter"],
                "body": info["body"], "allowed_tools": info["allowed_tools"],
            }, indent=2, ensure_ascii=False))
            return
        console.print(f"\n📚 Skill: {info['name']}")
        console.print(f"📄 {info['path']}")
        console.sep("─", 55)
        console.print(f"📝 描述: {info['description']}")
        if info["allowed_tools"]:
            console.print(f"🔧 工具白名单: {', '.join(info['allowed_tools'])}")
        console.sep("─", 55)
        # 原始 SKILL.md（含 frontmatter）——Agent 可直接读取完整使用说明
        if info["raw"]:
            console.force(info["raw"])
        return

    # 默认：list（原有逻辑）
    skills = list_skills()
    if not skills:
        console.print("\n暂无可用 Skill。在 ~/.agent_go/skills/<name>/SKILL.md 创建。")
        console.print("示例 Skill 格式: YAML frontmatter + Markdown body")
        return
    console.print(f"\n📚 可用 Skill ({len(skills)} 个)")
    console.sep("─", 55)
    for s in skills:
        desc = s["description"][:45] + "..." if len(s["description"]) > 45 else s["description"]
        console.print(f"{s['name']:<30} {desc}")
    console.sep("─", 55)
    console.print("查看完整内容: agent_go skills show <name>")

def cmd_cache(args=None):
    """Plan 缓存管理。"""
    from .api import list_cache_entries, clean_expired_cache

    if args and hasattr(args, 'subcommand'):
        sub = args.subcommand
    elif len(sys.argv) < 3:
        console.print("Usage: agent_go cache <list|clean|clear|stats>")
        return
    else:
        sub = sys.argv[2]
    config = load_config()

    if sub == "list":
        entries = list_cache_entries()
        if not entries:
            console.print("暂无缓存")
            return
        console.print(f"{'缓存键':<14} {'任务':<30} {'创建':<18} {'命中':<6}")
        console.sep("─", 70)
        for e in entries:
            m = e.get("meta", {})
            key = e.get("cache_key", "")[:12]
            task = m.get("task", "?")[:30]
            created = m.get("created_at", "?")[:16]
            hits = m.get("hit_count", 0)
            console.print(f"{key:<14} {task:<30} {created:<18} {hits:<6}")
    elif sub == "clean":
        removed = clean_expired_cache(config)
        console.print(f"清理 {removed} 条过期缓存")
    elif sub == "clear":
        import shutil
        from .api import _cache_dir
        d = _cache_dir()
        if d.exists():
            shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        console.print("已清除所有缓存")
    elif sub == "stats":
        entries = list_cache_entries()
        console.print(f"缓存条目: {len(entries)}")
        if entries:
            total_hits = sum(e.get("meta", {}).get("hit_count", 0) for e in entries)
            console.print(f"总命中: {total_hits}")
            console.print(f"磁盘: {_cache_size()}")
    else:
        console.print(f"未知子命令: {sub}。可用: list, clean, clear, stats")


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


def cmd_recover(args) -> None:
    """从 worktree 状态重建被异常中断的任务 meta.json。

    适用场景：
    - bench subprocess timeout → agent_go 被 SIGKILL → meta.json 永远停在 plan 阶段
    - 用户 Ctrl-C 在 verify 阶段
    - 任何 meta.status=running + meta.results=[] 但 worktree 有产出的情况

    工作流：
    1. 扫描每个 sub-N/work 的 git log 推断 commits 数量
    2. 如果有未提交的 orphan 工作，自动 commit（保留 claude 在 SIGKILL 前的工作）
    3. 从 execution.log 推断 verify 结果
    4. 原子写 meta.json（recovered=true 标记）
    """
    task_id = args.task_id
    dry_run = getattr(args, "dry_run", False)

    # 延迟导入（避免 core import 增强模块时拉起 recover）
    from .recover import recover_task

    console.print(f"🔧 恢复任务 {task_id}")
    console.print(f"   dry_run={dry_run}")

    result = recover_task(
        task_id,
        update_meta=not dry_run,
    )

    if "error" in result:
        console.error(f"{result['error']}")
        sys.exit(1)

    console.print(f"\n📊 扫描结果：")
    for sub in result.get("recovered", []):
        marker = "🆕" if sub.get("recovered") and sub.get("recovered_at") else "📦"
        orphan = " (orphan reset)" if sub.get("orphan_reset") else ""
        verify_str = f"verify_ok={sub.get('verify_ok')}" if sub.get("verify_ok") is not None else "verify=unknown"
        console.print(f"   {marker} {sub['subtask_id']:8s}: status={sub['status']:12s}  "
                      f"commits={sub.get('commits', 0)}  {verify_str}{orphan}")

    overall = result.get("overall_status", "unknown")
    console.print(f"\n   overall_status: {overall}")
    if dry_run:
        console.print(f"   (dry-run，未写入 meta.json)")
    else:
        console.print(f"   ✓ meta.json 已更新（recovered_at={result.get('recovered_at', '?')[:19]}）")


def cmd_checkpoint(args) -> None:
    """P4-2: 检查点快照管理。"""
    from .console import _LazyConsole
    _con = _LazyConsole()
    task_dir = AGENT_GO_DIR / args.task_id
    if not task_dir.exists():
        _con.error(f"任务不存在: {args.task_id}")
        return

    subcmd = args.checkpoint_command if args else "list"

    if subcmd == "list":
        snapshots = list_checkpoints(args.task_id)
        if not snapshots:
            _con.print(f"任务 {args.task_id} 没有检查点")
            return
        if getattr(args, "json_mode", False):
            import json as _json
            _con.force(_json.dumps(snapshots, indent=2, ensure_ascii=False))
            return
        _con.sep("─", 55)
        _con.title(f"📸 检查点: {args.task_id}")
        for s in snapshots:
            ts = s.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
            _con.print(f"  {s['subtask_id']:<10} {s.get('file_count', 0)} files  {time_str}")
        _con.sep("─", 55)

    elif subcmd == "restore":
        sub_id = args.name
        target = Path(args.target) if args.target else None
        n = restore_checkpoint(args.task_id, sub_id, target)
        if n > 0:
            _con.success(f"已恢复 {n} 个文件（{sub_id} → {target or '默认 worktree'}）")
        else:
            _con.warning(f"检查点 {sub_id} 不存在或无可恢复文件")

    elif subcmd == "delete":
        sub_id = args.name
        mgr = SnapshotManager(task_dir)
        if mgr.delete(sub_id):
            _con.success(f"已删除检查点 {sub_id}")
        else:
            _con.warning(f"检查点 {sub_id} 不存在")

    else:
        _con.print(f"未知操作: {subcmd}。可用: list | restore | delete")


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
        console.success("角色感知路由已启用")
        _print_router_config(router_cfg)
        return

    if subcmd == "disable":
        router_cfg["enabled"] = False
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        console.success("角色感知路由已禁用（回退到 plan_api）")
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
            console.print("⚠ ️  --fallback-provider 需要同时指定 --fallback-model 和 --fallback-base-url")

        # Planner 铁律：不允许配置降级到弱模型
        if role == "planner" and "fallback" in role_cfg:
            console.print("⚠ ️  政策违规：Planner 角色配置了 fallback 降级（规划 token 省小钱，Worker token 数倍膨胀）")
            console.print("路由执行时 metering 将标记 policy_violation=planner_fallback_configured")
            console.print("建议移除 fallback: agent_go router set-role planner --provider ... --model ... --base-url ...")

        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        console.success(f"{role} 角色已配置")
        _print_role_config(role, router_cfg)
        return

    console.print(f"未知操作: {subcmd}。可用: show | enable | disable | set-role")


def _print_router_config(router_cfg: dict) -> None:
    """打印路由器配置摘要。"""
    enabled = router_cfg.get("enabled", False)
    status = "🟢 启用" if enabled else "⚪ 禁用"
    console.print(f"\n🔀 角色感知路由: {status}")
    console.info(f"   熔断: {router_cfg.get('circuit_breaker', {}).get('failure_threshold', 5)} 次失败 → "
          f"{router_cfg.get('circuit_breaker', {}).get('cooldown_seconds', 60)}s 冷却")
    console.print(f"Agent 映射: {json.dumps(router_cfg.get('agent_type_mapping', {}), ensure_ascii=False)}")

    roles = router_cfg.get("roles", {})
    if roles:
        console.print("角色配置:")
        for role_name in ["planner", "worker", "reviewer"]:
            if role_name in roles:
                _print_role_config(role_name, router_cfg)
    else:
        console.print("⚠️  未配置任何角色，请使用 'agent_go router set-role' 配置")


def _print_role_config(role_name: str, router_cfg: dict) -> None:
    """打印单个角色配置。"""
    roles = router_cfg.get("roles", {})
    rc = roles.get(role_name, {})
    provider = rc.get("provider", "?")
    model = rc.get("model", "?")
    fallback = rc.get("fallback")
    fb_str = f" → fallback: {fallback['provider']}:{fallback['model']}" if fallback else " (不降级)"
    console.print(f"{role_name}: {provider}:{model}{fb_str}")


def cmd_agents() -> None:
    """列出所有可用的 Agent 类型。"""
    agents = list_agent_types()
    console.print(f"\n🤖 Agent 类型 ({len(agents)} 种)")
    console.sep("─", 55)
    for a in agents:
        src = "内置" if a.get("source") == "builtin" else "用户"
        desc = a["description"][:40] + "..." if len(a["description"]) > 40 else a["description"]
        console.print(f"{a['type']:<25} [{src}] {desc}")
    console.sep("─", 55)

def _install_sigterm_handler() -> None:
    """P0 Layer 2：注册 SIGTERM/SIGINT handler 优雅退出。

    收到信号时（来自 bench 的 cooperative timeout，或用户 Ctrl-C）：
    - re-raise 信号让 pipeline.py 已有的 handler 处理（kill children + save meta.json）
    - cli.py 层只确保信号不被静默吞掉
    """
    import signal as _sig
    def _re_raise(signum, frame):
        import os
        import signal as _sig
        _sig.signal(signum, _sig.SIG_DFL)
        os.kill(os.getpid(), signum)
    _sig.signal(_sig.SIGTERM, _re_raise)
    _sig.signal(_sig.SIGINT, _re_raise)


def _cleanup_stale_tasks(max_age_hours: int = 1) -> int:
    """P3 Layer 5：清理卡死的 running task。

    场景：agent_go run 被 SIGKILL 后 meta.json 永远 status=running，
    下次启动时这些 task 会阻塞 bench/recover。

    策略：扫描 ~/.agent_go/task-* 中所有 meta.json：
    - status=running 且 meta.json mtime > max_age_hours → 标记为 stale_aborted
    - 保留结果列表（不删除），让 recover 能继续处理

    Returns: 清理的 task 数量
    """
    import json as _json
    import time as _time
    cutoff = _time.time() - max_age_hours * 3600
    cleaned = 0
    if not AGENT_GO_DIR.exists():
        return 0
    for task_dir in AGENT_GO_DIR.glob("task-*"):
        meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            mtime = meta_path.stat().st_mtime
            if mtime < cutoff:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("status") == "running":
                    meta["status"] = "stale_aborted"
                    meta["stale_aborted_at"] = _time.time()
                    meta_path.write_text(_json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                    cleaned += 1
        except (OSError, _json.JSONDecodeError):
            continue
    return cleaned


def main() -> None:
    # P0 Layer 2：先注册 SIGTERM handler（确保 bench timeout 触发的信号被优雅处理）
    _install_sigterm_handler()

    # P3 Layer 5：启动时清理卡死的 running task（避免历史脏数据阻塞 bench/recover）
    stale_count = _cleanup_stale_tasks(max_age_hours=1)
    if stale_count > 0:
        console.print(f"⚠ ️  已清理 {stale_count} 个卡死的 stale task（meta.json 标记为 stale_aborted）")

    try:
        parser = _build_parser()
        args = parser.parse_args()

        # R-3: --profile 写入环境变量，供所有 load_config() 调用点统一解析
        profile = getattr(args, "profile", None)
        if profile:
            os.environ["AGENT_GO_PROFILE"] = profile

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
        elif args.command == "spec":
            cmd_spec(args)
        elif args.command == "clean":
            cmd_clean(args)
        elif args.command == "pr":
            cmd_pr(args)
        elif args.command == "skills":
            cmd_skills(args)
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
        elif args.command == "recover":
            cmd_recover(args)
        elif args.command == "inspect":
            cmd_inspect(args)
        elif args.command == "plan-history":
            task_dir = AGENT_GO_DIR / args.task_id
            if not task_dir.exists():
                console.error(f"任务不存在: {args.task_id}")
            else:
                _show_plan_history(task_dir)
        elif args.command == "plan-diff":
            task_dir = AGENT_GO_DIR / args.task_id
            if not task_dir.exists():
                console.error(f"任务不存在: {args.task_id}")
            else:
                _show_plan_diff(task_dir, args.v1, args.v2)
        elif args.command == "replay":
            cmd_replay(args)
        elif args.command == "checkpoint":
            cmd_checkpoint(args)
        elif args.command == "mcp":
            cmd_mcp(args)
        elif args.command == "web":
            cmd_web(args)
    except KeyboardInterrupt:
        console.print("\n\n⏹️  用户中断（Ctrl+C）")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)

