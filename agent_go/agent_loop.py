"""Agent 多轮对话循环 — 直接 API + 工具执行。

用于方案 C 混合策略的"简单任务"路径：
1. 通过 resolve_provider() 获取路由配置
2. 直接调用 LLM API（支持 tools 参数）
3. 解析响应中的 tool_calls
4. 通过 ToolRegistry 执行工具
5. 重复直到任务完成或达到最大轮数
6. 返回 subprocess.CompletedProcess（兼容现有接口）

不依赖 call_with_role()（因其不支持 tools），但复用路由配置解析。
"""

import json
import re
import time
import subprocess
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from .tool_executor import ToolRegistry
from .config import meter_event
from .metrics import estimate_cost

_logger = logging.getLogger(__name__)


def _anthropic_messages(messages: list) -> list:
    """将内部消息格式转换为 Anthropic 格式。"""
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                }],
            })
        else:
            result.append({"role": role, "content": content})
    return result


def _openai_messages(messages: list) -> list:
    """将内部消息格式转换为 OpenAI 兼容格式。"""
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "tool":
            result.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": content,
            })
        else:
            result.append({"role": role, "content": content})
    return result


def _parse_tool_calls(response_data: dict, provider: str) -> list[dict]:
    """从 API 响应中提取 tool_calls。

    Returns:
        list[dict]: 每个元素有 name, input, id 字段。空列表表示无工具调用。
    """
    tool_calls = []
    if provider == "anthropic":
        content_blocks = response_data.get("content", [])
        for block in content_blocks:
            if block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })
    else:
        choices = response_data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            raw_calls = msg.get("tool_calls", [])
            for tc in raw_calls:
                try:
                    arguments = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                tool_calls.append({
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "input": arguments,
                })
    return tool_calls


def _assistant_message(tool_calls: list[dict], text: str, provider: str) -> dict:
    """构建 assistant 角色消息，包含工具调用信息。"""
    if provider == "anthropic":
        content_blocks = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
        return {"role": "assistant", "content": content_blocks}
    else:
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["input"], ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        return msg


def _error_summary(e: Exception) -> str:
    """简洁的错误摘要（用于日志和异常消息）。"""
    msg = str(e)[:100]
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"URL 错误: {e.reason}"
    return msg


class AgentLoop:
    """多轮 Agent 对话循环 — 直接 API + 工具执行。"""

    def __init__(self, logger: logging.Logger = _logger):
        self.logger = logger

    def run(
        self,
        prompt: str,
        worktree: Path,
        pc: Any,
        api_key: str,
        config: dict,
        tag_name: str = "",
        sub_id: str = "",
        task_id: str = "",
        readonly: bool = False,
        scope_hint: str = "",
    ) -> subprocess.CompletedProcess:
        """执行多轮 Agent 对话。

        Args:
            prompt: 初始 prompt（TASK.md 内容）
            worktree: worktree 路径
            pc: ProviderConfig 或 RoleRoute（由调用方传入）
            api_key: API 密钥
            config: 完整配置
            tag_name: git tag 名称（由 executor 生成）
            sub_id: 子任务 ID
            task_id: 任务 ID
            readonly: explore 只读模式（只暴露只读工具，屏蔽 Write/Edit）
            scope_hint: 子任务声明的 files_hint（逗号/空白分隔），写工具越界时
                在工具结果中追加 advisory 警告（不硬阻断）

        Returns:
            subprocess.CompletedProcess（兼容现有接口）
        """
        # 兼容 RoleRoute 和 ProviderConfig 两种入参
        if hasattr(pc, 'primary'):
            pc = pc.primary
        provider = pc.provider
        base_url = pc.base_url
        model = pc.model

        agent_loop_cfg = config.get("agent_loop", {})
        max_turns = agent_loop_cfg.get("max_turns", 20)
        max_duration = agent_loop_cfg.get("max_duration", 600)  # 全局超时（秒）
        api_timeout = agent_loop_cfg.get("api_timeout", 120)     # 单次 API 调用超时（秒）
        metering_path = config.get("_metering_path", "")

        tools = ToolRegistry.definitions(readonly=readonly)
        # S9-A: 合并外部 MCP 工具（如有连接池透传）
        _mcp_pool = config.get("_mcp_pool") if config else None
        if _mcp_pool is not None:
            try:
                _mcp_tools = _mcp_pool.tool_definitions()
                if _mcp_tools:
                    tools = tools + _mcp_tools
            except Exception:
                pass  # MCP 工具获取失败，只用原生工具

        messages = [{"role": "user", "content": prompt}]

        exit_code = 0
        total_cost = 0.0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tool_calls = 0
        tool_stats: dict[str, int] = {}
        loop_start = time.time()

        # B2 stuck/no-progress/scope 检测（均可经 agent_loop.* 配置调整）
        _stuck_threshold = agent_loop_cfg.get("stuck_repeat_threshold", 3)
        _no_progress_turns = agent_loop_cfg.get("no_progress_turns", 8)
        _scope_hints = [t for t in re.split(r"[,\s]+", scope_hint or "") if t]
        _last_sig = None
        _repeat_count = 0
        _stuck_nudged = False
        stuck_detected = False
        no_progress = False
        _turns_since_write = 0

        for turn in range(max_turns):
            # 全局超时检查
            elapsed = time.time() - loop_start
            if elapsed > max_duration:
                self.logger.warning(
                    f"[AgentLoop] 全局超时 ({elapsed:.0f}s > {max_duration}s)，强制结束"
                )
                exit_code = 1
                break

            self.logger.info(f"[AgentLoop] turn={turn+1}/{max_turns} (elapsed={elapsed:.0f}s)")

            content, tool_calls, cost, pt, ct = self._call_api(
                provider, base_url, model, api_key, messages, tools,
                metering_path, task_id, sub_id, timeout=api_timeout,
            )
            total_cost += cost
            total_prompt_tokens += pt
            total_completion_tokens += ct

            # 记录每个工具调用次数
            for tc in tool_calls:
                total_tool_calls += 1
                tool_stats[tc["name"]] = tool_stats.get(tc["name"], 0) + 1

            if not tool_calls:
                self.logger.info(
                    f"[AgentLoop] 无工具调用，任务完成 (total_cost=${total_cost:.4f})"
                )
                break

            messages.append(_assistant_message(tool_calls, content, provider))

            _wrote_this_turn = False
            for tc in tool_calls:
                self.logger.info(f"[AgentLoop] 执行 {tc['name']}(...)")
                # stuck 检测：连续相同（工具+参数）签名计数
                _sig = (tc["name"], json.dumps(tc["input"], ensure_ascii=False, sort_keys=True))
                if _sig == _last_sig:
                    _repeat_count += 1
                else:
                    _repeat_count = 1
                    _last_sig = _sig
                # S9-A: MCP 工具按 mcp__ 前缀路由到连接池，原生工具走 ToolRegistry
                if tc["name"].startswith("mcp__"):
                    _mcp_pool = config.get("_mcp_pool") if config else None
                    result = _mcp_pool.dispatch(tc["name"], tc["input"]) if _mcp_pool is not None \
                        else {"success": False, "error": "MCP 池不可用"}
                else:
                    result = ToolRegistry.execute(tc["name"], tc["input"], worktree, readonly=readonly)
                # scope advisory：写工具落在 files_hint 声明范围外时追加提示（不阻断）
                if _scope_hints and tc["name"] in ("Write", "Edit") and result.get("success"):
                    _fp = str(tc["input"].get("file_path", ""))
                    if _fp and not any(h in _fp for h in _scope_hints):
                        result["output"] = (result.get("output", "")
                                            + f"\n⚠️ scope 提示：{_fp} 不在本子任务声明的 files_hint"
                                              f"（{scope_hint}）范围内，请确认是否越界。")
                        self.logger.warning(f"[AgentLoop] scope 越界写入: {_fp}（files_hint={scope_hint}）")
                if tc["name"] in ("Write", "Edit") and result.get("success"):
                    _wrote_this_turn = True
                result_str = result.get("output", "") or result.get("error", "")
                self.logger.debug(f"[AgentLoop] {tc['name']} 结果: {result_str[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False)[:4000],
                })

            # stuck 处置：首次达阈值注入提醒；提醒后仍重复则判定卡死，终止循环
            if _repeat_count > _stuck_threshold and _stuck_nudged:
                self.logger.warning(
                    f"[AgentLoop] stuck 检测：提醒后仍重复相同工具调用（{tc['name']}），强制结束"
                )
                stuck_detected = True
                exit_code = 1
                break
            if _repeat_count >= _stuck_threshold and not _stuck_nudged:
                _stuck_nudged = True
                self.logger.warning(f"[AgentLoop] stuck 检测：连续 {_repeat_count} 次重复相同工具调用")
                messages.append({
                    "role": "user",
                    "content": "系统提示：你已连续多次重复完全相同的工具调用且没有进展。"
                               "请换一种方式：检查之前的工具结果、调整参数或改用其他工具。",
                })

            # no-progress 信号：连续多轮无成功写入（只记信号，不终止——终态判定归 wrapper 验证）
            _turns_since_write = 0 if _wrote_this_turn else _turns_since_write + 1
            if not no_progress and _turns_since_write >= _no_progress_turns:
                no_progress = True
                self.logger.warning(
                    f"[AgentLoop] no-progress：连续 {_turns_since_write} 轮无成功 Write/Edit"
                )

            # 窗口管理：超过 40 条消息时丢弃早期历史
            if len(messages) > 40:
                keep = [messages[0]] + messages[-30:]
                self.logger.info(f"[AgentLoop] 消息窗口压缩: {len(messages)} → {len(keep)}")
                messages = keep
        else:
            self.logger.warning(f"[AgentLoop] 达到最大轮数 ({max_turns})，强制结束")
            exit_code = 1

        total_duration = round(time.time() - loop_start, 2)
        self.logger.info(
            f"[AgentLoop] 汇总: {turn+1} 轮, {total_tool_calls} 工具调用, "
            f"${total_cost:.4f}, {total_duration}s"
            f"{' [stuck]' if stuck_detected else ''}{' [no-progress]' if no_progress else ''}"
        )

        # 写入汇总计量事件
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker",
            "actual_provider": provider,
            "actual_model": model,
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "cost_usd": round(total_cost, 6),
            "result": "success" if exit_code == 0 else "failed",
            "loop_turns": turn + 1,
            "total_tool_calls": total_tool_calls,
            "tool_stats": tool_stats,
            "duration_sec": total_duration,
            "stuck_detected": stuck_detected,
            "no_progress": no_progress,
            "task_id": task_id,
            "subtask_id": sub_id,
        })

        # Git add + commit + tag
        if tag_name:
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=str(worktree), capture_output=True, timeout=30,
                )
                diff = subprocess.run(
                    ["git", "diff", "--cached", "--stat"],
                    cwd=str(worktree), capture_output=True, text=True, timeout=10,
                )
                if diff.stdout.strip():
                    subprocess.run(
                        ["git", "commit", "-m", f"{sub_id}: 直接 API 执行"],
                        cwd=str(worktree), capture_output=True, timeout=30,
                    )
                subprocess.run(
                    ["git", "tag", "-f", tag_name],
                    cwd=str(worktree), capture_output=True, timeout=10,
                )
            except Exception as e:
                self.logger.warning(f"[AgentLoop] git 操作失败: {e}")

        return subprocess.CompletedProcess([], exit_code, stdout="")

    def _call_api(
        self,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
        messages: list,
        tools: list,
        metering_path: str,
        task_id: str,
        sub_id: str,
        timeout: int = 120,
    ) -> tuple[str, list[dict], float, int, int]:
        """调用 LLM API 并解析响应。

        Args:
            timeout: 单次 API 调用超时（秒）

        Returns:
            (text_content, tool_calls, cost_usd, prompt_tokens, completion_tokens)
        """
        headers = {"Content-Type": "application/json"}
        if provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
            payload_messages = _anthropic_messages(messages)
            payload = {
                "model": model,
                "max_tokens": 4096,
                "temperature": 0.2,
                "messages": payload_messages,
                "tools": tools,
            }
        else:
            headers["Authorization"] = f"Bearer {api_key}"
            payload_messages = _openai_messages(messages)
            payload = {
                "model": model,
                "max_tokens": 4096,
                "messages": payload_messages,
                "tools": tools,
            }

        start = time.time()
        req = urllib.request.Request(
            base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        # API 重试逻辑：网络/HTTP 错误时指数退避，最多 3 次
        max_retries = 3
        last_error = None
        response_data = None
        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    response_data = json.loads(resp.read())
                last_error = None
                break
            except (urllib.error.HTTPError, urllib.error.URLError,
                    OSError, TimeoutError) as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt  # 指数退避：2s, 4s, 8s
                    self.logger.warning(
                        f"[AgentLoop] API 调用失败 (attempt={attempt}/{max_retries}): "
                        f"{_error_summary(e)}，{wait}s 后重试"
                    )
                    time.sleep(wait)
                else:
                    self.logger.error(
                        f"[AgentLoop] API 调用 {max_retries} 次均失败，放弃: {_error_summary(e)}"
                    )
                    raise RuntimeError(
                        f"API 调用失败（已重试 {max_retries} 次）: {_error_summary(e)}"
                    ) from e
        if response_data is None:
            raise RuntimeError(f"API 调用失败: {last_error}") from last_error
        data = response_data

        latency_ms = round((time.time() - start) * 1000, 2)

        usage = data.get("usage", {})
        pt = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
        ct = usage.get("output_tokens") or usage.get("completion_tokens", 0)
        cost = estimate_cost(provider, model, pt, ct)

        text = ""
        tool_calls = _parse_tool_calls(data, provider)

        if provider == "anthropic":
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    break
        else:
            choices = data.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "") or ""

        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker-loop",
            "actual_provider": provider,
            "actual_model": model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cost_usd": round(cost, 6),
            "latency_ms": latency_ms,
            "result": "success",
            "fallback_reason": "",
            "task_id": task_id,
            "subtask_id": sub_id,
        })

        self.logger.info(
            f"[AgentLoop] API 响应: {pt}+{ct} tokens, ${cost:.4f}, "
            f"{len(tool_calls)} tool_calls"
        )

        return text, tool_calls, cost, pt, ct
