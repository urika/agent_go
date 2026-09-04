"""ZCode Backend — 通过 ZCode 内置 CLI（zcode.cjs，ELECTRON_RUN_AS_NODE）执行子任务。

B7（阶段十三）：第五种 worker backend。独特价值：GLM Coding Plan 夜间免费活动
（23:00-09:00 北京时间，glm-5.3-flash）仅对 ZCode 本体完全免费，其他 agent
只是额度翻倍——ZCodeBackend 是该窗口的零成本通道。

输出契约（ZCode 0.16.5 实测）：
- 命令：ELECTRON_RUN_AS_NODE=1 <ZCode 二进制> <zcode.cjs> --json --mode <mode>
  --cwd <worktree> --prompt <task_md>
- stdout 为**单个 JSON 对象**（非事件流）：sessionId / response（最终回复文本）/
  usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens,...} / projection；
- 无 cost 字段（Coding Plan 套餐计费，免费窗口内为 0）；无 per-tool-call 统计；
- 退出码 0 = 成功，1 = turn 执行失败。

权限模式映射：readonly → --mode plan（内置只读档）；worker 写执行 → --mode yolo。

模型选择：zcode 0.16.5 内置 runtime 无 per-run 模型标志（--settings 仅存在于
非官方 npm 终端客户端）；模型由 ~/.zcode/cli/config.json 的 model.main 决定，
ctx.routed_model 与配置不一致时 log warning，计量按配置实际值记录。

依赖：本机安装 ZCode Desktop（闭源 Electron app）；升级可能改变契约，属于
社区已知风险（见 docs/design/stage13-b7-zcode-backend.md）。

仅支持 headless：resolve_backend_name 保证交互模式不路由到 zcode。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry
from ..config import meter_event
from ..console import _LazyConsole

_console = _LazyConsole()

# ZCode Desktop 默认安装路径（macOS）；可用环境变量 ZCODE_APP_PATH 覆盖。
ZCODE_APP_PATH = "/Applications/ZCode.app"
# 用户级 CLI 配置（provider/model 定义），routed_model 场景以此为基础生成临时 settings。
ZCODE_USER_CONFIG = Path.home() / ".zcode" / "cli" / "config.json"

# 只读权限档（plan 禁用写工具）；写执行用 yolo（--prompt 的默认档，显式写出）。
ZCODE_READONLY_MODE = "plan"
ZCODE_WRITE_MODE = "yolo"


def _zcode_command_prefix() -> tuple[list[str], str]:
    """返回 (命令前缀, 错误信息)。ZCode CLI 以 Electron node 模式运行 app 内置 bundle。"""
    app = os.environ.get("ZCODE_APP_PATH", ZCODE_APP_PATH)
    binary = Path(app) / "Contents" / "MacOS" / "ZCode"
    bundle = Path(app) / "Contents" / "Resources" / "glm" / "zcode.cjs"
    if not binary.exists() or not bundle.exists():
        return [], f"ZCode.app not found at {app}（安装 ZCode Desktop 或设置 ZCODE_APP_PATH）"
    return [str(binary), str(bundle)], ""


def _configured_model(logger) -> str:
    """读取用户 config 的 model.main（zcode 0.16.5 无 per-run 模型标志，模型由配置决定）。"""
    try:
        cfg = json.loads(ZCODE_USER_CONFIG.read_text())
        return (cfg.get("model", {}) or {}).get("main", "") or ""
    except (ValueError, OSError) as exc:
        logger.warning(f"[ZCodeBackend] 读取 {ZCODE_USER_CONFIG} 失败: {exc}")
        return ""


def _parse_output(stdout: str) -> dict:
    """解析 zcode --json 的单对象输出。容错：先整体解析，失败则截取首个 { 起再试。"""
    text = (stdout or "").strip()
    for candidate in (text, text[text.find("{"):]):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            usage = obj.get("usage", {}) or {}
            return {
                "ok": True,
                "final_text": obj.get("response", "") or "",
                "session_id": obj.get("sessionId", "") or "",
                "prompt_tokens": usage.get("inputTokens", 0) + usage.get("cacheReadTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "event_count": obj.get("eventCount", 0),
            }
    return {"ok": False, "final_text": "", "session_id": "",
            "prompt_tokens": 0, "completion_tokens": 0, "event_count": 0}


@BackendRegistry.register
class ZCodeBackend(BaseBackend):
    """通过 ZCode 内置 CLI 执行子任务（B7）。

    - 命令：ELECTRON_RUN_AS_NODE=1 <ZCode> <zcode.cjs> --json --mode <plan|yolo>
      --cwd <worktree> --prompt <task_md>
    - readonly（ctx.extra.readonly）→ --mode plan；
    - zcode 0.16.5 无 per-run 模型标志（--settings 仅存在于非官方 npm 客户端）：
      模型由 ~/.zcode/cli/config.json 的 model.main 决定；ctx.routed_model 与配置
      不一致时 log warning，计量按配置实际值记录；
    - Go/额度类挂起风险同 opencode，由 ctx.hard_timeout 兜底。
    """

    name = "zcode"

    @classmethod
    def available(cls) -> bool:
        """ZCode Desktop 已安装且用户级 CLI 配置存在。"""
        prefix, _ = _zcode_command_prefix()
        return bool(prefix) and ZCODE_USER_CONFIG.exists()

    def run(self, ctx: BackendContext) -> SubtaskResult:
        start = time.time()
        prefix, err = _zcode_command_prefix()
        if not prefix:
            ctx.logger.error(f"[ZCodeBackend] {err}")
            return SubtaskResult(
                returncode=127,
                stderr=err,
                sandbox_type="zcode",
                backend_time=time.time() - start,
            )

        mode = ZCODE_READONLY_MODE if ctx.extra.get("readonly") else ZCODE_WRITE_MODE
        actual_model = _configured_model(ctx.logger)
        if ctx.routed_model and actual_model and not actual_model.endswith(ctx.routed_model.split("/")[-1]):
            ctx.logger.warning(
                f"[ZCodeBackend] {ctx.sub_id} routed_model={ctx.routed_model} 与 zcode 配置 "
                f"model.main={actual_model} 不一致，zcode 无 per-run 模型标志，按配置执行"
            )

        cmd = prefix + ["--json", "--mode", mode,
                        "--cwd", str(ctx.worktree), "--prompt", ctx.task_md]

        ctx.logger.info(
            f"[ZCodeBackend] {ctx.sub_id} 启动 zcode "
            f"(mode={mode}, model={actual_model or 'unknown'})"
        )

        env = dict(ctx.env) if ctx.env else dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"

        proc = subprocess.Popen(
            cmd,
            cwd=str(ctx.worktree),
            env=env,
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
            ctx.logger.error(f"[ZCodeBackend] {ctx.sub_id} 硬超时 ({ctx.hard_timeout}s)，强制终止")
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
        parsed = _parse_output(stdout or "")

        # 零产出（无 tokens 且无最终文本）的退出 0 显式映射为失败（同 pi/opencode 语义）；
        # zcode 无工具调用统计，判定只能靠 tokens + response。
        zero_work = (
            parsed["prompt_tokens"] == 0
            and parsed["completion_tokens"] == 0
            and not parsed["final_text"]
        )
        if zero_work and proc.returncode == 0:
            stderr = (stderr or "") + "\nzcode error: zero output (no tokens/text)"

        ctx.logger.info(
            f"[ZCodeBackend] {ctx.sub_id} 结束: rc={proc.returncode}, "
            f"{parsed['prompt_tokens']}+{parsed['completion_tokens']} tokens, "
            f"events={parsed['event_count']}, {elapsed:.0f}s"
        )

        self._meter(ctx, parsed, elapsed, proc.returncode, kill_reason, actual_model)

        if ctx.progress:
            _console.print(f"  ➜ {ctx.sub_id}: ✓ zcode backend {elapsed:.0f}s")

        returncode = proc.returncode if proc.returncode is not None else 1
        if zero_work and returncode == 0:
            returncode = 1
        return SubtaskResult(
            returncode=returncode,
            stdout=parsed["final_text"] or (stdout or ""),
            stderr=stderr or "",
            sandbox_type="zcode",
            backend_time=elapsed,
            kill_reason=kill_reason,
        )

    def _meter(self, ctx: BackendContext, parsed: dict, elapsed: float,
               returncode: int, kill_reason, actual_model: str = "") -> None:
        """写一条聚合计量事件（zcode 无 cost 字段——Coding Plan 套餐计费记 0）。

        actual_model 取 zcode 配置的 model.main（无 per-run 模型标志，配置即真实）。
        """
        metering_path = (ctx.config or {}).get("_metering_path", "")
        if not metering_path:
            return
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker-zcode",
            "actual_provider": "zai",
            "actual_model": actual_model or ctx.routed_model,
            "prompt_tokens": parsed["prompt_tokens"],
            "completion_tokens": parsed["completion_tokens"],
            "cost_usd": 0.0,
            "latency_ms": round(elapsed * 1000, 2),
            "result": "success" if (returncode == 0 and not kill_reason) else "failure",
            "fallback_reason": kill_reason or "",
            "task_id": ctx.task_id,
            "subtask_id": ctx.sub_id,
        })
