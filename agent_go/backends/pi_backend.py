"""Pi Backend — 通过 pi CLI（pi -p --mode json）执行子任务。

B3（阶段十三）PoC：验证 pi 作为第三种 worker backend 的可行性。

输出契约（pi 0.84 实测）：
- stdout 为 NDJSON 事件流，每行一个 JSON 事件；
- ``session`` 事件携带 id/cwd；``message_end``（role=assistant）携带 usage/cost/stopReason；
- ``tool_execution_start/end`` 携带 toolName/args/result/isError；
- ``agent_end`` 携带完整消息列表；``agent_settled`` 为结束标志；
- 进程退出码 0 表示流程正常结束（不代表验证通过，验证仍归 executor）。

仅支持 headless：resolve_backend_name 保证交互模式不路由到 pi。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry
from ..config import meter_event
from ..console import _LazyConsole

_console = _LazyConsole()

# pi 内置只读工具（bash 在 pi 中不是只读工具，readonly 模式下不放行）。
PI_READONLY_TOOLS = "read,grep,find,ls"

# NDJSON 中需要解析的事件类型；其余（message_update 等增量事件）跳过。
_KNOWN_TYPES = {
    "session", "agent_start", "turn_start", "turn_end", "agent_end",
    "agent_settled", "message_start", "message_end", "message_update",
    "tool_execution_start", "tool_execution_end",
}


def _assistant_text(message: dict) -> str:
    """从 pi 消息对象中提取 text 内容块。"""
    parts = []
    for block in message.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _parse_events(lines: list) -> dict:
    """解析 pi NDJSON 事件流，聚合最终结果与用量。

    对无法解析的行容错跳过（pi 可能在 stdout 混入非 JSON 输出）。
    """
    final_text = ""
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    tool_calls = 0
    tool_errors = 0
    tool_stats: dict[str, int] = {}
    session_id = ""
    actual_model = ""
    actual_provider = ""
    saw_error_stop = False

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            continue
        etype = event.get("type", "")

        if etype == "session":
            session_id = event.get("id", "")
        elif etype == "message_end":
            msg = event.get("message", {})
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage", {})
            prompt_tokens += usage.get("input", 0) + usage.get("cacheRead", 0)
            completion_tokens += usage.get("output", 0)
            cost = usage.get("cost", {})
            cost_usd += cost.get("total", 0.0) if isinstance(cost, dict) else 0.0
            if msg.get("model"):
                actual_model = msg["model"]
            if msg.get("provider"):
                actual_provider = msg["provider"]
            stop = msg.get("stopReason", "")
            if stop == "error":
                saw_error_stop = True
            # 最终回复：stopReason != toolUse 的 assistant 文本（后者只是工具调用轮）
            if stop != "toolUse":
                text = _assistant_text(msg)
                if text:
                    final_text = text
        elif etype == "tool_execution_start":
            name = event.get("toolName", "")
            tool_calls += 1
            tool_stats[name] = tool_stats.get(name, 0) + 1
        elif etype == "tool_execution_end":
            if event.get("isError"):
                tool_errors += 1

    return {
        "final_text": final_text,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_stats": tool_stats,
        "session_id": session_id,
        "actual_model": actual_model,
        "actual_provider": actual_provider,
        "saw_error_stop": saw_error_stop,
    }


@BackendRegistry.register
class PiBackend(BaseBackend):
    """通过 pi CLI 执行子任务（B3 PoC）。

    - 命令：pi -p --mode json --no-session [--tools ...] [--model X] <task_md>
    - readonly 模式（ctx.extra.readonly）仅放行 pi 只读工具；
    - ctx.routed_model 透传 --model（pi 支持 "provider/id" 与模糊匹配）。
    """

    name = "pi"

    def run(self, ctx: BackendContext) -> SubtaskResult:
        start = time.time()
        pi_bin = shutil.which("pi")
        if not pi_bin:
            ctx.logger.error("[PiBackend] pi CLI 未安装（brew install pi / 见 pi.dev）")
            return SubtaskResult(
                returncode=127,
                stderr="pi CLI not found on PATH",
                sandbox_type="pi",
                backend_time=time.time() - start,
            )

        cmd = [pi_bin, "-p", "--mode", "json", "--no-session"]
        if ctx.extra.get("readonly"):
            cmd += ["--tools", PI_READONLY_TOOLS]
        if ctx.routed_model:
            cmd += ["--model", ctx.routed_model]
        cmd.append(ctx.task_md)

        ctx.logger.info(f"[PiBackend] {ctx.sub_id} 启动 pi (model={ctx.routed_model or 'default'})")

        proc = subprocess.Popen(
            cmd,
            cwd=str(ctx.worktree),
            env=ctx.env or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ctx.active_pids_lock:
            with ctx.active_pids_lock:
                ctx.active_pids.add(proc.pid)
        else:
            ctx.active_pids.add(proc.pid)

        kill_reason = None
        try:
            timeout = ctx.hard_timeout if ctx.hard_timeout > 0 else None
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            ctx.logger.error(f"[PiBackend] {ctx.sub_id} 硬超时 ({ctx.hard_timeout}s)，强制终止")
            proc.kill()
            stdout, stderr = proc.communicate()
            kill_reason = "hard_timeout"
        finally:
            if ctx.active_pids_lock:
                with ctx.active_pids_lock:
                    ctx.active_pids.discard(proc.pid)
            else:
                ctx.active_pids.discard(proc.pid)

        elapsed = time.time() - start
        parsed = _parse_events((stdout or "").splitlines())

        ctx.logger.info(
            f"[PiBackend] {ctx.sub_id} 结束: rc={proc.returncode}, "
            f"{parsed['tool_calls']} tool_calls ({parsed['tool_errors']} errors), "
            f"{parsed['prompt_tokens']}+{parsed['completion_tokens']} tokens, "
            f"${parsed['cost_usd']:.4f}, {elapsed:.0f}s"
        )
        if parsed["saw_error_stop"]:
            ctx.logger.warning(f"[PiBackend] {ctx.sub_id} 事件流中出现 stopReason=error")

        self._meter(ctx, parsed, elapsed, proc.returncode, kill_reason)

        if ctx.progress:
            _console.print(f"  ➜ {ctx.sub_id}: ✓ pi backend {elapsed:.0f}s")

        returncode = proc.returncode if proc.returncode is not None else 1
        return SubtaskResult(
            returncode=returncode,
            stdout=parsed["final_text"] or (stdout or ""),
            stderr=stderr or "",
            sandbox_type="pi",
            backend_time=elapsed,
            kill_reason=kill_reason,
        )

    def _meter(self, ctx: BackendContext, parsed: dict, elapsed: float,
               returncode: int, kill_reason) -> None:
        """写一条聚合计量事件（pi 在事件流中报告了精确 cost，直接采用）。"""
        metering_path = (ctx.config or {}).get("_metering_path", "")
        if not metering_path:
            return
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker-pi",
            "actual_provider": parsed["actual_provider"],
            "actual_model": parsed["actual_model"] or ctx.routed_model,
            "prompt_tokens": parsed["prompt_tokens"],
            "completion_tokens": parsed["completion_tokens"],
            "cost_usd": parsed["cost_usd"],
            "latency_ms": round(elapsed * 1000, 2),
            "result": "success" if (returncode == 0 and not kill_reason) else "failure",
            "fallback_reason": kill_reason or "",
            "task_id": ctx.task_id,
            "subtask_id": ctx.sub_id,
        })
