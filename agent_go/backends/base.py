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

    @classmethod
    def available(cls) -> bool:
        """backend 在当前机器上是否可用（如对应 CLI 已安装）。

        默认 True（claude/agent_loop 无额外依赖）；需要本机 CLI/应用的 backend
        （pi/opencode/zcode）可覆盖。promo 路由（backend_promo）据此跳过不可用 backend。
        """
        return True

    @abstractmethod
    def run(self, ctx: BackendContext) -> SubtaskResult:
        """执行子任务并返回 SubtaskResult。"""
        ...

    def harvest_trajectory(self, ctx: BackendContext, result: SubtaskResult) -> list[dict]:
        """可选钩子（ADR-010 阶段 1）：采集本次执行的轨迹事件（只采集不消费）。

        契约：
        - 返回平台格式事件列表，每事件 ``{"seq", "time", "type", "data"}``；
          backend 私有格式必须在实现内完成防腐翻译，平台消费端永不直接读
          backend 原始日志格式；
        - **fail-open**——任何采集失败（日志缺失/格式漂移/解析错误）都必须
          自行降级为 warning + 返回 []，不得抛异常影响任务结果；
        - 默认返回 []（无轨迹源的 backend 无需覆盖）。
        """
        return []
