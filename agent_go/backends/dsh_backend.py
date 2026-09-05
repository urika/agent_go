"""DSH Backend — 通过 DeepSeek Harness CLI（npx @deepseek-ai/dsh）执行子任务。

B8（阶段十三）：第六种 worker backend，契约调研/冒烟见
docs/design/stage13-b8-dsh-assessment.md；同时是 ADR-010 阶段 1 的首个
full-fidelity harvester 数据源（harvest_trajectory）。

输出契约（dsh 0.1.2-rc.1 实测）：
- 命令：npx -y @deepseek-ai/dsh@0.1.2-rc.1 --profile headless "<task>"，
  cwd 即工作区（无 --dir 标志，subprocess 以 cwd=worktree 拉起）；
- 退出码：0=turn completed / 1=失败 / 130=SIGINT；
- stdout 仅最终助手文本；stderr 为 reasoning 流（信息性，非错误）；
- 无 stdout JSON 事件流——token 计量须读 session 持久化日志。

审批：headless 下审批失败闭合；非 readonly 任务注入
DSH_PERMISSION_MODE=danger-full-access（approval=never，代价是同时关沙箱，
隔离由 agent_go worktree 承担）；readonly（ctx.extra.readonly）**不注入**
该变量（并显式移除继承值），审批失败闭合天然强制只读。

模型选择：无 per-run 模型标志，由 ~/.dsh/settings.yaml（provider 定义）+
~/.dsh/profiles/headless/cordis.patch.yml（agent-default-model 覆盖）决定；
计量的 actual_model 从 session 日志 assistant/message 的 source 回读（真源），
读不到时回退 ctx.routed_model。

会话日志（计量 + 轨迹数据源，fail-open）：
~/.dsh/sessions/<projectKey(cwd)>/session-<uuid>/session.jsonl.zstd。
projectKey 规则（dsh-session-persistence 源码）：路径分隔符 / \\ : 折叠为
单个 '-'，[A-Za-z0-9._-] 原样保留，其余字符转 ~XXXX（UTF-16 码元大写十六
进制），首尾包 '--'、去前导 '-'、截断 251。首行 header
{type:session, version, cwd, delegationDepth}；version != 0 时降级
（developer preview 防格式漂移）。解压用外部 zstd -dc（不加第三方依赖，
找不到 zstd 则降级 warning）。

仅支持 headless：resolve_backend_name 保证交互模式不路由到 dsh，
backend 内亦防御性拒绝。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import BackendContext, BaseBackend, SubtaskResult
from .registry import BackendRegistry
from ..config import meter_event
from ..console import _LazyConsole

_console = _LazyConsole()

# 版本 pin：developer preview 存在破坏性变更，锁定实测通过的版本。
DSH_PACKAGE = "@deepseek-ai/dsh@0.1.2-rc.1"
# 用户级配置 / 会话日志根目录（测试可 patch 注入临时 HOME）。
DSH_USER_SETTINGS = Path.home() / ".dsh" / "settings.yaml"
DSH_SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"
# 已实测的 session 日志格式版本（header.version）；不一致即降级。
DSH_LOG_FORMAT_VERSION = 0
# 写执行权限档（approval=never，同时关沙箱——隔离由 worktree 承担）。
DSH_PERMISSION_ENV = "DSH_PERMISSION_MODE"
DSH_FULL_ACCESS = "danger-full-access"
# 轨迹防腐翻译的大字段截断长度（tool 参数/结果摘要）。
_TRAJ_TRUNC = 300


def _project_key(cwd: str) -> str:
    """dsh 会话目录名编码（dsh-session-persistence projectKey 的 Python 移植）。

    分隔符折叠为 '-'；[A-Za-z0-9._-] 原样保留；其余按 ~XXXX 转义。
    注：dsh 按 UTF-16 码元转义，非 BMP 字符（罕见）会产生代理对差异，可接受。
    """
    out: list[str] = []
    sep_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not sep_run:
                out.append("-")
            sep_run = True
        elif ch != "~" and ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
            sep_run = False
        else:
            out.append("~" + format(ord(ch), "04X"))
            sep_run = False
    readable = "".join(out).lstrip("-") or "root"
    return f"--{readable[:251]}--"


def _decompress_log(log_path: Path, logger) -> Optional[str]:
    """zstd -dc 解压 session 日志；找不到 zstd / 解压失败返回 None（fail-open）。"""
    zstd = shutil.which("zstd")
    if not zstd:
        logger.warning(f"[DSHBackend] 未找到 zstd 命令，无法读取 {log_path.name}（计量/轨迹降级）")
        return None
    try:
        cp = subprocess.run([zstd, "-dc", str(log_path)],
                            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"[DSHBackend] zstd 解压失败 {log_path}: {exc}")
        return None
    if cp.returncode != 0:
        logger.warning(f"[DSHBackend] zstd 解压返回 {cp.returncode} {log_path}: {cp.stderr.strip()[:200]}")
        return None
    return cp.stdout


def _parse_session(text: str, logger) -> list[dict]:
    """解析 session 日志为事件列表。header.version 非实测版本时降级返回 []。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    try:
        header = json.loads(lines[0])
    except (ValueError, TypeError):
        logger.warning("[DSHBackend] session 日志 header 非 JSON，降级")
        return []
    if header.get("type") != "session":
        logger.warning(f"[DSHBackend] session 日志首行 type={header.get('type')!r}，降级")
        return []
    version = header.get("version")
    if version != DSH_LOG_FORMAT_VERSION:
        logger.warning(
            f"[DSHBackend] session 日志 version={version} 非实测版本 "
            f"{DSH_LOG_FORMAT_VERSION}（developer preview 格式漂移），降级"
        )
        return []
    events = []
    for raw in lines[1:]:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _find_session_log(worktree: Path, logger,
                      sessions_root: Optional[Path] = None) -> Optional[Path]:
    """定位本次 run 的 session 日志（cwd 编码目录内取最新 session）。

    编码目录不存在时兜底扫描全部会话目录，以 header.cwd 与 worktree 真实路径
    比对（容忍编码规则漂移）；全部失败返回 None（fail-open）。
    """
    root = sessions_root or DSH_SESSIONS_ROOT
    real = os.path.realpath(str(worktree))

    def _newest_log(session_dir: Path) -> Optional[Path]:
        try:
            logs = sorted(session_dir.glob("session-*/session.jsonl.zstd"),
                          key=lambda p: p.stat().st_mtime)
        except OSError:
            return None
        return logs[-1] if logs else None

    candidate = root / _project_key(real)
    if candidate.is_dir():
        log = _newest_log(candidate)
        if log:
            return log
    # 兜底：扫描所有会话目录，按 header.cwd 匹配（目录数有限，逐个读首行）
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return None
    for d in dirs:
        if d == candidate:
            continue
        log = _newest_log(d)
        if not log:
            continue
        text = _decompress_log(log, logger)
        if not text:
            continue
        first = text.splitlines()[0] if text.splitlines() else ""
        try:
            header = json.loads(first)
        except (ValueError, TypeError):
            continue
        if os.path.realpath(header.get("cwd", "") or "/") == real:
            return log
    return None


def _aggregate_usage(events: list[dict]) -> dict:
    """汇总 assistant/message 的 usage，并回读真实 provider/model（日志即真源）。"""
    prompt_tokens = 0
    completion_tokens = 0
    provider = ""
    model = ""
    for ev in events:
        if ev.get("type") != "assistant/message":
            continue
        data = ev.get("data", {}) or {}
        usage = data.get("usage", {}) or {}
        prompt_tokens += usage.get("inputTokens", 0) + usage.get("cacheReadTokens", 0)
        completion_tokens += usage.get("outputTokens", 0)
        source = (data.get("message", {}) or {}).get("source", {}) or {}
        if source.get("kind") == "model":
            provider = provider or source.get("provider", "")
            model = model or source.get("model", "")
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "provider": provider, "model": model}


def _truncate(text, limit: int = _TRAJ_TRUNC) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit}chars)"


def _translate_event(ev: dict) -> Optional[dict]:
    """dsh 事件 → 平台轨迹事件（防腐翻译，不原样透传 dsh 内部格式）。

    保留词汇：turn/start|end、step/start|end、user/message（仅 source/长度）、
    assistant/message（usage + interrupted + provider/model）、assistant/attempt、
    tool/call（name + 参数摘要截断）、tool/result（截断）。
    大字段（assistant/chunk、reasoning/text/tool-call chunk 流、request/* 等）丢弃。
    不认识的类型返回 None（跳过）。
    """
    etype = ev.get("type", "")
    data = ev.get("data", {}) or {}
    out: Optional[dict] = None

    if etype in ("turn/start", "turn/end", "step/start", "step/end"):
        out = {k: v for k, v in data.items() if k in ("turn", "step", "reason")}
    elif etype == "user/message":
        content = data.get("content", []) or []
        text_len = sum(len(p.get("text", "")) for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
        out = {"source_kind": (data.get("source", {}) or {}).get("kind", ""),
               "role": data.get("role", ""), "text_len": text_len}
    elif etype == "assistant/message":
        source = (data.get("message", {}) or {}).get("source", {}) or {}
        out = {"turn": data.get("turn"), "step": data.get("step"),
               "usage": data.get("usage", {}) or {},
               "interrupted": bool(data.get("interrupted", False))}
        if source.get("kind") == "model":
            out["provider"] = source.get("provider", "")
            out["model"] = source.get("model", "")
    elif etype == "assistant/attempt":
        # 失败尝试无损保留：usage + 错误摘要，丢弃 message 原文
        out = {"turn": data.get("turn"), "step": data.get("step"),
               "usage": data.get("usage", {}) or {}}
        for key in ("error", "reason", "stopReason"):
            if data.get(key) is not None:
                out[key] = _truncate(data[key])
    elif etype == "tool/call":
        out = {"turn": data.get("turn"), "step": data.get("step"),
               "call_id": data.get("callId", ""), "name": data.get("name", ""),
               "arguments_summary": _truncate(data.get("arguments", ""))}
    elif etype == "tool/result":
        message = data.get("message", {}) or {}
        content = message.get("content", []) or []
        block = content[0] if content and isinstance(content[0], dict) else {}
        inner = block.get("content", []) or []
        text = "".join(p.get("text", "") for p in inner
                       if isinstance(p, dict) and p.get("type") == "text")
        out = {"turn": data.get("turn"), "step": data.get("step"),
               "call_id": (message.get("source", {}) or {}).get("callId", ""),
               "is_error": bool(block.get("isError", False)),
               "result_summary": _truncate(text)}
    if out is None:
        return None
    return {"seq": ev.get("seq"), "time": ev.get("time"), "type": etype, "data": out}


@BackendRegistry.register
class DSHBackend(BaseBackend):
    """通过 DeepSeek Harness CLI 执行子任务（B8）。

    - 命令：npx -y @deepseek-ai/dsh@0.1.2-rc.1 --profile headless <task_md>
      （cwd=worktree，prompt 走 argv positional）；
    - 非 readonly 注入 DSH_PERMISSION_MODE=danger-full-access（approval=never，
      关沙箱，隔离由 worktree 承担）；readonly 不注入且移除继承值，审批失败
      闭合天然只读；
    - 计量/轨迹读 session 日志（zstd -dc），全程 fail-open——找不到日志、
      没有 zstd、版本漂移、解析失败均只记 warning，绝不影响任务结果；
    - 挂起风险由 ctx.hard_timeout 兜底（kill_reason=hard_timeout）。
    """

    name = "dsh"

    @classmethod
    def available(cls) -> bool:
        """npx 可用且用户级 dsh 配置存在（~/.dsh/settings.yaml）。"""
        return bool(shutil.which("npx")) and DSH_USER_SETTINGS.exists()

    def run(self, ctx: BackendContext) -> SubtaskResult:
        start = time.time()
        if not ctx.headless:
            err = "dsh backend 仅支持 headless 模式"
            ctx.logger.error(f"[DSHBackend] {err}")
            return SubtaskResult(returncode=2, stderr=err, sandbox_type="dsh",
                                 backend_time=time.time() - start)
        if not shutil.which("npx"):
            err = "npx not found on PATH（dsh 需要 Node.js，见 stage13-b8 评估）"
            ctx.logger.error(f"[DSHBackend] {err}")
            return SubtaskResult(returncode=127, stderr=err, sandbox_type="dsh",
                                 backend_time=time.time() - start)

        readonly = bool(ctx.extra.get("readonly"))
        cmd = ["npx", "-y", DSH_PACKAGE, "--profile", "headless", ctx.task_md]
        ctx.logger.info(
            f"[DSHBackend] {ctx.sub_id} 启动 dsh "
            f"(profile=headless, readonly={readonly}, model=配置驱动)"
        )

        env = dict(ctx.env) if ctx.env else dict(os.environ)
        if readonly:
            # 审批失败闭合即只读；显式移除可能继承的全放开变量
            env.pop(DSH_PERMISSION_ENV, None)
        else:
            env[DSH_PERMISSION_ENV] = DSH_FULL_ACCESS

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
            ctx.logger.error(f"[DSHBackend] {ctx.sub_id} 硬超时 ({ctx.hard_timeout}s)，强制终止")
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
        final_text = (stdout or "").strip()

        # 零产出（rc=0 但 stdout 空）显式映射为失败（同 zcode/opencode 语义）——
        # dsh 计量在 session 日志里，读不到时无法用 tokens 兜底判定，以 stdout 为准。
        zero_work = proc.returncode == 0 and not final_text
        if zero_work:
            stderr = (stderr or "") + "\ndsh error: zero output (empty stdout)"

        usage = self._collect_usage(ctx)

        ctx.logger.info(
            f"[DSHBackend] {ctx.sub_id} 结束: rc={proc.returncode}, "
            f"{usage['prompt_tokens']}+{usage['completion_tokens']} tokens, {elapsed:.0f}s"
        )

        self._meter(ctx, usage, elapsed, proc.returncode, kill_reason)

        if ctx.progress:
            _console.print(f"  ➜ {ctx.sub_id}: ✓ dsh backend {elapsed:.0f}s")

        returncode = proc.returncode if proc.returncode is not None else 1
        if zero_work and returncode == 0:
            returncode = 1
        return SubtaskResult(
            returncode=returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            sandbox_type="dsh",
            backend_time=elapsed,
            kill_reason=kill_reason,
        )

    def harvest_trajectory(self, ctx: BackendContext, result: SubtaskResult) -> list[dict]:
        """ADR-010 阶段 1：采集本次 run 的 dsh session 轨迹（只采集不消费）。

        定位 worktree 对应的最新 session 日志，防腐翻译为平台事件
        {seq,time,type,data}；全程 fail-open，失败仅 warning + 返回 []。
        """
        try:
            log = _find_session_log(ctx.worktree, ctx.logger)
            if not log:
                ctx.logger.warning(f"[DSHBackend] {ctx.sub_id} 未找到 session 日志，轨迹采集跳过")
                return []
            text = _decompress_log(log, ctx.logger)
            if text is None:
                return []
            events = _parse_session(text, ctx.logger)
            if not events:
                return []
            trajectory = [t for t in (_translate_event(ev) for ev in events) if t]
            ctx.logger.info(
                f"[DSHBackend] {ctx.sub_id} 轨迹采集: {log.parent.name} "
                f"{len(events)} 事件 → {len(trajectory)} 条平台轨迹"
            )
            return trajectory
        except Exception as exc:  # 防腐边界兜底：任何意外都不影响任务结果
            ctx.logger.warning(f"[DSHBackend] {ctx.sub_id} 轨迹采集失败（忽略）: {exc}")
            return []

    def _collect_usage(self, ctx: BackendContext) -> dict:
        """读 session 日志汇总 usage（fail-open：失败返回全零 + 空 model）。"""
        empty = {"prompt_tokens": 0, "completion_tokens": 0, "provider": "", "model": ""}
        try:
            log = _find_session_log(ctx.worktree, ctx.logger)
            if not log:
                ctx.logger.warning(f"[DSHBackend] {ctx.sub_id} 未找到 session 日志，计量按零记录")
                return empty
            text = _decompress_log(log, ctx.logger)
            if text is None:
                return empty
            return _aggregate_usage(_parse_session(text, ctx.logger))
        except Exception as exc:
            ctx.logger.warning(f"[DSHBackend] {ctx.sub_id} 计量采集失败（按零记录）: {exc}")
            return empty

    def _meter(self, ctx: BackendContext, usage: dict, elapsed: float,
               returncode: int, kill_reason) -> None:
        """写一条聚合计量事件。

        actual_model/actual_provider 从 session 日志 assistant/message 回读
        （dsh 无 per-run 模型标志，日志即真源）；读不到回退 ctx.routed_model。
        dsh 不报告 cost（计费在 provider 侧），记 0。
        """
        metering_path = (ctx.config or {}).get("_metering_path", "")
        if not metering_path:
            return
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker-dsh",
            "actual_provider": usage["provider"],
            "actual_model": usage["model"] or ctx.routed_model,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cost_usd": 0.0,
            "latency_ms": round(elapsed * 1000, 2),
            "result": "success" if (returncode == 0 and not kill_reason) else "failure",
            "fallback_reason": kill_reason or "",
            "task_id": ctx.task_id,
            "subtask_id": ctx.sub_id,
        })
