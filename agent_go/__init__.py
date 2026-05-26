"""agent_go — Plan Mode orchestration tool."""

__version__ = "2.0.0"

# Re-export all public API for backward compatibility with `from agent_go import ...`
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
)
from .skills import load_skill, load_skills, discover_skills, list_skills, render_skill_for_plan, render_skill_for_execution
from .agents import load_agent_type, list_agent_types
from .role_skill_map import load_role_skill_map, apply_rules
