"""子进程任务运行器（Web 操作台 M2）：run/resume/review-deep 异步操作管理。

设计约束（v2 §B2）：
  - 与 MCP "Thin shell" 同哲学：spawn `python -m agent_go <cmd> --yes --json` 子进程，
    行为与 CLI 完全等价；不在 web 进程内调用 cmd_*（避免全局状态污染）。
  - **meta.json + status.py 是唯一事实源**：本模块只持有进程句柄（内存态，web 重启即失效），
    不持久化任何任务状态。任务状态查询走 web_server 现有 api_task()/status.py。
  - cancel：对句柄发 SIGINT（与 CLI Ctrl+C 同语义，pipeline 的 SIGINT 处理负责收尾）。

句柄表 key 约定：run → 启动后解析出的 task_id；review-deep → "review:<task_id>"。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

START_EVENT_TIMEOUT = 30  # 等待 --json 首事件（含 task_id）的超时秒数


class TaskRunnerError(Exception):
    """任务启动/操作失败（消息面向用户可读）。"""


class TaskRunner:
    """agent_go 子进程句柄管理（线程安全）。"""

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # ── 内部 ─────────────────────────────────────────────────

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )

        def _drain_stderr() -> None:
            try:
                for _ in proc.stderr or []:
                    pass
            except Exception:
                pass

        threading.Thread(target=_drain_stderr, daemon=True).start()
        return proc

    def _register(self, key: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[key] = proc

    def _unregister(self, key: str) -> None:
        with self._lock:
            self._procs.pop(key, None)

    def _reap(self, key: str, proc: subprocess.Popen,
              on_exit: Optional[Callable[[str, int], None]]) -> None:
        proc.wait()
        self._unregister(key)
        if on_exit:
            try:
                on_exit(key, proc.returncode)
            except Exception:
                logger.exception("on_exit callback failed for %s", key)

    def _read_task_id(self, proc: subprocess.Popen,
                      timeout: float = START_EVENT_TIMEOUT) -> str:
        """从 --json 事件流读首个 task_id；fallback 取最新任务目录（MCP 同款逻辑）。

        注意：首行读取后 stdout 继续被后台线程消费（事件只需 task_id，
        任务进度由 web SSE 轮询 meta.json 提供，不依赖本流）。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                line = line.strip()
                if line:
                    try:
                        ev = json.loads(line)
                        tid = ev.get("task_id") or (ev.get("data") or {}).get("task_id")
                        if tid:
                            return str(tid)
                    except json.JSONDecodeError:
                        continue
            else:
                time.sleep(0.1)
        dirs = sorted(AGENT_GO_DIR.glob("task-*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if dirs:
            return dirs[0].name
        raise TaskRunnerError("任务已启动但未返回 task_id（--json 事件超时）")

    def _drain_remaining(self, proc: subprocess.Popen) -> None:
        """task_id 已读到后，后台 drain 剩余 stdout，防管道阻塞。"""
        def _drain() -> None:
            try:
                for _ in proc.stdout or []:
                    pass
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()

    # ── 公开操作 ─────────────────────────────────────────────

    def start_run(self, repo: str, task: str, parallel: int = 1,
                  goal: Optional[bool] = None, confirm_mode: str = "auto",
                  on_exit: Optional[Callable[[str, int], None]] = None) -> str:
        """启动新任务（agent_go --json run ... --yes）。

        confirm_mode：auto（默认，--yes 跳过计划确认）；
                      web（R5b，--confirm-mode web，Plan/子任务确认走 web 文件协议）。
        返回解析出的 task_id。子进程启动失败/首事件超时抛 TaskRunnerError。
        """
        argv = [sys.executable, "-m", "agent_go", "--json", "run", repo, task,
                "--parallel", str(max(1, min(8, int(parallel)))),
                "--yes"]
        if confirm_mode == "web":
            argv += ["--confirm-mode", "web"]
        if goal is True:
            argv.append("--goal")
        elif goal is False:
            argv.append("--no-goal")
        try:
            proc = self._spawn(argv)
        except OSError as e:
            raise TaskRunnerError(f"无法启动 agent_go 子进程: {e}") from e
        task_id = self._read_task_id(proc)
        if proc.poll() is not None and proc.returncode not in (0, None):
            raise TaskRunnerError(f"任务启动后立即退出（exit {proc.returncode}）")
        self._drain_remaining(proc)
        self._register(task_id, proc)
        threading.Thread(target=self._reap, args=(task_id, proc, on_exit), daemon=True).start()
        return task_id

    def start_resume(self, task_id: str, parallel: int = 1,
                     on_exit: Optional[Callable[[str, int], None]] = None) -> str:
        """恢复任务（agent_go --json resume <id> --yes）。"""
        argv = [sys.executable, "-m", "agent_go", "--json", "resume", task_id,
                "--parallel", str(max(1, min(8, int(parallel)))),
                "--yes"]
        try:
            proc = self._spawn(argv)
        except OSError as e:
            raise TaskRunnerError(f"无法启动 agent_go 子进程: {e}") from e
        self._drain_remaining(proc)
        self._register(task_id, proc)
        threading.Thread(target=self._reap, args=(task_id, proc, on_exit), daemon=True).start()
        return task_id

    def start_review_deep(self, task_id: str,
                          on_exit: Optional[Callable[[str, int], None]] = None) -> str:
        """触发深层审查（agent_go --json review --task <id> --deep --yes），独立模型逐子任务分析。"""
        key = f"review:{task_id}"
        argv = [sys.executable, "-m", "agent_go", "--json", "review", "--task", task_id,
                "--deep", "--yes"]
        try:
            proc = self._spawn(argv)
        except OSError as e:
            raise TaskRunnerError(f"无法启动 agent_go 子进程: {e}") from e
        self._drain_remaining(proc)
        self._register(key, proc)
        threading.Thread(target=self._reap, args=(key, proc, on_exit), daemon=True).start()
        return key

    def cancel(self, key: str, timeout: float = 10) -> bool:
        """对句柄发 SIGINT（Ctrl+C 同义）。句柄不存在返回 False。"""
        import signal
        with self._lock:
            proc = self._procs.get(key)
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            return False
        return True

    def kill_all(self, grace_timeout: float = 5.0) -> int:
        """终止全部托管子进程（U4：web 关闭时调用，防孤儿失控）。

        先 SIGINT（pipeline 优雅收尾），超时未退则 SIGKILL。返回终止进程数。
        """
        import signal
        with self._lock:
            items = [(k, p) for k, p in self._procs.items() if p.poll() is None]
        for key, p in items:
            try:
                p.send_signal(signal.SIGINT)
                logger.info("kill_all: SIGINT → %s", key)
            except OSError:
                pass
        deadline = time.time() + grace_timeout
        for key, p in items:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    logger.warning("kill_all: SIGKILL → %s（SIGINT 超时）", key)
                except OSError:
                    pass
        return len(items)

    def orphan_tasks(self) -> list[str]:
        """疑似孤儿任务：meta 状态为运行中但不在本实例句柄表（U4/U5 检测）。

        场景：web 重启后原 web 启动的任务进程已失（或 CLI 启动的任务）。
        返回 task_id 列表（状态读 meta.json，唯一事实源）。
        """
        from .status import normalize_task_status
        orphans = []
        for td in AGENT_GO_DIR.glob("task-*"):
            if not td.is_dir():
                continue
            meta_path = td / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            status = normalize_task_status(meta.get("status", ""), meta)
            # 仅 EXECUTING 视为疑似孤儿（PAUSED 是用户主动暂停的正常状态，不警告）
            if status == "EXECUTING" and not self.is_running(td.name):
                orphans.append(td.name)
        return sorted(orphans)

    def is_running(self, key: str) -> bool:
        with self._lock:
            proc = self._procs.get(key)
        return proc is not None and proc.poll() is None

    def running_keys(self) -> list[str]:
        with self._lock:
            return [k for k, p in self._procs.items() if p.poll() is None]


# web_server 模块级共享实例
task_runner = TaskRunner()
