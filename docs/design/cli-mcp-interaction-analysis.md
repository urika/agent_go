# agent_go CLI 与 MCP 交互过程分析与改进设计

> 作者：agent_go 架构分析  
> 日期：2026-08-01  
> 版本：v1.0  
> 状态：✅ 完成（§7 落地记录对应代码已全部核验）  
> 前置阅读：[cli-mcp-design-analysis.md](./cli-mcp-design-analysis.md) — 范式总结与最佳实践

---

## 目录

1. [概述](#1-概述)
2. [交互架构总览](#2-交互架构总览)
3. [场景分析](#3-场景分析)
   - 3.1 场景 A：人类首次执行任务（`agent_go run`）
   - 3.2 场景 B：人类恢复中断任务（`agent_go resume`）
   - 3.3 场景 C：人类审查任务结果（`agent_go review --task`）
   - 3.4 场景 D：Agent 异步执行任务（MCP `run_task`，wait=false）
   - 3.5 场景 E：Agent 同步执行任务（MCP `run_task`，wait=true）
   - 3.6 场景 F：Agent 轮询任务状态（MCP `inspect_task`）
   - 3.7 场景 G：Agent 审查任务（MCP `review_task`）
4. [交互设计问题诊断](#4-交互设计问题诊断)
   - 4.1 CLI 层问题
   - 4.2 MCP 层问题
   - 4.3 跨层问题
5. [改进设计方案](#5-改进设计方案)
   - 5.1 CLI 交互改进
   - 5.2 MCP 交互改进
   - 5.3 跨层能力增强
6. [改进优先级](#6-改进优先级)

---

## 1. 概述

本文档从**运行时交互过程**的角度分析 agent_go 的 CLI 和 MCP 两个入口，覆盖人类用户和 AI Agent 两类调用者的完整使用旅程。通过对 7 个核心交互场景的端到端流程分析，识别交互设计中的断点、摩擦点和缺失能力，并给出具体改进方案。

**核心问题**：agent_go 的功能层（Plan → Execute → Verify）已经完整，但交互层存在三个系统性差距：

1. **反馈回路不闭环**：错误发生后，用户/Agent 需要自己理解「接下来该做什么」
2. **上下文传递断裂**：Plan → Subtask → Verify → Review 各阶段之间的信息传递依赖隐式约定
3. **Agent 视角缺失**：MCP 层过于「thin shell」，缺少 Agent 真正需要的引导和 context engineering

---

## 2. 交互架构总览

```
                          ┌──────────────────────────┐
                          │       agent_go Core       │
                          │                            │
  ┌───────────┐           │  Plan → Decompose →       │
  │   Human   │──CLI──────│  Execute → Verify →       │
  │   User    │  (stdin/  │  Report                   │
  └───────────┘  stdout)  │                            │
                          │  ┌──────────────────────┐ │
  ┌───────────┐           │  │  Interaction Layer    │ │
  │   AI      │──MCP──────│  │  ┌──────┐ ┌────────┐ │ │
  │   Agent   │  (JSON-   │  │  │  UI  │ │Console │ │ │
  │  (LLM)    │   RPC)    │  │  └──────┘ └────────┘ │ │
  └───────────┘           │  │  ┌────────┐ ┌───────┐ │ │
                          │  │  │ Router │ │Eval   │ │ │
                          │  │  └────────┘ └───────┘ │ │
                          │  └──────────────────────┘ │
                          │                            │
                          │  ┌──────────────────────┐ │
                          │  │  Execution Engine     │ │
                          │  │  Pipeline → Executor  │ │
                          │  │  → Subtask → Worktree │ │
                          │  └──────────────────────┘ │
                          └──────────────────────────┘

  Interaction paths:
  ───────── CLI path (Human → agent_go):
     1. argparse parse → cmd_run/cmd_resume/cmd_review/...
     2. Console output (human-readable / --json)
     3. Interactive prompts (ui.py: confirm_plan, confirm_subtasks)
     4. Inline progress (subtask_activity events)
     5. Final report + exit code

  ═════════ MCP path (Agent → agent_go):
     1. JSON-RPC tools/call → _dispatch_tool
     2. Subprocess spawn → agent_go --yes --json
     3. JSON Lines stdout parsing → event stream
     4. notifications/progress → real-time push
     5. Structured result → JSON response
```

### 关键交互节点

```
  Plan            SubTask           Execute          Verify          Review
   │                │                 │                │               │
   ▼                ▼                 ▼                ▼               ▼
┌──────┐  ┌──────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│gen   │  │plan  │  │_run_     │  │_verify_  │  │_show_    │
│_plan │→│_to_  │→│claude    │→│changes   │→│task_     │
│      │  │sub-  │  │          │  │(retry    │  │review    │
│      │  │tasks │  │          │  │ loop)    │  │          │
└──┬───┘  └──┬───┘  └────┬─────┘  └────┬─────┘  └────┬─────┘
   │         │            │              │              │
   ▼         ▼            ▼              ▼              ▼
 交互确认  交互确认    进度事件      验证事件       审查仪表
 (Y/S/D/   (Y/N/E/    subtask_      verify_retry    per-file
  E/M/R/N)  A/D)      activity      verify_ok       summary
                      pipeline_     failure_        approve/
                      start/        reason          reject/
                      complete                      changes-
                                                    requested
```

---

## 3. 场景分析

### 3.1 场景 A：人类首次执行任务（`agent_go run`）

#### 3.1.1 完整交互序列

```
Step 1: 命令调用
  User: agent_go run ./myproject 'Add unit tests for auth module' --parallel 2 --remote origin

Step 2: CLI 参数解析 (cli.py::_build_parser → cmd_run)
  ├── Console(quiet=False, verbose=False, json_mode=False)
  ├── load_config()
  ├── 任务ID 生成: task-20260801-143022-123-ab
  └── setup_logger(task_id, task_dir)

Step 3: Skill/Agent 加载 (cmd_run → load_skills → load_agent_type)
  Output: 🔧 主任务: Add unit tests for auth module
          📁 项目: /Users/xxx/myproject
          🆔 任务ID: task-20260801-143022-123-ab

Step 4: Plan 生成循环 (cmd_run → generate_plan → confirm_plan)
  ├── Output: 🤖 进入 Plan Mode...
  ├── generate_plan() → LLM API 调用 → Plan JSON
  ├── print_plan(plan) → 终端显示 Plan 摘要
  ├── 交互提示:
  │   ┌─────────────────────────────────────────────┐
  │   │ [Y] 确认  [S] 补充  [D] 挂载文档             │
  │   │ [E] 编辑步骤  [M] 编辑器编辑  [R] 重试  [N] 取消 │
  │   └─────────────────────────────────────────────┘
  ├── User: Y (or S/D/E/M/R/N)
  └── 如 R: 重新 generate_plan → 再次确认 (最多 5 次)

Step 5: 子任务拆解 (confirm_plan → plan_to_subtasks)
  ├── plan_to_subtasks() → role_skill_map 规则应用
  └── print_subtasks(subtasks) → 终端显示子任务列表

Step 6: 子任务确认 (confirm_subtasks)
  ├── 交互提示:
  │   ┌─────────────────────────────────────────────┐
  │   │ [Y] 确认  [N] 取消  [E] 编辑  [A] 添加  [D] 删除 │
  │   └─────────────────────────────────────────────┘
  └── User: Y

Step 7: 时间预估 (estimate_task_duration)
  Output: ⏱️ 预计耗时: ~8 分钟 — 5 个子任务 / 2 个波次 / 并行 2（样本较少）

Step 8: Pipeline 执行 (_run_pipeline)
  ├── 禁用 git gc.auto
  ├── Wave 1: [sub-01, sub-02] (并行)
  │   ├── [sub-01] _create_worktree → _git_merge_upstream → _build_task_md → _run_claude → _verify_changes
  │   │   Progress: ➜ sub-01: Read src/auth.py (12s)
  │   │            ➜ sub-01: Write tests/test_auth.py (45s)
  │   │            ✅ sub-01: completed (2 changes, 2m3s)
  │   ├── [sub-02] (同上，并行)
  │   │   Progress: ➜ sub-02: Write tests/test_session.py (30s)
  │   │            ✅ sub-02: completed (1 change, 1m45s)
  ├── Wave 2: [sub-03, sub-04, sub-05] (并行)
  │   └── ...
  ├── 远程推送: git push origin agent_go/task-xxx/sub-*
  ├── Worktree 清理: 删除成功的 worktree，保留失败的
  └── 恢复 git gc.auto

Step 9: 最终报告
  Output:
  ┌──────────────────────────────────────────────────────┐
  │ ✅ 全部完成: 5/5                                     │
  │ 💰 预估成本: $0.234                                  │
  │ ⏱️ 总耗时: 6m42s                                    │
  │                                                      │
  │ ✅ sub-01  新增测试: auth.py               2m3s      │
  │ ✅ sub-02  新增测试: session.py            1m45s     │
  │ ✅ sub-03  新增测试: middleware.py          1m20s     │
  │ ✅ sub-04  新增测试: handlers.py            1m10s     │
  │ ✅ sub-05  集成测试完善                      24s      │
  │                                                      │
  │ 📋 审查: agent_go review --task task-20260801-...     │
  │ 🔀 创建 PR: agent_go pr task-20260801-...             │
  └──────────────────────────────────────────────────────┘
```

#### 3.1.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| A1 | Step 4 Plan 确认 | 用户不知道 Plan 被修改了多少次、每次改了什么。`plan-history` 只能事后查看 | 迭代效率低，重复修改已修正的问题 |
| A2 | Step 4→5 | Plan 确认后到拆解结果之间无过渡——如果拆解出来的 subtask 不符合预期，只能回退重来 | 无法在拆解后微调 Plan |
| A3 | Step 6 Subtask 确认 | 确认界面缺少 agent_type 来源标注（LLM/规则/默认），用户不知道为什么选了某个 Agent 类型 | 对 Agent 路由决策不透明 |
| A4 | Step 7 时间预估 | 置信度为 "low" 时仍然只是标注，没有给用户「是否继续」的选择 | 低置信度预估下可能浪费时间 |
| A5 | Step 8 执行中 | 并行执行时，进度输出交错混乱。「➜ sub-01: ...」和「➜ sub-02: ...」难以区分 | 可读性差 |
| A6 | Step 8 失败 | 如果 sub-03 验证失败 3 次，只有最后失败时才看到原因。中间两次重试的结果不展示 | 无法感知重试进展 |
| A7 | Step 8 失败 | 失败后推荐 `agent_go inspect` 和 `agent_go resume`，但没有给出可直接复制执行的命令 | 用户需要手动拼接 task_id |
| A8 | Step 9 报告 | 报告缺少「下一步行动」的明确引导。`review` 和 `pr` 是两个独立步骤 | 用户可能遗漏 review 环节 |

---

### 3.2 场景 B：人类恢复中断任务（`agent_go resume`）

#### 3.2.1 完整交互序列

```
Step 1: 命令调用
  User: agent_go resume task-20260801-143022-123-ab

Step 2: 状态重建 (cmd_resume)
  ├── 读取 meta.json → 获取 confirmed subtasks
  ├── 扫描各 sub-N/result.json → 重建 completed_ids
  ├── 扫描各 sub-N/work → 重建 worktree_map
  └── Output: ═══ 恢复任务 task-20260801-143022-123-ab ═══
              已完成: 2/5, 剩余: 3

Step 3: Pipeline 续跑 (_run_pipeline)
  ├── 跳过已完成 subtask
  ├── 从断点 wave 继续
  └── ...
```

#### 3.2.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| B1 | Step 1 | 用户需要记住或查找 task_id。`agent_go list` 给了列表但不够直观 | 「我该 resume 哪个任务」是个决策负担 |
| B2 | Step 1 | resume 命令不支持 `--last` 或 `--interactive` 选择最近中断的任务 | 多余操作 |
| B3 | Step 2 | 恢复时的 Plan/Subtask 确认被跳过（直接进入执行），但用户可能已经忘记上下文 | 盲目恢复 |
| B4 | Step 2 | 如果 resume 时原 repo 的代码已经变了（比如用户手动修了部分），可能导致 worktree 状态不一致 | 静默失败 |

---

### 3.3 场景 C：人类审查任务结果（`agent_go review --task`）

#### 3.3.1 完整交互序列

```
Step 1: 命令调用
  User: agent_go review --task task-20260801-143022-123-ab

Step 2: 审查仪表 (_show_task_review)
  ├── 读取 meta.json + results
  ├── 按文件分组变更: file_changes dict
  ├── Output: # 📋 任务审查: task-20260801-...
  │           ## 📁 文件变更汇总
  │           | 文件 | 涉及子任务 | 变更量 | 验证 |
  │           | src/auth.py | sub-01 | +45/-12 | ✅ |
  │           ## 🔍 子任务详情
  │           | 子任务 | 标题 | Agent | 状态 | 验证 | 耗时 |
  │           ## 📊 Quality Dashboard
  │           合并就绪: 🟢 可以合并

Step 3: 审查决策
  User: agent_go review --task task-20260801-... --approve
  Output: ✅ 审查通过 — 已写入 review.json
```

#### 3.3.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| C1 | Step 2 | `--deep` 选项独立于默认审查，用户容易遗漏深层分析 | 可能错过独立模型发现的深层问题 |
| C2 | Step 2→3 | 审查和决策是两步操作，review.json 写入后 pipeline 报告中的「下一步」不更新 | 状态不一致 |
| C3 | Step 2 | 文件变更汇总只显示涉及哪些子任务，不显示具体变更内容 | 审查深度不足，需要 cd 到 worktree 看 diff |

---

### 3.4 场景 D：Agent 异步执行任务（MCP `run_task`，wait=false）

#### 3.4.1 完整交互序列

```
Step 1: Agent 调用 MCP Tool
  tools/call {
    "name": "run_task",
    "arguments": {
      "repo": "/Users/xxx/myproject",
      "task": "Add unit tests for auth module",
      "parallel": 2,
      "wait": false
    }
  }

Step 2: MCP Server 处理 (_tool_run_task)
  ├── _check_repo_allowed(repo) → ✅
  ├── _spawn(["python", "-m", "agent_go", "run", repo, task, "--yes", "--json", ...])
  ├── _read_agentgo_start(proc) → task_id = "task-20260801-..."
  └── Return: {
        "task_id": "task-20260801-143022-123-ab",
        "status": "running",
        "task_dir": "/Users/xxx/.agent_go/task-20260801-...",
        "pid": 12345,
        "poll_hint": {
          "tool": "inspect_task",
          "params": {"task_id": "task-20260801-..."},
          "suggested_interval_sec": 30
        }
      }

Step 3: Agent 轮询 (由 Agent 自主决定)
  tools/call {
    "name": "inspect_task",
    "arguments": {"task_id": "task-20260801-..."}
  }
  → 返回 progress + subtasks + cost

Step 4: Agent 确认完成
  → status: "completed"
```

#### 3.4.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| D1 | Step 2 | `poll_hint` 是唯一的后续引导，但 Agent 仍然需要理解「轮询直到完成」这个模式 | Agent 需要写轮询循环 |
| D2 | Step 3 | `inspect_task` 返回全量数据（所有 subtask），即使 Agent 只想要进度摘要 | Context bloat |
| D3 | Step 2 | 异步模式下没有 push 通知机制。Agent 必须主动轮询 | 延迟感知慢，浪费轮询 token |
| D4 | Step 2→3 | 如果 Agent 忘记轮询（会话切换），任务在后台运行但无消费方 | 僵尸任务 |
| D5 | Step 2 | task_dir 路径暴露了服务器文件系统结构 | 信息泄露 |

---

### 3.5 场景 E：Agent 同步执行任务（MCP `run_task`，wait=true）

#### 3.5.1 完整交互序列

```
Step 1: Agent 调用 MCP Tool
  tools/call {
    "name": "run_task",
    "arguments": {
      "repo": "/Users/xxx/myproject",
      "task": "Add unit tests for auth module",
      "wait": true,
      "timeout_sec": 3600
    },
    "_meta": {"progressToken": "progress-001"}
  }

Step 2: MCP Server 处理
  ├── _spawn(...) → proc
  ├── _read_agentgo_start(proc) → task_id
  └── Thread: _wait_with_events(proc, task_id, timeout, token)

Step 3: 事件流处理 (_wait_with_events)
  ├── 后台线程: 解析 stdout JSON Lines
  │   ├── "event":"pipeline_start" → total_subtasks=5
  │   ├── "event":"subtask_start"  → sub_id=sub-01, title="..."
  │   ├── "event":"subtask_activity" → activity="Read src/auth.py"
  │   │   └── 更新 activity_store[task_id]
  │   ├── "event":"subtask_complete" → sub_id=sub-01, status="completed"
  │   └── "event":"pipeline_complete" → status="completed"
  │
  ├── 主线程: 轮询 meta.json (每 0.5s)
  │   └── 推送 notifications/progress:
  │       {
  │         "progressToken": "progress-001",
  │         "progress": 2,
  │         "total": 5,
  │         "current_activity": "Completed sub-01: completed",
  │         "message": "2/5 完成"
  │       }

Step 4: 返回最终结果 (_build_completed)
  → {
      "task_id": "task-20260801-...",
      "status": "completed",
      "duration_sec": 402,
      "cost_usd": 0.234,
      "results": [...],
      "preserved_worktrees": []
    }
```

#### 3.5.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| E1 | Step 3 | 事件解析依赖 subprocess stdout JSON Lines 格式——脆弱的字符串解析，agent_go 输出格式变化即断裂 | 耦合 |
| E2 | Step 3 | 主线程轮询 meta.json 和后台线程解析事件是两套独立的状态追踪——可能不一致 | 状态歧义 |
| E3 | Step 3 | `current_activity` 只追踪最后一个 subtask。并行执行时，其他 subtask 的活动会互相覆盖 | 丢失并行进度 |
| E4 | Step 3 | `activity_store` 是内存结构，MCP Server 重启后丢失 | 不可靠 |
| E5 | Step 4 | 返回结果包含所有 subtask details（每个有 changes/verify_ok/retry_count）——数据量大 | Context bloat |
| E6 | Step 4 | 失败时没有 `fix` 引导。Agent 看到失败后不知道该调用什么工具修复 | 恢复路径不明确 |
| E7 | Step 4 | `timeout_sec` 到达时，结果中有 `timeout_hint` 但 Agent 需要自行理解并调用 resume | 隐式约定 |

---

### 3.6 场景 F：Agent 轮询任务状态（MCP `inspect_task`）

#### 3.6.1 完整交互序列

```
Step 1: Agent 调用 MCP Tool
  tools/call {
    "name": "inspect_task",
    "arguments": {
      "task_id": "task-20260801-...",
      "include_log_tail": true,
      "log_lines": 30
    }
  }

Step 2: MCP Server 处理 (_tool_inspect)
  ├── 读取 meta.json → 解析 status/progress/subtasks
  ├── _aggregate_cost(task_dir) → cost_usd
  ├── _find_preserved(task_id, results) → preserved_worktrees
  ├── [可选] 读取 activity_store[task_id] → current_activity
  └── [可选] tail execution.log → log_tail

Step 3: 返回结果
  → {
      "task_id": "task-20260801-...",
      "status": "running",
      "progress": {"completed": 2, "failed": 0, "total": 5},
      "cost_usd": 0.105,
      "subtasks": [
        {"id": "sub-01", "status": "completed", "duration_sec": 123, ...},
        {"id": "sub-02", "status": "completed", "duration_sec": 105, ...},
        {"id": "sub-03", "status": "running", "current_activity": "Read src/middleware.py", ...},
        ...
      ],
      "current_activity": "Read src/middleware.py",
      "preserved_worktrees": [],
      "log_tail": ["..."]
    }
```

#### 3.6.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| F1 | Step 2 | `inspect_task` 总是返回全量 subtask list + preserved worktrees，即使 Agent 只关心进度数字 | Context bloat |
| F2 | Step 2 | `current_activity` 从 activity_store 读取，但 activity_store 只追踪最后一个 subtask | 并行场景信息不完整 |
| F3 | Step 2 | `cost_usd` 计算依赖 metering.jsonl 存在——如果 subtask 未完成，成本是 partial 的但没有标注 | 误导性数据 |
| F4 | Step 3 | 返回的 subtask 列表中，`changes` 是 `{files, insertions, deletions}`——如果 files=0 但没有说明是「无变更」还是「未统计」 | 歧义 |

---

### 3.7 场景 G：Agent 审查任务（MCP `review_task`）

#### 3.7.1 完整交互序列

```
Step 1: Agent 调用 MCP Tool (分析)
  tools/call {
    "name": "review_task",
    "arguments": {
      "task_id": "task-20260801-...",
      "action": "analyze",
      "deep": true
    }
  }

Step 2: MCP Server 处理
  ├── _ensure_task_dir(task_id)
  ├── _spawn(["python", "-m", "agent_go", "review", "--task", task_id, "--deep"])
  └── subprocess.run() → 解析 stdout JSON Lines → 返回结果

Step 3: Agent 调用 MCP Tool (决策)
  tools/call {
    "name": "review_task",
    "arguments": {
      "task_id": "task-20260801-...",
      "action": "approve",
      "comment": "All changes look correct"
    }
  }

Step 4: MCP Server 处理
  └── 写入 review_decision.json + review_history.jsonl
```

#### 3.7.2 交互摩擦点

| # | 阶段 | 问题 | 影响 |
|---|------|------|------|
| G1 | Step 1 | `analyze` 通过 subprocess 运行 agent_go review，300s 超时——对于大型 diff 可能不够 | 审查被截断 |
| G2 | Step 1→3 | analyze → approve/reject 是两步操作。Agent 需要从 analyze 结果中提取关键信息来做决策 | 增加推理步骤 |
| G3 | Step 2 | `analyze` 返回 JSON Lines 而非结构化对象——`_parse_jsonl_last` 只取最后一行 | 可能丢失之前的分析结果 |
| G4 | Step 4 | `review_history.jsonl` 追加写入，但没有任何机制通知 Agent 历史决策 | 审计信息不可达 |

---

## 4. 交互设计问题诊断

### 4.1 CLI 层问题

#### 问题 1：确认流程缺乏增量上下文

```
当前:  Plan → 确认 (Y/S/D/E/M/R/N) → 如果 R: 全量重新生成 → 再次确认
问题:  用户修改了 Step 3 的描述，但 Plan 重新生成后所有 Step 都可能变化。
       用户不知道「哪些变了」，只能全量重新审视。

改进:  支持增量 Plan 修改（仅重新生成指定 Step）+ Plan diff 实时对比
```

#### 问题 2：失败恢复路径不闭环

```
当前:  失败 → 输出 "agent_go inspect <task-id>" → 用户手动执行
问题:  用户需要: 1) 记住 task_id  2) 理解 inspect 的作用  3) 手动 cd 到 worktree
       这三步之间没有引导串联。

改进:  失败输出改为:
       ❌ sub-03 验证失败 (3/3 retries)
       📁 保留现场: /Users/xxx/.agent_go/task-xxx/sub-03/work
       🔗 git branch: agent_go/task-xxx/sub-03
       📝 失败原因: test_auth.py:45 assertion error - missing null check
       
       📋 下一步:
       1. 查看现场: agent_go inspect task-xxx
       2. 手动修复后恢复: agent_go resume task-xxx
       3. 或在 worktree 中直接修改后: agent_go resume task-xxx
       
       → 直接复制执行: agent_go inspect task-20260801-143022-123-ab
```

#### 问题 3：进度输出在多 wave 场景下混乱

```
当前:  Wave 1 完成后没有明确的「波次分隔」。用户看到一堆 ✅ 但不知道哪些是同一 wave。
      并行 subtask 的进度交替出现，无法关联到具体 subtask。

改进:  增加波次卡片 + 并行分组显示:

       ═══ Wave 1/2: 基础设施 (2 并行) ═══
       ┌─ sub-01: Adding auth tests ──────────────────────┐
       │ ➜ Read src/auth.py (12s)                          │
       │ ➜ Write tests/test_auth.py (45s)                  │
       │ ✅ completed (2 changes, 2m3s)                    │
       └──────────────────────────────────────────────────┘
       ┌─ sub-02: Adding session tests ───────────────────┐
       │ ➜ Write tests/test_session.py (30s)               │
       │ ✅ completed (1 change, 1m45s)                    │
       └──────────────────────────────────────────────────┘
```

#### 问题 4：报告缺少可执行的动作卡片

```
当前:  📋 审查: agent_go review --task task-...
       🔀 创建 PR: agent_go pr task-...

改进:  输出可点击/可复制的一键命令:

       ┌─────────── 后续操作 ───────────┐
       │                                │
       │  📋 审查变更                   │
       │  $ agent_go review \           │
       │    --task task-xxx --deep      │
       │                                │
       │  🔀 创建 Pull Request          │
       │  $ agent_go pr task-xxx \      │
       │    --push --remote origin      │
       │                                │
       │  🧹 清理任务                   │
       │  $ agent_go clean              │
       └────────────────────────────────┘
```

### 4.2 MCP 层问题

#### 问题 5：Context Bloat — 工具定义全量加载

```
基线（改进前）: 4 个 tool definitions (run_task, resume_task, inspect_task, review_task)
      现状: 6 个 tool definitions（+ list_tasks, cancel_task）+ Resources/Prompts 原语
      每个都有 description + annotations + 完整 inputSchema
      无论 Agent 当前是否需要，全部加载到 LLM context

量化:  run_task description: ~200 chars
       run_task inputSchema: ~600 chars
       resume_task: ~500 chars total
       inspect_task: ~600 chars total
       review_task: ~700 chars total
       ────────────────────────────
       总计: ~2600 chars ≈ ~650 tokens (每次会话固定消耗)

改进:  将低频 field (如 inputSchema 中的 enum 值描述) 移入 Resources 按需加载
       或使用 Tool Search Tool 模式 (AWS V4) 按需检索工具定义
```

#### 问题 6：缺少 Context Engineering — 没有 Resources 原语

```
当前:  Agent 想要了解任务状态 → 必须调用 inspect_task tool → 返回全量数据
       Agent 想要了解成本 → 必须解析 inspect_task 返回中的 cost_usd 字段
       Agent 想要了解 Plan → 没有途径（除非进入 worktree 读文件）

改进:  暴露 Resources:
       - agent_go://tasks/{task_id}/summary     → {status, progress, cost}
       - agent_go://tasks/{task_id}/plan        → Plan JSON
       - agent_go://tasks/{task_id}/metering    → cost breakdown
       - agent_go://tasks/{task_id}/log/recent  → last N log lines
       
       Agent 通过 resources/read 按需获取上下文，而非通过 tool call 获取全量
```

#### 问题 7：错误响应缺少可执行引导

```
当前:  {
         "error": {
           "code": "AGENT_GO_TASK_NOT_FOUND",
           "message": "任务不存在: task-xxx",
           "retryable": false
         }
       }

问题:  Agent 收到此错误后，无法自主恢复。它不知道:
       1. 这个 task_id 是否曾经存在但已清理？
       2. 如何获取有效的 task_id 列表？

改进:  {
         "error": {
           "code": "AGENT_GO_TASK_NOT_FOUND",
           "message": "任务不存在: task-xxx",
           "retryable": false,
           "fix": {
             "action": "调用 list_tasks 获取有效任务列表",
             "tool": "list_tasks",
             "params": {"status": "all", "limit": 10}
           }
         }
       }
```

#### 问题 8：异步模式缺少推送通知

```
当前:  wait=false → 返回 poll_hint → Agent 必须轮询
问题:  轮询延迟 + 浪费 token + Agent 可能忘记轮询

改进选项:
  A. MCP notifications 推送: Server 在关键状态变更时主动推送
  B. Webhook 回调: Agent 提供 webhook URL, Server 在完成时 POST
  C. SSE transport: 支持 SSE 长连接推送
```

#### 问题 9：Progress 事件只追踪单 subtask

```
当前:  _wait_with_events 中:
       subtask_activity → state["current_activity"] = ...  (覆盖)
       subtask_activity → activity_store[task_id] = ...    (覆盖)
       
问题:  并行执行 3 个 subtask 时，activity 互相覆盖，Agent 只能看到最后一个

改进:  activity_per_subtask = {sub-01: "Read auth.py", sub-02: "Write session.py"}
       每次 inspect_task 返回全部 subtask 的 activity 快照
```

### 4.3 跨层问题

#### 问题 10：CLI 和 MCP 的交互体验割裂

```
CLI:   丰富的交互确认 (Y/S/D/E/M/R/N)、TUI dashboard、内联进度
MCP:   无交互确认 (--yes 硬编码)、无 TUI、仅轮询

问题:  MCP 路径是 CLI 路径的 degraded 版本，不是优化版本。
       MCP 用户（Agent）需要的是不同类型的交互，而不是更少的交互。

改进:  MCP 路径应为 Agent 特化:
       - 用 Resources 替代交互确认（Agent 通过读 Plan Resource 来做决策）
       - 用 Sampling 替代交互输入（Agent 通过 sampling/createMessage 确认关键决策）
       - 用结构化错误替代人读的错误信息（Agent 解析 fix 字段直接行动）
```

#### 问题 11：任务生命周期管理不完整

```
当前生命周期:
  running → completed → (reviewed) → (pr_created)
  running → failed → (resume) → running → ...
  running → paused (SIGTERM)

缺失状态:
  - cancelled: Agent/用户主动取消
  - timed_out: 超时但可恢复
  - stale: 无人消费的后台任务

缺失操作:
  - cancel: 终止运行中的任务
  - forget: 清理已完成任务（保留 metering）
  - duplicate: 基于已有任务创建新任务（复用 Plan）
```

---

## 5. 改进设计方案

> **落地状态**：P0/P1/P2-1 已于 2026-08-01 完成实现（见 §7 落地记录），未落地项保留设计方案供后续迭代。

### 5.1 CLI 交互改进

#### 改进 1：增量 Plan 迭代 + 实时 Diff

**当前状态**：Plan 修改需要全量重新生成，用户无法追踪变更。

**设计方案**：

```
Plan 确认菜单改进:

  [Y] 确认当前 Plan
  [S] 补充上下文（全量重新生成）
  [D] 挂载参考文档
  [E] 编辑单个步骤（就地修改，不重新生成）
  [M] 在编辑器中编辑
  [R] 重新生成 Plan（带 diff 对比）
  [V] 查看版本历史 (plan-history 内联)
  [N] 取消

选择 R 时的新流程:
  1. 保存当前 Plan 为 v{N}
  2. 重新调用 generate_plan
  3. 自动显示 diff: 新增/删除/修改的步骤
  4. 用户确认是否接受新 Plan 或回退
```

**代码变更**：

```python
# ui.py — confirm_plan 增强
def confirm_plan(plan, config, repo, logger, iteration=1, task="", previous_plan=None):
    # 新增: 如果 previous_plan 存在，自动生成 diff
    if previous_plan:
        diff_summary = _compute_plan_diff(previous_plan, plan)
        console.print("\n📊 Plan 变更摘要:")
        for change in diff_summary:
            console.print(f"  {change['icon']} {change['description']}")
    # ... 原有交互逻辑
```

#### 改进 2：闭环失败恢复引导

**设计方案**：

```python
# pipeline.py — _run_pipeline 最终报告增强
def _print_failure_guidance(failed_subtasks, task_id, task_dir):
    """打印失败恢复的完整操作指引。"""
    console.sep("=", 70)
    console.title("🔧 失败恢复指引")
    
    for st in failed_subtasks:
        sub_id = st["id"]
        wt = task_dir / sub_id / "work"
        
        console.print(f"\n❌ {sub_id}: {st.get('title', '')}")
        console.print(f"   原因: {st.get('failure_reason', '未知')}")
        if wt.exists():
            console.print(f"   📁 {wt}")
            console.print(f"   🔗 git branch: agent_go/{task_id}/{sub_id}")
    
    console.print(f"\n📋 推荐操作 (可直接复制执行):")
    console.print(f"   agent_go inspect {task_id}           # 查看失败现场")
    console.print(f"   agent_go review --task {task_id}     # 审查已完成部分")
    console.print(f"   agent_go resume {task_id}            # 修复后继续执行")
    console.print(f"   agent_go resume {task_id} --no-verify-block  # 不阻断下游")
```

#### 改进 3：结构化波次进度显示

**设计方案**：

```python
# executor.py — 增强进度事件
def _emit_wave_progress(wave_num, total_waves, subtasks_in_wave):
    """发射波次进度事件，供 Console 和 TUI 消费。"""
    console.emit("wave_progress", {
        "wave": wave_num,
        "total_waves": total_waves,
        "subtasks": [s["id"] for s in subtasks_in_wave],
        "parallel": len(subtasks_in_wave) > 1,
    })
```

#### 改进 4：报告增加可执行动作卡片

**设计方案**：

```python
# pipeline.py — 最终报告
def _print_action_card(task_id, meta):
    """打印后续操作卡片。"""
    lines = [
        "┌" + "─" * 48 + "┐",
        "│" + "  📋 后续操作" + " " * 35 + "│",
        "│" + " " * 48 + "│",
        f"│  agent_go review --task {task_id}" + " " * (48 - 38 - len(task_id)) + "│",
        f"│  agent_go review --task {task_id} --deep --approve" + " " * max(0, 48 - 48 - len(task_id)) + "│",
        f"│  agent_go pr {task_id} --push" + " " * (48 - 28 - len(task_id)) + "│",
        "│" + " " * 48 + "│",
        "└" + "─" * 48 + "┘",
    ]
    console.force("\n".join(lines))
```

### 5.2 MCP 交互改进

#### 改进 5：Resources 原语实现（Context Engineering 核心）

**设计方案**：

```python
# mcp_server.py — 新增 Resources
RESOURCES = [
    {
        "uri": "agent_go://tasks/{task_id}/summary",
        "name": "Task Summary",
        "description": "任务概要：状态、进度、耗时、成本。比 inspect_task tool 更精简",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/plan",
        "name": "Latest Plan",
        "description": "最新版本的执行计划",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/metering",
        "name": "Metering Data",
        "description": "Token 用量和成本明细（按 role 和 subtask 拆分）",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/log/recent",
        "name": "Recent Log",
        "description": "最近 50 行执行日志，用于错误诊断",
        "mimeType": "text/plain",
    },
    {
        "uri": "agent_go://tasks/{task_id}/review",
        "name": "Review Status",
        "description": "审查决策状态（approved/rejected/changes_requested）",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/list",
        "name": "Task List",
        "description": "所有任务列表（ID、状态、描述、时间）",
        "mimeType": "application/json",
    },
]

# resources/read handler
def _handle_resources_read(self, uri: str) -> dict:
    parsed = self._parse_uri(uri)
    
    if parsed["resource"] == "summary":
        return self._build_task_summary(parsed["task_id"])
    elif parsed["resource"] == "plan":
        return self._read_plan(parsed["task_id"])
    elif parsed["resource"] == "metering":
        return self._read_metering(parsed["task_id"])
    elif parsed["resource"] == "log/recent":
        return self._read_log_tail(parsed["task_id"])
    elif parsed["resource"] == "review":
        return self._read_review_status(parsed["task_id"])
    elif parsed["resource"] == "list":
        return self._list_all_tasks()
```

**Agent 使用模式对比**：

```
之前 (仅 Tool):
  Agent: tools/call inspect_task → 返回全量 subtask + progress + cost
  Token: ~800 tokens per poll
  问题: 每次轮询都返回全部数据

之后 (Tool + Resource):
  Agent: resources/read agent_go://tasks/{id}/summary → {status, progress, cost}
  Token: ~150 tokens per poll
  需要详情时: resources/read agent_go://tasks/{id}/metering
  需要 Plan 时: resources/read agent_go://tasks/{id}/plan
  
  优势: 按需分层获取上下文，减少 80% 轮询 token 消耗
```

#### 改进 6：结构化错误 + 可执行修复指令

**设计方案**：

```python
# mcp_server.py — MCPError 增强
class MCPError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, 
                 fix: Optional[dict] = None, context: Optional[dict] = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fix = fix        # {"tool": "...", "params": {...}, "description": "..."}
        self.context = context  # 补充上下文

# 预定义错误类型
ERRORS = {
    "TASK_NOT_FOUND": {
        "code": "AGENT_GO_TASK_NOT_FOUND",
        "fix": {
            "description": "获取有效任务列表",
            "tool": "list_tasks",       # 或 resources/read
            "params": {"status": "all", "limit": 20}
        }
    },
    "REPO_INVALID": {
        "code": "AGENT_GO_REPO_INVALID",
        "fix": {
            "description": "检查仓库路径是否在 MCP Server 的 allowlist 中",
            "check_env": "AGENT_GO_MCP_ALLOWED_REPOS"
        }
    },
    "CAPACITY": {
        "code": "AGENT_GO_CAPACITY",
        "retryable": True,
        "fix": {
            "description": "等待当前任务完成或增加并发限制",
            "suggested_wait_sec": 30,
            "retry_tool": "run_task",
        }
    },
    "TASK_FAILED": {
        "code": "AGENT_GO_TASK_FAILED",
        "fix": {
            "description": "查看失败详情并决定下一步",
            "resources": ["agent_go://tasks/{task_id}/log/recent"],
            "tools": [
                {"tool": "inspect_task", "params": {"task_id": "{task_id}"}},
                {"tool": "resume_task", "params": {"task_id": "{task_id}"}},
            ]
        }
    },
}
```

#### 改进 7：增加 `list_tasks` Tool + `cancel_task` Tool

```python
# mcp_server.py — 新增工具
{
    "name": "list_tasks",
    "description": "列出任务。支持按状态过滤和分页。返回比 resources/read tasks/list 更精简的概要",
    "annotations": {"title": "List tasks", "readOnlyHint": True, "idempotentHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string", 
                "enum": ["running", "completed", "failed", "all"],
                "default": "all",
                "description": "按状态过滤"
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        }
    }
},
{
    "name": "cancel_task",
    "description": "取消正在运行的任务。终止子进程，保存已完成的结果。不可逆",
    "annotations": {"title": "Cancel task", "destructiveHint": True, "idempotentHint": True},
    "inputSchema": {
        "type": "object",
        "required": ["task_id"],
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
        }
    }
}
```

#### 改进 8：Prompts 原语 — 编码操作流程

```python
PROMPTS = [
    {
        "name": "diagnose_failure",
        "description": "诊断任务失败的 prompt 模板。引导 Agent 系统性地分析失败原因",
        "arguments": [
            {"name": "task_id", "description": "失败任务 ID", "required": True},
        ],
    },
    {
        "name": "review_and_decide",
        "description": "审查任务结果并做出批准/拒绝决策的 prompt 模板",
        "arguments": [
            {"name": "task_id", "description": "任务 ID", "required": True},
        ],
    },
    {
        "name": "resume_or_restart",
        "description": "决定是 resume 还是重新 run 的决策 prompt 模板",
        "arguments": [
            {"name": "task_id", "description": "任务 ID", "required": True},
        ],
    },
]

# prompts/get handler
def _handle_prompts_get(self, name: str, arguments: dict) -> dict:
    if name == "diagnose_failure":
        task_id = arguments["task_id"]
        return {
            "messages": [{
                "role": "user",
                "content": (
                    f"任务 {task_id} 执行失败。请按以下步骤诊断：\n\n"
                    f"1. 使用 resources/read agent_go://tasks/{task_id}/log/recent 获取最近日志\n"
                    f"2. 使用 inspect_task 查看各子任务状态和失败原因\n"
                    f"3. 分析失败原因是代码问题还是环境问题\n"
                    f"4. 给出修复建议和下一步操作\n\n"
                    f"如果失败原因是代码问题，建议手动修复后 resume。\n"
                    f"如果失败原因是环境问题，建议修复环境后 resume。\n"
                    f"如果 Plan 本身有问题，建议重新 run。"
                )
            }]
        }
    # ... 其他 prompt
```

#### 改进 9：并行活动追踪增强

**当前问题**：`activity_store` 只追踪最后一个 subtask 的 activity。

**改进方案**：使用 per-subtask activity + timestamp：

```python
# mcp_server.py — 增强 activity store
class ActivityTracker:
    """Per-subtask activity tracker with timestamp."""
    
    def __init__(self):
        self._activities: dict[str, dict[str, tuple[str, float]]] = {}  
        # task_id -> {sub_id: (activity_text, timestamp)}
    
    def update(self, task_id: str, sub_id: str, activity: str):
        if task_id not in self._activities:
            self._activities[task_id] = {}
        self._activities[task_id][sub_id] = (activity, time.time())
    
    def get_all(self, task_id: str) -> dict[str, str]:
        """获取所有 subtask 的最新 activity。"""
        if task_id not in self._activities:
            return {}
        return {
            sub_id: activity 
            for sub_id, (activity, ts) in self._activities[task_id].items()
        }
    
    def get_current(self, task_id: str) -> str:
        """获取最近更新的 activity（用于 progress notification 的 current_activity 字段）。"""
        activities = self._activities.get(task_id, {})
        if not activities:
            return ""
        latest_sub = max(activities.keys(), key=lambda k: activities[k][1])
        return activities[latest_sub][0]

# _wait_with_events 中使用
# 更新所有 subtask 的 activity，而非覆盖
tracker = ActivityTracker()
# ...
elif event == "subtask_activity":
    sub_id = payload.get("sub_id", "")
    activity = payload.get("activity", "")
    tracker.update(task_id, sub_id, activity)  # per-subtask 追踪
```

### 5.3 跨层能力增强

#### 改进 10：Sampling 原语 — Agent 决策确认

```python
# mcp_server.py — sampling/createMessage 支持

# 使用场景 1: Plan 置信度低时请求 Agent 确认
def _on_low_confidence_plan(plan_confidence: float) -> dict:
    """当 Plan 生成置信度低时，通过 Sampling 请求 Agent 确认。"""
    if plan_confidence < 0.5:
        return {
            "method": "sampling/createMessage",
            "params": {
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Plan 置信度较低 ({plan_confidence:.0%})。\n"
                        f"请选择: [1] 继续执行  [2] 重新生成 Plan  [3] 降级到规则拆解"
                    ),
                }],
                "maxTokens": 50,
            }
        }
    return None

# 使用场景 2: 高风险操作确认
def _on_destructive_action(action: str, detail: str) -> dict:
    return {
        "method": "sampling/createMessage",
        "params": {
            "messages": [{
                "role": "user",
                "content": (
                    f"⚠️ 即将执行: {action}\n"
                    f"详情: {detail}\n"
                    f"确认执行? [Y] 是  [N] 否"
                ),
            }],
            "maxTokens": 10,
        }
    }
```

#### 改进 11：任务生命周期完善

```
完整生命周期状态机:

  ┌─────────┐
  │ pending │  ← run_task 创建
  └────┬────┘
       │
       ▼
  ┌─────────┐     cancel_task    ┌───────────┐
  │ running │ ─────────────────→ │ cancelled │
  └────┬────┘                    └───────────┘
       │
       ├── 全部成功 ──→ ┌───────────┐
       │                │ completed  │
       │                └─────┬─────┘
       │                      │
       │                      ├── review approve ──→ ┌───────────┐
       │                      │                      │ approved   │
       │                      │                      └───────────┘
       │                      │
       │                      └── review reject ────→ ┌───────────┐
       │                                              │ rejected   │
       │                                              └─────┬─────┘
       │                                                    │
       │                                                    └── resume/重新run
       │
       ├── 部分失败 ──→ ┌────────┐     resume     ┌─────────┐
       │                │ failed  │ ───────────────→ │ running │
       │                └────────┘                    └─────────┘
       │
       ├── 超时 ──→ ┌─────────┐
       │            │ timeout  │ (后台继续，Agent 可 resume/inspect)
       │            └─────────┘
       │
       └── SIGTERM ──→ ┌────────┐
                        │ paused │  ← resume 可恢复
                        └────────┘

新增操作:
  cancel_task: running → cancelled (终止子进程，保存已完成结果)
  forget_task: completed/failed/cancelled → archived (删除 worktree，保留 metering)
```

---

## 6. 改进优先级

按「Agent 调用成功率提升 × 实现成本」排列：

```
  P0 (立即):  ｜ P1 (短期):    ｜ P2 (中期):       ｜ P3 (长期):
              ｜               ｜                  ｜
  错误 fix   ｜ Resources     ｜ Sampling 原语    ｜ SSE/HTTP
  字段       ｜ 原语实现      ｜                  ｜ Transport
              ｜               ｜                  ｜
  list_     ｜ 并行活动      ｜ 生命周期         ｜ 多 profile
  tasks      ｜ 追踪增强      ｜ 状态机           ｜ 支持
              ｜               ｜                  ｜
  cancel_   ｜ 失败恢复      ｜ 增量 Plan        ｜ SKILL.md
  task       ｜ 闭环引导      ｜ 迭代             ｜ 自描述
              ｜               ｜                  ｜
  Prompts    ｜ 波次进度      ｜ Sampling         ｜
  原语       ｜ 卡片显示      ｜ 决策确认         ｜
              ｜               ｜                  ｜
```

### P0 改进（提升 Agent 自主恢复能力）— 预计 3-5 天

| 改进 | 变更文件 | 效果 |
|------|---------|------|
| MCP 错误增加 `fix` 字段 | `mcp_server.py` | Agent 收到错误后可直接获取修复指引 |
| 增加 `list_tasks` tool | `mcp_server.py` | Agent 发现任务，无需提前知道 task_id |
| 增加 `cancel_task` tool | `mcp_server.py` + `pipeline.py` | Agent 可主动终止失控任务 |
| Prompts 原语基础实现 | `mcp_server.py` | Agent 获取标准化的诊断/审查/决策流程 |
| CLI 失败恢复闭环引导 | `pipeline.py` | 用户失败后可一键复制恢复命令 |

### P1 改进（减少 Context Bloat）— 预计 5-8 天

| 改进 | 变更文件 | 效果 |
|------|---------|------|
| Resources 原语实现 (6 个 Resource) | `mcp_server.py` | 轮询 token 消耗降低 80% |
| 并行活动追踪增强 (ActivityTracker) | `mcp_server.py` + `subtask.py` | 并行场景 progress 准确 |
| 结构化波次进度显示 | `executor.py` + `pipeline.py` | CLI/TUI 进度清晰 |
| CLI 报告 action card | `pipeline.py` | 用户明确下一步操作 |

### P2 改进（深化 Agent-Native）— 预计 5-10 天

| 改进 | 变更文件 | 效果 |
|------|---------|------|
| 任务生命周期状态机 | `config.py` + `pipeline.py` + `mcp_server.py` | cancelled/timeout/archived 状态完善 |
| Sampling 原语 (Plan 确认 + 高风险确认) | `mcp_server.py` | Agent 关键决策可暂停请求确认 |
| 增量 Plan 迭代 + 实时 Diff | `ui.py` + `cli.py` | 人类修改 Plan 更高效 |

### P3 改进（产品化）— 后续迭代

- SSE/HTTP Transport 支持远程 MCP 连接
- 多 profile 支持（不同 API endpoint / 不同默认 Skills）
- SKILL.md 自描述命令（`agent_go skills show <name>`）

---

> **文档维护者**：agent_go 架构组  
> **下次审查**：2026-09-01  
> **前置文档**：[cli-mcp-design-analysis.md](./cli-mcp-design-analysis.md)

---

## 7. 落地记录（2026-08-01）

### 已实现（✅）

| 编号 | 改进项 | 变更文件 | 验证 |
|------|--------|---------|------|
| P0-1 | MCP 错误增加 `fix` 字段 + `ERROR_TEMPLATES` 预定义错误类型 + `MCPError.to_dict()` | `mcp_server.py` | ✅ 63 MCP 测试通过 |
| P0-2 | `list_tasks` tool（状态过滤 + 分页） | `mcp_server.py` + `tests/test_mcp_server.py` | ✅ 含分页/过滤测试 |
| P0-3 | `cancel_task` tool（终止子进程 + meta.json 标记 cancelled） | `mcp_server.py` | ✅ 含运行中/已完成测试 |
| P0-4 | Prompts 原语：`prompts/list` + `prompts/get`（diagnose_failure / review_and_decide / resume_or_restart 三个 SOP 模板） | `mcp_server.py` | ✅ 端到端验证 |
| P0-5 | CLI 失败恢复闭环引导（`_run_pipeline` 报告输出可复制执行命令） | `pipeline.py` | ✅ 全量测试通过 |
| P1-1 | Resources 原语：`resources/list` + `resources/read`（summary / plan / metering / log/recent / review / list 六个 Resource） | `mcp_server.py` | ✅ 含 URI 解析/读取测试 |
| P1-2 | ActivityTracker 类（per-subtask + 时间戳 + 单调序号）+ `_start_activity_monitor`（异步任务后台活动监控） | `mcp_server.py` | ✅ 顺序性验证 |
| P1-3 | CLI 报告 action card（后续操作清单） | `pipeline.py` | ✅ 全量测试通过 |
| P2-1 | 生命周期状态机：`resume` 支持 cancelled/stale_aborted；`list` 显示 cancelled 图标；cancel_task 对 paused 同样标记 | `cli.py` + `mcp_server.py` | ✅ 全量测试通过 |

**测试基线**：`pytest tests/` → **1341 passed**（新增 77 个测试，覆盖新工具/原语/错误模型）。

### 未落地（📋 保留设计待迭代）

> **2026-08-01（第二批）**：全部保留项已落地，详见下方「保留项落地记录」。至此改进清单全部闭环。

| 编号 | 改进项 | 说明 |
|------|--------|------|
| — | （无） | 全部落地 |

### 保留项落地记录（2026-08-01 第二批，R-1~R-5）

| 编号 | 改进项 | 变更文件 | 验证 |
|------|--------|---------|------|
| R-1 | **波次进度卡片**：`_estimate_wave_count` 预估算总波次；wave_start/wave_complete 事件带 total_waves/done/failed；CLI 波次卡片（`═══ Wave N/M (并行数) ═══` + 完成汇总） | `pipeline.py` | ✅ 6 测试 |
| R-2 | **SKILL.md 自描述**：`agent_go skills show <name>`（输出完整 SKILL.md，Agent 可读）；`--json` 结构化（frontmatter + body + allowed_tools）；`get_skill_full()` | `skills.py` + `cli.py` | ✅ 2 测试 |
| R-3 | **多 profile**：顶层 `--profile <name>` / `AGENT_GO_PROFILE` 环境变量 → `~/.agent_go/profiles/<name>.json` 或 `config.<name>.json`；`--config` 优先于 profile；所有 load_config 调用点自动生效 | `config.py` + `cli.py` | ✅ 4 测试 |
| R-4 | **增量 Plan 迭代 + 实时 Diff**：`compute_plan_diff` / `show_plan_diff`（新增/删除/修改步骤 + 字段级差异）；confirm_plan 菜单 [V] 内联版本历史；S/D 重新生成与 cmd_run R 循环均展示 diff | `ui.py` + `cli.py` | ✅ 5 测试 |
| R-5 | **Sampling 原语**：`request_sampling()`（stdio 双向 request/response + 超时 + fail-open）；`sampling_confirm()` 确认包装；cancel_task 可选 `confirm` 参数（Host 拒绝时任务保持运行）；HTTP transport 无双向通道自动跳过 | `mcp_server.py` | ✅ 8 测试 |

**测试基线**：`pytest tests/` → **1387 passed**（新增 25 个测试）。

### 已补充落地：SSE/HTTP Transport（2026-08-01）

| 编号 | 改进项 | 变更文件 | 验证 |
|------|--------|---------|------|
| P3-1 | `mcp_server.py` 重构：抽取 `handle_message()`（stdio/HTTP 共用）、`_result_payload`/`_error_payload` 纯构造、`notification_sink` 注入 | `mcp_server.py` | ✅ 63 MCP 测试通过 |
| P3-2 | 新建 `mcp_http.py`：Streamable HTTP 模式（POST /mcp 处理 JSON-RPC；GET /mcp 为 SSE 推送通道；GET /health 健康检查）；Bearer token 鉴权（`AGENT_GO_MCP_HTTP_TOKEN`）；SSE 心跳/EOF 探测/空闲超时；CORS 预检 | `mcp_http.py`（新增） | ✅ 21 测试通过 |
| P3-3 | CLI `agent_go mcp --http --host --port`；`python3 -m agent_go.mcp_server --http` 同样支持 | `cli.py` | ✅ 端到端验证 |

**SSE 关键实现细节**：
- `wait=true` 的 tools/call 在 HTTP 下同步阻塞执行（长请求），progress notification 通过 SSE 连接推送（`notification_sink` 广播到所有 `sse_clients`）
- 客户端断开探测：`select` 探测 socket EOF + 30s 心跳保活 + 900s 空闲超时兜底
- 默认绑定 `127.0.0.1`（仅本地）；设置 `AGENT_GO_MCP_HTTP_TOKEN` 后所有端点（含 SSE）需 Bearer 鉴权

**测试基线**：`pytest tests/` → **1362 passed**（新增 21 个 HTTP transport 测试）。
