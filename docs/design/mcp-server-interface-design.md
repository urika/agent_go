# agent_go MCP Server 接口设计

| 字段 | 值 |
|---|---|
| 文档版本 | v1.1 |
| 状态 | ✅ M1+M2+M3 已完成 |
| 关联文档 | [`interaction-design-spec.md`](./interaction-design-spec.md)（§4.6 JSON 事件 schema）、[`design-decisions.md`](./design-decisions.md)（ADR-002）、`competitive-engineering-analysis.md`（§6 反向嵌入路径） |
| 上游输入 | OpenClaw/Hermes 集成评估（反向嵌入路径） |

> 目标：把 agent_go 包装为标准 MCP server，让 Hermes / OpenClaw / Claude Code 等 MCP 宿主在对话中触发**结构化工程编排**（Plan→Decompose→Execute），并接收流式进度与结构化结果。
>
> **架构原则：薄壳、零侵入。** server 不改 agent_go 内核，而是 spawn `python3 agent_go.py <cmd> --json` 子进程，解析 stdout 的 JSON Lines 并转发为 MCP 通知。唯一前置依赖是 `--json` 输出模式（IDS §4.6 / ADR-002，P0.5）；在其实现前提供 degraded 模式（轮询 meta.json），保证今天就能落地。

---

## 1. 目标与非目标

**目标**
- 4 个工具覆盖任务全生命周期：`run_task` / `resume_task` / `inspect_task` / `review_task`
- 长任务（分钟~小时级）的双模语义：异步立即返回 + 同步等待（progress notification 流式推进）
- 事件流复用 IDS §4.6 的 JSON 事件 taxonomy，单一 schema 三处消费（终端 / MCP / CI）
- stdio transport，Python stdlib 实现，不引入第三方依赖

**非目标**
- 不做 HTTP/SSE transport（stdio 覆盖本地宿主场景；远程场景由宿主侧 gateway 承担）
- 不做任务队列/多租户（MCP server 是单用户本地进程）
- 不暴露 agent_go 全部 CLI（`eval`/`bench`/`router` 等管理命令不进 MCP，面越小越好）

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ MCP 宿主（Hermes / OpenClaw / Claude Code）                  │
│   "把 auth 从 JWT 迁到 OAuth2"                               │
└──────────────────────┬──────────────────────────────────────┘
                       │ JSON-RPC 2.0 over stdio
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ agent_go-mcp (mcp_server.py, ~300 行 stdlib)                │
│                                                             │
│  tools/call ──► argv 构造 ──► subprocess.Popen              │
│                                  python3 agent_go.py run …  │
│                                     --yes --json --parallel 3│
│                                                             │
│  stdout JSONL ◄── 逐行解析 ◄── 子进程                        │
│       │                                                     │
│       ├─► notifications/progress（wait=true 时按 progressToken 转发）│
│       ├─► notifications/message（warning/error 日志）         │
│       └─► 终止事件 ──► tools/call result (structuredContent) │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
              ~/.agent_go/task-*/
              (meta.json / results / metering.jsonl)
```

**进程模型**：
- 每个 MCP server 实例是宿主的子进程（stdio），生命周期随宿主会话。
- `run_task`/`resume_task` 的 agent_go 子进程 **detached 启动**，不随 MCP server 退出而死亡（任务级 pause/resume 是 agent_go 既有能力，恰好匹配 MCP 会话易失的现实）。
- 同一 MCP server 内允许最多 N 个并发任务（默认 3，可配），超限返回 `AGENT_GO_CAPACITY` 错误。

**Degraded 模式**（`--json` 未实现前的过渡）：spawn 不带 `--json` 的 agent_go，server 侧以 2s 周期轮询 `task_dir/meta.json` + `*/result.json`，diff 结果数组合成进度事件。事件粒度较粗（无 plan/subtask 中间事件），但保证 run/resume/inspect/review 四工具全部可用。

---

## 3. 工具 Schema

四个工具均声明 MCP **tool annotations**（宿主可据此做审批策略：只读工具免审批，变更工具先确认）。

### 3.1 `run_task` — 创建并执行任务

```json
{
  "name": "run_task",
  "description": "对指定仓库执行结构化工程任务：LLM 生成 Plan → 拆解子任务 → git worktree 隔离并发执行 → 验证重试 → 报告。默认异步返回 task_id；wait=true 时阻塞至完成并流式推送进度。",
  "annotations": {
    "title": "Run structured engineering task",
    "readOnlyHint": false,
    "destructiveHint": false,
    "idempotentHint": false,
    "openWorldHint": true
  },
  "inputSchema": {
    "type": "object",
    "required": ["repo", "task"],
    "properties": {
      "repo": {
        "type": "string",
        "description": "目标仓库绝对路径。必须在 server 配置的 repo allowlist 内，否则 fail-closed 拒绝。"
      },
      "task": {
        "type": "string",
        "description": "自然语言任务描述，如 '重构认证模块，从 JWT 迁移到 OAuth2'"
      },
      "docs": {
        "type": "array",
        "items": { "type": "string" },
        "description": "参考文档路径（仓库相对或绝对），注入 Plan 上下文"
      },
      "skills": {
        "type": "array",
        "items": { "type": "string" },
        "description": "显式指定 skill 名；缺省走 role_skill_map 规则匹配"
      },
      "agent_type": {
        "type": "string",
        "enum": ["developer", "architect", "reviewer", "tester"],
        "description": "统一默认 Agent 类型；缺省由规则/LLM 决定"
      },
      "parallel": {
        "type": "integer", "minimum": 1, "maximum": 8, "default": 1,
        "description": "最大并发子任务数（隐含 headless）"
      },
      "max_retries": {
        "type": "integer", "minimum": 0, "maximum": 10,
        "description": "验证失败最大修复重试次数（默认读 config=3）"
      },
      "remote": {
        "type": "string",
        "description": "可选。完成后将 worktree 分支推送到该 remote"
      },
      "preserve_worktrees": {
        "type": "boolean",
        "description": "true=保留全部 worktree；缺省仅保留 failed/blocked"
      },
      "wait": {
        "type": "boolean", "default": false,
        "description": "false=异步立即返回 task_id；true=阻塞至完成/超时并推送 progress 通知"
      },
      "timeout_sec": {
        "type": "integer", "default": 3600, "minimum": 60, "maximum": 21600,
        "description": "仅 wait=true 生效。超时返回进行中快照（非错误），可继续轮询"
      }
    }
  }
}
```

**异步返回（wait=false）** `structuredContent`：

```json
{
  "task_id": "task-20260726-143021-ab3f",
  "status": "running",
  "task_dir": "~/.agent_go/task-20260726-143021-ab3f",
  "pid": 58231,
  "poll_hint": {
    "tool": "inspect_task",
    "params": { "task_id": "task-20260726-143021-ab3f" },
    "suggested_interval_sec": 30
  }
}
```

**同步完成返回（wait=true，未超时）**：

```json
{
  "task_id": "task-20260726-143021-ab3f",
  "status": "completed",
  "duration_sec": 272,
  "cost_usd": 0.12,
  "dollar_per_pass": 0.04,
  "results": [
    {
      "id": "sub-1", "title": "迁移数据模型",
      "status": "completed", "duration_sec": 45,
      "changes": { "files": 2, "insertions": 23, "deletions": 5 },
      "verify_ok": true, "retry_count": 1
    }
  ],
  "preserved_worktrees": [],
  "report_path": "~/.agent_go/task-.../REPORT.md"
}
```

`status` 枚举：`completed`（含 no_changes/degraded）/ `partial_failure` / `failed` / `paused` / `running`（超时快照）。

---

### 3.2 `resume_task` — 恢复中断/暂停的任务

```json
{
  "name": "resume_task",
  "description": "恢复 paused/interrupted 状态的任务，从断点续跑剩余子任务。语义与 wait 同 run_task。",
  "annotations": {
    "title": "Resume paused task",
    "readOnlyHint": false,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": true
  },
  "inputSchema": {
    "type": "object",
    "required": ["task_id"],
    "properties": {
      "task_id": { "type": "string", "description": "run_task 返回的任务 ID" },
      "parallel": { "type": "integer", "minimum": 1, "maximum": 8 },
      "max_retries": { "type": "integer", "minimum": 0, "maximum": 10 },
      "wait": { "type": "boolean", "default": false },
      "timeout_sec": { "type": "integer", "default": 3600 }
    }
  }
}
```

- 幂等：对 `completed` 任务调用返回当前结果快照（不报错、不重跑）；对 `running` 任务返回 `AGENT_GO_TASK_RUNNING` 错误。
- 返回结构与 `run_task` 完全一致，额外带 `resumed_from: {completed: 2, remaining: 2}`。

---

### 3.3 `inspect_task` — 查询任务状态与保留现场

**设计说明**：本工具同时承担两个职责——(1) 异步模式的**轮询端点**（任务状态/进度/各子任务结果）；(2) 失败排查入口（保留 worktree 的路径/分支/失败原因）。合并的理由：两者数据源相同（meta.json + result.json + .preserved 标记），拆开只会迫使宿主多打一次调用。

```json
{
  "name": "inspect_task",
  "description": "查询任务执行状态（进度/各子任务结果/成本）与保留的 worktree 现场（路径/git 分支/失败原因）。只读，可在任务运行中任意轮询。",
  "annotations": {
    "title": "Inspect task status and preserved worktrees",
    "readOnlyHint": true,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": false
  },
  "inputSchema": {
    "type": "object",
    "required": ["task_id"],
    "properties": {
      "task_id": { "type": "string" },
      "include_log_tail": {
        "type": "boolean", "default": false,
        "description": "是否附 execution.log 尾部"
      },
      "log_lines": {
        "type": "integer", "default": 30, "minimum": 1, "maximum": 200
      }
    }
  }
}
```

**返回 `structuredContent`**：

```json
{
  "task_id": "task-20260726-143021-ab3f",
  "status": "running",
  "task": "重构认证模块，从 JWT 迁移到 OAuth2",
  "repo": "/Users/x/proj",
  "elapsed_sec": 95,
  "progress": { "completed": 1, "failed": 0, "blocked": 0, "running": 1, "pending": 2, "total": 4 },
  "cost_usd": 0.05,
  "current_activity": "Editing src/routes.py",
  "subtasks": [
    {
      "id": "sub-1", "title": "迁移数据模型", "status": "completed",
      "duration_sec": 45, "verify_ok": true, "retry_count": 1,
      "changes": { "files": 2, "insertions": 23, "deletions": 5 }
    },
    {
      "id": "sub-2", "title": "修改路由配置", "status": "running",
      "duration_sec": 50, "current_activity": "编辑 src/routes.py"
    }
  ],
  "preserved_worktrees": [
    {
      "id": "sub-4", "status": "blocked",
      "path": "~/.agent_go/task-.../sub-4/work",
      "branch": "agent_go/task-.../sub-4",
      "failure_reason": "上游 sub-2 失败级联阻断",
      "inspect_hint": "cd 到 path 后用 git log/diff 排查"
    }
  ],
  "log_tail": ["…"]
}
```

`current_activity` 字段由 M3 实现，通过 `subtask_activity` 事件流推送（含子任务中间阶段活动，如 `"Editing src/routes.py"`、`"Verifying changes"`）。不可用时省略该字段。

---

### 3.4 `review_task` — 审查结果并记录决策

```json
{
  "name": "review_task",
  "description": "对已完成任务做结果审查：analyze 返回 per-file diff 摘要（deep=true 时附独立模型分析）；approve/reject/changes_requested 记录人工决策到任务目录，供宿主与后续流程消费。",
  "annotations": {
    "title": "Review task results and record decision",
    "readOnlyHint": false,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": false
  },
  "inputSchema": {
    "type": "object",
    "required": ["task_id", "action"],
    "properties": {
      "task_id": { "type": "string" },
      "action": {
        "type": "string",
        "enum": ["analyze", "approve", "reject", "changes_requested"],
        "description": "analyze=只读分析；其余三个记录决策"
      },
      "deep": {
        "type": "boolean", "default": false,
        "description": "analyze 专用：独立模型逐子任务分析（产生额外 LLM 成本）"
      },
      "comment": {
        "type": "string",
        "description": "决策意见，写入 review_decision.json"
      }
    }
  }
}
```

**analyze 返回**：

```json
{
  "task_id": "task-...",
  "files_changed": [
    { "path": "src/auth/jwt.py", "subtasks": ["sub-1"], "insertions": 12, "deletions": 40 },
    { "path": "src/auth/oauth2.py", "subtasks": ["sub-1", "sub-2"], "insertions": 85, "deletions": 0 }
  ],
  "conflict_files": ["src/auth/session.py"],
  "verification_summary": { "passed": 3, "failed": 0, "weak_confidence": ["sub-3"] },
  "deep_analysis": [
    { "subtask_id": "sub-1", "model": "claude-sonnet-4", "verdict": "pass", "notes": "…", "cost_usd": 0.02 }
  ]
}
```

**决策类返回**：`{"task_id": "…", "decision": "approve", "recorded_at": "…", "decision_path": "…/review_decision.json"}`。同一任务重复决策覆盖旧值（幂等），历史在 `review_history.jsonl` 追加。

---

## 4. JSON 事件流映射

### 4.1 事件来源与通道

事件统一采用 IDS §4.6 的 schema（`{event, ts, level, data}`），MCP server 按调用模式分发到不同通道：

| 内部事件（agent_go --json 输出） | wait=true | wait=false | MCP 通道 |
|---|---|---|---|
| `task_start` | ✅ 转发 | ❌（调用已返回） | `notifications/progress` |
| `plan_generated` | ✅ | ❌ | `notifications/progress` |
| `plan_confirmed` | ✅ | ❌ | `notifications/progress` |
| `subtask_start` | ✅ | ❌ | `notifications/progress` |
| `subtask_progress`（5s 心跳） | ⚠️ 节流至 15s | ❌ | `notifications/progress` |
| `subtask_verify` | ✅ | ❌ | `notifications/progress` |
| `subtask_complete` | ✅ | ❌ | `notifications/progress` |
| `subtask_failed` | ✅ | ❌ | `notifications/progress` + `notifications/message`(warning) |
| `task_paused` | ✅ | — | `notifications/message`(warning)，随后工具结果返回 |
| `task_complete` | — | — | 终止事件，触发工具结果返回（见 4.3） |
| 任意 level=warning/error | ✅ | ❌ | `notifications/message` |

**异步模式（wait=false）的事件去向**：调用立即返回，后续事件不落 MCP 通道（宿主已拿到 task_id），由 `inspect_task` 轮询消费。这避免了"server 持有无主通知流"的状态管理复杂度。

### 4.2 progress notification 载荷

宿主发起 `tools/call` 时携带 `_meta.progressToken`，server 据此转发：

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/progress",
  "params": {
    "progressToken": "host-supplied-token",
    "progress": 2,
    "total": 4,
    "current_activity": "Editing src/routes.py",
    "message": "sub-2 修改路由配置 — Editing src/routes.py"
  }
}
```

- `progress/total`：已完结子任务数 / 总子任务数（`total` 在 `plan_confirmed` 前未知，此前事件省略这两个字段，仅带 `message`）。
- `current_activity`：当前正在进行的子任务级活动描述（由 `subtask_activity` 事件实时更新），如 `"Editing src/routes.py"`、`"Verifying changes"`、`"Creating worktree"`。宿主编排 UI 可用此字段显示进度行；任务未运行或事件流不可用时省略该字段。
- `message`：面向宿主的单行摘要，由事件 data 合成。宿主可直接展示给用户。
- 节流：`subtask_progress` 心跳在 server 侧合并为 ≤1 条/15s/任务，防止刷爆宿主。

### 4.3 终止与结果组装

子进程 stdout 出现 `task_complete`（或进程退出且 meta.json 状态收敛）时，server：
1. 读取 `task_dir/meta.json`（results 数组）+ `metering.jsonl`（成本聚合）；
2. 组装 §3.1 的完成态 `structuredContent`；
3. 附带一个 `content[0].type="text"` 的人类可读摘要（给宿主直接复述给用户）：

```text
任务完成：4/4 子任务通过（1 次自动重试），耗时 4m32s，成本 $0.12。
修改 6 个文件（+134/-47）。验证全部通过。
```

### 4.4 Degraded 模式的事件降级

| 完整事件 | Degraded 替代 |
|---|---|
| `plan_generated` | 无（轮询无法感知），从首个 `subtask_start` 开始 |
| `subtask_start` | 轮询发现某 `result.json` 出现且 status=running |
| `subtask_verify` / `subtask_progress` | 无 |
| `subtask_complete` / `subtask_failed` | 轮询发现 result.json status 收敛 |
| `task_complete` | meta.json status ∈ {completed, failed} |

文档与宿主集成指南需明确：degraded 模式事件粒度粗，正式集成前建议完成 `--json`（P0.5）。

---

## 5. 错误模型

工具失败时 `isError: true`，`content[0].text` 含结构化错误：

```json
{
  "error": {
    "code": "AGENT_GO_TASK_NOT_FOUND",
    "message": "任务不存在: task-20260701-xxx",
    "retryable": false
  }
}
```

| code | 触发 | retryable |
|---|---|---|
| `AGENT_GO_TASK_NOT_FOUND` | task_id 不存在 | 否 |
| `AGENT_GO_REPO_INVALID` | repo 不存在/非 git/不在 allowlist | 否 |
| `AGENT_GO_TASK_RUNNING` | 对 running 任务调用 resume | 否（先 inspect） |
| `AGENT_GO_PLAN_FAILED` | Plan 生成重试耗尽且 fallback 失败 | 是（可换模型重试） |
| `AGENT_GO_TIMEOUT` | wait=true 超时（任务仍在跑，附快照） | 是（可再 wait） |
| `AGENT_GO_CAPACITY` | 并发任务数超限 | 是 |
| `AGENT_GO_CONFIG_ERROR` | API key 缺失等 | 否 |

约定：`AGENT_GO_TIMEOUT` 的 `structuredContent` 仍带任务快照，**不是失败语义**——宿主应提示"任务仍在后台运行，可稍后查询"。

---

## 6. 安全模型

对齐 Codex 的"两轴"思想（能力 × 审批），server 侧控制如下：

1. **repo allowlist（能力轴，fail-closed）**：server 启动配置 `AGENT_GO_MCP_ALLOWED_REPOS`（glob 列表，如 `/home/user/workspace/*`）。`run_task`/`resume_task` 的 repo 必须命中，否则 `AGENT_GO_REPO_INVALID`。缺省配置 = 仅 server 启动时的 cwd。**这是唯一的安全边界，必须默认收紧。**
2. **密钥隔离**：API key 只存在于 MCP server 进程环境（`AGENT_GO_API_KEY`），不出现在任何工具参数/返回值中。宿主与用户对话内容永不接触密钥。
3. **审批轴交给宿主**：工具 annotations 已标注只读/变更属性（`inspect_task` readOnly，其余 mutating）。Hermes/OpenClaw 可各自实现"变更类工具调用前询问用户"的策略，server 不重复造。
4. **命令注入免疫**：server 全部以 argv 数组 spawn（`["python3","agent_go.py","run",repo,task,...]`），task/docs 等用户输入不经 shell 拼接。
5. **资源限额**：单 server 并发任务数上限 + `timeout_sec` 上限（21600），防宿主失控发起。

---

## 7. 宿主接入示例

### 7.1 通用 MCP 客户端配置（示意）

大多数 MCP 宿主遵循如下配置模式（具体 key 名以各宿主文档为准）：

```json
{
  "mcpServers": {
    "agent_go": {
      "command": "python3",
      "args": ["/path/to/agent_go/mcp_server.py"],
      "env": {
        "AGENT_GO_API_KEY": "sk-ant-...",
        "AGENT_GO_MCP_ALLOWED_REPOS": "/home/user/workspace/*"
      }
    }
  }
}
```

### 7.2 宿主侧典型对话流

```
用户（Telegram → Hermes）: 把 auth 从 JWT 迁到 OAuth2，跑完告诉我结果

Hermes 内部:
  → tools/call run_task {repo:"~/proj", task:"…", parallel:3, wait:false}
  ← {task_id: "task-…", status:"running"}

Hermes: 收到，已开始执行（预计 12-18 分钟），完成后通知你。

  （后台周期轮询 inspect_task）
  ← progress: 3/4, running: sub-3

  → tools/call review_task {task_id, action:"analyze"}
  ← files_changed / verification_summary

Hermes: ✅ 完成。4/4 通过（1 次自动重试），改 6 文件 +134/-47，成本 $0.12。
        验证全部通过。要我在 GitHub 上开 PR 吗？
```

OpenClaw 路径同理（其 plugin SDK / MCP 工具接入等价），渠道渲染由宿主负责，agent_go 不感知消息渠道。

---

## 8. 实现路径与分期

| 期 | 内容 | 依赖 | 估时 |
|---|---|---|---|
| **M1** | `mcp_server.py`（stdio JSON-RPC + tools/list + tools/call + 4 工具 + degraded 事件轮询） | 无 | 3d |
| **M2** | 对接 `--json` 完整事件流（替换轮询） | P0.5（ADR-002） | 1d |
| **M3** | `current_activity` 进度字段（含子任务中间阶段活动 + inspect 查询） | P1-5（ADR-004） | ✅ M3 已完成 |
| **M4** | Hermes/OpenClaw 集成指南 + 示例 skill | M1 | 1d |

M1 先行不阻塞——degraded 模式保证四工具端到端可用，M2 仅提升事件粒度。

**验收标准**：
- Hermes `hermes tools` 可见 4 个 agent_go 工具；
- 从 Hermes 对话发起 run_task → 轮询 inspect_task → review_task analyze 全链路返回结构化结果；
- allowlist 外 repo 调用被 fail-closed 拒绝；
- server 崩溃不杀死已 detachment 的 agent_go 任务（可用 resume_task 续上）。

---

## 9. 开放问题

| # | 问题 | 当前倾向 | 需决策方 |
|---|---|---|---|
| Q1 | `wait=true` 的最长阻塞是否设宿主可配上限？ | server 侧硬上限 6h | 工程 |
| Q2 | 是否需要第 5 个工具 `cancel_task`（SIGINT 优雅暂停）？ | 需要但可后置——resume 已支持，cancel 就是向 detached 进程发 SIGINT | 产品 |
| Q3 | review 决策是否回写 GitHub PR（经 `gh`）？ | 后置到 M4，先落本地 decision.json | 产品 |
| Q4 | 多 server 实例并发（同一 repo 被两个宿主同时 run）？ | worktree 隔离天然安全；meta 层面不做锁，文档声明 best-effort | 工程 |

---

*文档结束。变更请更新顶部版本号与状态。*
