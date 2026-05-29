"""agent_go — Plan Mode orchestration tool."""

__version__ = "2.0.0"

# Minimal top-level public API.
# For module-specific symbols, import directly:
#   from agent_go.api import call_api, generate_plan
#   from agent_go.utils import _slugify, _format_commit
#   from agent_go.ui import plan_to_subtasks, confirm_plan

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
from .config import load_config, AGENT_GO_DIR, DEFAULT_CONFIG
from .executor import run_subtask

__all__ = [
    "__version__",
    # CLI
    "main", "cmd_run", "cmd_resume", "cmd_list", "cmd_show",
    "cmd_status", "cmd_config", "cmd_clean", "cmd_pr", "cmd_review",
    # Config
    "load_config", "AGENT_GO_DIR", "DEFAULT_CONFIG",
    # Execution
    "run_subtask",
]
