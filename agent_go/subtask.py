import subprocess, json, re, time, threading, logging, signal, os
from pathlib import Path
from datetime import datetime
from typing import Optional

from .config import log_event

__all__: list[str] = []

# claude 进程退出码常量：130 = SIGINT（视为检测到交互）
EXIT_CODE_INTERACTION = 130

# P1-5: 工具活动快照目标提取（从 stream-json tool_input 提取文件路径/命令）
def _extract_activity_target(tool_name: str, raw_input: str) -> str:
    """从工具调用的 partial_json 输入中提取人类可读的目标（文件路径或命令）。

    适用于 Read/Edit/Write 的 file_path 字段和 Bash 的 command 字段。
    若 json 解析失败或不包含目标字段，降级为工具名 + 原始输入前 50 字符。
    """
    if not raw_input or not raw_input.startswith("{"):
        return raw_input[:50] if raw_input else ""
    try:
        import json as _json
        parsed = _json.loads(raw_input)
    except _json.JSONDecodeError:
        return raw_input[:50] if raw_input else ""
    for key in ("file_path", "command", "url", "query"):
        val = parsed.get(key)
        if val and isinstance(val, str):
            if len(val) > 80:
                val = val[:77] + "..."
            return val
    # fallback: first string value in args
    for val in parsed.values():
        if isinstance(val, str) and len(val) > 3 and len(val) < 120:
            return val
    return raw_input[:50] if raw_input else ""

# P2 Layer 3：claude SIGTERM handler 占位
# 实际安装在 _run_headless 内（需要 worktree 路径）
_INTERRUPTED_FLAG = threading.Event()

def _git_merge_upstream(src_worktree: Path, dst_worktree: Path, tag: str, logger: logging.Logger, headless: bool = False) -> None:
    """将上游 worktree 的 tag 合并到当前 worktree。
    worktree 共享对象库，tag 在所有 worktree 间直接可见，无需 fetch。

    在 headless 模式下，冲突不会 abort，而是保留冲突标记状态，
    让 Claude Code 直接面对冲突现场并自动解决。
    """
    result = subprocess.run(
        ["git", "merge", tag, "--no-commit"],
        cwd=str(dst_worktree), capture_output=True, text=True)
    if result.returncode == 0:
        commit_result = subprocess.run(
            ["git", "commit", "--no-edit", "-m", f"merge upstream {tag}"],
            cwd=str(dst_worktree), capture_output=True)
        if commit_result.returncode != 0:
            logger.warning(f"merge commit 失败: {commit_result.stderr[:200]}")
        logger.info(f"git merge {tag} 成功")
    else:
        conflict_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(dst_worktree), capture_output=True, text=True)
        conflict_files = [f for f in conflict_result.stdout.strip().split("\n") if f]
        conflict_info = (
            f"merge {tag} 冲突文件:\n" + "\n".join(f"- {f}" for f in conflict_files)
            if conflict_files else "未知冲突"
        )
        logger.warning(f"git merge {tag} 冲突: {', '.join(conflict_files)}")

        conflict_file = dst_worktree / ".MERGE_CONFLICT"
        conflict_file.write_text(conflict_info, encoding="utf-8")

        if headless:
            # Headless 模式: 保留冲突状态，让 Claude Code 现场解决
            # 不执行 merge --abort，工作区保持冲突标记 (<<<<<<<)
            logger.info(f"headless 模式: 保留冲突标记，Claude Code 将自动解决")
        else:
            # 交互模式: abort，让用户手动重新 merge
            subprocess.run(["git", "merge", "--abort"],
                           cwd=str(dst_worktree), capture_output=True)

def _run_headless(task_md: str, worktree: Path, env: dict[str, str], logger: logging.Logger, sub_id: str, active_pids: Optional[set] = None, active_pids_lock: Optional[threading.Lock] = None, allowed_tools: Optional[list] = None, hard_timeout: int = 0, shared_activity: Optional[list] = None, config: Optional[dict] = None) -> subprocess.CompletedProcess:
    """无头模式：claude -p 带 stream-json 实时监控、交互检测和超时重试。

    allowed_tools: Agent 类型声明的工具白名单（如 architect 的 Read/Grep/Glob）。
    非空时通过 --allowedTools 强制约束；None/空列表表示不限制（developer 默认）。
    hard_timeout: 单次执行硬超时（秒），0=不限制。用于修复重试的 retry_timeout 控制。
    shared_activity: 可选 [dict] 列表，_run_headless 会将其 [0] 更新为当前工具活动快照
                     {tool, target, since}，供调用方（如进度行）无锁读取最新活动。
                    共享规则：写线程（daemon reader）替换列表元素 [0]；
                    读线程（调用方主线程）读取 [0]。CPython GIL 下 dict 引用赋值是原子的。
    config: 运行时配置字典（非 None 时优先于磁盘加载，避免 disk ↔ runtime 不一致）
    """
    # Phase 2: GoalInjector 看门狗配置。优先级：运行时 config（参数注入）> env > 磁盘 config > 默认
    GOAL_WATCHDOG_ENABLED = True
    MAX_GOAL_TURNS = 20
    GOAL_TIMEOUT = 600
    _cfg = config
    if _cfg is None:
        try:
            from .config import load_config
            _cfg = load_config()
        except Exception:
            _cfg = {}
    _goal_cfg = _cfg.get("goal", {})
    GOAL_WATCHDOG_ENABLED = _goal_cfg.get("enabled", True)
    MAX_GOAL_TURNS = _goal_cfg.get("max_turns", 20)
    GOAL_TIMEOUT = _goal_cfg.get("timeout_seconds", 600)
    if "AGENT_GO_GOAL_ENABLED" in env:
        GOAL_WATCHDOG_ENABLED = env["AGENT_GO_GOAL_ENABLED"] == "1"
    if "AGENT_GO_GOAL_MAX_TURNS" in env:
        try:
            MAX_GOAL_TURNS = int(env["AGENT_GO_GOAL_MAX_TURNS"])
        except ValueError:
            pass
    if "AGENT_GO_GOAL_TIMEOUT" in env:
        try:
            GOAL_TIMEOUT = int(env["AGENT_GO_GOAL_TIMEOUT"])
        except ValueError:
            pass

    PFX = f"[{sub_id}]"
    if active_pids is None:
        active_pids = set()

    # 交互检测模式（中英文）
    INTERACTION_PATTERNS = [
        r"waiting for input", r"approve\s+(Write|Edit|Bash|Read)",
        r"permission required", r"\[y/n\]", r"press.*to continue",
        r"是否继续", r"请确认", r"请输入", r"等待输入", r"选择操作",
        r"\[Y/n\]", r"\[y/N\]", r"是/否", r"确认.*操作",
    ]
    # 退出码常量已提升至模块级（EXIT_CODE_INTERACTION）
    IDLE_TIMEOUT = 600   # 10 分钟纯静默才 kill（思考阶段无任何事件）
    HEARTBEAT = 60       # 60s 无事件发心跳

    # Phase 1 配套：累计所有 attempt 的 Claude usage（result 事件提取）
    claude_usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "duration_ms": 0, "num_turns": 0, "model": ""}

    def _run_one(prompt: str, attempt: int) -> tuple[subprocess.Popen, list[str], bool]:
        """启动 claude -p (stream-json) 并实时解析事件。"""
        # S12-P0 G1：记录 kill 原因（stuck / hard_timeout / goal_timeout / goal_turns）。
        # kill_reason 通过闭包写入 _kill_reason，供外层 _run_headless 上报结果。
        _kill_reason: list[Optional[str]] = [None]
        run_start_ref: list[float] = [0.0]  # 供 _record_kill 计算耗时，循环开始时置真实值

        def _record_kill(reason: str) -> None:
            _kill_reason[0] = reason
            # 与 worker metering 同路径写 kill_state 事件（测量/控制解耦，审计完整）
            _mp = env.get("AGENT_GO_METERING_PATH", "")
            if _mp:
                try:
                    from .config import meter_event
                    meter_event(_mp, {
                        "role": "worker",
                        "event": "kill_state",
                        "sub_id": sub_id,
                        "attempt": attempt,
                        "kill_reason": reason,
                        "elapsed_sec": round(time.time() - run_start_ref[0], 2),
                    })
                except Exception:
                    logger.debug("kill_state metering 写入失败（忽略）")

        cmd = [
            "claude", "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        # S10 成本控制 L1：单次 claude 调用硬上限（--max-budget-usd，claude >=2.1 原生支持）
        # 按 difficulty 读取 cost_control.per_subtask_budget_usd；默认关闭（enabled=False 不注入）
        _cost_cfg = (_cfg or {}).get("cost_control") or {}
        if _cost_cfg.get("enabled"):
            _diff = env.get("AGENT_GO_DIFFICULTY", "medium")
            _budgets = _cost_cfg.get("per_subtask_budget_usd", {}) or {}
            # 未知难度回退 medium（避免该难度子任务无成本保护）
            _budget = _budgets.get(_diff) or _budgets.get("medium")
            if _budget and _budget > 0:
                cmd.extend(["--max-budget-usd", str(_budget)])
        # S4 复杂度双通道：difficulty 路由的模型（env 由 executor 注入）
        _routed_model = env.get("AGENT_GO_CLAUDE_MODEL", "")
        if _routed_model:
            cmd.extend(["--model", _routed_model])
        # 透传 max_tokens 到 Claude Code（claude -p 不支持 --max-tokens flag，只能用 env var）
        # Claude Code >=2.1 支持 CLAUDE_CODE_MAX_OUTPUT_TOKENS 环境变量
        # 实际生效：API 会按模型上限截断（opus-4-7 = 128K）
        _max_tokens = env.get("AGENT_GO_MAX_TOKENS", "")
        if _max_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = _max_tokens
        # S9-A: 透传外部 MCP server 配置给 claude CLI（claude 原生支持 MCP 消费）
        _mcp_servers_cfg = _cfg.get("mcp_servers") if _cfg else None
        if _mcp_servers_cfg:
            try:
                from .mcp_client import MCPClientPool
                _claude_mcp = MCPClientPool(_mcp_servers_cfg).mcp_config_for_claude()
                if _claude_mcp.get("mcpServers"):
                    import tempfile as _tempfile
                    _tf = _tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False, prefix="agent_go_mcp_")
                    json.dump(_claude_mcp, _tf)
                    _tf.close()
                    cmd.extend(["--mcp-config", _tf.name])
            except Exception as _mcp_err:
                logger.debug(f"[{sub_id}] MCP config 透传失败（已跳过）: {_mcp_err}")
        proc = subprocess.Popen(cmd, env=env, cwd=str(worktree), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if active_pids_lock:
            with active_pids_lock:
                active_pids.add(proc.pid)
        else:
            active_pids.add(proc.pid)
        last_ts = [time.time()]
        goal_start_ts = [time.time()]
        lines = []
        waiting = [False]
        current_tool = [None]
        tool_input = [""]
        goal_turn_count = [0]
        goal_watchdog_triggered = [False]

        def parse_and_log(raw_line: str, label: str) -> None:
            s = raw_line.rstrip()
            if not s:
                return
            ts = datetime.now().strftime("%H:%M:%S")
            last_ts[0] = time.time()

            # 交互检测（stderr 文本行）
            if label == "err":
                lines.append(f"[{ts}] {s[:200]}")
                logger.info(f"{PFX} [claude err] {s[:200]}")
                for pat in INTERACTION_PATTERNS:
                    if re.search(pat, s, re.IGNORECASE):
                        waiting[0] = True
                        logger.error(f"⚠️ 交互: (attempt={attempt}): {s[:200]}")
                return

            # 尝试解析 stream-json 事件
            try:
                event = json.loads(s)
            except json.JSONDecodeError:
                # 非 JSON 输出（如纯文本），直接记录
                lines.append(f"[{ts}] {s[:200]}")
                logger.debug(f"{PFX} [claude] {s[:200]}")
                return

            ev_type = event.get("type", "")

            # stream_event: 流式内容增量
            if ev_type == "stream_event":
                inner = event.get("event", {})
                it = inner.get("type", "")

                if it == "content_block_start":
                    cb = inner.get("content_block", {})
                    tool_name = cb.get("name", "")
                    if tool_name:
                        current_tool[0] = tool_name
                        tool_input[0] = ""
                        logger.info(f"{PFX} [{tool_name}] ...")
                        # Phase 2: 统计 goal 工具调用轮数
                        if GOAL_WATCHDOG_ENABLED:
                            goal_turn_count[0] += 1
                            if goal_turn_count[0] % 5 == 0:
                                logger.info(f"{PFX} goal turn count: {goal_turn_count[0]}/{MAX_GOAL_TURNS}")

                elif it == "content_block_delta":
                    delta = inner.get("delta", {})
                    dt = delta.get("type", "")
                    if dt == "text_delta":
                        text = delta.get("text", "")
                        # 只记录非纯空白的文本，降为 DEBUG 减少噪音
                        if text.strip():
                            lines.append(f"[{ts}] {text[:200]}")
                            logger.debug(f"{PFX} [text] {text[:200]}")
                    elif dt == "input_json_delta":
                        tool_input[0] += delta.get("partial_json", "")

                elif it == "content_block_stop":
                    if current_tool[0]:
                        ti = tool_input[0]
                        preview = ti[:200] if len(ti) > 200 else ti
                        logger.debug(f"{PFX} [{current_tool[0]}] 完成: {preview}")
                        # P1-5: 提取工具活动快照（供进度行消费）
                        if shared_activity is not None:
                            _target = _extract_activity_target(current_tool[0], ti)
                            shared_activity[0] = {
                                "tool": current_tool[0],
                                "target": _target,
                                "since": time.time(),
                            }
                        current_tool[0] = None

            # assistant: 消息批次
            elif ev_type == "assistant":
                content = event.get("message", {}).get("content", [])
                # 记录实际请求的模型名（claude 会把 claude-haiku-4-5 等路由名
                # 解析为后端真实模型，如 deepseek-v4-flash）
                _msg_model = event.get("message", {}).get("model", "")
                if _msg_model and not claude_usage_total["model"]:
                    claude_usage_total["model"] = _msg_model
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            t = block.get("text", "")
                            if t.strip():
                                lines.append(f"[{ts}] {t[:200]}")
                                logger.debug(f"{PFX} [assistant] {t[:200]}")
                        elif block.get("type") == "tool_use":
                            logger.debug(f"{PFX} [tool_use] {block.get('name', '?')}")

            # result: 最终结果（含 usage + cost）
            elif ev_type == "result":
                subtype = event.get("subtype", "")
                logger.info(f"{PFX} [result] {subtype}")
                logger.debug(f"{PFX} [result_event] keys={list(event.keys())[:20]}")
                logger.debug(f"{PFX} [result_usage] total_cost_usd={event.get('total_cost_usd')} usage={event.get('usage')}")
                # Phase 1 配套：提取 Claude 执行的 token/cost，写入 metering
                usage = event.get("usage", {}) or {}
                claude_cost = event.get("total_cost_usd")
                pt = usage.get("input_tokens") or usage.get("prompt_tokens", 0) or 0
                ct = usage.get("output_tokens") or usage.get("completion_tokens", 0) or 0
                if pt or ct or claude_cost:
                    claude_usage_total["prompt_tokens"] += pt
                    claude_usage_total["completion_tokens"] += ct
                    claude_usage_total["cost_usd"] += claude_cost if claude_cost is not None else 0.0
                    claude_usage_total["duration_ms"] += event.get("duration_ms", 0) or 0
                    claude_usage_total["num_turns"] += event.get("num_turns", 0) or 0

            # user: 工具结果
            elif ev_type == "user":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out = block.get("content", "")
                        if isinstance(out, str) and out.strip():
                            preview = out[:200] if len(out) > 200 else out
                            logger.info(f"{PFX} [tool_result] {preview}")

            else:
                # 其他事件类型，轻量记录
                pass

        def read_stdout() -> None:
            for line in iter(proc.stdout.readline, ''):
                parse_and_log(line, "out")

        def read_stderr() -> None:
            for line in iter(proc.stderr.readline, ''):
                parse_and_log(line, "err")

        t_out = threading.Thread(target=read_stdout, daemon=True)
        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        idle_logged_at = 0
        run_start = time.time()
        run_start_ref[0] = run_start  # 供 _record_kill 闭包读取耗时
        while proc.poll() is None:
            # 硬超时（如修复重试的 retry_timeout）：到点即 kill，不依赖事件活动
            if hard_timeout and time.time() - run_start > hard_timeout:
                logger.error(f"claude 硬超时 ({hard_timeout}s, attempt={attempt})，强制终止")
                log_event(logger, "headless_hard_timeout",
                          {"sub_id": sub_id, "attempt": attempt, "limit": hard_timeout})
                _record_kill("hard_timeout")
                proc.kill()
                break
            idle = time.time() - last_ts[0]
            if idle > IDLE_TIMEOUT:
                logger.error(f"claude {idle:.0f}s 无事件 (attempt={attempt})，强制终止")
                _record_kill("stuck")
                proc.kill()
                break
            # Phase 2: GoalInjector 看门狗
            if GOAL_WATCHDOG_ENABLED and not goal_watchdog_triggered[0]:
                elapsed = time.time() - goal_start_ts[0]
                if elapsed > GOAL_TIMEOUT:
                    logger.error(f"claude goal 循环超时 ({elapsed:.0f}s > {GOAL_TIMEOUT}s)，强制终止")
                    log_event(logger, "goal_timeout", {"sub_id": sub_id, "elapsed": elapsed, "limit": GOAL_TIMEOUT})
                    goal_watchdog_triggered[0] = True
                    _record_kill("goal_timeout")
                    proc.kill()
                    break
                if goal_turn_count[0] >= MAX_GOAL_TURNS:
                    logger.error(f"claude goal 轮数超限 ({goal_turn_count[0]} >= {MAX_GOAL_TURNS})，强制终止")
                    log_event(logger, "goal_turns_exceeded", {"sub_id": sub_id, "turns": goal_turn_count[0], "limit": MAX_GOAL_TURNS})
                    goal_watchdog_triggered[0] = True
                    _record_kill("goal_turns_exceeded")
                    proc.kill()
                    break
            if idle > HEARTBEAT and idle - idle_logged_at > HEARTBEAT:
                logger.info(f"{PFX} 等待中... (无事件 {idle:.0f}s, attempt={attempt})")
                idle_logged_at = idle
            time.sleep(2)

        t_out.join()
        t_err.join()
        proc.wait()
        # 终检：poll 循环因 time.sleep(2) 可能错过最后一个事件触发的 goal 轮数超限
        # （读线程处理最后一行累加 goal_turn_count 与主线程 sleep 期间的 poll 返回存在竞争）。
        # 线程 join 后所有事件已处理完毕，此时做一次确定性检查，消除 flaky。
        if (GOAL_WATCHDOG_ENABLED and not goal_watchdog_triggered[0]
                and goal_turn_count[0] >= MAX_GOAL_TURNS):
            logger.error(f"claude goal 轮数超限 ({goal_turn_count[0]} >= {MAX_GOAL_TURNS})，强制终止")
            log_event(logger, "goal_turns_exceeded",
                      {"sub_id": sub_id, "turns": goal_turn_count[0], "limit": MAX_GOAL_TURNS})
            goal_watchdog_triggered[0] = True
            _record_kill("goal_turns_exceeded")
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        if active_pids_lock:
            with active_pids_lock:
                active_pids.discard(proc.pid)
        else:
            active_pids.discard(proc.pid)
        return proc, lines, waiting[0], _kill_reason[0]

    RETRY_SUFFIX = (
"\n\n【系统指令】你必须立即完成上述所有任务，直接执行文件创建和修改操作。"
"不要询问任何问题，不要等待确认，不要输出中间讨论。"
"完成后输出简洁的状态报告和变更摘要。"
    )
    MAX_ATTEMPTS = 2

    logger.info(f"{PFX} 无头模式: claude -p")
    if allowed_tools:
        logger.info(f"{PFX} 工具白名单: {','.join(allowed_tools)}")
    log_event(logger, "subtask_headless_start", {"id": sub_id})

    all_lines = []
    final_rc = -1
    interaction = False
    final_kill_reason = None  # S12-P0 G1：最后一次 attempt 的 kill_reason

    for attempt in range(MAX_ATTEMPTS):
        if attempt == 0:
            prompt = task_md
        else:
            logger.warning(f"超时重试 (attempt={attempt+1})，注入催促指令")
            log_event(logger, "subtask_headless_retry", {"id": sub_id, "attempt": attempt + 1})
            prompt = task_md + RETRY_SUFFIX

        proc, lines, waiting, kill_reason = _run_one(prompt, attempt + 1)
        if kill_reason:
            final_kill_reason = kill_reason
        all_lines.extend(lines)
        all_lines.append(f"--- attempt={attempt+1} exit_code={proc.returncode} ---")
        # 正则检测 或 退出码为 SIGINT(130) 都视为交互
        interaction = interaction or waiting or proc.returncode == EXIT_CODE_INTERACTION
        final_rc = proc.returncode

        if final_rc == 0:
            break
        # 非交互原因失败（如 API 超时、退出码非 130 且非 0），不重试
        if not interaction:
            break

    log_event(logger, "subtask_headless_complete", {
        "id": sub_id, "exit_code": final_rc,
        "interaction_detected": interaction,
        "attempts": attempt + 1,
        "output_lines": len(all_lines),
    })

    # Phase 1 配套：记录 Claude 执行的 token/cost 到 metering.jsonl
    metering_path = env.get("AGENT_GO_METERING_PATH", "")
    if metering_path and (claude_usage_total["prompt_tokens"] or claude_usage_total["completion_tokens"] or claude_usage_total["cost_usd"]):
        from .config import meter_event
        from .pricing import MODEL_PRICES
        _model = env.get("AGENT_GO_CLAUDE_MODEL", "") or "claude-code-executor"
        # 实际请求模型：优先用 claude 响应中解析出的真实模型名
        # （如 --model claude-haiku-4-5 实际请求 deepseek-v4-flash），
        # 解析失败时回退路由名
        _resolved_model = claude_usage_total.get("model") or _model
        # 本地后端判定：executor 检测到 ANTHROPIC_BASE_URL 指向本机
        # （127.0.0.1/localhost，如本地 llama-server 代理 4000→8081）时注入
        # AGENT_GO_IS_LOCAL=1。此时 claude 响应中的 model（如 deepseek-v4-flash）
        # 是 claude 内置映射，不代表真实本地后端，用 AGENT_GO_LOCAL_MODEL
        # （executor 从 worker_backends/local_model_names 解析）覆盖。
        _is_local = env.get("AGENT_GO_IS_LOCAL", "") == "1"
        if _is_local:
            _local_model_name = env.get("AGENT_GO_LOCAL_MODEL", "") or _model
            _resolved_model = _local_model_name
        else:
            # 兼容旧配置：模型在 local_models 列表中视为本地，成本清零
            _local_models_raw = env.get("AGENT_GO_LOCAL_MODELS", "")
            _is_local = _resolved_model in [m.strip() for m in _local_models_raw.split(",")] if _local_models_raw else False
        # 成本重算：claude CLI 返回的 total_cost_usd 按 Anthropic 定价计算，
        # 但实际后端可能是 DeepSeek 等更便宜的模型。用实际模型名 + MODEL_PRICES
        # 定价重算，避免成本虚高（如 claude-haiku-4-5 实际是 deepseek-v4-flash，
        # 定价 $0.14/$0.28 而非 $1/$5）。
        _prompt_tok = claude_usage_total["prompt_tokens"]
        _comp_tok = claude_usage_total["completion_tokens"]
        if _is_local:
            _cost = 0.0
        else:
            _price = MODEL_PRICES.get(_resolved_model) or MODEL_PRICES.get(_model)
            if _price:
                _cost = round(
                    (_prompt_tok / 1_000_000 * _price["prompt"])
                    + (_comp_tok / 1_000_000 * _price["completion"]),
                    6,
                )
                # 若重算为 0（如未知模型无定价）则回退 claude 返回值
                if _cost <= 0:
                    _cost = round(claude_usage_total["cost_usd"], 6)
            else:
                _cost = round(claude_usage_total["cost_usd"], 6)
        meter_event(metering_path, {
            "role": "worker",
            "virtual_model": "agentgo-worker",
            "actual_provider": "claude-code",
            # S4：路由到具体模型时记录真实模型（claude 响应解析），否则为 CLI 默认
            "actual_model": _resolved_model,
            "routed_model": _model,
            "is_local": _is_local,
            "difficulty": env.get("AGENT_GO_DIFFICULTY", ""),
            "prompt_tokens": claude_usage_total["prompt_tokens"],
            "completion_tokens": claude_usage_total["completion_tokens"],
            "cost_usd": _cost,
            "latency_ms": claude_usage_total["duration_ms"],
            "result": "success" if final_rc == 0 else "failed",
            "fallback_reason": "",
            "task_id": env.get("AGENT_GO_TASK_ID", ""),
            "subtask_id": sub_id,
            "num_turns": claude_usage_total["num_turns"],
        })
        logger.info(f"{PFX} Claude 执行计量: {claude_usage_total['prompt_tokens']}+{claude_usage_total['completion_tokens']} tokens, ${_cost:.4f}{' (本地模型, 成本 0)' if _is_local else ''}")

    _cp = subprocess.CompletedProcess(
        [], final_rc,
        stdout="\n".join(all_lines),
        stderr=""
    )
    # S12-P0 G1：把 kill_reason 附到返回对象（executor 读取写入子任务结果）
    _cp.kill_reason = final_kill_reason  # type: ignore[attr-defined]
    return _cp
