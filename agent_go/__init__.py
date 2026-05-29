"""agent_go — Plan Mode orchestration tool."""

__version__ = "2.0.0"

# Re-export all public API for backward compatibility with `from agent_go import ...`
__all__ = [
    # config
    "AGENT_GO_DIR", "CONFIG_PATH", "DEFAULT_CONFIG", "DECOMPOSE_RULES",
    "load_config", "get_api_key", "setup_logger", "log_event", "safe_input",
    # utils
    "read_reference_docs", "_slugify", "_format_commit", "_detect_commit_prefix",
    "_detect_commit_scope", "_safe_append_to_file", "_is_safe_verification_command",
    "SAFE_VERIFICATION_PREFIXES", "_detect_tool_versions",
    # api
    "call_api", "generate_plan", "decompose_fallback", "get_cache_key",
    "load_cached_plan", "save_cached_plan", "list_cache_entries", "clean_expired_cache",
    # git_utils
    "analyze_project", "get_git_info", "get_resource_map",
    "_worktree_create", "_worktree_remove", "_worktree_prune", "_set_gc_auto",
    # ui
    "plan_to_md", "print_plan", "_prompt_fallback", "confirm_plan",
    "plan_to_subtasks", "print_subtasks", "confirm_subtasks", "verify_subtask",
    # subtask
    "_git_merge_upstream", "_run_headless",
    # executor & pipeline
    "run_subtask", "_run_pipeline",
    # cli
    "main", "cmd_run", "cmd_resume", "cmd_list", "cmd_show",
    "cmd_status", "cmd_config", "cmd_clean", "cmd_pr", "cmd_review",
    # skills & agents
    "load_skill", "load_skills", "discover_skills", "list_skills",
    "render_skill_for_plan", "render_skill_for_execution",
    "load_agent_type", "list_agent_types",
    # role_skill_map
    "load_role_skill_map", "apply_rules",
    # metrics & eval
    "collect_timing", "collect_change_stats", "collect_merge_result", "extract_usage",
    "analyze_quality", "analyze_performance", "aggregate_quality", "aggregate_performance", "cmd_eval",
    # tui & workflow_gen
    "cmd_status_tui", "cmd_ci",
]

from .config import (
    AGENT_GO_DIR,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    DECOMPOSE_RULES,
    load_config,
    get_api_key,
    setup_logger,
    log_event,
    safe_input,
)
from .utils import (
    read_reference_docs,
    _slugify,
    _format_commit,
    _detect_commit_prefix,
    _detect_commit_scope,
    _safe_append_to_file,
    _is_safe_verification_command,
    SAFE_VERIFICATION_PREFIXES,
    _detect_tool_versions,
)
from .api import (
    call_api,
    generate_plan,
    decompose_fallback,
    get_cache_key,
    load_cached_plan,
    save_cached_plan,
    list_cache_entries,
    clean_expired_cache,
)
from .git_utils import (
    analyze_project,
    get_git_info,
    get_resource_map,
    _worktree_create,
    _worktree_remove,
    _worktree_prune,
    _set_gc_auto,
)
from .ui import (
    plan_to_md,
    print_plan,
    _prompt_fallback,
    confirm_plan,
    plan_to_subtasks,
    print_subtasks,
    confirm_subtasks,
    verify_subtask,
)
from .subtask import (
    _git_merge_upstream,
    _run_headless,
)
from .executor import run_subtask
from .pipeline import _run_pipeline
from .cli import (
    main,
    cmd_run,
    cmd_resume,
    cmd_list,
    cmd_show,
    cmd_status,
    cmd_config,
    cmd_clean,
    cmd_pr,
    cmd_review,
)
from .skills import load_skill, load_skills, discover_skills, list_skills, render_skill_for_plan, render_skill_for_execution
from .agents import load_agent_type, list_agent_types
from .role_skill_map import load_role_skill_map, apply_rules
from .metrics import collect_timing, collect_change_stats, collect_merge_result, extract_usage
from .eval import analyze_quality, analyze_performance, aggregate_quality, aggregate_performance, cmd_eval
from .tui import cmd_status_tui
from .workflow_gen import cmd_ci
