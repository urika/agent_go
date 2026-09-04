"""OpenCode Backend — 通过 opencode CLI（opencode run --format json --auto）执行子任务。

B6（阶段十三）：继 pi 之后第四种 worker backend，契约调研见
docs/design/stage13-b6-opencode-assessment.md。

输出契约（opencode 1.18 实测）：
- stdout 为 NDJSON 事件流，每行一个 JSON 事件，顶层字段 type/sessionID/timestamp/part；
- ``step_finish`` 事件 part 携带 reason（stop=完成 / tool-calls=继续）、
  tokens{input,output,reasoning,cache{read,write}} 与 cost（美元，免费模型为 0）；
- ``tool_use`` 事件 part 携带 tool 工具名与 state.status（completed/error）；
- ``text`` 事件 part.text 为最终回复文本；
- 进程退出码 0 表示流程结束（不代表验证通过，验证仍归 executor）。

与 pi 的关键差异：
- **必须带 --auto**：否则 run 无限挂起等待权限批准（实测 5 分钟零输出零退出）；
- 额度耗尽（Go 套餐月限额）时 opencode 重试 3 次后静默挂起，不退出不报错——
  只能依赖 BackendContext.hard_timeout 兜底，务必配置；
- 事件流不携带 model/provider 信息，计量的 actual_model 取 ctx.routed_model。

仅支持 headless：resolve_backend_name 保证交互模式不路由到 opencode。
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

# opencode 内置只读 agent（plan 禁用 edit/write/bash 变更类工具）。
OPENCODE_READONLY_AGENT = "plan"


def _parse_events(lines: list) -> dict:
    """解析 opencode NDJSON 事件流，聚合最终结果与用量。

    对无法解析的行容错跳过（opencode 可能在 stdout 混入非 JSON 输出）。
    """
    final_text = ""
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    tool_calls = 0
    tool_errors = 0
    tool_stats: dict[str, int] = {}
    session_id = ""
    saw_error = False
    error_message = ""

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            continue
        etype = event.get("type", "")
        part = event.get("part", {}) or {}

        if event.get("sessionID") and not session_id:
            session_id = event["sessionID"]

        if etype == "step_finish":
            tokens = part.get("tokens", {}) or {}
            cache = tokens.get("cache", {}) or {}
            prompt_tokens += tokens.get("input", 0) + cache.get("read", 0)
            completion_tokens += tokens.get("output", 0)
            cost_usd += part.get("cost", 0.0) or 0.0
        elif etype == "tool_use":
            name = part.get("tool", "")
            tool_calls += 1
            tool_stats[name] = tool_stats.get(name, 0) + 1
            if (part.get("state") or {}).get("status") == "error":
                tool_errors += 1
        elif etype == "text":
            text = part.get("text", "")
            if text:
                final_text = text
        elif etype == "error":
            # 防御性处理：当前实测未见 error 事件，若未来版本输出则捕获
            saw_error = True
            error_message = str(part.get("message") or event.get("message") or "error event")

    return {
        "final_text": final_text,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": round(cost_usd, 6),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_stats": tool_stats,
        "session_id": session_id,
        "saw_error": saw_error,
        "error_message": error_message,
    }


@BackendRegistry.register
class OpenCodeBackend(BaseBackend):
    """通过 opencode CLI 执行子任务（B6）。

    - 命令：opencode run --format json --auto --pure [-m provider/model] <task_md>
    - readonly 模式（ctx.extra.readonly）切换内置只读 agent（--agent plan）；
    - ctx.routed_model 透传 -m（Zen 免费模型形如 opencode/mimo-v2.5-free，
      Go 套餐形如 opencode-go/qwen3.8-flash）；
    - Go 额度耗尽静默挂起由 ctx.hard_timeout 兜底（kill_reason=hard_timeout）。
    """

    name = "opencode"

    def run(self, ctx: BackendContext) -> SubtaskResult:
        start = time.time()
        oc_bin = shutil.which("opencode")
        if not oc_bin:
            ctx.logger.error("[OpenCodeBackend] opencode CLI 未安装（见 opencode.ai）")
            return SubtaskResult(
                returncode=127,
                stderr="opencode CLI not found on PATH",
                sandbox_type="opencode",
                backend_time=time.time() - start,
            )

        # --auto 必须：headless 下无交互批准权限的途径，不带会无限挂起；
        # --pure 禁外部插件，保证 bench 口径不受用户环境影响。
        cmd = [oc_bin, "run", "--format", "json", "--auto", "--pure"]
        if ctx.extra.get("readonly"):
            cmd += ["--agent", OPENCODE_READONLY_AGENT]
        if ctx.routed_model:
            cmd += ["-m", ctx.routed_model]
        cmd.append(ctx.task_md)

        ctx.logger.info(f"[OpenCodeBackend] {ctx.sub_id} 启动 opencode (model={ctx.routed_model or 'default'})")

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
            # Go 套餐额度耗尽等静默挂起场景走这里兜底
            ctx.logger.error(f"[OpenCodeBackend] {ctx.sub_id} 硬超时 ({ctx.hard_timeout}s)，强制终止")
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

        # 零产出（无 tokens/工具调用/最终文本）的退出 0 必须显式映射为失败，
        # 否则 executor 会把「什么都没做」当成功结果进入验证循环，
        # failure_class 被误记为 verification_failure。（同 pi 的处理逻辑）
        zero_work = (
            parsed["prompt_tokens"] == 0
            and parsed["completion_tokens"] == 0
            and parsed["tool_calls"] == 0
            and not parsed["final_text"]
        )
        if zero_work and proc.returncode == 0:
            stderr = (stderr or "") + (
                f"\nopencode error: {parsed['error_message'] or 'zero output (no tokens/tools/text)'}"
            )

        ctx.logger.info(
            f"[OpenCodeBackend] {ctx.sub_id} 结束: rc={proc.returncode}, "
            f"{parsed['tool_calls']} tool_calls ({parsed['tool_errors']} errors), "
            f"{parsed['prompt_tokens']}+{parsed['completion_tokens']} tokens, "
            f"${parsed['cost_usd']:.4f}, {elapsed:.0f}s"
        )
        if parsed["saw_error"]:
            ctx.logger.warning(f"[OpenCodeBackend] {ctx.sub_id} 事件流中出现 error 事件: {parsed['error_message']}")

        self._meter(ctx, parsed, elapsed, proc.returncode, kill_reason)

        if ctx.progress:
            _console.print(f"  ➜ {ctx.sub_id}: ✓ opencode backend {elapsed:.0f}s")

        returncode = proc.returncode if proc.returncode is not None else 1
        if zero_work and returncode == 0:
            returncode = 1
        return SubtaskResult(
            returncode=returncode,
            stdout=parsed["final_text"] or (stdout or ""),
            stderr=stderr or "",
            sandbox_type="opencode",
            backend_time=elapsed,
            kill_reason=kill_reason,
        )

    def _meter(self, ctx: BackendContext, parsed: dict, elapsed: float,
               returncode: int, kill_reason) -> None:
        """写一条聚合计量事件（step_finish 报告了精确 cost，直接采用；免费模型为 0）。"""
        metering_path = (ctx.config or {}).get("_metering_path", "")
        if not metering_path:
            return
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker-opencode",
            "actual_provider": "",
            "actual_model": ctx.routed_model,
            "prompt_tokens": parsed["prompt_tokens"],
            "completion_tokens": parsed["completion_tokens"],
            "cost_usd": parsed["cost_usd"],
            "latency_ms": round(elapsed * 1000, 2),
            "result": "success" if (returncode == 0 and not kill_reason) else "failure",
            "fallback_reason": kill_reason or "",
            "task_id": ctx.task_id,
            "subtask_id": ctx.sub_id,
        })
