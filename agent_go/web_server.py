"""Web 观察平台：以只读 HTTP 接口展示 agent_go 任务执行数据（组合/入口层）。

设计目标：
  - 只读：全部 GET，不触碰 worktree/git，不修改任何任务数据
  - 无框架：仅 stdlib http.server + 单文件 HTML/JS 前端
  - 复用现有解析：meta.json / metering.jsonl / execution.log / replay 时间线

ISSUE-55 模块拆分（行为等价，公共 API 不变）：本模块保留为组合/入口层，
持有 serve_web/main 入口与被测试 monkeypatch 的叶子符号；各关注点落入：
  - web_data.py      观测 GET 数据层（api_* 纯函数、id 校验、审计追加）
  - web_ops.py       写处置端点 mixin（do_POST/do_PUT/do_DELETE + _op_*）
  - web_kanban.py    看板切面（api_kanban/状态快照 + WebKanbanMixin 写端点）
  - web_handler.py   传输/鉴权/SSE（WebHandler = WebOpsMixin + BaseHTTPRequestHandler）
  - web_frontend.py  单文件前端 SPA 模板（_SPA_HTML）
原有公开符号全部经本模块 re-export，调用方与测试的 import 路径不变。

CLI 入口: `agent_go web [--host 127.0.0.1] [--port 8091] [--token <secret>]`

API 一览（前缀 /api）：
  GET /api/tasks                      任务清单
  GET /api/tasks/<id>                 任务详情（subtasks + results）
  GET /api/tasks/<id>/<sub>/detail    子任务验证结果/改动统计
  GET /api/tasks/<id>/<sub>/log       子任务执行日志段
  GET /api/tasks/<id>/metering        metering 按 role 聚合 + 明细
  GET /api/tasks/<id>/replay          执行时间线（复用 replay.py）
  GET /api/tasks/<id>/plan            PLAN.md + plans/
  GET /api/tasks/<id>/assessment      假阳性评估事件（assessment.jsonl）
  GET /api/overview                   总览大盘：KPI + 近 7 天成本趋势
  GET /api/cost                       全局成本：by_model/by_role + Top 任务
  GET /api/models                     模型生产力：生产 metering + bench 对照
  GET /api/cross-judge                交叉评判矩阵（cross_judge_scores.jsonl）
  GET /api/bench-results              bench 模型对照结果
  GET /api/baseline                   claude 裸跑基线 + $/pass 门禁基线
  GET /api/config                     用户配置只读展示（api_key 脱敏）
  GET /api/storage                    磁盘占用 + 孤儿目录检测
  GET /api/events                     SSE：任务状态变化实时推送
"""
from __future__ import annotations

import logging
import subprocess
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .config import AGENT_GO_DIR, CONFIG_PATH, load_config  # noqa: F401  (monkeypatch 面 + 兼容 re-export)
from .console import _LazyConsole
from .profiles import (  # noqa: F401  (下行为兼容 re-export；probe_local_models 兼作 patch 面)
    ProfileError,
    activate_cloud,
    activate_local,
    activate_profile,
    health_check,
    list_profiles,
    probe_local_models,
    read_current_profile,
)
from .status import normalize_task_status, task_status  # noqa: F401  (兼容 re-export)
from .task_runner import TaskRunnerError, task_runner  # noqa: F401  (TaskRunnerError 兼容 re-export)
from . import kanban  # noqa: F401  (兼容 re-export，测试经 ws.kanban 访问)
from .kanban import KanbanError  # noqa: F401  (兼容 re-export)

# ── 拆分模块组合（ISSUE-55）：原公开符号全部 re-export，import 路径不变 ──
from .web_data import (  # noqa: F401  (re-export)
    MAX_LOG_LINE,
    _SUB_ID_RE,
    _TASK_ID_RE,
    _TASK_PREFIX,
    _audit,
    _extract_subtask_log,
    _insights_dir,
    _list_task_dirs,
    _parse_date,
    _task_dir,
    _task_meta,
    _task_status_of,
    _valid_sub_id,
    _valid_task_id,
    _CONFIG_EDIT_WHITELIST,
    _INSIGHT_NAME_RE,
    _RUNNING_STATES,
    add_note,
    api_assessment,
    api_audit,
    api_baseline,
    api_bench_batches,
    api_bench_results,
    api_config,
    api_config_diff,
    api_cost,
    api_cross_judge,
    api_decisions,
    api_deviation,
    api_health,
    api_insight_report,
    api_insights,
    api_local_tco,
    api_merge_preview,
    api_metering,
    api_models,
    api_notes,
    api_overview,
    api_plan,
    api_profiles,
    api_proxy_policies,
    api_replay,
    api_storage,
    api_subtask_detail,
    api_task,
    api_task_report,
    api_task_review,
    api_tasks,
    api_worktrees,
    put_config_field,
)
from .web_frontend import _SPA_HTML  # noqa: F401  (re-export)
from .web_kanban import (  # noqa: F401  (re-export)
    WebKanbanMixin,
    _task_status_cache,
    _task_status_lock,
    _task_status_sig,
    _task_status_snapshot,
    api_kanban,
)
from .web_handler import WebHandler  # noqa: F401  (re-export)
from .web_ops import WebOpsMixin  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)
console = _LazyConsole()


# ── 可 patch 叶子符号（测试 monkeypatch 打在 web_server 命名空间）────────
# web_data/web_ops/web_kanban 通过 web_data._root() 在调用时动态解析以下符号，
# 因此它们必须留在本模块定义/绑定，拆分前后补丁语义一致。

def _bench_results_path() -> Path:
    """定位 eval_suite/results.jsonl（优先 cwd，回退仓库根）。"""
    cwd_candidate = Path.cwd() / "eval_suite" / "results.jsonl"
    if cwd_candidate.exists():
        return cwd_candidate
    # 回退：web_server.py 所在包的上两级（仓库根）
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "eval_suite" / "results.jsonl"


def _resolve_workspace_file(name: str) -> Path:
    """定位工作区下的文件（优先 cwd，回退仓库根）。"""
    cwd_candidate = Path.cwd() / name
    if cwd_candidate.exists():
        return cwd_candidate
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / name


def _run_cli(argv: list[str], timeout: float = 180) -> dict[str, Any]:
    """同步执行 agent_go 子命令（快操作：clean/review-decision/merge），返回结构化结果。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_go"] + argv,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"命令超时（{timeout}s）"}
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def serve_web(host: str = "127.0.0.1", port: int = 8091,
              token: Optional[str] = None,
              viewer_token: Optional[str] = None) -> None:
    """启动 Web 操作台服务（阻塞）。

    鉴权（P1.2 多用户角色）：
      --token/--admin-token：admin 角色（全部操作）
      --viewer-token：viewer 角色（只读 GET；写操作 403）
      两者均未配置 → 全开放（向后兼容）

    U4 失控防护：
      - 启动时扫描疑似孤儿任务（EXECUTING 但无托管句柄）并警告
      - 关闭时 atexit → task_runner.kill_all()（SIGINT 优雅收尾，超时 SIGKILL）
    """
    import atexit

    httpd = ThreadingHTTPServer((host, port), WebHandler)
    httpd.admin_token = token or ""  # type: ignore[attr-defined]
    httpd.viewer_token = viewer_token or ""  # type: ignore[attr-defined]

    orphans = task_runner.orphan_tasks()
    if orphans:
        console.warning(
            f"⚠️ 检测到 {len(orphans)} 个疑似孤儿任务（状态 EXECUTING 但非本实例托管）: "
            f"{', '.join(orphans[:5])}{' …' if len(orphans) > 5 else ''}。"
            "若为残留进程请手工 kill，再用 resume 续跑。"
        )

    @atexit.register
    def _kill_children() -> None:
        n = task_runner.kill_all()
        if n:
            console.print(f"🛑 web 关闭：已终止 {n} 个托管任务进程（SIGINT 收尾）")

    console.print(f"🌐 agent_go web 观察平台: http://{host}:{port}")
    if token:
        console.print("🔐 token 鉴权已启用（Authorization: Bearer <token>）")
    console.print("⏹️  Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n⏹️  web 服务已停止")
        httpd.server_close()


def main(args: Any = None) -> None:  # CLI 入口
    """agent_go web [--host H] [--port P] [--token T] [--viewer-token VT]"""
    import argparse

    # 兼容两种调用：CLI 分发传入已解析 Namespace（args 无 .split 方法），
    # 直接命令行调用传入 argv 列表。
    if isinstance(args, argparse.Namespace):
        serve_web(host=args.host, port=args.port, token=args.token,
                  viewer_token=getattr(args, "viewer_token", None))
        return

    parser = argparse.ArgumentParser(prog="agent_go web",
                                     description="只读 Web 观察平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--token", default=None,
                        help="可选 admin Bearer token 鉴权（全部操作，默认关闭）")
    parser.add_argument("--viewer-token", default=None,
                        help="可选 viewer Bearer token（只读 GET；写操作 403）")
    ns = parser.parse_args(args)
    serve_web(host=ns.host, port=ns.port, token=ns.token, viewer_token=ns.viewer_token)
