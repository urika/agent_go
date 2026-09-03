"""Claude Code Backend — 封装 claude -p / greywall 交互式路径。"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry
from ..agents import get_claude_command
from ..console import _LazyConsole
from ..subtask import _run_headless

_console = _LazyConsole()


@BackendRegistry.register
class ClaudeBackend(BaseBackend):
    """通过 Claude Code CLI 执行子任务。

    行为与迁移前的 _run_claude 保持一致：
    - headless=True → claude -p（stream-json + 进度行）
    - headless=False → greywall --watch -- claude <worktree>（或原生 claude）
    """

    name = "claude"

    def run(self, ctx: BackendContext) -> SubtaskResult:
        start = time.time()

        if ctx.headless:
            return self._run_headless(ctx, start)
        return self._run_interactive(ctx, start)

    def _run_headless(self, ctx: BackendContext, start: float) -> SubtaskResult:
        agent = ctx.agent
        sub_id = ctx.sub_id
        allowed_tools = agent.claude_config.get("allowed_tools", []) if agent else []
        shared_activity = [None]
        _progress_stop = threading.Event()
        _last_activity_emit = [None]

        def _tick():
            t0 = time.time()
            while not _progress_stop.is_set():
                elapsed = int(time.time() - t0)
                act = shared_activity[0]
                if act and act.get("target"):
                    _console.print(f"\r➜ {sub_id}: {act['tool']} {act['target']}  ({elapsed}s)", end="")
                elif act:
                    _console.print(f"\r➜ {sub_id}: {act['tool']}  ({elapsed}s)", end="")
                else:
                    _console.print(f"\r➜ {sub_id}: 运行中 ({elapsed}s)", end="")
                # Bridge shared_activity to event stream (only on change)
                if act != _last_activity_emit[0]:
                    _last_activity_emit[0] = act
                    if act and act.get("target"):
                        _console.emit("subtask_activity", {
                            "sub_id": sub_id,
                            "activity": f"{act['tool']} {act['target']}",
                        })
                    elif act:
                        _console.emit("subtask_activity", {
                            "sub_id": sub_id,
                            "activity": f"{act['tool']}",
                        })
                _progress_stop.wait(5)

        # progress=False（修复类执行）时保持控制台安静：不起 ticker 线程。
        t = None
        if ctx.progress:
            t = threading.Thread(target=_tick, daemon=True)
            t.start()

        try:
            result = _run_headless(
                ctx.task_md,
                ctx.worktree,
                ctx.env,
                ctx.logger,
                sub_id,
                active_pids=ctx.active_pids,
                active_pids_lock=ctx.active_pids_lock,
                allowed_tools=allowed_tools,
                shared_activity=shared_activity,
                hard_timeout=ctx.hard_timeout,
                config=ctx.config,
            )
        finally:
            if t is not None:
                _progress_stop.set()
                t.join(timeout=2)

        elapsed = int(time.time() - start)
        act = shared_activity[0]
        if ctx.progress:
            _activity_note = f" → {act['tool']} {act['target']}" if act and act.get("target") else ""
            _console.print(f"\r➜ {sub_id}: ✓ {elapsed}s{_activity_note}" + " " * 20)

        return SubtaskResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            sandbox_type="headless",
            backend_time=time.time() - start,
            kill_reason=getattr(result, "kill_reason", None),
        )

    def _run_interactive(self, ctx: BackendContext, start: float) -> SubtaskResult:
        worktree = ctx.worktree
        env = ctx.env
        agent = ctx.agent
        greywall_bin = shutil.which("greywall")

        if agent:
            claude_cmd = get_claude_command(agent, worktree, headless=False)
        else:
            # 观察期策略同 agents.py：--watch 全放行全记录
            claude_cmd = (["greywall", "--watch", "--"] if greywall_bin else []) + ["claude", str(worktree)]

        try:
            result = subprocess.run(claude_cmd, env=env, cwd=str(worktree))
            sandbox_type = "greywatch" if greywall_bin else "native"
        except FileNotFoundError:
            _console.warning("Greywall 未安装，降级原生")
            result = subprocess.run(["claude", str(worktree)], env=env, cwd=str(worktree))
            sandbox_type = "native"

        return SubtaskResult(
            returncode=result.returncode,
            stdout="",
            stderr="",
            sandbox_type=sandbox_type,
            backend_time=time.time() - start,
            kill_reason=None,
        )
