import sys
import os
import subprocess
import json
import time
import logging
import argparse
import shutil
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
from .spec import parse_spec, validate_spec_l1, render_spec_template, detect_step_conflicts, extract_do_not_touch
from .agents import load_agent_type, list_agent_types
from .eval import cmd_eval
from .replay import cmd_replay
from .checkpoint import list_checkpoints, restore_checkpoint, SnapshotManager
from .mcp_server import main as cmd_mcp
from .web_server import main as cmd_web
from .tui import cmd_status_tui
from .workflow_gen import cmd_ci
from .git_utils import init_git_repo, get_dirty_files, commit_baseline
from .status import task_status, set_task_status
from .exit_codes import EX_OK, EX_ERROR, EX_USAGE, EX_SYSTEM

logger = logging.getLogger(__name__)

console = _LazyConsole()

__all__ = [
    "main", "cmd_run", "cmd_resume", "cmd_list", "cmd_show",
    "cmd_status", "cmd_config", "cmd_clean", "cmd_pr", "cmd_review",
    "cmd_router",
]

def _parse_parallel(value: str) -> int:
    """--parallel 解析：clamp 1-8（M5.3 并发上限保护）。非法值回退 3（兼容历史行为）。"""
    try:
        p = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(8, p))


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
    run_parser.add_argument("--parallel", type=_parse_parallel, default=1, help="Max concurrent subtasks 1-8 (default: 1)")
    run_parser.add_argument("--e2e", action="store_true",
                            help="强制端到端模式（hard 任务不拆分子任务，保留全局上下文）")
    run_parser.add_argument("--split", action="store_true",
                            help="强制拆分模式（覆盖端到端判定，强制 Plan 拆分执行）")
    run_parser.add_argument("--confirm-mode", choices=["auto", "web"], default="auto",
                            dest="confirm_mode",
                            help="计划确认通道：auto=--yes 全自动；web=写 pending 文件等待 Web 确认（R5b）")
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
                            help="启用 goal 指令注入（TASK.md 追加 /goal 循环，默认关闭；等价 --goal-mode force）")
    run_parser.add_argument("--goal-hook", action="store_true", dest="goal_hook",
                            help="注入 Stop Hook（.claude/settings.local.json + verify-goal.sh，默认关闭；等价 --goal-mode hook）")
    run_parser.add_argument("--goal-mode", choices=["auto", "off", "force", "hook"], default=None,
                            help="Goal 执行策略：auto=系统按任务特征判断（默认关闭方向）、off=关闭、force=强制持续执行、hook=force+Stop Hook")
    run_parser.add_argument("--agent-loop", action="store_true",
                            help="启用混合策略：简单任务走直接 API，复杂任务保留 claude -p（默认关闭）")
    run_parser.add_argument("--interactive", action="store_true",
                            help="启动 TUI 仪表盘实时监控子任务执行")
    run_parser.add_argument("--step-confirm", action="store_true",
                            help="每波执行前暂停确认（适用于交互式非 TUI 场景）")
    run_parser.add_argument("--auto-init", action="store_true",
                            help="目标目录非 git 仓库时自动 git init + 首次 commit（默认关闭）")
    run_parser.add_argument("--allow-dirty", action="store_true", dest="allow_dirty",
                            help="允许在主工作区有未提交改动时直接运行（子任务将基于 HEAD，看不到这些改动，风险自负）")
    run_parser.add_argument("--baseline", action="store_true", dest="baseline",
                            help="运行前把主工作区未提交改动显式 commit 为基线（让子任务基于正确基线）")
    run_parser.add_argument("--artifact-dir", default=None,
                            help="产物导出目录：子任务写入 worktree/__artifacts__/ 的文件在此收集导出（默认不导出）")
    run_parser.add_argument("--max-cost", type=float, default=None, dest="max_cost",
                            help="任务级成本预算（USD）：累计 metering 成本超限即熔断剩余子任务（默认关闭）")
    run_parser.add_argument("--budget", type=float, default=None, dest="budget",
                            help="--max-cost 的别名（per-task 成本预算，S12-P1 G3）；同传时 --budget 生效")
    run_parser.add_argument("--budget-mode", choices=["strict", "degrade", "ignore"], default=None, dest="budget_mode",
                            help="预算策略（S12-P1 G3）：strict=超预算 block；degrade=切便宜模型继续；ignore=关 L3（默认 strict）")
    run_parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                            help="生成 Plan 并展示子任务拆解，但不执行任何操作。配合 --json 输出结构化结果")
    run_parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to config JSON file (default: ~/.agent_go/config.json)")

    # resume 子命令
    resume_parser = subparsers.add_parser("resume", help="Resume a paused/interrupted task")
    resume_parser.add_argument("task_id", help="Task ID to resume")
    resume_parser.add_argument("--yes", "-y", action="store_true", help="Skip all confirmations")
    resume_parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    resume_parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-error output")
    resume_parser.add_argument("--parallel", type=_parse_parallel, default=1, help="Max concurrent subtasks 1-8")
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

    # report 子命令（P1：任务共享——报告导出）
    report_parser = subparsers.add_parser("report", help="导出任务报告（md/html，用于分享/分发）")
    report_parser.add_argument("task_id", help="Task ID to report")
    report_parser.add_argument("--format", choices=["md", "html"], default="md",
                               help="报告格式（默认 md）")
    report_parser.add_argument("--output", default="",
                               help="输出路径（默认 <task_id>.md / .html，- 输出到 stdout）")

    # status 子命令
    status_parser = subparsers.add_parser("status", help="Live status monitoring")
    status_parser.add_argument("--watch", "-w", action="store_true", help="Auto-refresh status")
    status_parser.add_argument("--no-tui", action="store_true", help="Text mode instead of TUI")
    status_parser.add_argument("--verbose", "-v", action="store_true", help="Show Claude events")

    # clean 子命令
    _clean_parser = subparsers.add_parser("clean", help="Remove task data")
    _clean_parser.add_argument("--older-than", type=int, default=None,
                               help="只清理早于 N 天前的任务目录（保留期清理，S12 失败清理 #3）")
    _clean_parser.add_argument("--fixture-worktrees", action="store_true",
                               help="只清理 eval_suite/fixtures/ 下 fixture 仓库的失效 worktree 注册（ISSUE-38，不删任务目录）")

    # config 子命令
    config_parser = subparsers.add_parser("config", help="View/switch configuration (local/cloud profiles)")
    config_sub = config_parser.add_subparsers(dest="config_subcommand", help="Config operation")
    config_local_parser = config_sub.add_parser("local", help="一键生成并激活纯本地 profile")
    config_local_parser.add_argument("--url", default="http://localhost:4000",
                                     help="本地 OpenAI 兼容代理地址（默认 http://localhost:4000）")
    config_sub.add_parser("cloud", help="恢复云端配置（回退默认 config.json）")
    config_sub.add_parser("status", help="显示当前 profile + 端点健康检查")

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
    resolve_skill_parser = skills_sub.add_parser("resolve", help="Trace a Skill's symlink resolution chain")
    resolve_skill_parser.add_argument("name", help="Skill name")
    resolve_skill_parser.add_argument("--json", action="store_true", dest="json_mode",
                                      help="Output resolution chain as JSON")

    # agents 子命令
    subparsers.add_parser("agents", help="List available Agent types")

    # models 子命令（P3.2：模型池管理——① Model Registry 的查看与注册）
    models_parser = subparsers.add_parser("models", help="Model registry 管理（list/add）")
    models_sub = models_parser.add_subparsers(dest="models_subcommand", help="Models operation")
    models_sub.add_parser("list", help="列出 registry 中的模型")
    models_add_parser = models_sub.add_parser("add", help="注册新模型到 models.json")
    models_add_parser.add_argument("model_id", help="模型唯一名（如 kimi-k3）")
    models_add_parser.add_argument("--provider", default="openai", choices=["anthropic", "openai", "deepseek", "custom"])
    models_add_parser.add_argument("--base-url", required=True, dest="base_url", help="API 端点")
    models_add_parser.add_argument("--key-ref", default="", dest="key_ref", help="key 引用（env:VAR / VAR / secret:path#field）")
    models_add_parser.add_argument("--thinking", action="store_true", help="推理模型需 thinking enabled")
    models_add_parser.add_argument("--json-loose", action="store_true", dest="json_loose", help="JSON 输出不稳定（needs_response_format）")
    models_add_parser.add_argument("--tco", type=float, default=0.0, help="本地模型 TCO/次（USD）")
    models_add_parser.add_argument("--tags", default="", help="能力标签（逗号分隔，如 plan_strong,code_strong）")

    # pr 子命令
    pr_parser = subparsers.add_parser("pr", help="Generate and create PR")
    pr_parser.add_argument("task_id", help="Task ID to create PR from")
    pr_parser.add_argument("--offline", action="store_true", help="Only generate PR.md, do not create PR")
    pr_parser.add_argument("--push", action="store_true", help="Push branch to remote before creating PR")
    pr_parser.add_argument("--remote", default="origin", help="Remote name to push to (default: origin)")

    # merge 子命令（M1.2 显式交付命令）
    merge_parser = subparsers.add_parser("merge", help="Merge delivery branch into target branch (manual delivery)")
    merge_parser.add_argument("task_id", help="Task ID whose delivery branch to merge")
    merge_parser.add_argument("--push", action="store_true", help="Push target branch to remote after merge")
    merge_parser.add_argument("--remote", default="origin", help="Remote name to push to (default: origin)")

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

    migrate_parser = subparsers.add_parser("migrate", help="迁移历史任务元数据")
    migrate_parser.add_argument("subcommand", choices=["failure-metadata"])
    migrate_parser.add_argument("--apply", action="store_true", help="实际写入迁移结果（默认 dry-run）")
    migrate_parser.add_argument("--backup-dir", default="", help="写入 meta.json 备份的目录")

    # eval 子命令
    eval_parser = subparsers.add_parser("eval", help="Quality/performance/cost evaluation")
    eval_parser.add_argument("subcommand", choices=["quality", "perf", "cost", "reliability", "ux", "gate", "bench", "baseline", "cost-baseline", "models", "recommend", "judge", "validate-schema", "metric-freeze", "batch-manifest", "calibrate-difficulty", "insight", "all"],
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
    eval_parser.add_argument("--suite", dest="bench_suite", default="",
                             choices=["smoke", "core", "decision", "stress", "golden", "phaseD"],
                             help="Bench 案例套件（golden=阶段C Golden Tasks 固定 6 任务；默认运行全部 canonical 任务）")
    eval_parser.add_argument("--bench-parallel", dest="bench_parallel", type=int, default=2,
                             help="bench 并发度：同时运行的 (任务×模型×重复) 组合数（默认 2，受 API rate-limit 与本地资源约束）")
    eval_parser.add_argument("--hard-model", dest="hard_model", default="",
                             help="CR-建议#5：hard 难度子任务使用的更强模型（如 deepseek-v4-pro）；留空 = 与候选模型相同")
    eval_parser.add_argument("--with-delivery", dest="with_delivery", action="store_true",
                             help="bench 子命令：任务成功后做本地交付 merge，闭合 accepted_delivery 判定（不推进 target 引用，保持 fixture repeat 可复现）")
    eval_parser.add_argument("--with-knowledge", dest="with_knowledge", action="store_true",
                             help="bench 子命令：C4 KnowledgeStore A/B 注入臂——修复重试时注入跨任务历史经验（对照臂不加此 flag）")
    eval_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                             help="跳过 bench 预检等交互确认（headless/后台运行；bench.py _preflight_model_pricing 读取）")
    eval_parser.add_argument("--results", dest="results", default="eval_suite/results.jsonl",
                             help="读取结果文件（models/cost-baseline/recommend 子命令，逗号分隔多个文件）")
    eval_parser.add_argument("--tolerance", dest="tolerance", type=float, default=1.5,
                             help="成本基线预算 = P90 × tolerance（cost-baseline 子命令，默认 1.5）")
    eval_parser.add_argument("--report-output", dest="report_output", default="",
                             help="Metric Freeze 报告输出路径（metric-freeze 子命令）")
    eval_parser.add_argument("--analysis-goal", dest="analysis_goal", default="",
                             help="insight 子命令：分析目标（人类可读，如 'hard 通过率>=95%% 且 $/pass<=$0.1'）")
    eval_parser.add_argument("--analysis-plan", dest="analysis_plan", default="",
                             help="insight 子命令：预设计划/行动候选（可省略）")
    eval_parser.add_argument("--catalog", dest="catalog", default="",
                             help="任务 catalog 路径（metric-freeze 子命令）")
    eval_parser.add_argument("--config-file", dest="config_file", default="",
                             help="配置文件路径，用于计算 config hash（metric-freeze 子命令）")
    eval_parser.add_argument("--manifest-output", dest="manifest_output", default="",
                             help="批次 manifest 输出路径（batch-manifest 子命令）")
    eval_parser.add_argument("--proxy-context", dest="proxy_context", default="",
                             help="批次口径快照 sidecar 路径（batch-manifest 子命令，缺省自动拾取 {results}.proxy_context.json）")
    # recommend 子命令参数（CR-G5：bench 推荐写回 worker_models）
    eval_parser.add_argument("--apply", dest="apply", action="store_true",
                             help="recommend 子命令：把推荐写入 config.json 的 worker_models（默认 dry-run）")
    eval_parser.add_argument("--force", dest="force", action="store_true",
                             help="recommend --apply 时：tier 错配仍强制写入（默认 tier 错配拒绝写入）")
    eval_parser.add_argument("--llm", dest="llm", action="store_true",
                             help="recommend 子命令：规则初筛 + LLM 精排（M6.4，--results 关联证据）")
    eval_parser.add_argument("--apply-suggestion", dest="apply_index", default=None, metavar="N",
                             help="insight 子命令：应用第 N 条建议（M6.5 确认后自动应用，含备份+审计）")
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

    # governance 子命令（M1.4 SDD 治理闭环：traceability + architecture compliance）
    governance_parser = subparsers.add_parser("governance", help="Show traceability matrix & architecture compliance")
    governance_parser.add_argument("task_id", help="Task ID")
    governance_parser.add_argument("--json", action="store_true", dest="json_mode",
                                   help="Output as JSON")

    # deviation 子命令（M2.5 Spec/Architecture 偏差反馈：偏差记录查询与聚合）
    deviation_parser = subparsers.add_parser("deviation", help="Show Spec/Architecture/acceptance deviation records")
    deviation_parser.add_argument("task_id", nargs="?", default=None,
                                  help="Task ID（缺省时聚合全部任务）")
    deviation_parser.add_argument("--json", action="store_true", dest="json_mode",
                                  help="Output as JSON")
    decision_parser = subparsers.add_parser("decision", help="决策记录（decision log）：查看/审计关键配置与模型决策")
    decision_sub = decision_parser.add_subparsers(dest="decision_subcommand", help="Decision operation")
    decision_sub.add_parser("log", help="列出决策记录（最新在前）")


    # problems 子命令（M5 收尾：全局 Problem 实体查询——「越用越聪明」的查看入口）
    problems_parser = subparsers.add_parser("problems", help="Show global Problem records (cross-task failures, B4/H3)")
    problems_parser.add_argument("--aggregate", action="store_true",
                                 help="Show aggregate analysis (status/recurrence/top patterns)")
    problems_parser.add_argument("--only", default="", metavar="PROBLEM_ID",
                                 help="Show single Problem detail (history/lifecycle)")
    problems_parser.add_argument("--json", action="store_true", dest="json_mode",
                                 help="Output as JSON")

    # trust 子命令（#49 信任指标：阶段 D 自治决策放行门的查看入口）
    trust_parser = subparsers.add_parser("trust", help="Show trust metrics (review-modification / recurrence-visibility / blind-spot-hit rates)")
    trust_parser.add_argument("--json", action="store_true", dest="json_mode",
                              help="Output as JSON")
    trust_parser.add_argument("--all", action="store_true", dest="include_bench",
                              help="包含 bench/fixture 任务（默认只统计真实任务）")
    trust_parser.add_argument("--window", type=int, default=30, dest="recent_window",
                              help="观察窗口：最近 N 个任务（D-0 口径，默认 30；0=不限）")

    # kanban 子命令（看板任务编排）
    kanban_parser = subparsers.add_parser("kanban", help="看板任务编排（卡片管理/Spec 导入）")
    kanban_sub = kanban_parser.add_subparsers(dest="kanban_subcommand", help="Kanban operation")
    kanban_import_parser = kanban_sub.add_parser("import-spec", help="从 Task Spec 需求文档生成看板卡片")
    kanban_import_parser.add_argument("spec_path", help="Task Spec 文件路径（.md）")
    kanban_import_parser.add_argument("--stage", default="brainstorm",
                                      help="创建后的看板列（默认 brainstorm；合法: brainstorm/requirements/design/implementation/operations）")
    kanban_import_parser.add_argument("--repo", default="",
                                      help="目标仓库路径（implementation 卡片必填）")
    kanban_import_parser.add_argument("--type", default="implementation", choices=["discussion", "implementation", "periodic"],
                                      help="卡片类型（默认 implementation）")

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
    web_parser.add_argument("--token", default=None, help="可选 admin Bearer token 鉴权（全部操作，默认关闭）")
    web_parser.add_argument("--viewer-token", default=None,
                            help="可选 viewer Bearer token（只读 GET；写操作 403）")

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
    recommend_parser = router_sub.add_parser("recommend", help="基于 bench 结果推荐角色路由配置（P1）")
    recommend_parser.add_argument("--results", default="eval_suite/results.jsonl",
                                  help="结果文件（同 eval recommend）")
    recommend_parser.add_argument("--apply", action="store_true",
                                  help="把推荐写入 config.json 的 router.roles（默认 dry-run）")
    recommend_parser.add_argument("--force", action="store_true",
                                  help="apply 时：低置信（n<5）角色仍写入（默认跳过）")

    return parser


def _build_spec_context(spec_obj) -> tuple[str, list[str], list[str]]:
    """把 TaskSpec 的范围/约束/验收/风险组装成注入 Plan prompt 的结构化约束文本。

    返回 (context, req_ids, ac_ids)：
      §3 范围 / §4 约束 / §5 验收 / §7 风险 → system prompt 硬约束
      （§1 目标已在 cmd_run 中替代 task；§2 动机 / §6 参考已注入 user content）
      稳定 ID（REQ/AC）→ 从 §1 目标 + §5 验收提取，供 planner 写进 requirement_ids /
      acceptance_criteria_ids 字段；同时返回给 cmd_run 持久化进 meta（traceability 的
      spec 侧输入——缺了这步 assess_traceability 永远 no_spec_ids）。
    """
    parts = []
    req_ids: list[str] = []
    ac_ids: list[str] = []
    if spec_obj.scope:
        parts.append(f"【范围（必须遵守）】\n{spec_obj.scope.strip()}")
    if spec_obj.constraint:
        parts.append(f"【设计约束（必须遵守）】\n{spec_obj.constraint.strip()}")
    if spec_obj.acceptance:
        parts.append(f"【验收标准（verification 命令应覆盖这些）】\n{spec_obj.acceptance.strip()}")
    # 稳定 ID 提取（fail-open：提取失败不影响 plan 生成，仅丢失追踪能力）
    try:
        from .governance import extract_spec_requirements
        reqs = extract_spec_requirements(f"{spec_obj.goal}\n{spec_obj.acceptance}")
        req_ids = reqs.get("requirement_ids") or []
        ac_ids = reqs.get("acceptance_criteria_ids") or []
        if req_ids or ac_ids:
            id_lines = []
            if req_ids:
                id_lines.append("需求 ID: " + ", ".join(req_ids))
            if ac_ids:
                id_lines.append("验收 ID: " + ", ".join(ac_ids))
            id_lines.append("每个 step 必须在 requirement_ids / acceptance_criteria_ids 字段引用对应的 ID")
            parts.append("【稳定 ID（必须写进对应 step 字段）】\n" + "\n".join(id_lines))
    except Exception:
        pass
    if spec_obj.risk:
        parts.append(f"【已知风险（在 steps[].risks 和 difficulty 中体现）】\n{spec_obj.risk.strip()}")
    return "\n\n".join(parts), req_ids, ac_ids


def _apply_spec_id_hard_mapping(
    subtasks: list[dict],
    spec_obj,
    spec_req_ids: list[str],
    spec_ac_ids: list[str],
    logger: logging.Logger,
) -> None:
    """硬映射兜底（spec 闭环 §4.2）：planner 软映射不可靠（冒烟实证 deepseek planner
    对 AC ID 全部漏标），用「AC 验证命令 ⊆ step.verification」确定性回填。
    正常拆分路径与 e2e 路径共用；失败不阻断（仅丢追踪能力）。"""
    if not spec_obj or not (spec_ac_ids or spec_req_ids):
        return
    try:
        from .spec import map_acceptance_to_steps
        _unmapped_ac = map_acceptance_to_steps(spec_obj.acceptance, subtasks)
        if spec_req_ids:
            for st in subtasks:
                _rids = st.setdefault("requirement_ids", [])
                for rid in spec_req_ids:
                    if rid not in _rids:
                        _rids.append(rid)
        if _unmapped_ac:
            logger.warning(f"[spec] 硬映射未匹配的 AC（无法确定性归属 step）: {_unmapped_ac}")
        else:
            logger.info(f"[spec] 硬映射完成: REQ={spec_req_ids} AC={spec_ac_ids}")
    except Exception as _hme:
        logger.debug(f"[spec] 硬映射失败（非关键）: {_hme}")


def _preflight_repair_plan(
    plan: dict,
    *,
    task: str,
    repo: Path,
    config: dict,
    logger: logging.Logger,
    task_dir: Path,
    skill_plan_context: str,
    spec_context: str,
    initial_docs: str,
    iteration: int,
) -> tuple[dict, int, list[dict], dict]:
    """Run one bounded Plan preflight repair loop before user confirmation.

    The worker never sees a Plan with a repairable deterministic defect. A
    second failed validation is returned to the normal Plan blocking path.
    """
    from .planning import build_plan_repair_feedback, validate_plan_quality

    behavior = config.get("behavior", {})
    if not behavior.get("plan_preflight_repair_enabled", True):
        return plan, iteration, [], {}

    max_repairs = max(0, int(behavior.get("max_plan_repairs", 1)))
    repair_history: list[dict] = []
    current = plan
    current_iteration = iteration

    for repair_index in range(max_repairs + 1):
        # Plan steps already contain the fields consumed by the deterministic
        # quality checks; avoid materializing skills/subtasks before approval.
        probe_subtasks = current.get("steps") or []
        requirements = current.get("acceptance_criteria_ids") or current.get("requirements") or []
        quality = validate_plan_quality(probe_subtasks, requirements, repo=repo)
        repairable = quality.get("repairable_issues", [])
        if not repairable:
            return current, current_iteration, repair_history, quality
        if repair_index >= max_repairs:
            logger.warning(
                "[plan_preflight] 修订次数已用尽: %s 个可修复问题仍存在",
                len(repairable),
            )
            return current, current_iteration, repair_history, quality

        feedback = build_plan_repair_feedback(quality)
        previous_iteration = current_iteration
        _save_plan_snapshot(task_dir, current, previous_iteration)
        logger.warning(
            "[plan_preflight] Plan v%s 有 %s 个确定性问题，启动第 %s 次修订",
            previous_iteration,
            len(repairable),
            repair_index + 1,
        )
        try:
            current = generate_plan(
                task,
                repo,
                config,
                logger,
                feedback,
                initial_docs,
                current_iteration + 1,
                skill_plan_context,
                no_cache=True,
                spec_context=spec_context,
            )
            current["_original_task"] = task
            current_iteration += 1
            repair_history.append({
                "from_iteration": previous_iteration,
                "to_iteration": current_iteration,
                "issue_types": sorted({str(i.get("type", "")) for i in repairable}),
                "issue_count": len(repairable),
            })
        except Exception as exc:
            logger.warning("[plan_preflight] Plan 修订失败，保留当前版本: %s", exc)
            repair_history.append({
                "from_iteration": previous_iteration,
                "to_iteration": None,
                "issue_types": sorted({str(i.get("type", "")) for i in repairable}),
                "issue_count": len(repairable),
                "error": str(exc)[:300],
            })
            return current, current_iteration, repair_history, quality

    return current, current_iteration, repair_history, quality


def _confirm_plan_channel(plan, config, repo, logger, iteration, task, plan_dir):
    """Plan 确认通道分发（R5b）：web_confirm_plan 时走 web 文件协议，否则 CLI 交互。

    与 confirm_plan 同返回契约：(plan, doc_paths) 确认 / (None, doc_paths) 重新生成 /
    N 决策时 sys.exit(0)（与 CLI 的 N 一致）。
    """
    if config.get("behavior", {}).get("web_confirm_plan") and plan_dir:
        from .web_confirm import web_confirm
        decision = web_confirm("plan", plan, plan_dir, logger)
        if decision == "Y":
            logger.info("web 确认 Plan：Y")
            return plan, []
        if decision == "R":
            logger.info("web 确认 Plan：R（重新生成）")
            return None, []
        console.force("❌ 已取消（web 确认或超时）")
        sys.exit(0)
    return confirm_plan(plan, config, repo, logger, iteration=iteration, task=task, plan_dir=plan_dir)


def _confirm_subtasks_channel(subtasks, config, logger, task_dir=None):
    """子任务确认通道分发（R5b）：web_confirm_subtasks 时走 web 文件协议。"""
    if config.get("behavior", {}).get("web_confirm_subtasks") and task_dir:
        from .web_confirm import web_confirm
        decision = web_confirm("subtasks", {"subtasks": subtasks}, task_dir, logger)
        if decision == "Y":
            logger.info("web 确认子任务：Y")
            return subtasks
        console.force("❌ 已取消（web 确认或超时）")
        sys.exit(0)
    return confirm_subtasks(subtasks, config, logger)


# 架构级任务特征信号（L2 判定：需全局视野，拆分易失败）
_E2E_ARCH_SIGNALS = (
    "refactor", "重构", "并发", "race condition", "race", "架构", "architecture",
    "端到端", "end-to-end", "e2e", "performance", "性能优化", "跨文件", "cross-file",
    "atomic", "原子写", "并发安全", "thread-safe", "threading", "multi-process",
)


def _should_e2e(task_text: str, config: dict, args) -> tuple[bool, str]:
    """判定端到端模式（hard 任务不拆分，保留全局上下文）。

    判定优先级（"拆分 vs 端到端"框架）：
      L0 显式 flag：--e2e 强制端到端 / --split 强制拆分（覆盖一切）
      L1 显式输入难度：config.min_difficulty=hard → 端到端；easy/medium → 拆分
      L2 任务特征：含架构级信号（refactor/并发/race/架构/端到端/性能/跨文件…）→ 端到端
      L3 默认：拆分（medium 及以下已验证有效）
    返回 (是否 e2e, 判定理由)。
    """
    if getattr(args, "e2e", False):
        return True, "--e2e flag"
    if getattr(args, "split", False):
        return False, "--split flag"
    diff = str(config.get("min_difficulty", "") or "")
    if diff == "hard":
        return True, "min_difficulty=hard"
    if diff in ("easy", "medium"):
        return False, f"min_difficulty={diff}"
    if config.get("e2e_hard"):
        return True, "config.e2e_hard"
    text = (task_text or "").lower()
    for sig in _E2E_ARCH_SIGNALS:
        if sig in text:
            return True, f"架构级特征信号: {sig}"
    return False, "默认拆分"


def _build_e2e_subtask(task_text: str, config: dict) -> dict:
    """构造单个端到端子任务：完整原始任务 + 全局视野（hard 端到端模式）。

    与 plan_to_subtasks 产物字段兼容（run_subtask 直接消费）。verification 支持
    字符串或数组（bench 任务级 verification 数组，run_subtask 逐条执行）。
    difficulty=hard → worker_models.hard 路由（opus-4-7→云端强模型）。
    """
    verification = config.get("task_verification", []) or []
    title = (task_text or "").strip().split("\n")[0][:60] or "端到端任务"
    desc = (
        task_text.rstrip()
        + "\n\n【端到端模式】\n这是一个需要全局视野的 hard 任务（不拆分子任务）。"
          "请自主探索代码结构、理解整体架构后，端到端完成全部要求；"
          "修改后运行验证命令确保全部通过（失败则分析根因、修复后重新验证）。"
    )
    return {
        "id": "sub-e2e",
        "title": title,
        "description": desc,
        "files_hint": "*",
        "agent_prompt": desc,
        "verification": verification,
        "risks": [],
        "depends_on": [],
        "skills": [],
        "agent_type": "developer",
        "difficulty": "hard",
        "task_type": None,
        "cognitive_mode": "implement",
        "allowed_tools": [],
        "permission_mode": "",
        "rationale": "hard 端到端模式（保留全局上下文）",
        "scope_boundary": "",
        "do_not_touch": [],
        "requirement_ids": [],
        "acceptance_criteria_ids": [],
        "_agent_type_source": "llm",
    }


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
    dry_run = getattr(args, "dry_run", False)
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
        sys.exit(EX_USAGE)

    # --auto-init：目标目录非 git 仓库时自动 init + 首次 commit，
    # 保证 worktree / commit / tag / merge 机制可用
    if getattr(args, "auto_init", False) and not (repo / ".git").exists():
        console.warning(f"{repo} 不是 git 仓库，自动初始化 (--auto-init)")
        ok, err = init_git_repo(repo)
        if not ok:
            console.error(f"git init 失败: {err}")
            sys.exit(EX_SYSTEM)
        console.print("✓ git 初始化完成（本地，无 remote）")

    # ── A3 未提交基线处理 ──
    # worktree 从 HEAD 创建，看不到主工作区未提交改动。启动时检测 dirty：
    #   --allow-dirty  → 显式允许，记录风险继续
    #   --baseline     → 运行前显式 commit 为基线
    #   headless/--yes → 默认 fail-safe 中止（防静默基于错误基线跑完 pipeline）
    #   交互式         → 提示 ① commit 基线 ② 继续 ③ 中止
    baseline_dirty = False
    baseline_action = "clean"  # clean | committed | allowed
    baseline_dirty_files: list[str] = []
    if (repo / ".git").exists():
        baseline_dirty_files = get_dirty_files(repo)
        if baseline_dirty_files:
            allow_dirty = getattr(args, "allow_dirty", False)
            want_baseline = getattr(args, "baseline", False)
            preview = ", ".join(baseline_dirty_files[:5]) + (" ..." if len(baseline_dirty_files) > 5 else "")
            baseline_dirty = True
            if allow_dirty:
                baseline_action = "allowed"
                console.warning(f"⚠️ 主工作区有 {len(baseline_dirty_files)} 个未提交改动（--allow-dirty）：{preview}")
                console.warning("   子任务将基于 HEAD，看不到这些改动；合并时可能与之冲突。")
            elif want_baseline:
                ok, new_hash, err = commit_baseline(repo)
                if not ok:
                    console.error(f"基线 commit 失败: {err}")
                    sys.exit(EX_ERROR)
                baseline_action = "committed"
                console.print(f"✓ 已把 {len(baseline_dirty_files)} 个未提交改动 commit 为基线 ({new_hash[:7]})：{preview}")
            elif headless:
                console.error(f"❌ 主工作区有 {len(baseline_dirty_files)} 个未提交改动：{preview}")
                console.error("   worktree 从 HEAD 创建，子任务看不到这些改动，可能基于错误基线执行。")
                console.error("   headless 模式默认 fail-safe 中止。处理方式：")
                console.error("     --baseline    先 commit 为基线再运行")
                console.error("     --allow-dirty 明确接受风险继续")
                sys.exit(EX_ERROR)
            else:
                console.warning(f"⚠️ 主工作区有 {len(baseline_dirty_files)} 个未提交改动：{preview}")
                console.print("   worktree 从 HEAD 创建，子任务看不到这些改动，可能基于错误基线执行。")
                choice = safe_input("处理方式：[B] 先 commit 为基线 / [C] 继续（风险自负）/ [N] 中止 [B]: ").strip().upper() or "B"
                c = choice
                if c in ("B", "BASELINE", "1"):
                    ok, new_hash, err = commit_baseline(repo)
                    if not ok:
                        console.error(f"基线 commit 失败: {err}")
                        sys.exit(EX_ERROR)
                    baseline_action = "committed"
                    console.print(f"✓ 已 commit 为基线 ({new_hash[:7]})")
                elif c in ("C", "CONTINUE", "2"):
                    baseline_action = "allowed"
                    console.warning("已选择继续，子任务基于 HEAD（不含未提交改动）。")
                else:
                    console.print("已中止。请先提交或暂存改动后重试。")
                    sys.exit(EX_OK)

    config = load_config(config_path=getattr(args, "config", None))
    # A3 未提交基线：记录启动时主工作区 dirty 状态与处理方式（供 meta.json 持久化）
    config["_baseline_dirty"] = baseline_dirty
    config["_baseline_action"] = baseline_action
    config["_baseline_dirty_files"] = baseline_dirty_files
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
    # Goal Policy：--goal-mode 归一化用户覆盖（--goal=force、--no-goal=off、--goal-hook=hook）
    _goal_mode_flag = getattr(args, "goal_mode", None)
    if getattr(args, "goal", False) and not _goal_mode_flag:
        _goal_mode_flag = "force"
    if no_goal and not _goal_mode_flag:
        _goal_mode_flag = "off"
    if getattr(args, "goal_hook", False) and not _goal_mode_flag:
        _goal_mode_flag = "hook"
    if getattr(args, "agent_loop", False):
        config.setdefault("agent_loop", {})["enabled"] = True
    if getattr(args, "artifact_dir", None):
        config["artifact_dir"] = args.artifact_dir

    if auto_yes:
        config["behavior"]["auto_confirm_plan"] = True
        config["behavior"]["auto_confirm_subtasks"] = True
        config["behavior"]["auto_verify_subtask"] = True

    # R5b：--confirm-mode web → Plan/子任务确认走 web 文件协议（覆盖 --yes 的自动确认；
    # 子任务验证仍自动，避免 worker 卡 input()）
    if getattr(args, "confirm_mode", "auto") == "web":
        config["behavior"]["auto_confirm_plan"] = False
        config["behavior"]["auto_confirm_subtasks"] = False
        config["behavior"]["auto_verify_subtask"] = True
        config["behavior"]["web_confirm_plan"] = True
        config["behavior"]["web_confirm_subtasks"] = True

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
        logging.getLogger(__name__).info(f"[cost_control] --budget ${_budget_flag} 已启用 L3 任务级熔断 (mode={_cc.get('budget_mode', 'strict')})")
    elif _budget_mode_flag:
        _cc = dict(config.get("cost_control") or {})
        _cc["budget_mode"] = _budget_mode_flag
        config["cost_control"] = _cc
        logging.getLogger(__name__).info(f"[cost_control] --budget-mode {_budget_mode_flag}（未指定预算，仅设策略）")

    logger = setup_logger(task_id, task_dir)
    logger.info("=" * 60)
    logger.info("任务启动")
    logger.info(f"ID: {task_id}, 任务: {task}, 项目: {repo}")
    if doc_paths:
        logger.info(f"参考文档: {doc_paths}")

    tool_versions = _detect_tool_versions(logger)
    if tool_versions:
        logger.info(f"工具版本: {tool_versions}")

    # CR-G2：worker_models × MODEL_TIER 错配 advisory 校验（启动时提醒配置失误，
    # 如 hard 槽填 lite / easy 槽填 frontier）。不阻断运行。
    try:
        from .pricing import validate_worker_tier
        for _slot, _mdl, _tier, _msg in validate_worker_tier(config.get("worker_models") or {}):
            logger.warning(f"[tier] {_msg}: worker_models.{_slot}={_mdl}（{_tier}）")
    except Exception as _te:
        logger.debug(f"[tier] 校验失败（忽略）: {_te}")

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
    spec_snapshot = ""
    spec_do_not_touch: list[str] = []
    spec_req_ids: list[str] = []
    spec_ac_ids: list[str] = []
    if spec_path is not None:
        if not spec_path.exists():
            console.error(f"Spec 文件不存在: {spec_path}")
            sys.exit(EX_USAGE)
        spec_obj = parse_spec(spec_path)
        if spec_obj is None:
            console.error(f"Spec 解析失败: {spec_path}")
            sys.exit(EX_USAGE)
        # A4 spec 快照：拷贝 SPEC.md 到 task_dir，保证任务可复现（SpecSource 演化不影响历史任务）
        try:
            _snap = task_dir / "spec_snapshot.md"
            _snap.write_text(spec_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            spec_snapshot = "spec_snapshot.md"
        except OSError as _se:
            logger.warning(f"[spec] 快照保存失败（非关键）: {_se}")
        console.print(f"📋 Task Spec: {spec_obj.title or spec_path.name}")
        # --yes 模式仍跑 L1（确定性检查，0 误判，不跳过）；--force 全跳过
        if not force_spec:
            console.print("🔍 L1 准入审查中...")
            violations = validate_spec_l1(spec_obj, repo)
            errors = [v for v in violations if v.severity != "warning"]
            warnings_ = [v for v in violations if v.severity == "warning"]
            if warnings_:
                console.print(f"⚠️ L1 软警告（{len(warnings_)} 项，不阻断）：")
                for i, v in enumerate(warnings_, 1):
                    sec = f" §{v.section}" if v.section else ""
                    console.print(f"  {i}. [{v.check}{sec}] {v.message}")
                    if v.suggestion:
                        console.print(f"     💡 {v.suggestion}")
            if errors:
                console.error(f"❌ L1 准入审查未通过（{len(errors)} 项违规）：")
                for i, v in enumerate(errors, 1):
                    sec = f" §{v.section}" if v.section else ""
                    console.error(f"  {i}. [{v.check}{sec}] {v.message}")
                    if v.suggestion:
                        console.error(f"     💡 {v.suggestion}")
                console.error("\n修正 Spec 后重试，或用 --force 跳过审查（不推荐）。")
                sys.exit(EX_ERROR)
            console.print("✅ L1 准入审查通过")
        else:
            console.print("⚠️ --force 已跳过 Spec 准入审查")
        logger.info(f"Task Spec 加载: {spec_path}（完整={spec_obj.is_complete}, force={force_spec}）")
        # Spec §1 目标作为任务描述的增强（若 Spec 完整，目标替代一句话 task 的模糊性）
        if spec_obj.goal:
            task = spec_obj.goal.strip()
        # CR-TD：Spec `budget:` 字段 → 任务级 L3 预算（覆盖 config 默认；CLI --budget 优先，不覆盖）
        if spec_obj.budget:
            _cc = dict(config.get("cost_control") or {})
            if not _cc.get("max_budget_usd"):
                _cc["max_budget_usd"] = spec_obj.budget
                _cc["enabled"] = True  # 显式给了预算 → 开启 L3 熔断
                config["cost_control"] = _cc
                logger.info(f"[cost_control] Spec budget=${spec_obj.budget} 已设任务级 L3 预算")
        # 结构化约束注入（由 generate_plan 的 spec_context 参数消费）
        spec_context, spec_req_ids, spec_ac_ids = _build_spec_context(spec_obj)
        # 后段注入（spec 闭环）：§5 验收 + §3 范围 存入 runtime config，供 _build_task_md 注入 TASK.md
        if spec_obj.acceptance:
            config["_spec_acceptance"] = spec_obj.acceptance.strip()
        if spec_obj.scope:
            config["_spec_scope"] = spec_obj.scope.strip()
        # §4/§3 架构硬约束：提取「明确不动的区域」文件，供 plan 预检做确定性 fail-close
        spec_do_not_touch = extract_do_not_touch(spec_obj.scope)
        if spec_do_not_touch:
            logger.info(f"[spec] do-not-touch 硬约束: {', '.join(spec_do_not_touch[:8])}{'...' if len(spec_do_not_touch) > 8 else ''}")

    # Plan Mode
    console.print("\n🤖 进入 Plan Mode...")
    initial_docs = read_reference_docs(doc_paths, repo, logger) if doc_paths else ""
    # Spec §6 参考资料并入 initial_docs
    if spec_obj and spec_obj.reference:
        initial_docs = (initial_docs + "\n\n" if initial_docs else "") + f"===== Task Spec §6 参考资料 =====\n{spec_obj.reference}\n===== 结束 ====="

    plan = None
    confirmed_plan = None

    # ── hard 端到端模式（e2e）：跳过 Plan 拆分，单子任务保留全局上下文 ──
    # 依据"拆分 vs 端到端"判定框架：hard / 架构级 / 强耦合任务拆分时代价
    # （上下文丢失）超过收益，对照实验证实端到端 v4-pro 可完成而拆分失败。
    _e2e, _e2e_reason = _should_e2e(task, config, args)
    if _e2e:
        console.print(f"\n🎯 hard 端到端模式（不拆分）: {_e2e_reason}")
        logger.info(f"[e2e] 端到端模式触发: {_e2e_reason}")
        subtasks = [_build_e2e_subtask(task, config)]
        confirmed = _confirm_subtasks_channel(subtasks, config, logger, task_dir=task_dir)
        # 硬映射兜底（e2e 路径同样需要 ID 回填——冒烟实证 docs 任务被误判 e2e 后 ID 全空）
        _apply_spec_id_hard_mapping(confirmed, spec_obj, spec_req_ids, spec_ac_ids, logger)
        # confirmed_plan 保持 None（无 Plan），直接跳过后续 plan 生成/拆解，
        # 复用下方 plan_quality / pipeline 流程
    iteration = 1
    last_error = None
    preflight_repair_history: list[dict] = []

    if not _e2e:
        max_iter = config.get("behavior", {}).get("max_plan_iterations", 5)
        for attempt in range(3):
            try:
                plan = generate_plan(task, repo, config, logger, "", initial_docs, iteration, skill_plan_context, no_cache=no_cache, spec_context=spec_context)
                plan["_original_task"] = task
                break
            except Exception as e:
                last_error = e
                logger.error(f"Plan 失败 (尝试 {attempt+1}): {e}")

        if plan is not None:
            # API 成功 → 执行前 Plan 预检。确定性问题最多自动修订一次，
            # 修订后的 Plan 仍需经过 confirm_plan；未解决问题在最终门禁阻断。
            try:
                plan, iteration, preflight_repair_history, _preflight_quality = _preflight_repair_plan(
                    plan,
                    task=task,
                    repo=repo,
                    config=config,
                    logger=logger,
                    task_dir=task_dir,
                    skill_plan_context=skill_plan_context,
                    spec_context=spec_context,
                    initial_docs=initial_docs,
                    iteration=iteration,
                )
            except Exception as _pe:
                # 预检本身不是外部增强依赖；异常时保留原 Plan，最终质量门继续兜底。
                logger.warning(f"[plan_preflight] 预检失败，保留原 Plan: {_pe}")
            # API 成功 → Plan 确认流程
            # --yes must remain non-interactive even when preflight produced Plan v2;
            # the repair version is still shown/persisted separately in plan snapshots.
            _confirm_iteration = 1 if auto_yes else iteration
            confirmed_plan, final_doc_paths = _confirm_plan_channel(
                plan, config, repo, logger, iteration=_confirm_iteration, task=task, plan_dir=task_dir)
            # 检查降级信号
            if confirmed_plan == "__FALLBACK__":
                console.print("\n⚠️ 降级到本地规则拆解...")
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
                    confirmed_plan, final_doc_paths = _confirm_plan_channel(plan, config, repo, logger, iteration, task=task, plan_dir=task_dir)
                    if confirmed_plan == "__FALLBACK__":
                        console.print("\n⚠️ 降级到本地规则拆解...")
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
                                sys.exit(EX_ERROR)
                except Exception as _e:
                    # 冲突检测是辅助功能，失败不阻断主流程
                    logger.warning(f"L1.5 冲突检测失败（跳过）: {_e}")
                subtasks = plan_to_subtasks(
                    confirmed_plan, logger, repo=repo,
                    default_skills=[s.name for s in skills] if skills else None,
                    disable_rule_skills=not config.get("skills", {}).get("auto_discover", False),
                    task_type_override=(spec_obj.task_type if spec_obj else None),
                    min_difficulty=config.get("min_difficulty", ""))
                # 硬映射兜底（spec 闭环 §4.2）：planner 软映射不可靠（冒烟实证 deepseek
                # planner 对 AC ID 全部漏标），用「AC 验证命令 ⊆ step.verification」确定性回填。
                _apply_spec_id_hard_mapping(subtasks, spec_obj, spec_req_ids, spec_ac_ids, logger)
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
                # CR-G4：planner 主观难度交叉核对——planner 标的 difficulty 与启发式 hint
                # 跨两档不一致（如 easy 标注实为跨文件重构）时告警，提醒可能用错档模型。
                try:
                    from .planning import check_difficulty_mismatch
                    check_difficulty_mismatch(subtasks, logger)
                except Exception as _de:
                    logger.debug(f"[G4] 难度交叉核对失败（忽略）: {_de}")
            elif 'subtasks' in locals() and subtasks is not None:
                # 降级路径中已通过 decompose_fallback 生成 subtasks，无需重复调用
                pass
            else:
                # 降级拆解
                console.print(f"\n⚠️ Plan Mode 失败: {last_error}")
                subtasks = decompose_fallback(task, repo, config, logger)

        else:
            # plan is None：3 次 generate_plan 全部失败，降级到本地规则拆解。
            # 不加此分支会导致 subtasks 未定义 → confirm_subtasks 抛 UnboundLocalError。
            # decompose_fallback 有三级降级（本地模型→规则→单任务），永不抛异常。
            console.print(f"\n⚠️ Plan 生成 3 次均失败: {last_error}")
            console.print("⚠️ 降级到本地规则拆解...")
            subtasks = decompose_fallback(task, repo, config, logger)

        # 子任务确认
        confirmed = _confirm_subtasks_channel(subtasks, config, logger, task_dir=task_dir)
    from .planning import validate_plan_quality
    _plan_requirements = []
    if isinstance(confirmed_plan, dict):
        _plan_requirements = confirmed_plan.get("acceptance_criteria_ids") or confirmed_plan.get("requirements") or []
    # 传 repo 启用 L1.5 符号级冲突判定（ISSUE-45：与 preflight 修复循环同口径，
    # 符号级并行冲突在两道门都阻断）；P2 函数引用/import 关系检查同步生效（warning 级）。
    plan_quality = validate_plan_quality(confirmed, _plan_requirements, repo=repo, do_not_touch=spec_do_not_touch)

    # Goal Policy Resolver（goal-mechanism-design.md §3.3/§4）：
    # 用户覆盖 > config.goal.policy > 系统确定性策略 > 默认 off。
    # 决议应用到运行时 config，并写入 meta 供审计。
    try:
        from .goal_policy import resolve_goal_policy
        goal_policy = resolve_goal_policy(
            _goal_mode_flag,
            config_policy=(config.get("goal") or {}).get("policy"),
            subtasks=confirmed,
            headless=headless,
        )
        config.setdefault("goal", {})["enabled"] = goal_policy["enabled"]
        config["goal"]["enable_goal_hook"] = goal_policy["enable_hook"]
    except Exception as _gpe:
        logger.debug(f"[goal_policy] 解析失败（忽略，保留默认关闭）: {_gpe}")
        goal_policy = {"mode": "off", "enabled": False, "enable_hook": False,
                       "reason_codes": ["resolver_error"], "backend": "internal"}

    # M1.5 架构审查：执行前生成最小 Architecture Decision（fail-open，默认关闭）。
    # 结果写入 meta.architecture_review，供 traceability / architecture_compliance 消费。
    architecture_review = None
    try:
        from .governance import architecture_review as _arch_review
        architecture_review = _arch_review(task, confirmed, config, logger)
    except Exception as _ae:
        logger.debug(f"[governance] 架构审查接入失败（忽略）: {_ae}")

    meta = {
        "task_id": task_id, "task": task, "repo": str(repo),
        "created": ts, "status": "EXECUTING",
        "status_schema_version": 1,
        "reference_docs": doc_paths, "issue": issue_ref,
        "subtasks": confirmed, "results": [],
        "tool_versions": tool_versions,
        "skills": [s.name for s in skills],
        "agent_type": agent_type.type_name if agent_type else "developer",
        "remote_url": remote_url,
        "target_branch": "",
        "delivery_branch": "",
        "accepted_delivery": False,
        "delivery_failed": False,
        "accepted_delivery_reasons": ["delivery_not_attempted"],
        "delivery_attempted": False,
        "plan_quality": plan_quality,
        "plan_quality_status": plan_quality["status"],
        "plan_requirement_coverage": plan_quality["plan_requirement_coverage"],
        "plan_acceptance_coverage": plan_quality["plan_acceptance_coverage"],
        "plan_conflict_count": plan_quality["plan_conflict_count"],
        "plan_warning_count": plan_quality["plan_warning_count"],
        "plan_repair_count": len(preflight_repair_history),
        "plan_repair_attempted": bool(preflight_repair_history),
        "plan_repair_history": preflight_repair_history,
        "plan_repairable_issue_count": plan_quality.get("plan_repairable_issue_count", 0),
        "architecture_review": architecture_review,
        "spec_snapshot": spec_snapshot,
        # spec 级稳定 ID 持久化（traceability 的 spec 侧输入；空列表 = 无 spec 任务）
        "requirement_ids": spec_req_ids,
        "acceptance_criteria_ids": spec_ac_ids,
    }
    # Goal Contract: 从 Task + Plan + Subtask 提取完成契约（确定性，不调 LLM）
    try:
        from .planning import build_goal_contract
        meta["goal_contract"] = build_goal_contract(task, confirmed, delivery_required=True)
    except Exception as _gce:
        logger.debug(f"[goal_contract] 构建失败（忽略）: {_gce}")
    meta["goal_mode"] = goal_policy["mode"]
    meta["goal_backend"] = goal_policy["backend"]
    meta["goal_policy_reason_codes"] = goal_policy["reason_codes"]
    # recover 必须基于本次运行的确切基准提交，不能依赖默认分支名或提交时间窗口。
    if (repo / ".git").exists():
        try:
            _base_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
                text=True, check=True, timeout=10).stdout.strip()
            _base_branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=str(repo), capture_output=True,
                text=True, check=True, timeout=10).stdout.strip()
            meta["base_commit"] = _base_commit
            meta["base_branch"] = _base_branch
            meta["target_branch"] = _base_branch
        except (OSError, subprocess.SubprocessError):
            logger.warning("无法记录 base_commit，recover 将降级为兼容模式")
    # A3 未提交基线：记录启动时主工作区是否 dirty 及处理方式（可追溯任务基线）
    meta["baseline_dirty"] = config.get("_baseline_dirty", False)
    meta["baseline_action"] = config.get("_baseline_action", "clean")
    if config.get("_baseline_dirty"):
        meta["baseline_dirty_files"] = config.get("_baseline_dirty_files", [])
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # repairable_issues 是 blocking_issues + warnings 的类型过滤子集（planning.py），
    # 直接拼接会把每条 blocking issue 计两次（ISSUE-50②：实测 meta 2 文件存 4 条）。
    _unresolved_plan_issues = list(plan_quality["blocking_issues"])
    _seen_issue_keys = {
        (i.get("type"), i.get("subtask_id"), i.get("file"), i.get("reason"))
        for i in _unresolved_plan_issues
    }
    _unresolved_plan_issues += [
        i for i in plan_quality.get("repairable_issues", [])
        if (i.get("type"), i.get("subtask_id"), i.get("file"), i.get("reason")) not in _seen_issue_keys
    ]
    if _unresolved_plan_issues and not _e2e:
        if dry_run:
            _print_dry_run_summary(confirmed_plan, confirmed, plan_quality, {}, console, task_id, task_dir)
            try:
                shutil.rmtree(task_dir)
            except OSError:
                pass
            return
        console.error("Plan 预检未通过，任务标记为 BLOCKED（约束阻断），未进入执行。")
        for issue in _unresolved_plan_issues:
            console.error(f"  [{issue['type']}] subtask={issue.get('subtask_id', '?')} {issue.get('reason', '')}")
        meta["status"] = "BLOCKED"
        # A Plan defect is not an external infrastructure outage. Keep
        # verification-generation defects separate from orchestration blocks.
        meta["failure_class"] = (
            "verification_failure" if plan_quality.get("repairable_issues") else "system_error"
        )
        meta["failure_reason"] = "plan_quality_blocked"
        meta["blocked_without_result"] = True
        meta["plan_quality_status"] = plan_quality.get("status", "blocked")
        meta["blocking_issues"] = _unresolved_plan_issues
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return

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

    # ── Dry-Run：生成 Plan + 展示子任务即退出，不执行 ──
    if dry_run:
        _print_dry_run_summary(confirmed_plan, confirmed, plan_quality, est, console, task_id, task_dir)
        # 清理临时 task_dir（仅含 PLAN.md 快照，无执行产物）
        try:
            shutil.rmtree(task_dir)
        except OSError:
            pass
        return

    _interactive_mode = getattr(args, 'interactive', False)
    _step_confirm = getattr(args, 'step_confirm', False)

    if _interactive_mode:
        import threading as _th
        import signal as _sig
        from .tui import cmd_status_tui
        _interrupted = _th.Event()
        _prev_int = _sig.getsignal(_sig.SIGINT)
        _prev_term = _sig.getsignal(_sig.SIGTERM)

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


def _print_dry_run_summary(plan: Optional[dict], subtasks: list, plan_quality: dict,
                           time_est: dict, console, task_id: str, task_dir: Path) -> None:
    """Print plan + subtask summary for --dry-run mode. No side effects."""

    console.title("DRY-RUN：Plan 预览（未执行任何操作）")
    console.print(f"   Task ID: {task_id}")

    if plan:
        goal = plan.get("goal") or plan.get("_original_task", "")
        console.subtitle("目标")
        console.print(f"   {goal[:200]}")
        steps = plan.get("steps") or []
        console.subtitle(f"Plan 步骤 ({len(steps)} 步)")
        for s in steps:
            skills_str = ", ".join(s.get("skills", []) or [])
            files_str = ", ".join(s.get("files", []) or [])[:80]
            console.print(f"   [{s.get('id', '?')}] {s.get('title', '?')}")
            if skills_str:
                console.print(f"       skills: {skills_str}")
            if files_str:
                console.print(f"       files:  {files_str}")

    console.subtitle(f"子任务拆解 ({len(subtasks)} 个)")
    for st in subtasks:
        deps = st.get("depends_on", []) or []
        deps_str = f" ← {', '.join(deps)}" if deps else ""
        diff = st.get("difficulty", "medium")
        console.print(f"   [{st['id']}] {st.get('title', '?')}  (difficulty={diff}){deps_str}")

    console.subtitle("Plan 质量报告")
    pq_status = plan_quality.get("status", "unknown")
    icon = {"ok": "✅", "warn": "⚠️", "blocked": "🔴"}.get(pq_status, "❓")
    console.print(f"   {icon} 状态: {pq_status}")
    console.print(f"   需求覆盖率: {plan_quality.get('plan_requirement_coverage', '?')}")
    console.print(f"   验收覆盖率: {plan_quality.get('plan_acceptance_coverage', '?')}")
    console.print(f"   冲突数:     {plan_quality.get('plan_conflict_count', 0)}")
    console.print(f"   警告数:     {plan_quality.get('plan_warning_count', 0)}")
    warnings = plan_quality.get("warnings", []) or []
    for w in warnings[:5]:
        console.print(f"   ⚠️  {w}")
    blocking = plan_quality.get("blocking_issues", []) or []
    for b in blocking:
        console.print(f"   🔴 BLOCKING: [{b.get('type', '?')}] {b.get('reason', '?')}")

    console.subtitle("时间预估")
    conf = time_est.get("confidence", "none")
    conf_label = {"high": "（高置信度）", "medium": "（样本较少）", "low": "（样本很少，仅供参考）",
                  "none": "（无历史数据，经验值）"}.get(conf, "")
    console.print(f"   预计耗时: ~{time_est.get('estimated_sec', 0) / 60:.0f} 分钟 {conf_label}")
    console.print(f"   子任务数: {time_est.get('subtasks', '?')}")
    console.print(f"   波次数:   {time_est.get('waves', '?')}")
    console.print(f"   置信度:   {conf}")

    if console.json_mode:
        console.data({
            "mode": "dry_run",
            "task_id": task_id,
            "plan": plan,
            "subtasks": subtasks,
            "plan_quality": plan_quality,
            "time_estimate": time_est,
        })

    console.sep()
    console.success("Dry-run 完成。使用 `agent_go run ... --yes` 执行。")


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
        sys.exit(EX_USAGE)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print(f"任务不存在: {task_id}")
        sys.exit(EX_USAGE)
    # logger 需在 result.json 恢复循环之前初始化，否则损坏文件触发 UnboundLocalError
    logger = setup_logger(task_id, task_dir)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    if task_status(meta) not in ("PAUSED", "EXECUTING", "VERIFICATION_FAILED", "BLOCKED", "CANCELLED", "running", "paused", "interrupted", "cancelled", "stale_aborted"):
        console.print(f"任务状态为 {meta['status']}，无法恢复。仅 running/paused/interrupted/cancelled/stale_aborted 状态可恢复")
        sys.exit(EX_ERROR)

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
        # resume 语义修复：blocked 是条件态（因上游 failed 而阻断），不是终态。
        # 上游可能在本次 resume 中被修复（failed→completed），此时 blocked 失去依据。
        # 清空 blocked 结果，让 pipeline 基于当前 failed_ids 重新评估级联：
        #   - 上游已 completed → 下游进入 wave 正常执行
        #   - 上游仍 failed → pipeline 自然重新标 blocked
        if r.get("status") == "blocked":
            logger.info(f"[resume] 解锁 blocked 子任务 {wid}（上游状态已变，重新评估）")
            continue
        # resume 语义修复 2：failed 同样是条件态，不是终态——failed 子任务本身
        # 会被本 resume 重跑（不在 completed_ids → 进入 remaining），若把历史失败
        # 结果 seed 进 results_map，wave-0 级联阻断会用「过期失败」把其下游永久
        # 标 blocked（sub 已重跑成功也无法解锁）。故 failed 结果也不 seed，让
        # pipeline 基于本次重跑的真实结果重新评估级联：
        #   - 上游重跑成功 → 下游进入 wave 正常执行
        #   - 上游重跑仍失败 → pipeline 自然重新标 blocked
        if r.get("status") == "failed":
            logger.info(f"[resume] 清空 failed 结果 {wid}（乐观重跑，级联按本次结果重估）")
            continue
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
            parallel = max(1, min(8, int(sys.argv[pi + 1])))
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
    if meta.get("status_schema_version"):
        set_task_status(meta, "EXECUTING")
    else:
        meta["status"] = "running"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta,
                  worktree_map, results_map, completed_ids, remote_url=remote_url,
                  preserve_worktrees=preserve_worktrees,
                  step_confirm=getattr(args, 'step_confirm', False) if args else False)
    try:
        final_meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        if final_meta.get("status") in {"VERIFICATION_FAILED", "BLOCKED", "DELIVERY_FAILED", "CANCELLED"}:
            raise SystemExit(EX_ERROR)
    except (OSError, json.JSONDecodeError):
        pass

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
            "problem_id": result.get("problem_id", ""),
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
    # 失败历史关联（#52：决定 resume 修复前看到历史解法——最有价值的召回时机）
    try:
        from .problems import load as load_problems
        problems_map = {p.id: p for p in load_problems(AGENT_GO_DIR / "problems.jsonl")}
    except Exception:
        problems_map = {}
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
        if e["status"] == "failed" and e.get("problem_id"):
            _prob = problems_map.get(e["problem_id"])
            if _prob:
                _hist = f"💡 失败历史: 该模式第 {_prob.occurrence_count} 次出现"
                if _prob.root_cause:
                    _hist += f"；历史根因: {_prob.root_cause[:60]}"
                if _prob.resolution_summary:
                    _hist += f"；历史解法: {_prob.resolution_summary[:60]}"
                console.print(_hist)
                if _prob.status == "opened" and _prob.resolved_by:
                    console.print("   （上次修复未生效，已重开跟踪）")
        if e["verify_ok"] is not None:
            console.print(f"验证: {'通过' if e['verify_ok'] else '失败'}")
        if e["worktree_exists"]:
            console.print(f"📁 {e['worktree_path']}")
            console.print(f"🔗 git branch: {e['branch']}")
            if e["has_task_md"]:
                console.print("📝 TASK.md | result.json")
        else:
            console.print("(worktree 不存在 — 已清理或未创建)")
    console.sep("─", 70)
    console.print("提示: cd 到 worktree 路径查看完整文件状态")
    _print_diag_hints(task_id, entries)


def _print_diag_hints(task_id: str, entries: list) -> None:
    """C6 复盘入口：失败 subtask 的代理诊断查询提示（R14/R15/R16，只读）。

    视角正确性：压缩后行为复盘以代理 sent_view 为准（模型实际所见 ≠ 客户端转录）。
    无本地代理配置时不显示。
    """
    failed = [e for e in entries if e.get("status") in ("failed", "blocked")]
    if not failed:
        return
    try:
        from .config import load_config
        from . import diag
        base_url = diag.local_proxy_base_url(load_config())
    except Exception:
        base_url = ""
    if not base_url:
        return
    console.sep("─", 70)
    console.print("🩺 代理诊断（llama-defender R14-R16，以代理 sent_view 为准）:")
    for e in failed:
        key8 = diag.session_key8(diag.session_key(task_id, e["id"]))
        console.print(f"  {e['id']}（会话 {key8}）:")
        console.print(f"    curl -s {base_url}/api/session/{key8}/ledger | python3 -m json.tool    # 重复轮/材料台账")
        console.print(f"    curl -s '{base_url}/api/session/{key8}/archive?view=sent'              # 模型实际所见 payload")
        console.print(f"    curl -s {base_url}/api/session/{key8}/metrics | python3 -m json.tool   # 命中率/延迟分档")


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
        status = task_status(meta)
        icon = {"completed": "🟢", "aborted": "🟡", "failed": "🔴", "cancelled": "⏹️"}.get(status, "⚪")
        docs = ",".join(meta.get("reference_docs", []))[:15]
        console.print(f"{t.name:<25} {icon} {status:<10} {len(meta.get('subtasks',[])):<8} {docs:<12} {meta.get('task','')[:30]}")

def cmd_show(args=None):
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
    elif len(sys.argv) < 3:
        console.print("Usage: agent_go show <task-id>")
        sys.exit(EX_USAGE)
    else:
        task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.print("任务不存在")
        sys.exit(EX_USAGE)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    console.print(f"\n🆔 {task_id}")
    console.print(f"📝 {meta['task']}")
    console.print(f"📁 {meta['repo']}")
    console.print(f"📊 {meta.get('status','unknown')}")
    # M4 goal 回溯：合规度不足时显式提示（执行全过但漏验收不得静默）
    _ga = meta.get("goal_adherence") or {}
    if _ga.get("level") and _ga["level"] != "unknown":
        _ga_icon = "✅" if _ga["level"] == "full" else "⚠️"
        console.print(f"🎯 Goal 合规度: {_ga_icon} {_ga['level']}（score={_ga.get('score')}）")
        if _ga.get("needs_human_review"):
            console.warning("   执行全过但验收存疑，建议人工补验收（agent_go review --task 查看缺口）")
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
    config = load_config()
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
    subtask_map = {s["id"]: s for s in subtasks}

    # 收集文件变更：按文件路径分组
    file_changes: dict[str, list[dict]] = {}
    for r in results:
        sid = r.get("subtask_id", "")
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
        "",
        f"**任务**: {task_title}",
        f"**创建时间**: {created}",
        f"**状态**: {status}",
        f"**子任务数**: {len(subtasks)}",
        "",
    ]

    # 文件变更摘要
    if file_changes:
        lines.append("## 📁 文件变更汇总")
        lines.append("")
        lines.append("| 文件 | 涉及子任务 | 变更量 | 验证 |")
        lines.append("|------|-----------|--------|------|")
        for file_path, changes in sorted(file_changes.items()):
            sub_ids = ", ".join(c["subtask_id"] for c in changes)
            total_ins = sum(c["insertions"] for c in changes)
            total_del = sum(c["deletions"] for c in changes)
            all_verified = all(c["verify_ok"] for c in changes)
            verify_icon = "✅" if all_verified else "❌"
            lines.append(f"| `{file_path}` | {sub_ids} | +{total_ins}/-{total_del} | {verify_icon} |")
        lines.append("")

    # 已知盲区（谦逊层 H1+H4：系统主动交底，不阻断）
    blind_spots = meta.get("blind_spots") or {}
    uncovered_perspectives = meta.get("uncovered_perspectives") or []
    _blind_lines: list[str] = []
    if blind_spots.get("uncovered_acceptance_ids"):
        _blind_lines.append(f"- 未覆盖验收 ID: {', '.join(map(str, blind_spots['uncovered_acceptance_ids']))}")
    if blind_spots.get("weakly_anchored_subtasks"):
        _blind_lines.append(f"- 弱锚定验证（整仓测试）子任务: {', '.join(map(str, blind_spots['weakly_anchored_subtasks']))}")
    if blind_spots.get("unattributed_failures"):
        _blind_lines.append(f"- 无根因分析的失败子任务: {', '.join(map(str, blind_spots['unattributed_failures']))}")
    if blind_spots.get("baseline_dirty"):
        _blind_lines.append("- 任务启动时工作区有未提交改动（baseline_dirty）")
    if blind_spots.get("inconclusive_evaluations"):
        _blind_lines.append(f"- 语义评估不确定的子任务: {', '.join(map(str, blind_spots['inconclusive_evaluations']))}")
    for p in uncovered_perspectives:
        _blind_lines.append(f"- 未覆盖视角 [{p.get('perspective')}]: {p.get('reason', '')}")
    if _blind_lines:
        lines.append("## ⚠️ 已知盲区（系统主动交底，供审查参考）")
        lines.append("")
        lines.extend(_blind_lines)
        lines.append("")

    # M4 goal 回溯：goal 合规度（与 status 正交；执行全过但漏验收时显式标记）
    goal_adherence = meta.get("goal_adherence") or {}
    if goal_adherence and goal_adherence.get("level") not in (None, "unknown", "full"):
        lines.append("## 🎯 Goal 合规度（M4 回溯，与任务状态正交）")
        lines.append("")
        _ga_warn = " ⚠️ **执行全过但验收存疑，建议人工补验收**" if goal_adherence.get("needs_human_review") else ""
        lines.append(f"- 合规等级: **{goal_adherence.get('level')}**（score={goal_adherence.get('score')}）{_ga_warn}")
        for gap in goal_adherence.get("gaps", []):
            lines.append(f"- [{gap.get('type')}] {gap.get('detail', '')}")
        lines.append("")

    # 层间归因（谦逊层 H2：失败定位到「层」，回答「该修 spec、修 planner、调预算还是换模型」）
    layer_attr = meta.get("layer_attribution") or {}
    _layer_lines: list[str] = []
    _primary = layer_attr.get("primary")
    if _primary:
        _layer_lines.append(f"- 任务级归因: **{_primary}**")
    for _sid, _layer in sorted((layer_attr.get("by_subtask") or {}).items()):
        _layer_lines.append(f"- {_sid}: {_layer}")
    if _layer_lines:
        lines.append("## 🎯 层间归因（失败定位到「层」）")
        lines.append("")
        lines.extend(_layer_lines)
        lines.append("")

    # 失败历史关联（#50：让用户看见「越用越聪明」——失败时告知「这不是第一次」）
    try:
        from .problems import load as load_problems
        problems_map = {p.id: p for p in load_problems(AGENT_GO_DIR / "problems.jsonl")}
        _hist_lines: list[str] = []
        for r in results:
            _pid = r.get("problem_id") or ""
            prob = problems_map.get(_pid)
            if not prob or r.get("status") != "failed":
                continue
            _sid = r.get("subtask_id", "")
            _line = f"- {_sid}: 该失败模式第 **{prob.occurrence_count}** 次出现"
            if prob.root_cause:
                _line += f"；历史根因: {prob.root_cause[:80]}"
            if prob.resolution_summary:
                _line += f"；历史解法: {prob.resolution_summary[:80]}"
            if prob.status == "opened" and prob.resolved_by:
                _line += "（上次修复未生效，已重开跟踪）"
            _hist_lines.append(_line)
        if _hist_lines:
            lines.append("## 💡 失败历史关联（这不是第一次）")
            lines.append("")
            lines.extend(_hist_lines)
            lines.append("")
    except Exception:
        pass

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
        lines.append("📝 **需要修改** — 已写入 review.json")
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

    # 三个例外点显式产品承诺（#50：拒绝权是权利，不是负担）
    lines.append("")
    lines.append("---")
    lines.append("> 🤝 **你可以随时说不**：Plan 确认时改/删/重生成、merge 前喊停、失败后 inspect/resume——")
    lines.append("> 这是你的权利，不是负担。agent_go 永不自动 merge，永远等你点头。")

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
        "| 指标 | 值 |",
        "|------|-----|",
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
        console.print("  📝 概述: ✏️ 已修改")

    # 全局字段对比
    _global_keys = ["estimated_effort"]
    _global_diffs = [(k, plan1.get(k, ""), plan2.get(k, "")) for k in _global_keys if plan1.get(k) != plan2.get(k)]
    if _global_diffs:
        console.subtitle("全局变更")
        for _k, _v1, _v2 in _global_diffs:
            console.print(f"  {_k}: \"{str(_v1)[:60]}\" → \"{str(_v2)[:60]}\"")

    # 步骤对比详情
    console.subtitle("步骤详情")
    _TITLE = 0
    _DESC = 1
    _FILES = 2
    _VER = 3
    _DIFF = 4
    _AGENT = 5
    _SKILL = 6
    _headers = ["#", "标题", "变更"]
    _rows: list[list[str]] = []
    _all_ids = sorted(_s1_ids | _s2_ids)
    for sid in _all_ids:
        s1 = next((s for s in steps1 if s["id"] == sid), None)
        s2 = next((s for s in steps2 if s["id"] == sid), None)
        if not s1:
            _rows.append([str(sid), (s2 or {}).get("title", "")[:50], "🆕 新增"])
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
        sys.exit(EX_USAGE)
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
        sys.exit(EX_USAGE)

    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))

    # P1 互斥：任务已通过显式 merge 交付后，禁止再走 PR 路径（避免双交付与 commit 不一致）。
    if not offline and meta.get("explicit_merge_commit"):
        console.error(
            f"任务 {task_id} 已通过显式 merge 交付（explicit_merge_commit="
            f"{meta['explicit_merge_commit'][:12]}）。PR 与 merge 是互斥交付路径，"
            "请勿重复交付。"
        )
        sys.exit(EX_ERROR)

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

    # M1.2: --push 推 delivery branch 到远程（不推 HEAD 到 base branch）。
    # 禁止把当前工作目录 HEAD 误推到目标分支——只推 task 的 delivery branch。
    delivery_branch = meta.get("delivery_branch") or ""
    if do_push and not offline:
        repo = meta.get("repo", "")
        if repo and Path(repo).exists():
            if not delivery_branch:
                console.error(f"任务 {task_id} 没有 delivery_branch，无法安全推送。"
                              "请先运行 pipeline 生成交付分支，或手动指定 head。")
                sys.exit(EX_ERROR)
            push_result = subprocess.run(
                ["git", "push", remote, f"{delivery_branch}:{delivery_branch}"],
                cwd=str(Path(repo)), capture_output=True, text=True,
            )
            if push_result.returncode == 0:
                console.success(f"分支已推送到 {remote}/{delivery_branch}")
            else:
                console.print(f"⚠️  推送失败: {push_result.stderr.strip()[:200]}")

    if offline:
        out = task_dir / "PR.md"
        out.write_text(pr_body, encoding="utf-8")
        console.print(f"PR 描述已写入 {out}")
        push_hint = " --push" if not do_push else ""
        console.print(f"请手动创建 PR 或稍后执行: agent_go pr {task_id}{push_hint}")
    else:
        # 在线模式：通过 gh CLI 创建 PR，显式指定 head/base。
        # M1: PR head/base 关系校验——head 必须是 delivery_branch，base 必须是 target_branch。
        base = meta.get("base_branch") or meta.get("target_branch") or "main"
        head = delivery_branch or meta.get("base_branch", "main")
        if not delivery_branch:
            console.error(f"任务 {task_id} 没有 delivery_branch，无法创建 PR（head 必须指向交付分支）。")
            sys.exit(EX_ERROR)
        # mergeability 预检：创建 PR 前检查 delivery_branch 能否 clean merge 到 base。
        from .delivery import check_mergeability
        repo = meta.get("repo", "")
        if repo and Path(repo).exists():
            _mc = check_mergeability(repo, delivery_branch, base)
            if _mc.get("error"):
                console.error(f"mergeability 检查失败: {_mc['error']}")
            elif not _mc.get("mergeable"):
                console.error(
                    f"delivery branch 无法 clean merge 到 {base}，发现冲突文件: "
                    f"{', '.join(_mc.get('conflicts', []) or ['<未知>'])}。"
                    "请先解决冲突（在 delivery branch 上合并 target 或人工处理）再创建 PR。"
                )
                sys.exit(EX_ERROR)
            elif _mc.get("ahead") == 0:
                console.warning(f"delivery branch 相对 {base} 无新增 commit，PR 可能为空。")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as tf:
            tf.write(pr_body)
            pr_file = tf.name
        title = meta.get("task", "agent_go task")[:72]
        try:
            if not shutil.which("gh"):
                console.error("未安装 gh CLI。请先安装: brew install gh")
                (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
                console.print(f"PR 描述已备份到 {task_dir}/PR.md")
                meta["delivery_failed"] = True
                meta["delivery_error"] = "gh CLI 未安装"
                (task_dir / "meta.json").write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                return
            result = subprocess.run([
                "gh", "pr", "create", "--title", f"{title}",
                "--body-file", pr_file, "--base", base, "--head", head,
            ], capture_output=True, text=True, cwd=str(Path(repo)))
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                console.print(pr_url)
                # 持久化 PR 元数据（M1.2）
                meta["pr_url"] = pr_url
                meta["pr_head"] = head
                meta["pr_base"] = base
                meta["delivery_attempted"] = True
                meta["delivery_failed"] = False
                meta["delivery_error"] = ""
                meta.pop("accepted_delivery_reasons", None)
                meta["accepted_delivery"] = True
                if meta.get("status_schema_version"):
                    meta["status"] = "ACCEPTED_DELIVERY"
                from .planning import refresh_goal_adherence  # ISSUE-52
                refresh_goal_adherence(meta)
                (task_dir / "meta.json").write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            else:
                # M1.2 真实远端：gh 报 "already exists" 说明该 delivery branch 已有 PR
                # （head/base 正确），应视为成功——从错误信息提取已有 PR URL，而非误判交付失败。
                import re as _re
                _err = result.stderr.strip()
                _existing = _re.search(r"(https://\S+/pull/\d+)", _err)
                if _existing and "already exists" in _err:
                    pr_url = _existing.group(1)
                    console.print(pr_url)
                    console.warning("该 delivery branch 已有 PR，复用已有 PR。")
                    meta["pr_url"] = pr_url
                    meta["pr_head"] = head
                    meta["pr_base"] = base
                    meta["delivery_attempted"] = True
                    meta["delivery_failed"] = False
                    meta["delivery_error"] = ""
                    meta.pop("accepted_delivery_reasons", None)
                    meta["accepted_delivery"] = True
                    if meta.get("status_schema_version"):
                        meta["status"] = "ACCEPTED_DELIVERY"
                    from .planning import refresh_goal_adherence  # ISSUE-52
                    refresh_goal_adherence(meta)
                    (task_dir / "meta.json").write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                    console.print("PR 元数据已持久化，任务标记为 ACCEPTED_DELIVERY")
                else:
                    console.error(f"gh pr create 失败: {_err}")
                    (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
                    console.print(f"PR 描述已备份到 {task_dir}/PR.md")
                    # 交付失败归类（M1.2）：不能报告 completed，标记 delivery_failed。
                    meta["delivery_attempted"] = True
                    meta["delivery_failed"] = True
                    meta["delivery_error"] = _err[:300]
                    meta["accepted_delivery"] = False
                    meta.pop("accepted_delivery_reasons", None)
                    if meta.get("status_schema_version") and not any(
                        r.get("status") in ("failed", "blocked") for r in meta.get("results", [])
                    ):
                        meta["status"] = "DELIVERY_FAILED"
                    from .planning import refresh_goal_adherence  # ISSUE-52
                    refresh_goal_adherence(meta)
                    (task_dir / "meta.json").write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        finally:
            os.unlink(pr_file)


def _fetch_merged_pr_commit(pr_url: str, repo: str) -> str:
    """若 ``pr_url`` 对应的 PR 已在 GitHub 合并，返回其 merge commit sha；否则返回空串。"""
    if not pr_url or not repo or not Path(repo).exists() or not shutil.which("gh"):
        return ""
    import re as _re
    m = _re.search(r"/pull/(\d+)", pr_url)
    if not m:
        return ""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", m.group(1), "--json", "state,mergeCommit,mergedAt"],
            cwd=str(Path(repo)), capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return ""
        data = json.loads(r.stdout or "{}")
        if data.get("state") == "MERGED":
            return (data.get("mergeCommit") or {}).get("oid", "") or ""
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return ""


def cmd_merge(args=None):
    """将 delivery branch 显式合并到 target branch（人工交付命令，M1.2）。

    等价于用户在 CI/CD 或本地执行 merge。合并成功后在 meta 记录
    explicit_merge_commit，使 Accepted Delivery 判定可通过。
    """
    if args and hasattr(args, 'task_id'):
        task_id = args.task_id
        do_push = getattr(args, 'push', False)
        remote = getattr(args, 'remote', "origin")
    elif len(sys.argv) < 3:
        console.print("Usage: agent_go merge <task-id> [--push] [--remote <name>]")
        sys.exit(EX_USAGE)
    else:
        task_id = sys.argv[2]
        do_push = "--push" in sys.argv
        remote = "origin"
        if "--remote" in sys.argv:
            try:
                remote = sys.argv[sys.argv.index("--remote") + 1]
            except (IndexError, ValueError):
                pass
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.error(f"任务不存在: {task_id}")
        sys.exit(EX_USAGE)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    repo = meta.get("repo", "")
    if not repo or not Path(repo).exists():
        console.error(f"任务 {task_id} 的仓库不存在: {repo}")

    # merge 前快照：当前 checkout 工作区干净度（供 merge 后同步决策，见 _sync_checked_out_worktree）
    merge_start_clean = False
    try:
        _st = subprocess.run(["git", "status", "--porcelain"], cwd=str(Path(repo)),
                             capture_output=True, text=True, timeout=10)
        merge_start_clean = _st.returncode == 0 and not _st.stdout.strip()
    except Exception:
        pass
        sys.exit(EX_SYSTEM)
    delivery_branch = meta.get("delivery_branch") or ""
    target = meta.get("target_branch") or meta.get("base_branch") or "main"

    # P1 互斥：任务已走 PR 交付路径时，禁止重复本地 merge。
    # 若对应 PR 已在 GitHub 合并，直接同步其 merge commit 完成交付（避免双 merge commit 不一致）。
    _pr_url = meta.get("pr_url") or ""
    if _pr_url:
        _merged = _fetch_merged_pr_commit(_pr_url, repo)
        if _merged:
            meta["explicit_merge_commit"] = _merged
            meta["delivery_attempted"] = True
            meta["delivery_failed"] = False
            meta["delivery_error"] = ""
            meta.pop("accepted_delivery_reasons", None)
            meta["accepted_delivery"] = True
            if meta.get("status_schema_version"):
                meta["status"] = "ACCEPTED_DELIVERY"
            from .planning import refresh_goal_adherence  # ISSUE-52
            refresh_goal_adherence(meta)
            (task_dir / "meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            console.success(f"PR {_pr_url} 已在 GitHub 合并，已同步 explicit_merge_commit={_merged[:12]}")
            return
        console.error(
            f"任务 {task_id} 已走 PR 交付路径（{_pr_url}），PR 与 merge 是互斥交付路径。"
            "请先在 GitHub 合并该 PR，或先移除 meta.pr_url 再执行本地 merge。"
        )
        sys.exit(EX_ERROR)

    if not delivery_branch:
        console.error(f"任务 {task_id} 没有 delivery_branch，无法合并。")
        sys.exit(EX_ERROR)

    # 校验 delivery branch 存在
    check = subprocess.run(
        ["git", "rev-parse", "--verify", delivery_branch],
        cwd=str(Path(repo)), capture_output=True, text=True,
    )
    if check.returncode != 0:
        console.error(f"delivery branch {delivery_branch} 不存在于仓库中。")
        sys.exit(EX_SYSTEM)

    # mergeability 预检：合并前检查能否 clean merge（避免污染 target 后才发现冲突）。
    from .delivery import check_mergeability
    _mc = check_mergeability(repo, delivery_branch, target)
    if _mc.get("error"):
        console.error(f"mergeability 检查失败: {_mc['error']}")
        sys.exit(EX_SYSTEM)
    if not _mc.get("mergeable"):
        console.error(
            f"delivery branch 无法 clean merge 到 {target}，发现冲突文件: "
            f"{', '.join(_mc.get('conflicts', []) or ['<未知>'])}。"
            "已中止，请先解决冲突。"
        )
        sys.exit(EX_ERROR)
    if _mc.get("ahead") == 0:
        console.warning(f"delivery branch 相对 {target} 无新增 commit，merge 为空操作。")

    # 用临时 worktree 在 target branch 上执行 merge，避免污染主工作区。
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp(prefix=f"agent_go_merge_{task_id}_"))
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp), target],
            cwd=str(Path(repo)), capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            console.error(f"无法创建 merge worktree: {add.stderr.strip()[:200]}")
            sys.exit(EX_SYSTEM)
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "-m", f"agent_go: merge delivery of {task_id}", delivery_branch],
            cwd=str(tmp), capture_output=True, text=True, timeout=60,
        )
        if merge.returncode != 0:
            console.error(f"merge 冲突，已保留现场: {merge.stderr.strip()[:300]}")
            console.print(f"  冲突 worktree: {tmp}")
            console.print(f"  delivery branch: {delivery_branch}")
            meta["delivery_failed"] = True
            meta["delivery_error"] = f"merge 冲突: {merge.stderr.strip()[:200]}"
            (task_dir / "meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            sys.exit(EX_ERROR)
        # 合并成功：记录 explicit_merge_commit 并推进 target branch。
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tmp),
            capture_output=True, text=True, timeout=10,
        )
        merge_commit = head.stdout.strip() if head.returncode == 0 else ""
        if merge_commit:
            # 用 update-ref 推进分支（branch -f 在 target 被当前 checkout 时静默失败）
            update_ref = subprocess.run(
                ["git", "update-ref", f"refs/heads/{target}", merge_commit],
                cwd=str(Path(repo)), capture_output=True, text=True, timeout=10,
            )
            if update_ref.returncode != 0:
                console.error(f"更新 {target} 分支失败: {update_ref.stderr.strip()[:200]}")
                sys.exit(EX_SYSTEM)
            meta["explicit_merge_commit"] = merge_commit
            meta["delivery_attempted"] = True
            meta["delivery_failed"] = False
            meta["delivery_error"] = ""
            meta.pop("accepted_delivery_reasons", None)
            if not any(
                r.get("status") in ("failed", "blocked") for r in meta.get("results", [])
            ):
                meta["accepted_delivery"] = True
                if meta.get("status_schema_version"):
                    meta["status"] = "ACCEPTED_DELIVERY"
            from .planning import refresh_goal_adherence  # ISSUE-52
            refresh_goal_adherence(meta)
            (task_dir / "meta.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            console.success(f"已合并到 {target}: {merge_commit[:12]}")
            # 工作区同步：update-ref 推进 target 分支后，若当前 checkout 恰在 target，
            # 工作区/index 与 HEAD 失配（产物显示 staged deletion）。merge 前工作区
            # 干净 → reset 同步；脏 → 警告用户手动处理，避免误提交删除产物。
            _sync_checked_out_worktree(repo, target, merge_start_clean)
            if do_push:
                push = subprocess.run(
                    ["git", "push", remote, f"{target}:{target}"],
                    cwd=str(Path(repo)), capture_output=True, text=True,
                )
                if push.returncode == 0:
                    console.success(f"已推送 {target} 到 {remote}")
                else:
                    console.print(f"⚠️  推送失败: {push.stderr.strip()[:200]}")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tmp)],
            cwd=str(Path(repo)), capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(tmp, ignore_errors=True)


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
        claude_lines = [ln for ln in tail if "[claude" in ln or "[text]" in ln
                        or "[Read]" in ln or "[Write]" in ln or "[Bash]" in ln
                        or "[tool_result]" in ln or "[result]" in ln]
        return claude_lines[-count:]

    def _get_task_status(task_dir: Path) -> Optional[dict[str, Any]]:
        meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = task_status(meta)
        log_path = task_dir / "execution.log"
        ZOMBIE_TIMEOUT = 600  # 10 分钟无日志输出视为僵尸任务

        # 僵尸检测：status=running 但日志已超过 ZOMBIE_TIMEOUT 未更新
        if status == "running" and log_path.exists():
            log_mtime = log_path.stat().st_mtime
            if time.time() - log_mtime > ZOMBIE_TIMEOUT:
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

def cmd_report(args=None) -> None:
    """导出任务报告（P1：任务共享——md/html 报告分发）。"""
    task_id = args.task_id
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        console.error(f"任务不存在: {task_id}")
        sys.exit(EX_USAGE)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    results = meta.get("results", []) or []
    subtasks = meta.get("subtasks", []) or []
    status = meta.get("status", "unknown")
    repo = meta.get("repo", "")

    # metering 汇总（成本/耗时）
    metering_records = []
    mj = task_dir / "metering.jsonl"
    if mj.exists():
        for line in mj.read_text(encoding="utf-8").splitlines():
            try:
                metering_records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    total_cost = round(sum(r.get("cost_usd", 0) or 0 for r in metering_records), 6)
    total_elapsed = round(sum(r.get("duration_sec", 0) or 0 for r in results), 1)
    role_models: dict[str, set] = {}
    for r in metering_records:
        role = r.get("role", "?")
        model = r.get("route_actual_model") or r.get("actual_model") or r.get("routed_model") or "?"
        role_models.setdefault(role, set()).add(model)

    # 交付信息
    review_decision = None
    rj = task_dir / "review.json"
    if rj.exists():
        try:
            review_decision = json.loads(rj.read_text(encoding="utf-8")).get("decision")
        except (json.JSONDecodeError, OSError):
            pass

    lines: list[str] = []
    lines.append(f"# 任务报告: {meta.get('task', task_id)[:80]}")
    lines.append("")
    lines.append(f"- **任务 ID**: `{task_id}`")
    lines.append(f"- **状态**: `{status}`")
    lines.append(f"- **仓库**: `{repo}`")
    lines.append(f"- **创建**: {meta.get('created', '?')}")
    lines.append(f"- **总成本**: ${total_cost:.4f} | **总耗时**: {total_elapsed}s")
    if role_models:
        _model_desc = ", ".join(
            k + "=" + ",".join(sorted(v)) for k, v in role_models.items())
        lines.append(f"- **模型**: {_model_desc}")
    if review_decision:
        lines.append(f"- **审批决策**: `{review_decision}`")
    delivery_branch = meta.get("delivery_branch", "")
    if delivery_branch:
        lines.append(f"- **交付分支**: `{delivery_branch}`")
    if meta.get("explicit_merge_commit"):
        lines.append(f"- **合并提交**: `{meta['explicit_merge_commit'][:12]}`")
    lines.append("")

    # 子任务明细
    done = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    lines.append(f"## 子任务（{done} 完成 / {failed} 失败 / 共 {len(subtasks)}）")
    lines.append("")
    lines.append("| 子任务 | 状态 | 验证 | 耗时 | 摘要 |")
    lines.append("|--------|------|------|------|------|")
    for i, st in enumerate(subtasks):
        r = results[i] if i < len(results) else {}
        st_status = r.get("status", "pending")
        verify = "✅" if r.get("verify_ok") else ("❌" if r.get("verify_ok") is False else "—")
        dur = r.get("duration_sec", 0)
        summary = (r.get("summary", "") or "")[:50].replace("|", "/")
        lines.append(f"| `{st.get('id','')}` | {st_status} | {verify} | {dur}s | {summary} |")
    lines.append("")

    # 失败原因
    failures = [(st.get('id', ''), r.get('failure_reason', ''))
                for i, st in enumerate(subtasks) if i < len(results) and results[i].get("failure_reason")]
    if failures:
        lines.append("## 失败原因")
        lines.append("")
        for sid, reason in failures:
            lines.append(f"- **{sid}**: {reason[:200]}")
        lines.append("")

    # 成本明细
    if metering_records:
        lines.append("## 成本明细（metering）")
        lines.append("")
        lines.append("| 角色 | 模型 | 调用 | 成本 |")
        lines.append("|------|------|------|------|")
        agg: dict = {}
        for r in metering_records:
            role = r.get("role", "?")
            model = r.get("route_actual_model") or r.get("actual_model") or "?"
            key = (role, model)
            slot = agg.setdefault(key, {"calls": 0, "cost": 0.0})
            slot["calls"] += 1
            slot["cost"] += r.get("cost_usd", 0) or 0
        for (role, model), slot in sorted(agg.items()):
            lines.append(f"| {role} | `{model}` | {slot['calls']} | ${slot['cost']:.4f} |")
        lines.append("")

    md = "\n".join(lines)
    if args.format == "html":
        import html as _html
        # md → html：标题/列表/表格/段落
        parts: list[str] = []
        in_table = False
        for ln in lines:
            if ln.startswith("|"):
                if not in_table:
                    parts.append("<table>")
                    in_table = True
                cells = [c.strip() for c in ln.strip("|").split("|")]
                is_header = any(c in ("子任务", "状态", "验证", "耗时", "摘要",
                                      "角色", "模型", "调用", "成本") for c in cells)
                tag = "th" if is_header else "td"
                parts.append("<tr>" + "".join(
                    f"<{tag}>{_html.escape(c)}</{tag}>" for c in cells) + "</tr>")
            else:
                if in_table:
                    parts.append("</table>")
                    in_table = False
                if ln.startswith("## "):
                    parts.append(f"<h2>{_html.escape(ln[3:])}</h2>")
                elif ln.startswith("# "):
                    parts.append(f"<h1>{_html.escape(ln[2:])}</h1>")
                elif ln.startswith("- "):
                    parts.append(f"<p>• {_html.escape(ln[2:])}</p>")
                elif ln:
                    parts.append(f"<p>{_html.escape(ln)}</p>")
        if in_table:
            parts.append("</table>")
        report = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                  f"<title>任务报告 {task_id}</title>"
                  f"<style>body{{font-family:-apple-system,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}"
                  f"table{{border-collapse:collapse;width:100%;margin:8px 0}}"
                  f"td,th{{border:1px solid #ddd;padding:6px 10px;font-size:13px;text-align:left}}"
                  f"th{{background:#f7f7f7}}"
                  f"h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px}}"
                  f"code{{background:#f4f4f4;padding:1px 5px;border-radius:3px}}"
                  f"p{{font-size:14px;line-height:1.6}}</style></head><body>"
                  f"{''.join(parts)}</body></html>")
        out = report
    else:
        out = md

    if args.output == "-":
        console.print(out)
        return
    path = Path(args.output) if args.output else task_dir.parent / f"{task_id}.{args.format}"
    path.write_text(out, encoding="utf-8")
    console.success(f"报告已导出: {path}")


def cmd_kanban_import_spec(args) -> None:
    """kanban import-spec：从 Task Spec 需求文档生成看板卡片（进入看板编排流）。"""
    from .spec import parse_spec
    from . import kanban

    spec_path = Path(args.spec_path)
    spec = parse_spec(spec_path)
    if spec is None:
        console.error(f"Spec 解析失败或文件不存在: {spec_path}")
        sys.exit(EX_USAGE)
    stage = args.stage or "brainstorm"
    repo = (args.repo or "").strip()
    ctype = args.type or "implementation"

    # 组装卡片：spec 字段 → 看板卡片字段
    title = spec.title or spec_path.stem
    desc_parts = []
    if spec.goal:
        desc_parts.append(f"【目标】{spec.goal}")
    if spec.acceptance:
        desc_parts.append(f"【验收】{spec.acceptance}")
    if spec.scope:
        desc_parts.append(f"【范围】{spec.scope}")
    description = "\n\n".join(desc_parts)

    try:
        card = kanban.create_card(
            title=title, type=ctype, stage=stage, repo=repo,
            description=description,
            spec_path=str(spec_path),
        )
    except Exception as e:
        console.error(f"创建卡片失败: {e}")
        sys.exit(EX_ERROR)

    console.print(f"✅ 已从 Spec 生成看板卡片: {card['id']}")
    console.print(f"   标题: {card['title']}")
    console.print(f"   列: {card['stage']} | 类型: {card['type']} | automation: {card.get('automation', '-')}")
    console.print(f"   Spec: {card.get('spec_path', '-')}")
    console.print(f"   → 进入看板编排流（{card['stage']} → design → implementation → operations）")


def cmd_config(args=None) -> None:
    """config 子命令：无参=打印当前配置；local/cloud 一键切换；status 健康检查（M1/R1-R4）。"""
    sub = getattr(args, "config_subcommand", None) if args else None
    if sub == "local":
        from .profiles import activate_local, ProfileError
        try:
            result = activate_local(getattr(args, "url", "http://localhost:4000"))
        except ProfileError as e:
            console.error(str(e))
            sys.exit(EX_ERROR)
        console.print(f"✅ 已激活纯本地模式（profile: {result['profile']}）")
        console.print(f"   代理: {result['local_url']}  模型: {result['real_model'] or '(未探测到)'}")
        console.print(f"   配置: {result['profile_path']}")
        console.print(f"   备份: {result['backup_path']}（agent_go config cloud 恢复云端）")
        return
    if sub == "cloud":
        from .profiles import activate_cloud
        result = activate_cloud()
        console.print("✅ 已恢复云端配置（默认 config.json）")
        if result["previous_profile"]:
            console.print(f"   此前 profile: {result['previous_profile']}（备份: {result['backup_path']}）")
        return
    if sub == "status":
        from .profiles import health_check, list_profiles
        info = list_profiles()
        mode_label = {"local": "🟢 纯本地", "cloud": "☁️ 云端", "custom": f"🔧 自定义({info['current']})"}.get(info["mode"], info["mode"])
        console.print(f"当前模式: {mode_label}")
        health = health_check()
        for role in ("plan", "worker", "evaluator", "local_proxy"):
            h = health.get(role, {})
            if h.get("skipped"):
                console.print(f"  {role:12s} ⏭ {h.get('reason', '')}")
                continue
            mark = "✅" if h.get("ok") else "❌"
            model = f"  模型: {h['model']}" if h.get("model") else ""
            err = f"  错误: {h['error']}" if h.get("error") else ""
            console.print(f"  {role:12s} {mark} {h.get('url', '')}{model}{err}")
        if health.get("mismatch"):
            console.print(f"\n⚠️  {health['suggestion']}")
        return
    config = load_config()
    console.print(json.dumps(config, indent=2, ensure_ascii=False))

def cmd_spec(args) -> None:
    """Task Spec 工具：template（生成模板）/ validate（L1 准入审查）。"""
    sub = getattr(args, "spec_subcommand", None)
    if sub == "template":
        repo = Path(args.repo).resolve() if args.repo else None
        if repo and not repo.exists():
            console.error(f"路径不存在: {repo}")
            sys.exit(EX_USAGE)
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
            sys.exit(EX_USAGE)
        spec = parse_spec(spec_path)
        if spec is None:
            console.error(f"Spec 解析失败: {spec_path}")
            sys.exit(EX_USAGE)
        repo = Path(args.repo).resolve() if args.repo else None
        violations = validate_spec_l1(spec, repo)
        errors = [v for v in violations if v.severity != "warning"]
        warnings_ = [v for v in violations if v.severity == "warning"]
        console.print(f"\n📋 Task Spec: {spec.title or spec_path.name}")
        console.print(f"   完整性: {'✅ 全部必填章节就绪' if spec.is_complete else '❌ 缺失必填章节'}")
        if spec.source_path:
            console.print(f"   来源: {spec.source_path}")
        if not errors:
            console.print("\n✅ L1 准入审查通过（0 项违规）")
        else:
            console.print(f"\n❌ L1 准入审查未通过（{len(errors)} 项违规）：")
            for i, v in enumerate(errors, 1):
                sec = f" §{v.section}" if v.section else ""
                console.print(f"  {i}. [{v.check}{sec}] {v.message}")
                if v.suggestion:
                    console.print(f"     💡 {v.suggestion}")
        if warnings_:
            console.print(f"\n⚠️ L1 软警告（{len(warnings_)} 项，不阻断）：")
            for i, v in enumerate(warnings_, 1):
                sec = f" §{v.section}" if v.section else ""
                console.print(f"  {i}. [{v.check}{sec}] {v.message}")
                if v.suggestion:
                    console.print(f"     💡 {v.suggestion}")
        if errors:
            sys.exit(EX_ERROR)
    else:
        console.print("Usage: agent_go spec <template|validate> [args]")
        console.print("  template [repo] [--output PATH]  生成空白 Task Spec 模板")
        console.print("  validate <spec_path> [repo]       对 Spec 文件运行 L1 准入审查")

def _prune_fixture_repo_worktrees(fixtures_base: str = "eval_suite/fixtures") -> None:
    """清理 fixture 仓库与历史任务关联仓库的失效 worktree 注册（ISSUE-38）。

    bench 直接对 fixture 源仓库跑 `agent_go run`，executor 的 `git worktree add`
    把 worktree 注册到 fixture 源仓库 `.git/worktrees/`；timeout/SIGKILL 打断时
    pipeline 清理不执行，注册项残留。`git worktree prune` 清除指向不存在目录的
    注册——廉价、幂等、安全。本函数扫描：
      1. <fixtures_base>/*（bench fixture 源仓库，默认 eval_suite/fixtures）
      2. 所有任务 meta.repo 引用的本地仓库

    Args:
        fixtures_base: fixture 目录基路径（测试可注入 tmp 路径隔离）。
    """
    from .git_utils import _worktree_prune

    candidates: set[str] = set()
    # 1. fixture 源仓库
    for _base in (fixtures_base, f"{fixtures_base.rstrip('/')}/*"):
        _glob = sorted(Path(_base).glob("*")) if Path(_base).exists() else []
        for p in _glob:
            if (p / ".git").exists():
                candidates.add(str(p))
    # 2. 任务 meta.repo 引用的仓库
    for _td in sorted(AGENT_GO_DIR.glob("task-*")):
        _mp = _td / "meta.json"
        if not _mp.exists():
            continue
        try:
            _meta = json.loads(_mp.read_text(encoding="utf-8"))
            _repo = _meta.get("repo", "")
            if _repo and Path(_repo).exists() and (Path(_repo) / ".git").exists():
                candidates.add(_repo)
        except (json.JSONDecodeError, OSError):
            continue

    _total = 0
    for _repo in sorted(candidates):
        try:
            ok, err = _worktree_prune(Path(_repo))
            # 统计 prune 后残留（反映实际清理量）
            _wt = subprocess.run(["git", "worktree", "list"],
                                 cwd=_repo, capture_output=True, text=True, timeout=15)
            _count = len([_line for _line in _wt.stdout.strip().split("\n") if _line.strip()])
            if ok:
                console.print(f"✅ {_repo}: worktree 注册 {_count} 条")
            else:
                console.warning(f"⚠️  {_repo}: prune 失败: {err}")
            _total += 1
        except (subprocess.SubprocessError, OSError) as e:
            console.warning(f"⚠️  {_repo}: 清理异常: {e}")
    if not _total:
        console.print("未发现可清理的仓库")
    else:
        console.print(f"已处理 {_total} 个仓库（worktree prune）")


def clean_task_dirs(tasks: list) -> dict:
    """删除指定任务目录 + 关联 repo 的 worktree prune + 任务 tag 清理。

    cmd_clean（交互确认后）与 Web 写端点（M2/R7）共用的执行层；
    返回 {"removed": [task_dir_name...], "repos_pruned": n}。
    """
    import shutil as _shutil

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
    removed = []
    for t in tasks:
        _shutil.rmtree(t, ignore_errors=True)
        removed.append(t.name)
    for repo_path, task_ids in repo_task_ids.items():
        subprocess.run(["git", "worktree", "prune"], cwd=repo_path, capture_output=True)
        for tid in task_ids:
            tag_list = subprocess.run(["git", "tag", "-l", f"{tid}/*"], cwd=repo_path, capture_output=True, text=True)
            for tag in tag_list.stdout.strip().split("\n"):
                if tag:
                    subprocess.run(["git", "tag", "-d", tag], cwd=repo_path, capture_output=True)
    return {"removed": removed, "repos_pruned": len(repo_task_ids)}


def _sync_checked_out_worktree(repo_path: str, target_branch: str,
                               merge_start_clean: bool = False) -> None:
    """merge 后工作区同步（update-ref 推进分支不更新已 checkout 的工作区）。

    - 当前 checkout 非 target 分支 → 无操作（checkout 回 target 时 git 自动同步）
    - 当前 checkout 在 target：
        - merge 开始前工作区干净 → `git reset --hard HEAD` 同步（status 中 M/D
          均为 update-ref 推进引入的 index/HEAD 失配，reset 安全）
        - merge 开始前工作区脏 → 仅警告（不自动 reset，防丢改动）
    """
    try:
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        cur_branch = current.stdout.strip() if current.returncode == 0 else ""
    except Exception:
        return
    if not cur_branch or cur_branch != target_branch:
        return
    if not merge_start_clean:
        console.warning(
            f"当前 checkout 在 {target_branch} 且 merge 前工作区有未提交改动，未自动同步。"
            "请手动执行 `git reset --hard`（确认不丢改动后）或 `git checkout -f` "
            "同步工作区，避免后续提交误删 merge 产物。"
        )
        return
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    console.success(f"工作区已同步到 {target_branch}（reset --hard）")


def cmd_clean(args=None) -> None:
    import time as _time

    # ISSUE-38：--fixture-worktrees → 只清理 fixture 仓库的失效 worktree 注册，
    # 不删任务目录（一次性兜底清理历史残留；bench 已内建任务后自动 prune）。
    if getattr(args, "fixture_worktrees", False):
        _prune_fixture_repo_worktrees()
        return

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
        result = clean_task_dirs(tasks)
        console.print(f"已清理 {len(result['removed'])} 个任务")
    else:
        console.print("已取消")

def cmd_skills(args=None) -> None:
    """列出或查看 Skill。agent_go skills [list | show <name> | resolve <name>]。"""
    from .skills import get_skill_full, resolve_skill_chain

    sub = getattr(args, "skills_subcommand", None) if args else None

    # resolve <name>：追踪 symlink 解析链（多级 skill 目录诊断）
    if sub == "resolve":
        name = args.name
        info = resolve_skill_chain(name)
        if not info:
            console.error(f"Skill 不存在: {name}。可用: agent_go skills list")
            return
        if getattr(args, "json_mode", False):
            console.print(json.dumps(info, indent=2, ensure_ascii=False))
            return
        console.print(f"\n🔗 Skill 解析链: {info['name']}")
        console.sep("─", 55)
        for i, hop in enumerate(info["dir_chain"]):
            icon = "→" if i < len(info["dir_chain"]) - 1 else "✔"
            console.print(f"  {icon} {hop}")
        console.sep("─", 55)
        console.print(f"📄 最终解析: {info['resolved']}")
        console.print(f"✅ 文件存在: {'是' if info['exists'] else '否（🔴 断裂）'}")
        console.print(f"🔗 是否为 symlink: {'是' if info['is_symlink'] else '否'}")
        if not info["exists"]:
            console.warning(f"Skill '{name}' 的解析目标不存在——请检查 symlink 是否断裂。")
        return

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
        sys.exit(EX_SYSTEM)

    console.print("\n📊 扫描结果：")
    for sub in result.get("recovered", []):
        marker = "🆕" if sub.get("recovered") and sub.get("recovered_at") else "📦"
        orphan = " (orphan reset)" if sub.get("orphan_reset") else ""
        verify_str = f"verify_ok={sub.get('verify_ok')}" if sub.get("verify_ok") is not None else "verify=unknown"
        console.print(f"   {marker} {sub['subtask_id']:8s}: status={sub['status']:12s}  "
                      f"commits={sub.get('commits', 0)}  {verify_str}{orphan}")

    overall = result.get("overall_status", "unknown")
    console.print(f"\n   overall_status: {overall}")
    if dry_run:
        console.print("   (dry-run，未写入 meta.json)")
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


def cmd_governance(args) -> None:
    """M1.4: 展示任务级 traceability_matrix 与 architecture_compliance 摘要。"""
    from .console import _LazyConsole
    from .governance import build_traceability_matrix

    _con = _LazyConsole()
    task_id = args.task_id
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        _con.error(f"任务不存在: {task_id}")
        return

    try:
        with open(task_dir / "meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as _ge:
        _con.error(f"读取 meta.json 失败: {_ge}")
        return

    report = build_traceability_matrix(meta)

    if getattr(args, "json_mode", False):
        _con.force(json.dumps(report, indent=2, ensure_ascii=False))
        return

    assessment = report["assessment"]
    status_icon = {"complete": "✅", "incomplete": "⚠️", "no_spec_ids": "ℹ️"}.get(
        assessment["status"], "❓")

    _con.sep("─", 60)
    _con.title(f"📋 治理报告: {task_id}")
    _con.print(f"  追踪状态: {status_icon} {assessment['status']}")
    if assessment["requirement_count"]:
        _con.print(f"  Spec 需求/验收 ID 数: {assessment['requirement_count']}")
        if assessment["missing_requirement_ids"]:
            _con.warning(f"  未覆盖: {', '.join(assessment['missing_requirement_ids'])}")
        if assessment["unmapped_subtask_ids"]:
            _con.warning(f"  无需求映射的子任务: {', '.join(assessment['unmapped_subtask_ids'])}")
    _con.print(f"  验证覆盖: {assessment['verification_coverage']:.0%}  "
               f"  交付记录: {'✓' if assessment['delivery_coverage'] else '✗'}")

    arch = report["architecture_compliance"]
    if arch["reviewed"]:
        _con.print(f"  架构审查: {arch['decision']} — {arch['summary']}")
        for c in arch["constraints"]:
            _con.print(f"    · 约束: {c}")
        for r in arch["risks"]:
            _con.warning(f"    · 风险: {r}")
    else:
        _con.print(f"  架构审查: {arch['summary']}")

    _con.sep("─", 60)
    _con.print("  Requirements → Subtasks:")
    if report["traceability"]["requirements"]:
        for r in report["traceability"]["requirements"]:
            _con.print(f"    {r['id']} → {', '.join(r['subtasks'])}")
    else:
        _con.print("    (无 requirement → subtask 映射)")

    _con.sep("─", 60)
    for st in report["traceability"]["subtasks"]:
        vmark = "✓" if st["verification_passed"] else "·"
        reqs = ",".join(st["requirements"]) or "-"
        _con.print(f"    {st['id']:<8} [{vmark}] reqs={reqs}  {st['title'][:50]}")

    for issue in assessment["issues"]:
        _con.warning(f"  ⚠️ {issue}")
    _con.sep("─", 60)


def cmd_decision(args) -> None:
    """M6.2: 决策记录（decision log）查看。"""
    from .decision_log import list_decisions
    sub = getattr(args, "decision_subcommand", None)
    if sub == "log":
        records = list_decisions(limit=100)
        if not records:
            console.print("暂无决策记录（~/.agent_go/decision_log.jsonl）")
            return
        console.print(f"\n📋 决策记录（共 {len(records)} 条，最新在前）\n")
        for r in records:
            ts = r.get("ts", "")[:19].replace("T", " ")
            change = r.get("change", "")[:80]
            source = r.get("source", "")
            confirmer = r.get("confirmer", "")
            console.print(f"  [{ts}] {change}")
            if source or confirmer:
                console.print(f"      来源: {source or '-'} | 确认: {confirmer or '-'}")
            if r.get("goal"):
                console.print(f"      目标: {r.get('goal', '')[:70]}")
            if r.get("evidence_refs"):
                console.print(f"      证据: {', '.join(r['evidence_refs'][:3])}")
            if r.get("expected_impact"):
                console.print(f"      预期: {r.get('expected_impact', '')[:70]}")
            console.print()
    else:
        console.print("Usage: agent_go decision log")


def cmd_deviation(args) -> None:
    """M2.5: 展示 Spec/架构/验收偏差记录与聚合。"""
    from .console import _LazyConsole
    from .deviation import aggregate_deviations, load, load_all

    _con = _LazyConsole()
    task_id = getattr(args, "task_id", None)
    if task_id:
        task_dir = AGENT_GO_DIR / task_id
        if not task_dir.exists():
            _con.error(f"任务不存在: {task_id}")
            return
        events = load(task_dir)
        scope_title = f"偏差记录: {task_id}"
    else:
        events = load_all(AGENT_GO_DIR)
        scope_title = f"偏差记录: 全部任务（{len(events)} 条）"

    agg = aggregate_deviations(events)

    if getattr(args, "json_mode", False):
        _con.force(json.dumps({
            "task_id": task_id,
            "aggregate": agg,
            "events": [e.__dict__ for e in events],
        }, indent=2, ensure_ascii=False))
        return

    if not events:
        _con.print(f"{scope_title} — 无偏差记录")
        return

    _con.sep("─", 60)
    _con.title(f"📊 {scope_title}")
    _con.print(f"  总数: {agg['total']}  需人工决策: {agg['require_approval']}  "
               f"已处理: {agg['resolved']}  待回写 Spec: {agg['spec_rewrite_pending']}")
    if agg["by_type"]:
        _con.print(f"  类型分布: {', '.join(f'{k}={v}' for k, v in sorted(agg['by_type'].items()))}")
    if agg["by_root_cause"]:
        _con.print(f"  根因分布: {', '.join(f'{k}={v}' for k, v in sorted(agg['by_root_cause'].items()))}")
    if agg["by_failure_class"]:
        _con.print(f"  失败类分布: {', '.join(f'{k}={v}' for k, v in sorted(agg['by_failure_class'].items()))}")
    _con.sep("─", 60)
    for e in events:
        flag = "🔴" if e.requires_approval else "⚪"
        _con.print(f"  {flag} {e.subtask_id:<8} [{e.deviation_type}] {e.summary}")
        if e.evidence:
            _con.print(f"      证据: {e.evidence[:120]}")
        if e.human_decision:
            _con.print(f"      人工决策: {e.human_decision}")
    _con.sep("─", 60)


def cmd_problems(args=None) -> None:
    """M5 收尾：展示全局 Problem 实体（B4/H3——跨任务失败记忆的查看入口）。

    功能：
      - 缺省：列出全部 Problem（按 occurrence_count 降序）
      - --aggregate：聚合分析（total/状态分布/复发数/top 模式）
      - --only <id>：单个 Problem 详情（生命周期/历史解法/复发重开）
      - --json：机器可读
    """
    from .console import _LazyConsole
    from .problems import PROBLEM_STATES, aggregate, load

    _con = _LazyConsole()
    problems_path = AGENT_GO_DIR / "problems.jsonl"
    problems = load(problems_path)
    as_json = bool(getattr(args, "json_mode", False))

    if as_json:
        _con.force(json.dumps({
            "problems": [p.__dict__ for p in problems],
            "aggregate": aggregate(problems),
        }, indent=2, ensure_ascii=False))
        return

    only_id = getattr(args, "only", "") or ""
    if only_id:
        prob = next((p for p in problems if p.id == only_id), None)
        if not prob:
            _con.error(f"Problem 不存在: {only_id}")
            return
        _con.sep("─", 60)
        _con.title(f"🧠 Problem: {prob.id}")
        _con.print(f"  模式: {prob.failure_pattern}   类别: {prob.failure_class or '-'}")
        _con.print(f"  状态: {prob.status}   出现次数: {prob.occurrence_count}"
                   f"   半衰期: {prob.stale_after_days}d"
                   f"{'（已休眠）' if prob.is_dormant() else ''}")
        _con.print(f"  首次: {prob.first_seen_at[:19]}   最近: {prob.last_seen_at[:19]}")
        if prob.task_id:
            _con.print(f"  首现任务: {prob.task_id}{' / ' + prob.subtask_id if prob.subtask_id else ''}")
        if prob.summary:
            _con.print(f"  摘要: {prob.summary}")
        if prob.evidence:
            _con.print(f"  证据: {prob.evidence[:200]}")
        if prob.root_cause:
            _con.print(f"  根因: {prob.root_cause}")
        if prob.resolution_summary:
            _con.print(f"  历史解法: {prob.resolution_summary}")
            _con.print(f"  解决于: {prob.resolved_by}")
        return

    if getattr(args, "aggregate", False):
        agg = aggregate(problems)
        _con.sep("─", 60)
        _con.title(f"📊 Problem 聚合（全局 {agg['total']} 个）")
        _con.print(f"  状态分布: {', '.join(str(s) + '=' + str(agg['status_counts'].get(s, 0)) for s in PROBLEM_STATES)}")
        _con.print(f"  休眠中: {agg['dormant_count']}   复发过: {agg['recurrence_count']}"
                   f"   总出现: {agg['total_occurrences']}")
        if agg["top_patterns"]:
            _con.print("  Top 失败模式:")
            for pat, cnt in agg["top_patterns"]:
                _con.print(f"    {pat}: {cnt} 次")
        return

    if not problems:
        _con.print("暂无 Problem 记录（~/.agent_go/problems.jsonl 为空）——失败会在执行时自动录制。")
        return
    _con.sep("─", 60)
    _con.title(f"🧠 全局 Problem（{len(problems)} 个，按出现次数降序）")
    for p in sorted(problems, key=lambda x: -x.occurrence_count):
        icon = {"opened": "🟠", "analyzed": "🔵", "resolved": "🟢"}.get(p.status, "⚪")
        dormant = "（休眠）" if p.is_dormant() else ""
        _con.print(f"{icon} {p.id}  {p.failure_pattern}  ×{p.occurrence_count}  [{p.status}]{dormant}"
                   f"{'  💡' + p.resolution_summary[:40] if p.resolution_summary else ''}")


def cmd_trust(args=None) -> None:
    """#49 信任指标（阶段 D 自治决策放行门）查看入口。

    三指标（metrics.compute_trust_metrics）：
      审查后修改率 = (rejected + changes_requested) / 有 review 决策的任务数（方向：下降）
      复发可见率   = 失败子任务带 problem_id 的比例（方向：上升）
      盲区命中率   = 盲区标注项最终真出问题的比例（目标区间：50%~90%，防狼来了/过保守）

    默认只统计真实任务（repo 非 eval_suite/fixture）；--all 包含 bench 任务。
    """
    from .console import _LazyConsole
    from .metrics import compute_post_delivery_rework, compute_trust_metrics

    _con = _LazyConsole()
    window = int(getattr(args, "recent_window", 30) or 0)
    task_dirs = sorted(d for d in AGENT_GO_DIR.glob("task-*") if (d / "meta.json").exists())
    include_bench = bool(getattr(args, "include_bench", False))
    if not include_bench:
        real_dirs = []
        for d in task_dirs:
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            repo = str(meta.get("repo", "") or meta.get("repo_path", ""))
            if "fixture" not in repo and "eval_suite" not in repo:
                real_dirs.append(d)
        task_dirs = real_dirs

    r = compute_trust_metrics(task_dirs, recent_window=window)
    rework = compute_post_delivery_rework(task_dirs, recent_window=window)
    if bool(getattr(args, "json_mode", False)):
        _con.force(json.dumps({"scope": "all" if include_bench else "real",
                               "task_count": len(task_dirs),
                               "recent_window": window,
                               **r,
                               "post_delivery_rework": rework},
                              indent=2, ensure_ascii=False))
        return

    def _pct(v):
        return f"{v * 100:.1f}%" if v is not None else "无数据"

    _con.sep("─", 62)
    _win = "全部" if not window else f"最近 {window}"
    _con.title(f"🛡 信任指标（{'全部任务' if include_bench else '真实任务'} {len(task_dirs)} 个，{_win}）")
    _con.print(f"  审查后修改率: {_pct(r['review_modification_rate'])}"
               f"  （显式 review，{r['reviewed_tasks']} 个决策）")
    _con.print(f"  交付后返工率: {_pct(rework['post_delivery_rework_rate'])}"
               f"  （自动信号，{rework['reworked_tasks']}/{rework['rework_eligible_tasks']} 个交付任务 "
               f"{rework['window_days']}d 内被返工；放行方向：下降，提案 ≤20%→10%）")
    _con.print(f"  复发可见率:   {_pct(r['recurrence_visibility_rate'])}"
               f"  （{r['failed_subtasks']} 个失败子任务；放行方向：上升，提案 ≥80%）")
    _con.print(f"  盲区命中率:   {_pct(r['blind_spot_hit_rate'])}"
               f"  （{r['blind_spot_hits']}/{r.get('blind_spot_judged', 0)} 已判定标注项命中"
               f"{('，' + str(r['blind_spot_pending']) + ' 条挂起（观察期未满）') if r.get('blind_spot_pending') else ''}"
               f"{('，' + str(r['blind_spot_na']) + ' 条不可观察（repo 已删/无关联文件，已排除）') if r.get('blind_spot_na') else ''}"
               f"；目标区间 50%~90%）")
    by_signal = r.get("blind_spot_by_signal") or {}
    for sig, v in by_signal.items():
        if v.get("items") or v.get("na"):
            _pend = f"，{v['pending']} 挂起" if v.get("pending") else ""
            _na = f"，{v['na']} 不可观察" if v.get("na") else ""
            _con.print(f"    - {sig}: {v['hits']}/{v['items'] - v.get('pending', 0)} 命中{_pend}{_na}")
    if rework["rework_eligible_tasks"] < 10 or r.get("blind_spot_judged", 0) < 20:
        _con.print("  ⚠️ 样本不足（放行评估需 ≥10 个有效交付任务 / ≥20 条已判定盲区标注），"
                   "指标仅供参考——见 docs/design/trust-metrics-baseline-2026-08-21.md")


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
        _atomic_write_config(config, CONFIG_PATH)
        console.success("角色感知路由已启用")
        _print_router_config(router_cfg)
        return

    if subcmd == "disable":
        router_cfg["enabled"] = False
        _atomic_write_config(config, CONFIG_PATH)
        console.success("角色感知路由已禁用（回退到 plan_api）")
        return

    if subcmd == "recommend":
        _cmd_router_recommend(args)
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

        _atomic_write_config(config, CONFIG_PATH)
        console.success(f"{role} 角色已配置")
        _print_role_config(role, router_cfg)
        return

    console.print(f"未知操作: {subcmd}。可用: show | enable | disable | set-role | recommend")


def _cmd_router_recommend(args) -> None:
    """router recommend：基于 bench 结果推荐完整路由（roles + worker_models + mapping）。

    P1（docs/design/model-evaluation-and-tiering.md §5）：闭环到配置——读
    analyze_model_productivity 结果 → build_recommendation（roles 铁律：planner
    不降级、reviewer 不同源；worker_models 难度槽）→ dry-run 展示 / --apply
    一次原子写 config.json 的 router.roles + worker_models。
    provider 推断失败的角色跳过写入（advisory，不静默写坏配置）。
    """
    from .bench import analyze_model_productivity, build_recommendation, apply_recommendation
    from .config import CONFIG_PATH

    results_path = Path(getattr(args, "results", "eval_suite/results.jsonl") or "eval_suite/results.jsonl")
    data = analyze_model_productivity(results_path)
    if "error" in data:
        console.warning(f"{data['error']} → 先跑 agent_go eval bench")
        return

    rec = build_recommendation(data["models"])
    console.print(f"\n🔀 角色路由推荐（基于 {data['total_runs']} 次执行）")
    console.print("─" * 88)
    for _role in ("planner", "worker", "reviewer"):
        _p = (rec["roles"] or {}).get(_role)
        if not _p:
            console.print(f"  {_role:<9} → （无合格候选）")
            continue
        _low = " ⚠小样本" if _p.get("low_confidence") else ""
        _fb = _p.get("fallback")
        _fb_str = (f" → fallback: {_fb['provider']}:{_fb['model']}" if _fb else "（不降级）")
        _dpp = _p.get("dollar_per_pass")
        console.print(f"  {_role:<9} → {_p.get('provider')}:{_p['model']}{_fb_str}{_low}")
        console.print(f"             (通过率 {_p.get('avg_pass_rate', 0):.0%}, "
                      f"$/pass ${_dpp or 0:.4f}; {_p.get('reason')})")
    _wm = rec["worker_models"]
    console.print("  " + "─" * 84)
    for _slot in ("hard", "medium", "easy"):
        _p = _wm.get(_slot)
        if not _p:
            console.print(f"  {_slot:<7} → （无合格候选，留空）")
            continue
        _dpp = _p["dollar_per_pass"]
        console.print(f"  {_slot:<7} → {_p['model']}  (通过率 {_p['avg_pass_rate']:.0%}, "
                      f"$/pass ${_dpp or 0:.4f}, {_p['criterion']})")
    console.print("─" * 88)
    _note = rec.get("note")
    if _note:
        console.warning(_note)

    if not getattr(args, "apply", False):
        console.print("（dry-run，未写入。用 --apply 一次写入 config.json 的 router.roles + worker_models）")
        return

    # CR-P1-1：小样本（n<5 低置信）不自动路由——--apply 默认跳过，--force 覆盖。
    _force = getattr(args, "force", False)
    _roles = dict(rec.get("roles") or {})
    if not _force:
        for _role in ("planner", "worker", "reviewer"):
            _p = _roles.get(_role)
            if _p and _p.get("low_confidence"):
                console.warning(f"{_role}: 低置信（n<5），--apply 跳过（--force 覆盖）")
                _roles[_role] = None
    _rec2 = dict(rec)
    _rec2["roles"] = _roles
    _skipped = apply_recommendation(_rec2, apply_roles=True, apply_worker_models=True)
    if _skipped:
        for _role in _skipped:
            console.warning(f"{_role}: provider 无法推断或已跳过，未写入（用 set-role 手动配置）")
    console.success(f"已写入 {CONFIG_PATH} 的 router.roles + worker_models；"
                    f"router.enabled 请用 'agent_go router enable' 启用")
    # M6.2：决策落 log（router recommend --apply 写回配置的关键决策）
    try:
        from .decision_log import record_decision
        record_decision(
            change="router recommend --apply：写入 router.roles + worker_models",
            evidence_refs=[str(results_path)],
            confirmer="cli",
            source="router recommend --apply",
        )
    except Exception:
        pass


def _atomic_write_config(config: dict, config_path) -> None:
    """原子写 config.json：tmp + rename，避免写中断导致配置损坏。"""
    _tmp = config_path.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    _tmp.replace(config_path)


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


def cmd_models(args) -> None:
    """models 子命令：模型池管理（P3.2）——list 查看 / add 注册（① Model Registry）。"""
    from .models_registry import list_models, load_registry
    from .config import AGENT_GO_DIR

    sub = getattr(args, "models_subcommand", None)
    if sub == "list":
        models = list_models()
        if not models:
            console.print("模型池为空（~/.agent_go/models.json 不存在或无模型）")
            return
        console.print(f"\n🧠 模型池（{len(models)} 个模型，~/.agent_go/models.json）")
        console.sep("─", 100)
        for m in models:
            thinking = ""
            if m.thinking.required:
                thinking = f"thinking:{m.thinking.format}"
            elif m.thinking.format:
                thinking = "thinking:optional"
            json_c = m.output.json_compliance
            tco = f"TCO ${m.cost.tco_per_call}" if m.cost.tco_per_call else ""
            tags = ",".join(m.quality_tags) if m.quality_tags else ""
            console.print(f"{m.id:<22} [{m.provider}] {m.base_url[:38]}")
            parts = [p for p in (thinking, f"json:{json_c}", tco, tags) if p]
            if parts:
                console.print(f"{'':22}  {' | '.join(parts)}")
        console.sep("─", 100)
        return
    if sub == "add":
        registry_path = AGENT_GO_DIR / "models.json"
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        entry: dict = {
            "provider": args.provider,
            "endpoint": {"base_url": args.base_url, "key_ref": args.key_ref},
            "reasoning": {"thinking": {"format": args.provider,
                                       "required": bool(args.thinking)}},
            "output": {"json_compliance": "loose" if args.json_loose else "strict",
                       "needs_response_format": bool(args.json_loose)},
            "cost": {"pricing": args.model_id, "tco_per_call": float(args.tco or 0)},
            "quality_tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        }
        data[args.model_id] = entry
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"✅ 模型已注册: {args.model_id} → {registry_path}")
        console.print(f"   {args.provider} | {args.base_url[:40]} | thinking={'required' if args.thinking else 'optional'}")
        # 强制重载 registry 缓存使新模型立即可用
        load_registry(force_reload=True)
        return
    console.print("Usage: agent_go models <list|add>")


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


def _stale_liveness_ts(task_dir: Path, meta_path: Path) -> float:
    """判活时间戳：优先 heartbeat 文件 mtime，回退 meta.json mtime。

    heartbeat 文件由 _run_pipeline 的心跳线程在任务运行期间周期刷新（HEARTBEAT_INTERVAL）。
    仅靠 meta.json mtime 会在"合法长任务（>1h）仍在运行"时误判为 stale_aborted；
    心跳冻结（进程被 SIGKILL）才是可靠的死亡信号。
    """
    hb_path = task_dir / "heartbeat"
    if hb_path.exists():
        try:
            return hb_path.stat().st_mtime
        except OSError:
            pass
    return meta_path.stat().st_mtime


def _cleanup_stale_tasks(max_age_hours: int = 1) -> int:
    """P3 Layer 5：清理卡死的 running task。

    场景：agent_go run 被 SIGKILL 后 meta.json 永远 status=running，
    下次启动时这些 task 会阻塞 bench/recover。

    策略：扫描 ~/.agent_go/task-* 中所有 meta.json：
    - status=running 且判活时间戳（heartbeat 或 meta.json mtime）> max_age_hours → 标记为 stale_aborted
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
            mtime = _stale_liveness_ts(task_dir, meta_path)
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
        elif args.command == "report":
            cmd_report(args)
        elif args.command == "status":
            cmd_status(args)
        elif args.command == "config":
            cmd_config(args)
        elif args.command == "kanban":
            sub = getattr(args, "kanban_subcommand", None)
            if sub == "import-spec":
                cmd_kanban_import_spec(args)
            else:
                console.print("Usage: agent_go kanban <import-spec> [args]")
        elif args.command == "spec":
            cmd_spec(args)
        elif args.command == "clean":
            cmd_clean(args)
        elif args.command == "pr":
            cmd_pr(args)
        elif args.command == "merge":
            cmd_merge(args)
        elif args.command == "skills":
            cmd_skills(args)
        elif args.command == "agents":
            cmd_agents()
        elif args.command == "models":
            cmd_models(args)
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
        elif args.command == "migrate":
            from .metadata_migration import repair_all_tasks
            report = repair_all_tasks(apply=args.apply, backup_dir=args.backup_dir or None)
            console.print(json.dumps(report, ensure_ascii=False, indent=2))
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
        elif args.command == "governance":
            cmd_governance(args)
        elif args.command == "deviation":
            cmd_deviation(args)
        elif args.command == "decision":
            cmd_decision(args)
        elif args.command == "problems":
            cmd_problems(args)
        elif args.command == "trust":
            cmd_trust(args)
        elif args.command == "mcp":
            cmd_mcp(args)
        elif args.command == "web":
            cmd_web(args)
    except KeyboardInterrupt:
        console.print("\n\n⏹️  用户中断（Ctrl+C）")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
