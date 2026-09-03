"""Backend 抽象层 — 统一 Worker 执行入口。

B1（阶段十三）引入：将原本耦合在 executor.py 中的 Claude Code 执行逻辑
（_run_claude）与 AgentLoop 直接 API 逻辑抽象为可插拔 Backend。
当前仅完成接口抽象与既有两条路径的迁移，不新增 Pi / OpenCode 实现。
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional


@dataclass
class BackendContext:
    """Backend 执行所需的完整上下文。

    字段覆盖当前 Claude / AgentLoop 两条路径的公共输入；
    backend 特有数据（如 AgentLoop 的 ProviderConfig）通过 extra 透传，
    避免抽象层被特定 backend 污染。
    """

    task_md: str
    worktree: Path
    env: dict[str, str]
    headless: bool
    agent: Optional[Any] = None
    agent_type: str = "developer"
    sub_id: str = ""
    task_id: str = ""
    tag_name: str = ""
    difficulty: str = "medium"
    routed_model: str = ""
    active_pids: set = field(default_factory=set)
    active_pids_lock: Optional[threading.Lock] = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    config: dict = field(default_factory=dict)
    hard_timeout: int = 0
    # 进度展示开关：False 时 ClaudeBackend headless 路径不起 ticker 线程、
    # 不打印结束行（修复路径保持控制台安静的既有行为）。
    progress: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubtaskResult:
    """Backend 执行结果 — 兼容原 _run_claude 三元组 (result, sandbox_type, elapsed)。"""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    sandbox_type: str = ""
    backend_time: float = 0.0
    kill_reason: Optional[str] = None


class BaseBackend(ABC):
    """Worker Backend 抽象基类。

    子类需声明唯一 name（注册表键），并实现 run 方法。
    """

    name: ClassVar[str] = ""

    @abstractmethod
    def run(self, ctx: BackendContext) -> SubtaskResult:
        """执行子任务并返回 SubtaskResult。"""
        ...
