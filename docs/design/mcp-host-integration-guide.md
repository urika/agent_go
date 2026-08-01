# agent_go MCP Server 宿主集成指南

| 字段 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | ✅ 完成 |
| 关联文档 | [`mcp-server-interface-design.md`](./mcp-server-interface-design.md)（工具 Schema / 事件映射 / 错误模型）、[`interaction-design-spec.md`](./interaction-design-spec.md)（§4.6 JSON 事件 schema） |
| 覆盖宿主 | Hermes / OpenClaw / Claude Code / Cursor / Windsurf / 通用 MCP 客户端 |

> **目标**：让任意 MCP 宿主在对话中触发 agent_go 的结构化工程编排（Plan→Decompose→Execute→Verify），并消费流式进度与结构化结果。
>
> **前提**：agent_go MCP server 已启动（`python3 -m agent_go.mcp_server`）。详见 [`mcp-server-interface-design.md`](./mcp-server-interface-design.md) §7.1 基础配置。

---

## 1. 快速接入

### 1.1 宿主无关的验证步骤

完成 MCP server 配置后，用以下操作验证集成是否成功：

```
# 宿主对话中输入：
可用哪些工具？

# 宿主应返回 6 个工具：
- run_task（Run structured engineering task）
- resume_task（Resume paused task）
- inspect_task（Inspect task status）
- review_task（Review task results）
- list_tasks（List tasks with status filter）
- cancel_task（Cancel a running task）

# 验证 allowlist 安全边界：
对 ~/unauthorized/path 执行 "重构登录模块"
→ 宿主应返回错误：AGENT_GO_REPO_INVALID
```

> **传输方式**：默认 stdio（`agent_go mcp`）。远程/集成场景可用 HTTP/SSE 模式：`agent_go mcp --http --host 127.0.0.1 --port 8090`（POST /mcp + GET /mcp SSE 事件推送 + GET /health 健康检查；`AGENT_GO_MCP_HTTP_TOKEN=xxx` 启用 Bearer token 鉴权）。各宿主的 `command`/`url` 配置见 §2/§4 对应条目。

### 1.2 典型调用流

```
异步模式（推荐）        │  同步模式（快速任务）
────────────────────────┼────────────────────────
run_task {wait:false}  │  run_task {wait:true}
  → task_id + running    │    → 阻塞 + progress 通知
                         │    → 完成返回
inspect_task (轮询)     │
  → progress + results  │
                         │
review_task {analyze}   │
  → diff 摘要           │
```

异步模式适用于分钟~小时级任务；同步模式适用于已验证过的快速场景（<5min）。

---

## 2. Hermes 集成

### 2.1 MCP Server 配置

Hermes 的 `config.yaml` 中声明 agent_go 工具：

```yaml
mcp_servers:
  - name: agent_go
    command: python3
    args:
      - /path/to/agent_go/mcp_server.py
    env:
      AGENT_GO_API_KEY: "sk-ant-..."          # 必填
      AGENT_GO_MCP_ALLOWED_REPOS: "/home/user/projects/*"  # 必填，fail-closed
      AGENT_GO_MCP_MAX_CONCURRENT: "3"         # 可选，默认 3
```

### 2.2 对话模式设计

Hermes 作为 IM/聊天机器人宿主（Telegram / Discord / Slack），应遵循"异步 + 主动通知"模式：

```
用户: 把 auth 模块从 JWT 迁到 OAuth2

Hermes 内部逻辑:
  1. tools/call run_task {repo:"~/proj", task:"auth JWT→OAuth2迁移",
                          parallel:3, wait:false}
  2. 返回 {task_id:"task-xxx", status:"running"}

Hermes 回复用户:
  ✅ 已收到任务。正在分析代码库并生成执行计划…
  预计 3-5 分钟完成。完成后我会通知你。

Hermes 后台:
  3. 每 30s 调用 inspect_task {task_id:"task-xxx"}
  4. 获取 progress/completed/current_activity
  5. 当 status 收敛为 completed/failed 时：
     → review_task {task_id:"task-xxx", action:"analyze"}
     → 组装最终回复 + 执行结果摘要

Hermes 最终通知:
  ✅ 任务完成！auth 模块迁移成功（4/4 子任务通过）
  • 修改 6 个文件（+134/-47）
  • 耗时 4m32s，成本 $0.12
  • 验证全部通过
  需要我在 GitHub 上开 PR 吗？
```

### 2.3 进度通知建议

Hermes 可渐进式地向用户推送进度（避免信息过载）：

| 阶段 | 回复策略 |
|---|---|
| 任务启动 | 立即回复"已收到，开始执行" |
| Plan 阶段 | 可选：回复"分析完成，将分 4 步执行" |
| 子任务完成 | 不主动推送（静默积累） |
| 最终完成 | 回复完整报告 |

### 2.4 skill 封装建议

Hermes 可将 agent_go 调用封装为 skill，供其他 Agent 零成本复用：

```markdown
---
name: structured-refactor
description: Execute a structured code refactoring task using agent_go's plan-decompose-execute workflow
argument-hint: "<task description>"
allowed-tools: "mcp__agent_go__run_task, mcp__agent_go__inspect_task, mcp__agent_go__review_task"
---

You are a structured refactoring coordinator. When given a refactoring task:

1. Call `run_task` with `wait: false` to start the task.
2. Poll `inspect_task` every 30 seconds until status != "running".
3. Call `review_task` with `action: "analyze"` to get the diff summary.
4. Present the result to the user.
```

---

## 3. OpenClaw 集成

### 3.1 Plugin 注册

OpenClaw 通过 `plugin.json` 声明 MCP server：

```json
{
  "name": "agent_go",
  "description": "Structured engineering task orchestrator",
  "mcp_server": {
    "command": "python3",
    "args": ["/path/to/agent_go/mcp_server.py"],
    "env": {
      "AGENT_GO_API_KEY": "sk-ant-...",
      "AGENT_GO_MCP_ALLOWED_REPOS": "/workspace/*"
    }
  }
}
```

### 3.2 Plugin SDK 接入（推荐）

OpenClaw 的 Plugin SDK 可作为接入的推荐路径（比裸 MCP 更结构化）：

```python
# OpenClaw plugin: agent_go_plugin.py

from openclaw_sdk import MCPPlugin, tool

class AgentGoPlugin(MCPPlugin):
    def __init__(self):
        super().__init__(
            name="agent_go",
            command="python3",
            args=["/path/to/agent_go/mcp_server.py"],
            env={"AGENT_GO_API_KEY": "sk-ant-...",
                 "AGENT_GO_MCP_ALLOWED_REPOS": "/workspace/*"}
        )

    @tool("run_task")
    def run_task(self, repo: str, task: str, **kwargs) -> dict:
        return self.call("run_task", repo=repo, task=task, **kwargs)

    @tool("inspect_task")
    def inspect_task(self, task_id: str) -> dict:
        return self.call("inspect_task", task_id=task_id)

    @tool("review_task")
    def review_task(self, task_id: str, action: str) -> dict:
        return self.call("review_task", task_id=task_id, action=action)
```

### 3.3 对话模式设计

OpenClaw 作为 Agent 框架，Agent 应自主编排 agent_go 调用流：

```
Agent 规划阶段:
  意识到用户需求需要结构性工程改造
  → 选择 agent_go.run_task 作为执行策略
  
Agent 执行阶段:
  1. run_task(repo, task, parallel=2) → task_id
  2. 进入子任务调度循环：
     while status == "running":
         inspect = inspect_task(task_id)
         status = inspect.status
         # Agent 可根据进度决定是否介入
  3. review_task(task_id, "analyze") → diff 摘要
  4. 决策：approve / reject / changes_requested

Agent 回复用户:
  执行结果 + diff 链接 + 决策建议
```

### 3.4 并发控制

OpenClaw 的多 Agent 编排需注意 `AGENT_GO_MCP_MAX_CONCURRENT` 限制。建议：

- 同一 Agent 实例不要同时发起多个 agent_go 任务（虽然技术上可行，但增加排查复杂度）
- 不同 Agent 共享同一 MCP server 时，总并发不超过 server 上限
- 超限返回 `AGENT_GO_CAPACITY` 错误，Agent 应等轮询退一任务后再重试

---

## 4. Claude Code 集成

### 4.1 MCP Server 配置

Claude Code 用户全局或项目级 `settings.json`：

```json
{
  "mcpServers": {
    "agent_go": {
      "command": "python3",
      "args": ["/path/to/agent_go/mcp_server.py"],
      "env": {
        "AGENT_GO_API_KEY": "sk-ant-...",
        "AGENT_GO_MCP_ALLOWED_REPOS": "${CLAUDE_PROJECT_DIR}/*"
      }
    }
  }
}
```

`${CLAUDE_PROJECT_DIR}` 是 Claude Code 内置占位符，自动展开为当前项目根目录。

### 4.2 对话模式设计

Claude Code 作为交互式终端宿主，应优先使用 **同步模式 + progress 通知**：

```
用户: 把 auth 模块从 JWT 迁到 OAuth2

Claude Code 内部（Plan Mode）:
  ① 分析代码库，确认需要重构的范围
  ② 调用 agent_go run_task:
     repo: 当前项目
     task: 详细任务描述（含 Claude Code 分析结果）
     parallel: 3
     wait: true  ← 利用 progress notification 获得实时进度
  ③ 收到 stream-json 进度通知：
     → "Executing sub-1: 迁移数据模型"
     → "sub-1 完成 (verify_ok)"
     → "Executing sub-2: 修改路由配置"
  ④ 任务完成后，自动 review_task analyze

Claude Code 回复用户:
  ✅ 迁移完成（4/4 子任务通过）
  ─────────────────────────────────
  sub-1 迁移数据模型        ✅ 23s
  sub-2 修改路由配置        ✅ 45s
  sub-3 更新测试用例        ✅ 12s
  sub-4 验证全链路          ✅ 8s
  
  修改 6 个文件（+134/-47），成本 $0.12
  
  需要我 review 结果还是直接提交？
```

### 4.3 与 Claude Code 权限系统的集成

Claude Code 的权限模型（`permissions.allow/ask/deny`）可直接应用于 agent_go 工具：

```json
{
  "mcpServers": {
    "agent_go": {
      …
    }
  },
  "permissions": {
    "allow": [
      "mcp__agent_go__inspect_task",   // 只读，自动允许
      "mcp__agent_go__review_task"     // 如果不想每次确认
    ],
    "ask": [
      "mcp__agent_go__run_task",       // 变更类，先确认
      "mcp__agent_go__resume_task"
    ]
  }
}
```

> 注意：Claude Code 允许通过 `"Yes, don't ask again"` 持久化规则到 `settings.local.json`。

### 4.4 Subagent 编排模式

Claude Code 的 Subagent 机制可与 agent_go 搭配使用：

```yaml
# .claude/agents/refactoring-coordinator.md
---
name: refactoring-coordinator
description: Coordinates structured refactoring via agent_go MCP server
tools: mcp__agent_go__run_task, mcp__agent_go__inspect_task, mcp__agent_go__review_task
---

You coordinate structured code refactoring using agent_go.

Workflow:
1. First analyze the codebase to understand scope.
2. Call agent_go run_task with a detailed task description.
3. Monitor progress via inspect_task.
4. On completion, call review_task analyze.
5. Present results to the user.
```

---

## 5. Cursor / Windsurf 集成

### 5.1 MCP Server 配置

**Cursor**（`.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "agent_go": {
      "type": "command",
      "command": "python3",
      "args": ["/path/to/agent_go/mcp_server.py"],
      "env": {
        "AGENT_GO_API_KEY": "sk-ant-...",
        "AGENT_GO_MCP_ALLOWED_REPOS": "/home/user/projects/*"
      }
    }
  }
}
```

**Windsurf**（`.windsurf/mcp_config.json`）：

```json
{
  "mcpServers": {
    "agent_go": {
      "command": "python3",
      "args": ["/path/to/agent_go/mcp_server.py"],
      "env": {
        "AGENT_GO_API_KEY": "sk-ant-...",
        "AGENT_GO_MCP_ALLOWED_REPOS": "/home/user/projects/*"
      }
    }
  }
}
```

### 5.2 对话模式设计

Cursor/Windsurf 的 Agent 可在 Plan Mode 中调用 agent_go 做粗粒度重构，自身专注细粒度代码修改：

```
Cursor Plan Mode 中的用户请求:
  "把 auth 从 JWT 迁到 OAuth2"

Cursor Agent 规划:
  ① 用 Cursor 自身的代码分析能力确认范围
  ② 调用 agent_go run_task 做批量重构 ← 这是 agent_go 的价值
  ③ 用 Cursor 的 Checkpoint 机制做变更预览
  ④ 接受或退回 agent_go 的结果
```

### 5.3 Checkpoint 集成建议

Cursor/Windsurf 的 Checkpoint 机制与 agent_go 的 worktree 隔离天然互补：

- **agent_go 负责粗粒度**：每个 run_task 在独立 worktree 中完成，不污染 IDE 工作区
- **IDE 负责精调**：通过 `inspect_task` 获取 worktree 路径后，IDE 可直接 open 进行审查和微调
- **工作流**：`agent_go run` → `inspect_task 获取 worktree 路径` → `IDE 打开 worktree` → `预览/修改` → `IDE 原生 git commit`

---

## 6. 通用 MCP 客户端集成

### 6.1 命令行测试

任何支持 MCP 的 CLI 客户端均可接入：

```bash
# 用 mcp-cli 工具
$ npx @anthropic/mcp-cli --server 'python3 /path/to/mcp_server.py'

# MCP CLI 交互示例
> tools/list
→ agent_go: run_task, resume_task, inspect_task, review_task

> call run_task {"repo":"/home/user/proj","task":"添加搜索功能","wait":false}
→ {"task_id":"task-xxx","status":"running",...}
```

### 6.2 Node.js / Python SDK 直接调用

```python
# Python: subprocess JSONRPC over stdio
import subprocess, json

server = subprocess.Popen(
    ["python3", "/path/to/agent_go/mcp_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    text=True, bufsize=1
)

def mcp_call(method, params=None, msg_id=1):
    req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params: req["params"] = params
    server.stdin.write(json.dumps(req) + "\n")
    server.stdin.flush()
    return json.loads(server.stdout.readline())

# Initialize
print(mcp_call("initialize"))
print(mcp_call("notifications/initialized"))  # empty response (no id)

# List tools
tools = mcp_call("tools/list")
print([t["name"] for t in tools["result"]["tools"]])

# Run task (async)
result = mcp_call("tools/call", {
    "name": "run_task",
    "arguments": {"repo": "/home/user/proj", "task": "添加搜索功能"}
})
print(result)
```

---

## 7. 进度通知消费模式

### 7.1 progress notification 示例序列

```
wait=true 调用的完整通知序列：

[initial]    进度: 0/4  "Executing sub-1: 迁移数据模型"
[sub-1 ✅]   进度: 1/4  "sub-1 完成 (completed)"
[sub-2 act]  进度: 1/4  "sub-2: 编辑 src/routes.py — 修改路由配置"
[sub-2 ✅]   进度: 2/4  "sub-2 完成 (completed)"
[sub-3 act]  进度: 2/4  "sub-3: 创建 worktree"
[sub-3 ✅]   进度: 3/4  "sub-3 完成 (completed)"
[sub-4 act]  进度: 3/4  "sub-4: 验证变更"
[sub-4 ✅]   进度: 4/4  "Pipeline completed"
```

### 7.2 宿主侧解析建议

```python
# 示例：从 progress notification 提取进度的宿主侧逻辑
class AgentGoProgressTracker:
    def __init__(self, total_subtasks: int):
        self.total = total_subtasks
        self.completed = 0
        self.current_activity = ""
        self.finished = False

    def handle_progress(self, params: dict) -> str:
        self.completed = params.get("progress", self.completed)
        self.total = params.get("total", self.total)
        self.current_activity = params.get("current_activity", "")

        # 格式化进度行
        eta = self._format_eta(params.get("total", 0) - self.completed)
        return f"[{self.completed}/{self.total}] {self.current_activity}  ETA {eta}"

    def handle_complete(self, result: dict) -> str:
        self.finished = True
        status = result.get("status", "completed")
        dur = result.get("duration_sec", 0)
        cost = result.get("cost_usd", 0)
        n_pass = sum(1 for r in result.get("results", [])
                     if r.get("verify_ok"))
        return (
            f"{'✅' if status=='completed' else '❌'} 任务{status}，"
            f"耗时 {dur//60}m{dur%60}s，成本 ${cost}\n"
            f"验证通过: {n_pass}/{len(result.get('results', []))}"
        )
```

### 7.3 超时处理

`wait=true` 的任务可能超时（默认 3600s）。宿主应：

```python
# 超时返回值中的 timeout_hint 字段
result["status"]  # "running" (非失败语义)
result["timeout_hint"]  # "任务仍在后台运行，可稍后 inspect_task 轮询或 resume_task 续跑"

# 宿主应对：
# 推荐：告知用户任务仍在运行，提供后续轮询方法
```

---

## 8. 错误排查指南

| 症状 | 原因 | 排查 |
|---|---|---|
| `AGENT_GO_REPO_INVALID` | repo 不在 allowlist | 检查 `AGENT_GO_MCP_ALLOWED_REPOS` 配置 |
| `AGENT_GO_TASK_NOT_FOUND` | task_id 不存在 | 检查 `~/.agent_go/` 下是否有该目录 |
| `AGENT_GO_TASK_RUNNING` | 对 running 任务调 resume | 用 inspect_task 先查状态 |
| `AGENT_GO_CAPACITY` | 并发已达上限 | 等现有任务完成或增大 `AGENT_GO_MCP_MAX_CONCURRENT` |
| `AGENT_GO_TIMEOUT` | wait=true 超时 | 非错误，任务仍在后台跑 |
| server 启动后无响应 | stdin 未正确连接 | 确认宿主用 stdio transport（`agent_go mcp`）而不是 HTTP；若走 HTTP 则确认 `agent_go mcp --http` 已启动且端口正确 |
| `AGENT_GO_PLAN_FAILED` | LLM 无法生成 Plan | 检查 API key 是否有效、模型是否可用 |
| server 启动报错 | Python 环境问题 | 确认 `python3 -m agent_go.mcp_server` 能否独立运行 |

### 8.1 服务器健康检查

```bash
# 健康检查脚本 (host-agnostic)
$ echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  | python3 -m agent_go.mcp_server \
  | head -1

# 预期返回: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",...}}

# HTTP/SSE 模式的健康检查
$ curl -s http://127.0.0.1:8090/health
# 预期返回: {"status":"ok",...}（需配 AGENT_GO_MCP_HTTP_TOKEN 时带 Authorization: Bearer <token>）
```

### 8.2 日志查看

```bash
# MCP server 日志 (stderr)
$ AGENT_GO_API_KEY="sk-..." python3 -m agent_go.mcp_server 2>/tmp/agent_go_mcp.log

# 查看任务执行日志
$ cat ~/.agent_go/task-xxx/execution.log
```

---

## 9. 安全注意事项

### 9.1 Repo Allowlist

**这是唯一的安全边界，必须默认收紧。** 配置应只包含可信的工作目录：

```bash
# ❌ 过于宽松
AGENT_GO_MCP_ALLOWED_REPOS="/*"

# ✅ 推荐
AGENT_GO_MCP_ALLOWED_REPOS="/home/user/projects/*"

# ✅ 多个路径
AGENT_GO_MCP_ALLOWED_REPOS="/home/user/proj1/*,/home/user/proj2/*"
```

### 9.2 API Key 管理

```bash
# ❌ 不要在宿主对话/工具参数中传入 API key
# ✅ 只通过 server 的 env 透传
```

### 9.3 宿主侧审批策略建议

| 工具 | 建议审批级别 | 理由 |
|---|---|---|
| `inspect_task` | 自动允许 | 只读，不产生副作用 |
| `review_task analyze` | 自动允许 | 只读分析 |
| `review_task approve/reject` | 确认后放行 | 变更类决策 |
| `run_task` | 确认后放行 | 修改文件系统 |
| `resume_task` | 确认后放行 | 修改文件系统 |

---

## 10. 各宿主配置速查表

| 宿主 | 配置文件位置 | 配置格式 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` 或 `.claude/settings.json` | `{mcpServers: {agent_go: {command, args, env}}}` |
| Hermes | `config.yaml` | `{mcp_servers: [{name, command, args, env}]}` |
| OpenClaw | `plugin.json` | `{mcp_server: {command, args, env}}` |
| Cursor | `.cursor/mcp.json` | `{mcpServers: {agent_go: {type, command, args, env}}}` |
| Windsurf | `.windsurf/mcp_config.json` | `{mcpServers: {agent_go: {command, args, env}}}` |
| 通用 MCP 客户端 | 各客户端定义 | JSON-RPC 2.0 over stdio |

---

*文档结束。如发现宿主配置变更，请更新速查表和对应章节。*
