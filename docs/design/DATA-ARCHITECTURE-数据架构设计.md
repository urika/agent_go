# agent_go 数据架构设计

> 版本: v2.2  
> 日期: 2026-05-27  
> 关联: [PRD-项目评估体系设计.md](PRD-项目评估体系设计.md) | [REVIEW-数据架构设计.md](REVIEW-数据架构设计.md)

**版本标注说明**:
- ✅ `[v0.4]` — v0.4 已产出
- ✅ `[v0.5]` — Phase1 已实现 (metrics.py + timing/change_stats/merge/verify/retry/token)
- 📋 `[Phase2]` — 关联 PRD-评估体系 Phase2
- 📋 `[缓存]` — 关联 PRD-Plan缓存机制

---

## 第一部分：文档型数据模型

### 1.1 存储范式

agent_go 使用**文档型存储**，以 `meta.json` 为聚合根。每个 Task 的所有信息（定义、结果、元数据）集中在单个 JSON 文件内。

```
~/.agent_go/<task_id>/
├── meta.json           ← 聚合根：Task + Plan 定义 + 执行结果
├── execution.log       ← 结构化事件流 (JSON Lines)
├── PLAN.md             ← 人类可读摘要 (从 subtasks[] 生成)
├── sub-1/
│   ├── work/           ← git worktree (运行时，执行后清理)
│   ├── TASK.md         ← 子任务指令 (运行时产物)
│   └── context.md      ← 上游上下文 (运行时产物，仅下游读取)
├── sub-2/
│   └── ...
```

**为什么是文档型，不是关系型**：
- 所有 CLI 命令按 task_id 单键访问（`show`、`resume`、`clean`）
- 无跨任务 JOIN 需求
- Schema 变更只需追加字段
- 并发写入仅在 subtask 完成后追加一条 result 记录

### 1.2 meta.json — 聚合根结构

```json
{
  "task_id": "task-20260526-112220-871-23b3",
  "task": "...",
  "repo": "/path/to/project",
  "created": "20260526-112220-871",
  "status": "completed",
  "reference_docs": [],
  "issue": "",
  "tool_versions": {"claude": "2.1.150"},
  "skills": [],
  "agent_type": "developer",
  "remote_url": "",

  "subtasks": [
    {
      "id": "sub-1",
      "title": "...",
      "description": "...",
      "files_hint": "*",
      "agent_prompt": "...",
      "verification": "go build ./...",
      "risks": [],
      "depends_on": [],
      "skills": ["python-patterns"],
      "agent_type": "architect"
    }
  ],

  "results": [
    {
      "subtask_id": "sub-1",
      "status": "completed",
      "exit_code": 0,
      "summary": "2 files changed, +45/-2",
      "worktree": "/path/to/.agent_go/.../sub-1/work",
      "sandbox_type": "headless",
      "verify_ok": true,
      "duration_sec": 95.3,
      "agent_type_source": "llm",
      "skills_unresolved": []
    }
  ]
}
```

**关联方式**：`results[i].subtask_id === subtasks[j].id`（字符串匹配，1:1）

### 1.3 数据分类

meta.json 内容按生命周期分为三类：

| 分类 | 存放位置 | 产出时机 | 生命周期 |
|------|---------|---------|---------|
| **Task 元数据** | 顶层字段 | cmd_run() 初始化 | 与 Task 同生命周期 |
| **StepDefinition** | subtasks[] | plan_to_subtasks() | Plan 确认后不变 |
| **StepExecution** | results[] | run_subtask() 返回时追加 | 执行后不变 |

**StepDefinition vs StepExecution**：

| 字段 | StepDefinition (subtasks[]) | StepExecution (results[]) |
|------|---------------------------|--------------------------|
| id / subtask_id | ✅ `id: "sub-1"` | ✅ `subtask_id: "sub-1"` |
| title | ✅ | — |
| description | ✅ | — |
| agent_type | ✅ | — |
| skills | ✅ | — |
| status | — | ✅ |
| exit_code | — | ✅ |
| summary | — | ✅ |
| duration_sec | — | ✅ |
| verify_ok | — | ✅ |
| agent_type_source | — | ✅ [v0.4] |
| skills_unresolved | — | ✅ [v0.4] |
| retry_count | — | ✅ [v0.5] |
| timing{} | — | ✅ [v0.5] |
| change_stats{} | — | ✅ [v0.5] |
| merge_results[] | — | ✅ [v0.5] |
| verification_results[] | — | ✅ [v0.5] |

### 1.4 外部引用（字符串匹配，非 FK）

```
subtasks[].agent_type ──→ ~/.agent_go/agents/<name>.json    (字符串匹配)
subtasks[].skills[]   ──→ ~/.agent_go/skills/<name>/SKILL.md  (字符串匹配)
results[].subtask_id  ──→ subtasks[].id                       (数组内匹配)
```

**关键区别**：AgentRole 和 Skill 不是数据库外键，而是配置文件的字符串引用。引用可以指向不存在的文件（代码会静默降级或告警）。

### 1.5 值对象（Phase1 新增，嵌套子结构）

Phase1 计划在 `results[]` 中新增三个**值对象**（非独立实体）：

**timing** — 各阶段计时：

| 字段 | 类型 | 状态 |
|------|------|------|
| worktree_create_ms | int | ✅ [v0.5] |
| merge_upstream_ms | int | ✅ [v0.5] |
| claude_execute_ms | int | ✅ [v0.5] |
| verification_ms | int | ✅ [v0.5] |
| git_commit_ms | int | ✅ [v0.5] |

**change_stats** — 变更统计：

| 字段 | 类型 | 状态 |
|------|------|------|
| files_changed | int | ✅ [v0.5] |
| insertions | int | ✅ [v0.5] |
| deletions | int | ✅ [v0.5] |
| new_files | int | ✅ [v0.5] |
| modified_files | int | ✅ [v0.5] |
| actual_files | string[] | ✅ [v0.5] |

**merge_results[]** — 产物合并记录：

| 字段 | 类型 | 状态 |
|------|------|------|
| upstream | string | ✅ [v0.5] |
| status | enum | ✅ [v0.5] |
| conflict_files | string[] | ✅ [v0.5] |

**verification_results[]** — 验证执行记录：

| 字段 | 类型 | 状态 |
|------|------|------|
| command | string | ✅ [v0.5] |
| exit_code | int | ✅ [v0.5] |
| duration_ms | int | ✅ [v0.5] |
| attempt | int | ✅ [v0.5] |

---

## 第二部分：事件日志模型

### 2.1 execution.log 结构

实际格式: `时间戳 | 级别 | logger名 | JSON事件`（pipe分隔）。JSON 事件通过文件路径隐式关联到 Task（无需内部记录 task_id）。

```jsonl
{"event":"plan_generate","iteration":1,"has_supplement":false,"has_docs":false,"has_skills":true}
{"event":"api_call","provider":"anthropic","latency_ms":3150,"response_len":4520}
{"event":"plan_complete","iteration":1,"step_count":4}
{"event":"plan_auto_confirmed","iteration":1}
{"event":"plan_decomposed","count":4}
{"event":"subtasks_auto_confirmed","count":4}
{"event":"subtask_start","id":"sub-1","title":"...","depends_on":[],"headless":true}
{"event":"subtask_headless_start","id":"sub-1"}
{"event":"subtask_headless_retry","id":"sub-1","attempt":2}
{"event":"subtask_headless_complete","id":"sub-1","exit_code":0,"interaction_detected":false,"attempts":1,"output_lines":156}
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless","clone_sec":0.3,"claude_sec":95.3,"summary":"2 files changed, +45/-2","verify_ok":true}
```

### 2.2 事件定义

| 事件 | 时机 | 字段 (✅已实现) | 字段 (📋计划) |
|------|------|---------------|-------------|
| `plan_generate` | Plan API 调用前 | ✅ iteration, has_supplement, has_docs, has_skills | — |
| `api_call` | API 响应后 | ✅ provider, latency_ms, response_len | ✅ [v0.5] model, prompt_tokens, completion_tokens |
| `api_error` | API 异常时 | — | ✅ [v0.5] provider, status_code, error_message |
| `plan_complete` | Plan JSON 解析后 | ✅ iteration, step_count | ✅ [v0.5] plan_duration_ms, 📋 [缓存] cache_hit |
| `plan_auto_confirmed` | 自动确认 Plan | ✅ iteration | — |
| `plan_decomposed` | Plan→Subtask 拆解后 | ✅ count | — |
| `subtasks_auto_confirmed` | 自动确认子任务 | ✅ count | — |
| `subtask_start` | Subtask 开始 | ✅ id, title, depends_on, headless | — |
| `subtask_headless_start` | Claude -p 启动 | ✅ id | — |
| `subtask_headless_retry` | Claude -p 因交互退出重试 | ✅ id, attempt | — |
| `subtask_headless_complete` | Claude -p 退出 | ✅ id, exit_code, interaction_detected, attempts, output_lines | — |
| `subtask_complete` | Subtask 完成 | ✅ id, status, sandbox_type, clone_sec, claude_sec, summary, verify_ok | — |

注: `subtask_complete` status 枚举: `completed` / `no_changes` / `failed`

### 2.3 当前可用查询

注：execution.log 格式为 `时间戳 | 级别 | logger | JSON`，JSON 部分在最后一个 `|` 之后。

```bash
# 提取 JSON 的 helper
alias ejq='sed "s/.* | //" | jq'

# ✅ 所有失败 subtask
grep '"event":"subtask_complete"' ~/.agent_go/task-*/execution.log | ejq 'select(.status=="failed")'

# ✅ 所有 subtask Claude 耗时分布
grep '"event":"subtask_complete"' ~/.agent_go/task-*/execution.log \
  | ejq -s 'map({id: .id, claude_sec}) | sort_by(.claude_sec)'

# ✅ 发生过重试的 subtask
grep '"event":"subtask_headless_retry"' ~/.agent_go/task-*/execution.log | ejq '.id, .attempt'

# ✅ 某任务所有 API 调用
grep '"event":"api_call"' ~/.agent_go/task-xxx/execution.log

# 📋 需 Phase1（prompt_tokens 字段到位后生效）
grep '"event":"api_call"' ~/.agent_go/task-*/execution.log \
  | ejq -s 'map(.prompt_tokens+.completion_tokens)|add'
```

---

## 第三部分：维度分析模型

### 3.1 说明

维度模型是**分析层的抽象**，不改变存储层。数据从 meta.json（聚合根）和 execution.log（事件流）中提取，按维度聚合计算。

### 3.2 事实表

#### FACT-1: subtask_execution

数据源: meta.json.results[] + meta.json.subtasks[]

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| task_id | ✅ | duration_sec | ✅ |
| agent_type (from subtasks[]) | ✅ | verify_ok | ✅ |
| agent_type_source | ✅ [v0.4] | retry_count | ✅ [v0.5] |
| sandbox_type | ✅ | timing.* | ✅ [v0.5] |
| skills[] (from subtasks[]) | ✅ | change_stats.* | ✅ [v0.5] |
| status | ✅ | merge_results | ✅ [v0.5] |

#### FACT-2: plan_generation

数据源: execution.log plan_generate + plan_complete events

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| iteration | ✅ | step_count | ✅ |
| provider | ✅ [v0.5] | plan_duration_ms | ✅ [v0.5] |
| model | ✅ [v0.5] | prompt_tokens | ✅ [v0.5] |
| cache_hit | 📋 [缓存] | completion_tokens | ✅ [v0.5] |

#### FACT-3: api_call

数据源: execution.log api_call events

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| provider | ✅ | latency_ms | ✅ |
| status_code | ✅ [v0.5] | prompt_tokens | ✅ [v0.5] |
| | | completion_tokens | ✅ [v0.5] |

### 3.3 维度表

全部可用（数据源为配置文件和 meta.json）：

| 维度 | 来源 |
|------|------|
| DIM_Task | meta.json (task_id, repo, task...) |
| DIM_Agent | 内置 + `~/.agent_go/agents/*.json` |
| DIM_Skill | `~/.agent_go/skills/*/SKILL.md` |
| DIM_Provider | API 配置 |
| DIM_Model | API 配置 |
| DIM_Time | task.created 派生 |
| DIM_Sandbox | results[].sandbox_type |

### 3.4 典型查询

| 问题 | 当前可行 | 需要数据 |
|------|---------|---------|
| 哪种 Agent 成功率最高？ | ✅ | agent_type + verify_ok |
| tester 的 verify 耗时占比？ | ✅ v0.5 | timing.verification_ms |
| Skill 对重试率影响？ | ✅ v0.5 | retry_count |
| 本周 API 费用趋势？ | ✅ v0.5 | tokens + model price |
| 哪个 provider 错误率最低？ | ✅ v0.5 | status_code |
| v0.3 vs v0.4 首次通过率？ | ✅ v0.5 | retry_count |

---

## 附录A：实施状态总览

| 层级 | 总数 | ✅ v0.4 | ✅ v0.5 | 📋 Phase2+ |
|------|------|--------|--------|-----------|
| meta.json 顶层 | 13 | 13 | — | — |
| subtasks[] (StepDefinition) | 10 | 10 | — | — |
| results[] (StepExecution) | 10 | 10 | — | — |
| results[].timing (value obj) | 5 | — | 5 | — |
| results[].change_stats (value obj) | 6 | — | 6 | — |
| results[].merge_results (value obj) | 3 | — | 3 | — |
| results[].verification_results | 4 | — | 4 | — |
| execution.log events | 12 | 12 | — | — |
| api_call event fields (扩展) | 4 | — | 4 | — |
| plan_complete event fields (扩展) | 2 | — | 2 | — |
| **合计** | **69** | **45 (65%)** | **24 (35%)** | **0 (0%)** |

**指标可计算率**: 当前 23/44 (52%, eval 命令已可查询) → Phase3 后 35/44 (80%)

**指标可计算率**: 当前 11/44 (25%) → Phase1 后 23/44 (52%) → Phase2 后 35/44 (80%)

---

*文档结束*
