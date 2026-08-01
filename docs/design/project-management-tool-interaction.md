# 项目管理工具与 agent_go 交互设计

> **版本**：v1.0（预研）
>
> **目的**：探讨将「软件开发全流程」中的 Phase 0-2 落地为独立项目管理工具时，它应该如何与 agent_go 交互。agent_go 不做任何改动——只使用已有的 MCP 协议和 Task Spec 接口。
>
> **日期**：2026-08-01

---

## 一、定位：上游管理 + 下游调度

```
┌──────────────────────────────────────────────────────────────┐
│              项目管理工具（Phase 0-2）                          │
│                                                              │
│  PRD 管理  │  Roadmap 排期  │  Task Spec 编写  │  准入审查      │
│  Issue 追踪 │  人员分工       │  依赖关系        │  状态流转      │
│                                                              │
│  关键动作：                                                   │
│  - 人决策（做什么、谁做、什么时候做）                            │
│  - AI 辅助（需求分析、Spec 生成、完整性检查）                    │
│  - 状态机驱动（需求 → Spec → 执行 → 审查 → 完成）               │
└──────────────────────────┬───────────────────────────────────┘
                           │
                    Task Spec + MCP
                           │
┌──────────────────────────▼───────────────────────────────────┐
│              agent_go（Phase 3-4）                             │
│                                                              │
│  agent_go mcp --http --port 8090                              │
│                                                              │
│  Tools:  run_task / resume_task / inspect_task /             │
│          review_task / list_tasks / cancel_task               │
│  Resources: summary / plan / metering / review / list         │
│  Notifications: SSE progress events                           │
└──────────────────────────────────────────────────────────────┘
```

**项目管理工具不替代 agent_go，agent_go 不往上延伸。** 交互的唯一通道是 MCP 协议 + Task Spec 文件。

---

## 二、交互模型：三个通道

### 通道 1：文件系统（Task Spec 读写）

项目管理工具将 Task Spec 写入项目仓库的 `docs/tasks/` 目录。agent_go 通过 `--spec` 参数读取。

```
# 工具侧
工具 UI 中编写 Task Spec → 保存 → git commit + push docs/tasks/task-xxx.md

# agent_go 侧（人手动或工具自动触发）
agent_go run ./repo --spec docs/tasks/task-xxx.md --yes
```

### 通道 2：MCP Tools（调度与控制）

项目管理工具作为 MCP Client，通过 agent_go 的 MCP HTTP/SSE 接口调度执行。

```
# 工具侧（程序化调用，用户不碰 CLI）
POST /mcp  {"method": "tools/call", "params": {"name": "run_task", "arguments": {"repo": "./my-repo", "task": "...", "wait": false}}}
→ {"task_id": "task-20260801-...", "status": "running"}

POST /mcp  {"method": "tools/call", "params": {"name": "inspect_task", "arguments": {"task_id": "task-20260801-..."}}}
→ {"status": "running", "progress": "2/4", "subtasks": [...]}

POST /mcp  {"method": "tools/call", "params": {"name": "review_task", "arguments": {"task_id": "task-20260801-...", "action": "approve"}}}
→ {"reviewed": true, "status": "approved"}
```

### 通道 3：SSE Notifications（事件流）

项目管理工具订阅 agent_go 的 SSE 事件流，实时更新任务状态。

```
# 工具侧
GET /mcp  (SSE)

→ event: notifications/progress
  data: {"task_id": "task-xxx", "subtask": "add-email-verification", "status": "completed", "progress": "3/4"}

→ event: notifications/progress
  data: {"task_id": "task-xxx", "status": "completed", "pass_rate": 1.0}
```

---

## 三、全流程交互时序

以一个功能的完整生命周期为例：

```
项目管理工具                              agent_go
───────────                              ────────
Phase 0-1: 需求与方案
  ├─ PM 在工具中创建「需求卡片」
  ├─ Claude Code 对话辅助需求分析
  ├─ 关联 PRD 段落 / 设计文档
  └─ 工程师在工具中关联技术方案
      │
Phase 2: Task Spec 生成
  ├─ 工程师在工具中打开「Spec 编辑器」
  ├─ AI 辅助补全（读代码库 → 预填范围/约束）
  ├─ 人审核修正
  └─ 提交准入审查
      │
      │  Spec 通过审查
      │
      ▼
Phase 3: 派发执行 ──────────────────────────→
  MCP: run_task(                                 run_task() 执行
    repo="./my-repo",                              ├─ Plan → Decompose
    task="<Spec 目标>",                             ├─ Execute → Verify
    spec_path="docs/tasks/task-xxx.md",             └─ SSE 实时推送进度
    wait=false
  )
      │
      │  ← SSE: progress events ──────────────
      │  ← SSE: subtask status updates ────────
      │
  UI 展示实时进度（进度条 + 子任务状态 + 成本累加）
      │
      │  ← SSE: task completed ────────────────
      │
Phase 4: 审查交付
  ├─ MCP: review_task(task_id, action="review")
  │     → 获取聚合 diff 摘要
  ├─ 人在工具中审查（或跳转到 GitHub PR）
  ├─ 人在工具中 approve/reject
  ├─ MCP: review_task(task_id, action="approve")
  │
  └─ 需求卡片状态 →「已完成」
```

---

## 四、核心交互场景

### 场景 1：PM 创建一个需求

```
工具侧                                    agent_go 角色
──────                                    ────────────
1. PM 点击「新建需求」
2. 填写标题、描述、优先级                     不参与
3. 关联 PRD 段落 / Roadmap 条目
4. AI 辅助提取关键信息（目标、动机）
5. 需求卡片进入「待方案设计」状态
```

### 场景 2：工程师生成 Task Spec

```
工具侧                                    agent_go 角色
──────                                    ────────────
1. 工程师打开需求卡片，点击「生成 Spec」
2. 工具调用 agent_go scope（如果已实施）：      ← MCP 或 CLI 调用
   → 读代码库 → 生成 Spec 草稿                 scope 返回 Spec 草稿
3. 工程师在 Spec 编辑器中审核修改
4. 点击「提交审查」
5. 工具运行 L1 硬门禁（本地确定性检查）          L1/L2 逻辑可在工具侧实现
   + L2 软警告（调用 LLM API）                 （不依赖 agent_go）
6. 审查通过 → Spec 存入 docs/tasks/
   → git commit + push
```

### 场景 3：派发执行

```
工具侧                                    agent_go 角色
──────                                    ────────────
1. 工程师点击「开始执行」
                                          →
2. MCP: run_task(                           agent_go 开始执行
     repo,                                    Plan → Decompose → Execute
     task=spec.目标,
     spec_path=spec 文件路径,                  SSE 推送进度
     model_tier=spec 标注的模型档位,
     wait=false
   )
3. 需求卡片状态 →「执行中」
4. UI 展示：
   - 当前波次 [2/3]
   - 子任务状态列表
   - 实时成本累加（metering SSE）
   - 预计剩余时间
5. SSE 通知：执行完成
6. 需求卡片状态 →「待审查」
```

### 场景 4：审查与合并

```
工具侧                                    agent_go 角色
──────                                    ────────────
1. 工程师收到「待审查」通知
2. 点击「查看结果」
3. MCP: review_task(task_id, "review")      → 返回聚合 diff 摘要
4. 工具展示：
   - 变更文件列表（按子任务分组）
   - 每个文件的 diff（可从 Resources 获取）
   - 质量仪表（通过率/验证率/重试次数）
   - 成本明细（per-subtask cost）
5. 工程师点击「Approve」
6. MCP: review_task(task_id, "approve")
7. MCP: run_task → 自动创建 PR（通过 agent_go pr --push）
   或 手动创建 PR（跳转到 GitHub）
8. 需求卡片状态 →「已完成」
```

### 场景 5：执行失败处理

```
工具侧                                    agent_go 角色
──────                                    ────────────
1. SSE 通知：子任务 xxx 验证失败
2. 需求卡片状态 →「执行失败」
3. 工具展示失败信息：
   - 失败子任务名称
   - failure_reason（stderr + exit code）   ← MCP: inspect_task()
   - worktree 路径
4. 工程师决策：
   [A] 编辑 Spec 修正范围/约束 → 重新执行
   [B] MCP: resume_task(task_id)         → 从断点续跑
   [C] 手动 inspect worktree 修复 → resume
5. 需求卡片状态 →「执行中」（重试）或「已打回」（需修正 Spec）
```

---

## 五、交互接口定义

### 5.1 工具 → agent_go（MCP Tools）

| 工具操作 | MCP Tool | 参数 | 返回 |
|---------|----------|------|------|
| 派发执行 | `run_task` | repo, task, spec_path, model_tier, wait=false | task_id |
| 断点续跑 | `resume_task` | task_id, wait=false | task_id |
| 查询进度 | `inspect_task` | task_id | status, progress, subtasks[], cost |
| 获取审查 | `review_task` | task_id, action="review" | diff_summary, quality_dashboard |
| 审批通过 | `review_task` | task_id, action="approve" | reviewed: true |
| 审批打回 | `review_task` | task_id, action="reject" | reviewed: true |
| 取消执行 | `cancel_task` | task_id, confirm=true | cancelled: true |
| 任务列表 | `list_tasks` | status, page, page_size | tasks[] |

### 5.2 工具 → agent_go（MCP Resources）

| 查询操作 | MCP Resource | 返回 |
|---------|-------------|------|
| 任务概要 | `summary/{task_id}` | 状态、进度、耗时、成本 |
| 执行计划 | `plan/{task_id}` | Plan JSON（steps + dependencies） |
| 计量数据 | `metering/{task_id}` | per-request 成本明细 |
| 最近日志 | `log/recent/{task_id}` | 最近 N 条日志 |
| 审查结果 | `review/{task_id}` | 审查状态和 diff 摘要 |
| 任务列表 | `list` | 精简版任务列表（比 list_tasks tool 更轻量） |

### 5.3 agent_go → 工具（SSE Notifications）

| 事件 | 触发时机 | payload |
|------|---------|---------|
| `task.started` | 执行开始 | task_id, repo, subtask_count |
| `subtask.started` | 子任务开始 | task_id, subtask_id, title |
| `subtask.completed` | 子任务完成 | task_id, subtask_id, status, verify_ok, cost |
| `subtask.failed` | 子任务失败 | task_id, subtask_id, failure_reason |
| `subtask.blocked` | 子任务被阻断 | task_id, subtask_id, blocked_by |
| `task.completed` | 全部完成 | task_id, pass_rate, total_cost, elapsed |
| `task.failed` | 部分失败 | task_id, failed_subtasks[], total_cost |
| `cost.update` | 成本更新 | task_id, cumulative_cost |

### 5.4 文件系统接口（Task Spec）

工具写入 → agent_go 读取：

```
# 工具侧
写入: docs/tasks/task-<slug>.md    # Task Spec（7 章节 Markdown）
关联: Spec 文件中引用 Issue 编号    # 如 "相关 Issue: #142"
关联: Spec 文件中引用 PRD 段落      # 如 "PRD 引用: PRD §2.3"

# agent_go 侧
agent_go run ./repo --spec docs/tasks/task-<slug>.md
→ 解析 Spec → 注入 Plan prompt → 执行
```

---

## 六、状态机

### 6.1 需求/任务状态流转（工具侧维护）

```
                    ┌─────────────┐
                    │   Backlog   │  ← PM 创建需求卡片
                    └──────┬──────┘
                           │ 排入迭代
                    ┌──────▼──────┐
                    │   Ready     │  ← 已排期，等待工程师
                    └──────┬──────┘
                           │ 工程师开始 Scoping
                    ┌──────▼──────┐
                    │  Scoping    │  ← 工程师写 Task Spec
                    └──────┬──────┘
                           │ Spec 通过准入审查
                    ┌──────▼──────┐
                    │  Spec Ready │  ← 等待执行
                    └──────┬──────┘
                           │ MCP: run_task()
                    ┌──────▼──────┐
                    │  Running    │  ← agent_go 执行中
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼───┐  ┌────▼─────┐  ┌───▼──────┐
     │  Reviewing │  │  Failed  │  │ Blocked  │
     │  (全部通过) │  │ (部分失败) │  │ (级联阻断) │
     └────┬───────┘  └────┬─────┘  └────┬─────┘
          │               │             │
          │  approve      │ 重试/resume │ 上游恢复
          │               │             │
    ┌─────▼──────┐  ┌─────▼─────┐       │
    │   Done     │  │  Running  │◄──────┘
    └────────────┘  └───────────┘
```

### 6.2 agent_go 内部状态（agent_go 侧维护，工具轮询/SSE 获取）

```
running → completed
        → failed（部分子任务最终失败）
        → cancelled（人工取消）
```

---

## 七、哪些逻辑放在工具侧，哪些放在 agent_go 侧

| 能力 | 工具侧 | agent_go 侧 | 理由 |
|------|--------|------------|------|
| PRD/Roadmap 管理 | ✅ | ❌ | agent_go 不做需求管理 |
| Issue 状态流转 | ✅ | ❌ | agent_go 不管理 Issue |
| Task Spec 编辑器 | ✅ | ❌ | 编辑 Spec 是人的工作 |
| Spec 准入审查（L1 硬门禁） | ✅ | ❌ | 确定性检查，工具侧本地执行即可。agent_go 的 `--spec` 也会跑一遍以确保安全 |
| Spec 准入审查（L2 软警告） | ✅ | ❌ | LLM 辅助判断，工具侧调用 LLM API。不依赖 agent_go |
| Spec → Plan prompt 注入 | ❌ | ✅ | agent_go 的 Plan 阶段核心逻辑 |
| 子任务分解、执行、验证 | ❌ | ✅ | agent_go 的核心能力 |
| 模型路由、成本控制 | ❌ | ✅ | agent_go 的 difficulty 路由 |
| 进度实时展示 | ✅（UI） | ✅（SSE 推送） | agent_go 推事件，工具展示 |
| 聚合 diff 审查 | ✅（UI） | ✅（review_task tool） | agent_go 产出审查数据，工具渲染 UI |
| 审批（approve/reject） | ✅（触发） | ✅（review_task tool） | 人在工具中决策，通过 MCP 调用 agent_go 记录 |
| metering 成本分析 | ✅（图表） | ✅（metering Resource） | agent_go 提供原始数据，工具做可视化 |
| PR 创建 | ✅（触发） | ✅（agent_go pr） | 工具触发 MCP 或 CLI 调用 |

---

## 八、安全与权限

### 8.1 agent_go MCP Server 的安全边界

```
项目管理工具（Web UI）
  → 用户登录鉴权（工具侧自己管理，agent_go 不感知）
  → 工具调用 agent_go MCP（本地 localhost，Bearer token 鉴权）
  → agent_go 在本地执行（只有文件系统权限，无网络暴露）
```

**安全约定**：
- agent_go MCP HTTP 默认绑定 `127.0.0.1`（仅本地访问）
- 项目管理工具与 agent_go 部署在同一台机器上（或通过 SSH tunnel）
- Bearer token 通过 `AGENT_GO_MCP_HTTP_TOKEN` 环境变量管理
- repo allowlist 保护文件系统边界（agent_go 已有此能力）

### 8.2 不需要的能力

| 不需要 | 理由 |
|--------|------|
| agent_go 暴露到公网 | 项目管理工具是本地的，不需要远程执行 |
| 多用户权限管理 | 单人使用（agent_go 定位），多用户是工具的职责 |
| agent_go 感知 Issue 状态 | agent_go 不管理 Issue，工具侧独立维护状态 |

---

## 九、技术架构建议

```
┌─────────────────────────────────────────────────────┐
│              项目管理工具                               │
│                                                     │
│  Frontend: Web UI (React/Vue/...)                    │
│                                                     │
│  Backend:                                            │
│  ├─ PRD/Roadmap 管理（Markdown 读写 + Git 操作）       │
│  ├─ Task Spec 编辑器（Markdown + AI 辅助补全）         │
│  ├─ Spec Gate（L1 确定性检查 + L2 LLM 判断）           │
│  ├─ 状态机引擎（需求卡片状态流转）                       │
│  ├─ MCP Client（调用 agent_go MCP Server）            │
│  └─ SSE Consumer（订阅 agent_go 事件流）               │
│                                                     │
│  外部接口（全部已在 agent_go 侧可用）：                  │
│  ├─ agent_go MCP HTTP/SSE (localhost:8090)            │
│  ├─ Git 操作（读写 docs/，commit Task Spec）           │
│  ├─ Claude Code（AI 辅助需求分析和 Spec 生成）          │
│  └─ GitHub Issues（需求追踪，可选）                     │
└─────────────────────────────────────────────────────┘
```

**关键设计选择**：

| 选择 | 理由 |
|------|------|
| MCP 作为唯一交互协议 | agent_go 已有完整的 MCP server。工具不做 CLI 包装 |
| agent_go 不需要任何改动 | 所有 MCP tools/resources/SSE 已可用 |
| Task Spec 文件为唯一的执行输入 | `--spec` 是 agent_go 的标准输入接口 |
| 工具侧独立维护状态机 | agent_go 的状态是执行状态，不感知需求管理状态 |
| 工具部署在本地 | agent_go 的 MCP HTTP 默认绑定 localhost，不暴露公网 |

---

## 十、与现有工具链的关系

```
GitHub Issues / Jira / Linear           ← 可选的 Issue 追踪层
        │
        │ 需求卡片可关联 Issue 编号
        ▼
项目管理工具（本设计方案）                ← Phases 0-2 的管理层
        │
        │ MCP + Task Spec
        ▼
agent_go                                ← Phases 3-4 的执行层
        │
        │ Git PR
        ▼
GitHub / GitLab                         ← 代码托管和 CI/CD
```

**项目管理工具不是要替代 GitHub Issues 或 Jira**——它是一个介于 Issue 追踪和 agent_go 执行之间的「Scoping 工作台」，专注于把需求变成可执行的 Spec。

**如果你已经在用 GitHub Issues**：工具可以消费 Issue API（读取 Issue 内容 → 辅助生成 Spec → 关联 Issue 编号），执行完成后回写 Issue 状态。

**如果你不需要 Issue 系统**：工具的 PRD → Roadmap → Spec → agent_go 链路本身就构成了完整的轻量需求管理。

---

*关联文档：*
- [software-development-lifecycle.md](software-development-lifecycle.md) — 软件开发全流程定义
- [agent-go-input-spec.md](agent-go-input-spec.md) — Task Spec 规范和 agent_go 输入准则
- [../prd.md](../prd.md) — agent_go 产品定位和 KPI
