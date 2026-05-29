from .common_imports import *
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import log_event

def _git_merge_upstream(src_worktree, dst_worktree, tag, logger, headless=False):
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

def _run_headless(task_md, worktree, env, logger, sub_id, active_pids=None, active_pids_lock=None):
    """无头模式：claude -p 带 stream-json 实时监控、交互检测和超时重试。"""
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
    # 退出码含义：130 = SIGINT（被中断），其他非零 = 错误
    EXIT_CODE_INTERACTION = 130
    IDLE_TIMEOUT = 600   # 10 分钟纯静默才 kill（思考阶段无任何事件）
    HEARTBEAT = 60       # 60s 无事件发心跳

    def _run_one(prompt, attempt):
        """启动 claude -p (stream-json) 并实时解析事件。"""
        proc = subprocess.Popen([
            "claude", "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ], env=env, cwd=str(worktree), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if active_pids_lock:
            with active_pids_lock:
                active_pids.add(proc.pid)
        else:
            active_pids.add(proc.pid)
        last_ts = [time.time()]
        lines = []
        waiting = [False]
        current_tool = [None]
        tool_input = [""]

        def parse_and_log(raw_line, label):
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
                        logger.debug(f"{PFX} [{current_tool[0]}] 完成")
                        current_tool[0] = None

            # assistant: 消息批次
            elif ev_type == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            t = block.get("text", "")
                            if t.strip():
                                lines.append(f"[{ts}] {t[:200]}")
                                logger.debug(f"{PFX} [assistant] {t[:200]}")
                        elif block.get("type") == "tool_use":
                            logger.debug(f"{PFX} [tool_use] {block.get('name', '?')}")

            # result: 最终结果
            elif ev_type == "result":
                subtype = event.get("subtype", "")
                logger.info(f"{PFX} [result] {subtype}")

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

        def read_stdout():
            for line in iter(proc.stdout.readline, ''):
                parse_and_log(line, "out")

        def read_stderr():
            for line in iter(proc.stderr.readline, ''):
                parse_and_log(line, "err")

        t_out = threading.Thread(target=read_stdout, daemon=True)
        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        idle_logged_at = 0
        while proc.poll() is None:
            idle = time.time() - last_ts[0]
            if idle > IDLE_TIMEOUT:
                logger.error(f"claude {idle:.0f}s 无事件 (attempt={attempt})，强制终止")
                proc.kill()
                break
            if idle > HEARTBEAT and idle - idle_logged_at > HEARTBEAT:
                logger.info(f"{PFX} 等待中... (无事件 {idle:.0f}s, attempt={attempt})")
                idle_logged_at = idle
            time.sleep(2)

        t_out.join()
        t_err.join()
        proc.wait()
        if active_pids_lock:
            with active_pids_lock:
                active_pids.discard(proc.pid)
        else:
            active_pids.discard(proc.pid)
        return proc, lines, waiting[0]

    RETRY_SUFFIX = (
"\n\n【系统指令】你必须立即完成上述所有任务，直接执行文件创建和修改操作。"
"不要询问任何问题，不要等待确认，不要输出中间讨论。"
"完成后输出简洁的状态报告和变更摘要。"
    )
    MAX_ATTEMPTS = 2

    logger.info(f"{PFX} 无头模式: claude -p")
    log_event(logger, "subtask_headless_start", {"id": sub_id})

    all_lines = []
    final_rc = -1
    interaction = False

    for attempt in range(MAX_ATTEMPTS):
        if attempt == 0:
            prompt = task_md
        else:
            logger.warning(f"超时重试 (attempt={attempt+1})，注入催促指令")
            log_event(logger, "subtask_headless_retry", {"id": sub_id, "attempt": attempt + 1})
            prompt = task_md + RETRY_SUFFIX

        proc, lines, waiting = _run_one(prompt, attempt + 1)
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

    return subprocess.CompletedProcess(
        [], final_rc,
        stdout="\n".join(all_lines),
        stderr=""
    )
