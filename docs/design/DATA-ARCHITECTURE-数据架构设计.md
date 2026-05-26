# agent_go 数据架构设计

> 版本: v1.0
> 日期: 2026-05-26
> 关联: [PRD-项目评估体系设计.md](PRD-项目评估体系设计.md)

---

## 第一部分：概念数据模型（ER）

### 1.1 核心实体

从执行过程识别 6 个核心实体 + 3 个事件实体：

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│   Task   │────→│   Plan   │────→│ Subtask  │
│ (任务)   │ 1:N │ (方案)   │ 1:N │ (子任务) │
└────┬─────┘     └──────────┘     └────┬─────┘
     │                                │
     │                          ├────→ AgentRole (1:1)
     │                          ├────→ Skill (N:M)
     │                          ├────→ MergeOp (1:N)
     │                          └────→ VerifyRun (1:N)
     │
     └────→ APICall (1:N)
```

### 1.2 实体定义

#### E1: Task — 任务

一次 `agent_go run` 的执行实例。

| 属性 | 类型 | 说明 |
|------|------|------|
| task_id | string PK | `task-YYYYMMDD-HHMMSS-mmm-xxxx` |
| task | string | 用户原始任务描述 |
| repo | string | 项目根路径 |
| status | enum | running / paused / completed / failed |
| created | string | `YYYYMMDD-HHMMSS` |
| parallel | int | --parallel N |
| headless | bool | --yes / --headless |
| remote_url | string | --remote URL |
| issue | string | GitHub Issue # |
| reference_docs | string[] | --docs 路径 |
| interrupted | bool | Ctrl+C 中断 |
| resumed | bool | resume 恢复 |
| total_duration_sec | float | 端到端耗时 |

**存储**: `~/.agent_go/<task_id>/meta.json`

#### E2: Plan — 方案

LLM 生成的结构化执行方案。一个 Task 可能多次迭代。

| 属性 | 类型 | 说明 |
|------|------|------|
| task_id | string FK | 关联任务 |
| iteration | int | 第几次生成 |
| step_count | int | 步骤数 |
| provider | string | API provider |
| model | string | 模型名 |
| plan_duration_ms | int | 生成耗时 |
| prompt_tokens | int | 输入 token |
| completion_tokens | int | 输出 token |
| cache_hit | bool | 是否缓存命中 |

**存储**: `execution.log` plan_complete event + `PLAN.md`

#### E3: Subtask — 子任务

| 属性 | 类型 | 说明 |
|------|------|------|
| subtask_id | string PK | sub-1, sub-2... |
| task_id | string FK | 关联任务 |
| title | string | 步骤标题 |
| agent_type | string | developer / architect / reviewer / tester |
| agent_type_source | enum | llm / rule / default |
| skills | string[] | 已加载 Skill |
| skills_unresolved | string[] | 未找到的 Skill |
| status | enum | completed / no_changes / failed |
| duration_sec | float | 总耗时 |
| retry_count | int | 重试次数 |
| verify_ok | bool | 验证通过 |
| sandbox_type | enum | headless / greywall / native |

**timing** (度量组):

| 属性 | 类型 |
|------|------|
| worktree_create_ms | int |
| merge_upstream_ms | int |
| claude_execute_ms | int |
| verification_ms | int |
| git_commit_ms | int |

**change_stats** (度量组):

| 属性 | 类型 |
|------|------|
| files_changed | int |
| insertions | int |
| deletions | int |
| new_files | int |
| modified_files | int |
| actual_files | string[] |

**merge_results[]**:

| 属性 | 类型 |
|------|------|
| upstream | string |
| status | success / conflict |
| conflict_files | string[] |

**verification_results[]**:

| 属性 | 类型 |
|------|------|
| command | string |
| exit_code | int |
| duration_ms | int |
| attempt | int |

**存储**: `<task_dir>/<sub_id>/result.json`

#### E4: AgentRole（配置实体）

| 属性 | 说明 |
|------|------|
| type_name | developer / architect / reviewer / tester |
| permission_mode | default / bypassPermissions / acceptEdits |
| allowed_tools | string[] |

**存储**: `~/.agent_go/agents/<name>.json`（定义）+ result.json agent_type（执行记录）

#### E5: Skill（配置实体）

| 属性 | 说明 |
|------|------|
| name | 唯一标识 |
| description | 一句话描述 |
| body | 知识正文 |
| allowed_tools | string[] |

**存储**: `~/.agent_go/skills/<name>/SKILL.md`（定义）+ result.json skills（使用记录）

#### E6: Worktree（运行时实体，不持久化）

| 属性 | 说明 |
|------|------|
| path | 工作区路径 |
| branch | agent_go/{task_id}/{sub_id} |

---

### 1.3 事件实体

#### EV1: APICall

| 属性 | 类型 | 说明 |
|------|------|------|
| task_id | string FK | 关联任务 |
| call_type | enum | plan_generate / decompose_fallback |
| provider | string | API 供应商 |
| model | string | 模型名 |
| status_code | int | HTTP 状态码 |
| latency_ms | int | 耗时 |
| prompt_tokens | int | 输入 token |
| completion_tokens | int | 输出 token |

**存储**: `execution.log` api_call / api_error event

#### EV2: MergeOp

| 属性 | 说明 |
|------|------|
| upstream_id | 来源 subtask |
| downstream_id | 目标 subtask |
| status | success / conflict |
| conflict_files | string[] |

**存储**: result.json merge_results

#### EV3: VerifyRun

| 属性 | 说明 |
|------|------|
| subtask_id | 关联 subtask |
| command | 验证命令 |
| exit_code | 0=通过 |
| duration_ms | 耗时 |
| attempt | 1=首次, >1=重试 |

**存储**: result.json verification_results

---

### 1.4 ER 图

```
                    ┌──────────┐         ┌──────────┐
                    │AgentRole │         │  Skill   │
                    │type_name │         │  name    │
                    └────┬─────┘         └────┬─────┘
                         │ 1                 │ N:M
                         │                   │
    ┌──────────┐    ┌────┴──────────┐        │
    │   Plan   │←───│   Subtask     │────────┘
    │iteration │ N:1│ subtask_id PK │
    │tokens    │    │ agent_type    │──→ MergeOp (1:N)
    └────┬─────┘    │ timing{}      │──→ VerifyRun (1:N)
         │ N        │ change_stats{}│
    ┌────┴─────┐    └──────┬────────┘
    │   Task   │           │ 1
    │ task_id  │←──────────┘
    │ status   │
    │ repo     │──→ APICall (1:N)
    └──────────┘
```

---

## 第二部分：维度分析模型

### 2.1 事实表

#### FACT-1: subtask_execution

| 维度键 | 度量 |
|--------|------|
| task_id → DIM_Task | duration_sec (SUM/AVG/P50/P95/P99) |
| agent_type → DIM_Agent | retry_count (SUM/AVG) |
| agent_type_source | verify_ok (COUNT TRUE) |
| sandbox_type | timing.* (SUM/AVG) |
| skills[] → DIM_Skill | change_stats.* (SUM/AVG) |
| status | |

#### FACT-2: plan_generation

| 维度键 | 度量 |
|--------|------|
| task_id | plan_duration_ms |
| provider → DIM_Provider | prompt_tokens |
| model → DIM_Model | completion_tokens |
| iteration | step_count |
| cache_hit | |

#### FACT-3: api_call

| 维度键 | 度量 |
|--------|------|
| task_id | latency_ms |
| provider | prompt_tokens |
| status_code | completion_tokens |

### 2.2 维度表

| 维度 | 来源 | 值域大小 |
|------|------|---------|
| DIM_Task | meta.json | ~N tasks |
| DIM_Agent | Agent 定义 | 4-6 |
| DIM_Skill | Skill 目录 | ~N installed |
| DIM_Provider | API 配置 | 3-4 |
| DIM_Model | API 配置 | 3-5 |
| DIM_Time | task.created 派生 | 无限 |
| DIM_Sandbox | result | 3 (headless/greywall/native) |

### 2.3 典型查询

| 问题 | 事实表 | 维度 | 度量 |
|------|--------|------|------|
| 哪种 Agent 成功率最高？ | FACT-1 | DIM_Agent | verify_ok |
| tester 的 verify 耗时占比？ | FACT-1 | DIM_Agent | timing.verification_ms/duration_sec |
| Skill 对重试率影响？ | FACT-1 | DIM_Skill | retry_count |
| 本周 API 费用趋势？ | FACT-2 | DIM_Time × DIM_Provider | tokens × price |
| 哪个 provider 错误率最低？ | FACT-3 | DIM_Provider | status_code≠200 |
| v0.3 vs v0.4 首次通过率？ | FACT-1 | task.created(分段) | retry_count=0 |

---

## 第三部分：日志数据规范

### 3.1 结构化 Event 定义

#### plan_generate
```json
{"event":"plan_generate","iteration":1,"has_supplement":false,"has_docs":false,"has_skills":true}
```

#### api_call
```json
{"event":"api_call","provider":"anthropic","model":"claude-sonnet-4","latency_ms":3150,"prompt_tokens":12000,"completion_tokens":2400}
```

#### api_error
```json
{"event":"api_error","provider":"anthropic","status_code":429,"error_message":"rate_limit_exceeded"}
```

#### plan_complete
```json
{"event":"plan_complete","iteration":1,"step_count":3,"plan_duration_ms":3200,"cache_hit":false}
```

#### subtask_start
```json
{"event":"subtask_start","id":"sub-1","title":"JWT中间件","depends_on":[],"headless":true}
```

#### subtask_complete
```json
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless","duration_sec":95,"verify_ok":true}
```

### 3.2 日志查询

```bash
# 某任务所有 API 调用
grep '"event":"api_call"' ~/.agent_go/task-xxx/execution.log

# 本月 token 消耗
grep '"event":"api_call"' ~/.agent_go/task-*/execution.log \
  | jq -s 'map(.prompt_tokens+.completion_tokens)|add'

# 所有失败 subtask
grep '"event":"subtask_complete"' ~/.agent_go/task-*/execution.log \
  | jq 'select(.status=="failed")'

# 按 agent_type 统计
grep '"event":"subtask_complete"' ~/.agent_go/task-*/execution.log \
  | jq -s 'group_by(.agent_type)|map({type:.[0].agent_type,count:length})'
```

### 3.3 数据保留

| 数据 | 保留 | 清理方式 |
|------|------|---------|
| meta.json / result.json | 永久 | `agent_go clean` |
| execution.log | 永久 | `agent_go clean` |
| PLAN.md / TASK.md / context.md | 永久 | `agent_go clean` |
| session_stats.json | 30 天滚动 | 自动 |

---

*文档结束*
