# agent_go 基础设施化 — API 与集成设计

> 状态：草案（2026-07-25）
> 目的：论证将 agent_go 从「单机 CLI 工具」升级为「可编程开发基础设施」的必要性和可行性。
> 后续：补充必要性论证和可行性分析后再决策是否实施。

---

## 1. 目标定位

### 当前

```
agent_go = CLI 工具
  用户 → 手动输入命令 → 看终端输出
  每个 run 独立，不与其他系统交互
```

### 目标

```
agent_go = 可编程开发基础设施
  CI/CD Pipeline ─┐
  IDE Plugin      ─┼──→ agent_go Core → TaskResult / Event / Knowledge
  Git Hooks       ─┘
  项目管理平台     ─→ Webhook ← agent_go emit_event
```

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                     Integration Layer                        │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌──────────────┐  │
│  │ GitHub    │  │ GitLab   │  │ VS Code│  │ Pre-commit   │  │
│  │ Actions   │  │ CI/CD    │  │ Plugin │  │ Hook         │  │
│  └────┬─────┘  └────┬─────┘  └───┬────┘  └──────┬───────┘  │
│       │              │            │               │          │
├───────┴──────────────┴────────────┴───────────────┴─────────┤
│                     Protocol Layer                           │
│  ┌──────────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ CLI (--json 输出) │  │ Python API   │  │ Webhook/Event  │  │
│  │ 子进程调用入口    │  │ import 入口   │  │ JSON 协议      │  │
│  └────────┬─────────┘  └──────┬───────┘  └───────┬────────┘  │
│           │                   │                    │          │
├───────────┴───────────────────┴────────────────────┴─────────┤
│                        Core Layer                             │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌──────────────┐   │
│  │ Plan     │  │ Execute  │  │ Verify │  │ Knowledge    │   │
│  │ Engine   │  │ Engine   │  │ Engine │  │ Store        │   │
│  └──────────┘  └──────────┘  └────────┘  └──────────────┘   │
│  ┌──────────┐  ┌──────────┐  ┌────────┐                      │
│  │ Events   │  │ Query    │  │ Config │                      │
│  │ Bus      │  │ API      │  │        │                      │
│  └──────────┘  └──────────┘  └────────┘                      │
└──────────────────────────────────────────────────────────────┘
```

### 分层职责

| 层 | 职责 | 技术选型 |
|----|------|---------|
| **Integration** | 与外部系统对接 | GitHub Action / VS Code Extension / pre-commit |
| **Protocol** | 跨进程/跨语言通信契约 | JSON Schema / CLI `--json` / Webhook |
| **Core** | 业务逻辑 + 数据存储 | Python stdlib（零外部依赖） |

### 仓储策略

```
┌─────────────────────────────────────┐
│  agent_go（核心仓库）                │
│  新增模块：                          │
│    knowledge/  — 知识存储与注入      │
│    events.py   — 事件总线            │
│    query.py    — 状态查询 API        │
│  增强模块：                          │
│    __init__.py — 公共 Python API     │
│    cli.py      — --json 输出模式     │
│    notify.py   — 全生命周期事件通知   │
└──────────┬──────────────────────────┘
           │
           │ pip install agent-go
           │
           ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ agent-go-action  │  │ vscode-agent-go  │  │ pre-commit-      │
│ GitHub Action    │  │ VS Code 插件     │  │ agent-go         │
│ （独立仓库）      │  │ （独立仓库）      │  │ Hook（独立仓库）  │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

**原则**：
- Core 层与 CLI 放在同一仓库——共享数据模型、配置、数据目录，避免接口漂移
- 外围集成工具独立仓库——解耦发布节奏，支持不同语言（TypeScript/Shell/Python）

---

## 3. 接口契约

### 3.1 Python API — `agent_go/__init__.py`

```python
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "run_task", "resume_task",
    "query_task", "query_project_trend",
    "emit_event", "subscribe_event",
    "KnowledgeStore",
    "TaskResult", "SubtaskResult", "Event",
]

# ── 数据模型 ──────────────────────────────────

@dataclass
class SubtaskResult:
    id: str
    title: str
    status: str              # "completed"|"failed"|"blocked"|"no_changes"
    agent_type: str
    duration_sec: float
    verify_ok: bool
    retry_count: int
    failure_reason: str = ""
    cost_usd: float = 0.0
    change_stats: dict = field(default_factory=dict)

@dataclass
class TaskResult:
    task_id: str
    status: str              # "completed"|"failed"|"paused"
    pass_rate: float         # 0.0 ~ 1.0
    cost_usd: float
    duration_sec: float
    subtasks: list[SubtaskResult]
    meta_path: Path

# ── 核心 API ──────────────────────────────────

def run_task(
    repo: str | Path,
    task: str,
    *,
    config: Optional[dict] = None,
    parallel: int = 1,
    auto_yes: bool = False,
    docs: Optional[list[str]] = None,
    remote: str = "",
    skills: Optional[list[str]] = None,
    agent_type: str = "",
    timeout: Optional[int] = None,
) -> TaskResult:
    """执行一个完整的 Plan → Execute → Verify 管道。

    Returns:
        TaskResult: 结构化结果，含所有子任务明细。
        调用方可通过 .status 判断成功/失败，.pass_rate 了解通过率。
    """

def resume_task(task_id: str) -> TaskResult:
    """恢复暂停/中断的任务。"""

# ── 查询 API ──────────────────────────────────

def query_task(task_id: str) -> Optional[TaskResult]:
    """根据 task_id 读取已完成/运行中的任务结果。"""

def query_project_trend(
    repo: str | Path,
    days: int = 30,
) -> dict:
    """返回该仓库近 N 天的质量和成本趋势。

    Returns:
        {
            "tasks_analyzed": int,
            "avg_pass_rate": float,
            "avg_cost_per_subtask": float,
            "avg_dollar_per_pass": float,
            "trend": [
                {"date": "2026-07-01", "pass_rate": 0.85, "cost": 0.12},
                ...
            ]
        }
    """

# ── 事件 API ──────────────────────────────────

@dataclass
class Event:
    type: str                 # "subtask.started"|"subtask.completed"|"pipeline.completed"|...
    task_id: str
    subtask_id: str = ""
    payload: dict = field(default_factory=dict)

EventHandler = callable[[Event], None]

def emit_event(event: Event) -> None:
    """同步触发所有已注册的 handler。"""

def subscribe_event(
    event_type: str,
    handler: EventHandler,
    *,
    once: bool = False,
) -> None:
    """注册事件监听器。

    Args:
        event_type: 事件类型（支持 glob 模式，如 "subtask.*" 匹配所有子任务事件）
        handler: 回调函数
        once: True 时触发一次后自动移除
    """

# ── 知识存储 API ──────────────────────────────

class KnowledgeStore:
    """项目级经验沉淀与注入。

    数据存储在 ~/.agent_go/knowledge/<repo-hash>/ 下，
    纯 JSON 文件，零外部依赖。
    """

    def __init__(self, repo_path: str | Path): ...

    def record_success(
        self,
        category: str,        # "verified_cmd"|"decomposition"|"project_pattern"
        content: str,
        source_task: str,
        score: float = 1.0,
    ) -> None:
        """记录一次成功经验。"""

    def record_failure(
        self,
        category: str,        # "failure_signal"|"flakey_test"
        signal: str,
        source_task: str,
        context: str = "",
    ) -> None:
        """记录一次失败信号。"""

    def get_relevant(
        self,
        task_description: str,
        max_results: int = 5,
    ) -> list[dict]:
        """获取与任务描述最相关的历史经验。

        匹配策略：关键词重叠 + 高频优先。
        Returns:
            [{"type": "verified_cmd", "content": "pytest tests/ -x -q",
              "score": 0.9, "source": "task-xxx"}, ...]
        """

    def get_project_profile(self) -> dict:
        """获取项目特征（从历史运行中自动推断）。

        Returns:
            {"language": "python", "test_framework": "pytest",
             "build_tool": "poetry", "verified_commands": [...],
             "common_failures": [...]}
        """
```

### 3.2 JSON Schema — 文件交换格式

跨进程/跨语言集成时，不依赖 Python，通过标准 JSON 文件交换。

#### meta.json（增强字段）

```json
{
  "schema_version": "2.1",
  "task_id": "task-1721800000",
  "status": "completed",
  "task": "为支付模块补充边界测试",
  "created": "2026-07-25T10:00:00",
  "repo": "/path/to/repo",
  "base_branch": "main",
  "pass_rate": 0.92,
  "cost_usd": 0.15,
  "duration_sec": 240,
  "subtasks": [
    {
      "id": "sub-1",
      "title": "编写测试用例",
      "agent_type": "developer",
      "status": "completed",
      "verify_ok": true,
      "duration_sec": 120,
      "retry_count": 0,
      "cost_usd": 0.03
    }
  ],
  "review": {
    "decision": "approved",
    "reviewed_at": "2026-07-26T09:00:00"
  },
  "metrics": {
    "k1_subtask_pass_rate": 0.92,
    "k2_first_pass_rate": 0.80,
    "k3_cost_per_subtask": 0.03
  }
}
```

#### events.jsonl（新增）

```jsonl
{"type":"subtask.started","ts":"2026-07-25T10:01:00","task_id":"task-xxx","subtask_id":"sub-1","payload":{}}
{"type":"subtask.completed","ts":"2026-07-25T10:05:00","task_id":"task-xxx","subtask_id":"sub-1","payload":{"status":"completed","duration_sec":240,"verify_ok":true}}
{"type":"pipeline.completed","ts":"2026-07-25T10:20:00","task_id":"task-xxx","payload":{"status":"completed","pass_rate":0.92,"cost_usd":0.15}}
```

### 3.3 Webhook Payload — 外部系统集成

```http
POST /webhook/agent-go HTTP/1.1
Content-Type: application/json
X-Agent-Go-Signature: sha256=...

{
  "schema_version": "1.0",
  "event": "pipeline.completed",
  "task_id": "task-1721800000",
  "ts": "2026-07-25T10:20:00Z",
  "payload": {
    "status": "completed",
    "repo": "git@github.com:org/repo.git",
    "branch": "agent_go/task-xxx/main",
    "subtasks": {
      "total": 5,
      "passed": 4,
      "failed": 1,
      "blocked": 0
    },
    "cost_usd": 0.15,
    "pass_rate": 0.8,
    "pr_url": "https://github.com/org/repo/pull/42",
    "preserved_worktrees": [
      "/tmp/agent_go/task-xxx/sub-3"
    ]
  }
}
```

### 3.4 CLI `--json` 标志

所有 CLI 子命令增加 `--json` 标志，输出可解析的 JSON，供外部脚本调用：

```bash
# 核心执行
agent_go run <repo> '<task>' --yes --json
# → {"task_id":"task-xxx","status":"completed","pass_rate":0.92,...}

# 恢复
agent_go resume <task-id> --json

# 状态查询
agent_go status --json
# → {"tasks":[{"id":"task-xxx","status":"running","progress":"3/5"},...]}

# 成本分析
agent_go eval cost --json
# → {"total_cost":0.15,"by_model":{"claude-sonnet-4":0.12},...}
```

---

## 4. 事件系统

### 4.1 事件全生命周期

```python
# 事件类型枚举（按阶段）
EVENT_TYPES = {
    # Plan 阶段
    "plan.generated":       "Plan 生成完毕",
    "plan.confirmed":       "Plan 已确认",
    "plan.rejected":        "Plan 被拒绝（重新生成）",

    # 执行阶段
    "subtask.started":      "子任务开始",
    "subtask.retrying":     "子任务重试（含第 N 次+失败原因）",
    "subtask.completed":    "子任务完成（含验证结果）",
    "subtask.failed":       "子任务失败（含最终失败原因）",
    "subtask.blocked":      "子任务被阻断",

    # 管道阶段
    "pipeline.completed":   "全部完成",
    "pipeline.failed":      "整体失败",
    "pipeline.paused":      "中断暂停",

    # 审查阶段
    "review.approved":      "审查通过",
    "review.changes_requested": "审查打回",
    "review.rejected":      "审查拒绝",
}
```

### 4.2 事件存储

```python
# events.jsonl 文件（每个 task 一个）
~/.agent_go/task-xxx/events.jsonl

# 格式：JSON Lines，每行一个事件
# 行级别用 \n 分隔，不包含内部换行
```

### 4.3 事件订阅

```python
# 方式 1：进程内订阅
from agent_go import subscribe_event

def on_subtask_failed(event):
    notify_slack(f"子任务 {event.subtask_id} 失败: {event.payload['failure_reason']}")

subscribe_event("subtask.failed", on_subtask_failed)

# 方式 2：Webhook 订阅（配置驱动）
# ~/.agent_go/config.json
{
  "events": {
    "webhooks": [
      {
        "url": "https://hooks.slack.com/xxx",
        "events": ["pipeline.completed", "pipeline.failed"],
        "format": "slack"
      },
      {
        "url": "https://api.github.com/repos/org/proj/dispatches",
        "events": ["review.approved"],
        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
      }
    ]
  }
}
```

---

## 5. 知识存储

### 5.1 数据目录结构

```
~/.agent_go/knowledge/
  └── <repo-hash>/
      ├── patterns.json          # 成功模式（验证命令/分解策略）
      ├── failure-signals.json   # 失败信号（高频失败原因 + 上下文）
      ├── verified-cmds.json     # 已验证命令（exit_code=0 的命令行）
      └── project-meta.json      # 项目特征（语言/框架/构建工具/测试框架）
```

### 5.2 数据格式

```json
// patterns.json
{
  "schema_version": "1.0",
  "repo_path": "/path/to/repo",
  "updated_at": "2026-07-25T10:00:00",
  "source_tasks": 12,
  "patterns": [
    {
      "type": "verified_cmd",
      "content": "pytest tests/ -x -q --timeout=30",
      "score": 0.92,
      "source": "task-xxx",
      "created_at": "2026-07-20T..."
    },
    {
      "type": "decomposition",
      "content": "数据库迁移类任务应拆为 3 步：schema 变更→数据迁移→回滚脚本",
      "score": 0.85,
      "source": "task-yyy",
      "created_at": "2026-07-22T..."
    }
  ],
  "failure_signals": [
    {
      "signal": "pytest 超时（测试环境网络不通）",
      "frequency": 4,
      "last_seen": "2026-07-24T..."
    }
  ]
}
```

### 5.3 Plan 注入机制

Planner prompt 末尾自动追加知识注入段：

```
## 项目历史经验（来自本仓库 ${N} 次历史运行）

已验证的验证命令（成功率 Top-3）：
  1. pytest tests/ -x -q （成功率 92%）
  2. poetry run pytest tests/ （成功率 85%）

高频失败模式：
  - pytest 超时 → 可能是测试环境网络不通，建议设置 --timeout=30
  - 数据库 migration 冲突 → 建议拆为 schema→数据→回滚 三步

项目特征：
  - 语言: Python
  - 测试框架: pytest
  - 构建工具: poetry
```

### 5.4 增量更新

每次 pipeline 完成后自动调用 `_extract_patterns()`：

```python
def _extract_patterns(task_dir: Path, repo: Path) -> None:
    """从本次运行结果中提取知识，增量更新 KnowledgeStore。"""
    store = KnowledgeStore(repo)

    # 1. 提取成功验证命令
    for r in meta.get("results", []):
        if r.get("verify_ok"):
            for v in r.get("verification_commands", []):
                store.record_success("verified_cmd", v, task_id)

    # 2. 提取失败信号
    for r in meta.get("results", []):
        if r.get("failure_reason"):
            store.record_failure("failure_signal",
                                  r["failure_reason"][:200],
                                  task_id)

    # 3. 提取项目特征（仅首次）
    if not store.get_project_profile():
        profile = detect_project_profile(repo)
        store.save_project_profile(profile)
```

---

## 6. 集成场景

### 6.1 CI/CD — GitHub Actions

```yaml
# .github/workflows/agent-go-nightly.yml
name: agent-go nightly refactor
on:
  schedule:
    - cron: '0 6 * * 1-5'  # 工作日早上 6 点
jobs:
  refactor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run agent_go
        uses: agent-go/action@v1
        with:
          task: '清理废弃代码和过时的 TODO 注释'
          parallel: 3
          auto-yes: true
          remote: origin
      - name: Gate check
        run: |
          agent_go eval gate --baseline 0.05
```

### 6.2 Pre-commit — 提交前自动验证

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/agent-go/pre-commit
    rev: v2.0.0
    hooks:
      - id: agent-go-verify
        args: ["--timeout", "120"]
        # 提交前自动跑验证命令，失败则阻止 commit
      - id: agent-go-format
        args: ["--check-only"]
        # 检查代码格式（只读，不改写）
```

### 6.3 IDE — VS Code Extension

```
功能提案：
  左侧 Activity Bar 新增 agent_go 面板
    ├── 当前任务进度（子步骤轮播 + 预计剩余时间）
    ├── 历史任务列表（通过率/成本/耗时）
    ├── 一键运行（input box 输入任务描述）
    └── 审查入口（diff 展示 + approve/reject 按钮）

实现方式：
  VS Code Extension（TypeScript）→ 子进程调用 agent_go --json
  → 解析 stdout JSON → 渲染 WebView
```

### 6.4 项目管理平台 — Jira / Linear

```
Webhook 集成流程：
  1. Jira 创建 Task → Webhook POST → 触发 agent_go run
  2. agent_go 执行中 → emit_event → Webhook → Jira 更新状态为 "In Progress"
  3. agent_go 完成 → emit_event → Webhook → Jira 更新状态为 "Review"
  4. agent_go review --approve → Webhook → Jira 更新状态为 "Done"
```

---

## 7. 安全模型

```python
# 4 层安全控制
Layer 1: API Key 管理
  ├── AGENT_GO_API_KEY 环境变量（首选）
  ├── config.json `api_key` 字段
  ├── 不记录到日志，不暴露在 metering 中
  └── 子进程环境变量已做净化（_build_sandbox_env）

Layer 2: 命令白名单（现有）
  ├── 4 阶段校验：shlex 解析 → 注入检测 → 白名单匹配 → token 验证
  ├── 28 种安全工具
  └── Python API 调用不走此校验（调用方自行负责）

Layer 3: Webhook 安全
  ├── HTTPS 强制
  ├── Signature header（HMAC-SHA256）
  └── 敏感字段不在 webhook payload 中出现

Layer 4: 跨进程调用安全
  ├── CLI --json 输出不包含 API Key
  ├── events.jsonl 不包含敏感信息
  └── KnowledgeStore 数据仅限项目级经验，不含凭据
```

---

## 8. 迭代路线

| Phase | 能力 | 涉及模块 | 预估 |
|-------|------|---------|------|
| **P0** | Python API 增强：`run_task()` 返回 `TaskResult` | `__init__.py`, `cli.py` | ~2d |
| **P0** | CLI `--json` 标志：所有子命令支持 JSON 输出 | `cli.py`, `eval.py` | ~1d |
| **P1** | Event Bus + `emit_event` / `subscribe_event` | 新增 `events.py` | ~1d |
| **P1** | 事件 Webhook 订阅 + `events.jsonl` 持久化 | `notify.py`, `events.py` | ~1d |
| **P1** | `query_task()` / `query_project_trend()` | 新增 `query.py` | ~1d |
| **P2** | `KnowledgeStore` 数据模型 + 文件读写 | 新增 `agent_go/knowledge/` | ~1d |
| **P2** | `_extract_patterns` 增量更新 + Plan 注入 | `executor.py`, `api.py` | ~1d |
| **P2** | `get_project_profile()` 自动推断 | `knowledge/` | ~1d |
| **P3** | `agent-go-action` GitHub Action | 独立仓库 | ~2d |
| **P3** | `pre-commit-agent-go` hook | 独立仓库 | ~1d |
| **P4** | `vscode-agent-go` extension | 独立仓库 | ~3d |

### 依赖关系

```
P0 ───────────────────────────────────── P1 ──────── P2 ──────── P3/P4
       run_task() API          →     Event Bus     → 知识存储    → 外围集成
       --json CLI              →     query API        Plan注入      GitHub Action
                                                      _extract      VS Code
```

### 对现有代码的影响

| 影响面 | 程度 | 说明 |
|--------|------|------|
| `cli.py` 重构 | 中 | `cmd_run` 拆为 `_execute_plan()`（返回 dict）+ CLI 包装层（print+exit） |
| `__init__.py` 扩展 | 小 | 新增公共 API 导出 |
| `executor.py` 扩展 | 小 | 末尾追加 `_extract_patterns()` 调用 |
| `notify.py` 扩展 | 中 | 新增全生命周期事件，非仅 completion |
| `api.py` 扩展 | 小 | Plan prompt 末尾追加 knowledge 注入段 |
| 新增文件 | 3 个模块 | `events.py`, `query.py`, `knowledge/` |
| 不修改 | — | `subtask.py`, `git_utils.py`, `ui.py`, `agents.py`, `skills.py` |

---

## 9. 设计原则（约束）

1. **零外部依赖不动** — 所有新增模块只用 Python stdlib。KnowledgeStore 用 JSON 文件，不用 SQLite/向量库。
2. **CLI 优先** — 所有 API 功能均可通过 CLI `--json` 调用。Python API 是 CLI 的超集，不是替代。
3. **增量不改既有路径** — 现有 `cmd_run()` 交互式路径不变。新增 `run_task()` 是可选入口。
4. **事件不阻塞主流程** — `emit_event()` 同步调用 handler，但 handler 失败不影响 pipeline 继续。Webhook 异步发送。
5. **文件系统是数据平面** — 所有状态通过文件共享（`meta.json`, `events.jsonl`, `knowledge/`）。不引入独立服务/守护进程。
6. **知识注入是可选增强** — 即使 KnowledgeStore 为空，Planner 也能工作（回退到现有行为）。
