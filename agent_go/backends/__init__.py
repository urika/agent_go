"""agent_go Worker Backend 包。

提供标准 BaseBackend 接口、BackendRegistry 注册表，
以及 Claude Code / AgentLoop / pi / opencode 的实现。
"""

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry, resolve_backend_name
from .dispatch import repair_timeout, run_repair

# 导入具体实现以完成注册表登记（无副作用，仅类定义加载）。
from .claude_backend import ClaudeBackend
from .agent_loop_backend import AgentLoopBackend
from .pi_backend import PiBackend
from .opencode_backend import OpenCodeBackend

__all__ = [
    "BackendContext",
    "BaseBackend",
    "SubtaskResult",
    "BackendRegistry",
    "resolve_backend_name",
    "repair_timeout",
    "run_repair",
    "ClaudeBackend",
    "AgentLoopBackend",
    "PiBackend",
    "OpenCodeBackend",
]
