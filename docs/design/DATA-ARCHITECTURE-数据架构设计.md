# agent_go 数据架构设计

> 版本: v1.1
> 日期: 2026-05-26
> 关联: [PRD-项目评估体系设计.md](PRD-项目评估体系设计.md)

**版本标注说明**:
- ✅ `[已实现 v0.4]` — 当前代码已产出
- 📋 `[计划 Phase1]` — 关联 PRD-评估体系 Phase1，近期实施
- 📋 `[计划 Phase2]` — 关联 PRD-评估体系 Phase2，择机实施
- 📋 `[计划 - Plan缓存]` — 关联 PRD-Plan缓存机制
- 📋 `[计划 - 可靠性]` — 关联 PRD-评估体系 可靠性维度

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
     │                          ├────→ AgentRole (1:1) ✅
     │                          ├────→ Skill (N:M) ✅
     │                          ├────→ MergeOp (1:N) 📋
     │                          └────→ VerifyRun (1:N) 📋
     │
     └────→ APICall (1:N) ⚠️ 部分字段
```

### 1.2 实体定义

#### E1: Task — 任务

一次 `agent_go run` 的执行实例。存储: `~/.agent_go/<task_id>/meta.json`

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| task_id | string PK | ✅ | `task-YYYYMMDD-HHMMSS-mmm-xxxx` |
| task | string | ✅ | 用户原始任务描述 |
| repo | string | ✅ | 项目根路径（绝对路径） |
| status | enum | ✅ | running / paused / completed / failed |
| created | string | ✅ | `YYYYMMDD-HHMMSS` |
| reference_docs | string[] | ✅ | --docs 挂载的文档路径 |
| issue | string | ✅ | GitHub Issue 编号 |
| remote_url | string | ✅ | --remote URL |
| subtasks | object[] | ✅ | Plan 步骤列表（子任务定义） |
| results | object[] | ✅ | Subtask 执行结果数组（引用 result.json） |
| tool_versions | object | ✅ | claude/greywall 版本信息 |
| skills | string[] | ✅ | 全局 --skill 指定的 Skill 名称 |
| agent_type | string | ✅ | 全局 --agent-type 指定的默认类型 |
| parallel | int | 📋 [Phase2] | --parallel N |
| headless | bool | 📋 [Phase2] | --yes / --headless |
| interrupted | bool | 📋 [Phase2] | Ctrl+C 中断标记 |
| resumed | bool | 📋 [Phase2] | resume 恢复标记 |
| total_duration_sec | float | 📋 [Phase2] | 端到端耗时（从日志计算） |

---

#### E2: Plan — 方案

LLM 生成的结构化执行方案。一个 Task 可能多次迭代。

存储: `execution.log` plan_complete event + `task_dir/PLAN.md`

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| task_id | string FK | ✅ | 关联任务 |
| iteration | int | ✅ | 第几次生成（1=首次, >1=迭代） |
| step_count | int | ✅ | 步骤数（2-5） |
| has_supplement | bool | ✅ | 是否有用户补充（plan_generate event） |
| has_docs | bool | ✅ | 是否有参考文档 |
| has_skills | bool | ✅ | 是否有 Skill 上下文 |
| provider | string | 📋 [Phase1] | API provider |
| model | string | 📋 [Phase1] | 模型名 |
| plan_duration_ms | int | 📋 [Phase1] | 生成耗时 |
| prompt_tokens | int | 📋 [Phase1] | 输入 token |
| completion_tokens | int | 📋 [Phase1] | 输出 token |
| cache_hit | bool | 📋 [计划 - Plan缓存] | 是否命中缓存 |

**当前 plan_complete event（v0.4）**:
```json
{"event":"plan_complete","iteration":1,"step_count":3}
```
**计划扩展后（Phase1）**:
```json
{"event":"plan_complete","iteration":1,"step_count":3,"plan_duration_ms":3200,"cache_hit":false}
```

---

#### E3: Subtask — 子任务

Plan 中一个步骤的执行实例。存储: `<task_dir>/<sub_id>/result.json`

**主属性**:

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| subtask_id | string PK | ✅ | sub-1, sub-2... |
| task_id | string FK | ✅ | 隐式（从 task_dir 路径推断） |
| title | string | ✅ | 步骤标题（在 subtask 定义中） |
| description | string | ✅ | 完整描述含 Agent Prompt |
| files_hint | string | ✅ | Plan 预测的涉及文件 |
| agent_type | string | ✅ | developer / architect / reviewer / tester |
| agent_type_source | enum | ✅ | llm / rule / default |
| skills | string[] | ✅ | 已加载 Skill 名称 |
| skills_unresolved | string[] | ✅ | 引用但未安装的 Skill |
| depends_on | string[] | ✅ | 上游依赖 subtask_id |
| verification | string\|string[] | ✅ | 验证命令（v0.4 支持数组） |
| status | enum | ✅ | completed / no_changes / failed / degraded |
| duration_sec | float | ✅ | 总耗时 |
| verify_ok | bool | ✅ | 验证是否通过 |
| sandbox_type | enum | ✅ | headless / greywall / native |
| exit_code | int | ✅ | Claude 进程退出码 |
| summary | string | ✅ | 变更摘要文本 |
| worktree | string | ✅ | 工作区路径 |
| retry_count | int | 📋 [Phase1] | 验证失败重试次数 |

**timing**（度量组）:

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| worktree_create_ms | int | 📋 [Phase1] | git worktree add 耗时 |
| merge_upstream_ms | int | 📋 [Phase1] | 所有上游 merge 总耗时 |
| claude_execute_ms | int | 📋 [Phase1] | Claude 进程存活时间 |
| verification_ms | int | 📋 [Phase1] | 所有验证命令总耗时 |
| git_commit_ms | int | 📋 [Phase1] | git add+commit+tag 耗时 |

**change_stats**（度量组）:

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| files_changed | int | 📋 [Phase1] | 变更文件总数 |
| insertions | int | 📋 [Phase1] | 新增行数 |
| deletions | int | 📋 [Phase1] | 删除行数 |
| new_files | int | 📋 [Phase1] | 新建文件数 |
| modified_files | int | 📋 [Phase1] | 修改文件数 |
| actual_files | string[] | 📋 [Phase1] | 实际被修改文件路径列表 |

**merge_results[]**:

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| upstream | string | 📋 [Phase1] | 来源 subtask_id |
| status | enum | 📋 [Phase1] | success / conflict |
| conflict_files | string[] | 📋 [Phase1] | 冲突文件（仅 conflict） |

**verification_results[]**:

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| command | string | 📋 [Phase1] | 完整命令文本 |
| exit_code | int | 📋 [Phase1] | 0=成功 |
| duration_ms | int | 📋 [Phase1] | 命令耗时 |
| attempt | int | 📋 [Phase1] | 1=首次, 2=重试 |

**当前 result.json（v0.4）**:
```json
{
  "subtask_id": "sub-1", "status": "completed",
  "exit_code": 0, "summary": "2 files changed",
  "worktree": "/path/to/worktree",
  "sandbox_type": "headless", "verify_ok": true,
  "duration_sec": 95.3,
  "agent_type_source": "llm",
  "skills_unresolved": []
}
```
**计划扩展后（Phase1）**:
```json
{
  "subtask_id": "sub-1", "status": "completed", "retry_count": 0,
  "...": "... (以上字段全部保留)",
  "timing": {"worktree_create_ms": 320, "merge_upstream_ms": 150, "claude_execute_ms": 93000, "verification_ms": 1500, "git_commit_ms": 200},
  "change_stats": {"files_changed": 3, "insertions": 45, "deletions": 2, "new_files": 1, "modified_files": 2, "actual_files": ["cli.py", "version.py"]},
  "merge_results": [{"upstream": "sub-0", "status": "success"}],
  "verification_results": [{"command": "pytest tests/", "exit_code": 0, "duration_ms": 1500, "attempt": 1}]
}
```

---

#### E4: AgentRole（配置实体）

| 属性 | 状态 | 说明 |
|------|------|------|
| type_name | ✅ | developer / architect / reviewer / tester |
| description | ✅ | 角色说明 |
| permission_mode | ✅ | default / bypassPermissions / acceptEdits |
| allowed_tools | ✅ v0.4 | `--allowedTools` CLI 传递 |
| preload_skills | ⚠️ 定义存在但未消费 | |

存储: `~/.agent_go/agents/<name>.json`（定义）+ result.json agent_type/agent_type_source（执行记录）

---

#### E5: Skill（配置实体）

| 属性 | 状态 | 说明 |
|------|------|------|
| name | ✅ | 唯一标识 |
| description | ✅ | YAML frontmatter |
| body | ✅ | Markdown 正文 |
| allowed_tools | ✅ | YAML frontmatter |
| version | ❌ 无 | mtime-based 版本追踪（未实现） |

存储: `~/.agent_go/skills/<name>/SKILL.md`（定义）+ result.json skills/skills_unresolved（使用记录）

---

#### E6: Worktree（运行时实体，不持久化）

| 属性 | 状态 | 说明 |
|------|------|------|
| path | ✅ | 工作区路径 |
| branch | ✅ | agent_go/{task_id}/{sub_id} |
| status | ❌ | active / removed / leaked（仅运行时查询 `git worktree list`） |

---

### 1.3 事件实体

#### EV1: APICall

存储: `execution.log` api_call / api_error event

| 属性 | 类型 | 状态 | 说明 |
|------|------|------|------|
| task_id | string FK | ✅ | 关联任务（从 logger name 推断） |
| provider | string | ✅ | API 供应商 |
| latency_ms | int | ✅ | 响应耗时 |
| response_len | int | ✅ | 响应体长度（bytes） |
| call_type | enum | 📋 [Phase2] | plan_generate / decompose_fallback |
| model | string | 📋 [Phase1] | 模型名 |
| status_code | int | 📋 [Phase1] | HTTP 状态码 |
| prompt_tokens | int | 📋 [Phase1] | 输入 token |
| completion_tokens | int | 📋 [Phase1] | 输出 token |
| error_message | string | 📋 [Phase1] | 仅 status_code != 200 |

**当前 api_call event（v0.4）**:
```json
{"event":"api_call","provider":"anthropic","latency_ms":3150,"response_len":4520}
```
**计划扩展后（Phase1）**:
```json
{"event":"api_call","provider":"anthropic","model":"claude-sonnet-4","latency_ms":3150,"prompt_tokens":12000,"completion_tokens":2400,"response_len":4520}
```
**api_error event（Phase1 新增）**:
```json
{"event":"api_error","provider":"anthropic","status_code":429,"error_message":"rate_limit_exceeded"}
```

#### EV2: MergeOp

| 属性 | 状态 | 说明 |
|------|------|------|
| upstream_id | 📋 [Phase1] | 来源 subtask |
| downstream_id | 📋 [Phase1] | 目标 subtask（可从 subtask_id 推断） |
| status | 📋 [Phase1] | success / conflict |
| conflict_files | 📋 [Phase1] | 冲突文件列表 |

存储: result.json merge_results（Phase1 新增）

#### EV3: VerifyRun

| 属性 | 状态 | 说明 |
|------|------|------|
| subtask_id | 📋 [Phase1] | 关联 subtask |
| command | 📋 [Phase1] | 验证命令 |
| exit_code | 📋 [Phase1] | 0=通过 |
| duration_ms | 📋 [Phase1] | 耗时 |
| attempt | 📋 [Phase1] | 1=首次, >1=重试 |

存储: result.json verification_results（Phase1 新增）

---

### 1.4 ER 图（标注实现状态）

```
                    ┌──────────┐         ┌──────────┐
                    │AgentRole │         │  Skill   │
                    │ ✅ 全部   │         │ ✅ 全部   │
                    └────┬─────┘         └────┬─────┘
                         │ 1 ✅             │ N:M ✅
                         │                   │
    ┌──────────┐    ┌────┴──────────┐        │
    │   Plan   │←───│   Subtask     │────────┘
    │ ⚠️ 部分   │ N:1│ ✅ 主属性     │
    │ 📋 tokens│    │ 📋 timing{}  │──→ MergeOp 📋 (1:N)
    │ 📋 ms    │    │ 📋 change{}  │──→ VerifyRun 📋 (1:N)
    └────┬─────┘    └──────┬────────┘
         │ N ✅           │ 1 ✅
    ┌────┴─────┐          │
    │   Task   │←─────────┘
    │ ✅ 主属性 │
    │ 📋 stats │──→ APICall ⚠️ 部分 (1:N)
    └──────────┘
```

---

## 第二部分：维度分析模型

### 2.1 事实表（标注实现状态）

#### FACT-1: subtask_execution

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| task_id → DIM_Task | ✅ | duration_sec | ✅ |
| agent_type → DIM_Agent | ✅ | retry_count | 📋 [Phase1] |
| agent_type_source | ✅ | verify_ok | ✅ |
| sandbox_type | ✅ | timing.* | 📋 [Phase1] |
| skills[] → DIM_Skill | ✅ | change_stats.* | 📋 [Phase1] |
| status | ✅ | merge_results | 📋 [Phase1] |

**当前可计算**: Q1, Q2, Q4, P3, P4, P6, U5, U6（8 项指标）
**Phase1 后可计算**: +Q3, Q5, Q6, Q7, Q8, P5, R8（+7 项）

#### FACT-2: plan_generation

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| task_id | ✅ | step_count | ✅ |
| provider → DIM_Provider | 📋 [Phase1] | plan_duration_ms | 📋 [Phase1] |
| model → DIM_Model | 📋 [Phase1] | prompt_tokens | 📋 [Phase1] |
| iteration | ✅ | completion_tokens | 📋 [Phase1] |
| cache_hit | 📋 [Plan缓存] | | |

**当前可计算**: U1（1 项）
**Phase1 后可计算**: +P2, C2, C3（+3 项）

#### FACT-3: api_call

| 维度键 | 状态 | 度量 | 状态 |
|--------|------|------|------|
| task_id | ✅ | latency_ms | ✅ |
| provider | ✅ | prompt_tokens | 📋 [Phase1] |
| status_code | 📋 [Phase1] | completion_tokens | 📋 [Phase1] |

**当前可计算**: 无（缺 tokens 无法算费用）
**Phase1 后可计算**: +C1, C3, R3（+3 项）

### 2.2 维度表

| 维度 | 状态 | 来源 |
|------|------|------|
| DIM_Task | ✅ | meta.json |
| DIM_Agent | ✅ | Agent 定义（内置+用户） |
| DIM_Skill | ✅ | Skill 目录扫描 |
| DIM_Provider | ✅ | API 配置 |
| DIM_Model | ✅ | API 配置 |
| DIM_Time | ✅ | task.created 派生 |
| DIM_Sandbox | ✅ | result.sandbox_type |

所有维度表均已可用（数据源为配置文件和 meta.json）。

### 2.3 典型查询（标注当前可行性）

| 问题 | 当前 | 说明 |
|------|------|------|
| 哪种 Agent 成功率最高？ | ✅ | agent_type + verify_ok 已有 |
| tester 的 verify 耗时占比？ | ❌ 📋 | 需 timing.verification_ms |
| Skill 对重试率影响？ | ❌ 📋 | 需 retry_count |
| 本周 API 费用趋势？ | ❌ 📋 | 需 tokens |
| 哪个 provider 错误率最低？ | ❌ 📋 | 需 status_code |
| v0.3 vs v0.4 首次通过率？ | ❌ 📋 | 需 retry_count |

**当前 6 条典型查询中仅 1 条可执行。Phase1 完成后全部可执行。**

---

## 第三部分：日志数据规范

### 3.1 结构化 Event 定义

#### plan_generate ✅（已实现）
```json
{"event":"plan_generate","iteration":1,"has_supplement":false,"has_docs":false,"has_skills":true}
```

#### api_call ⚠️（当前）→ 📋（Phase1 扩展）
```json
// v0.4 当前
{"event":"api_call","provider":"anthropic","latency_ms":3150,"response_len":4520}
// Phase1 扩展后
{"event":"api_call","provider":"anthropic","model":"claude-sonnet-4","latency_ms":3150,"prompt_tokens":12000,"completion_tokens":2400,"response_len":4520}
```

#### api_error 📋 [Phase1]（新增）
```json
{"event":"api_error","provider":"anthropic","status_code":429,"error_message":"rate_limit_exceeded"}
```

#### plan_complete ⚠️（当前）→ 📋（Phase1 扩展）
```json
// v0.4 当前
{"event":"plan_complete","iteration":1,"step_count":3}
// Phase1 扩展后
{"event":"plan_complete","iteration":1,"step_count":3,"plan_duration_ms":3200,"cache_hit":false}
```

#### subtask_start ✅（已实现）
```json
{"event":"subtask_start","id":"sub-1","title":"JWT中间件","depends_on":[],"headless":true}
```

#### subtask_complete ✅（已实现）
```json
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless","duration_sec":95,"verify_ok":true}
```

### 3.2 日志查询

```bash
# 某任务所有 API 调用 ✅ 当前可用
grep '"event":"api_call"' ~/.agent_go/task-xxx/execution.log

# 本月 token 消耗 📋 需 Phase1 数据
grep '"event":"api_call"' ~/.agent_go/task-*/execution.log \
  | jq -s 'map(.prompt_tokens+.completion_tokens)|add'

# 所有失败 subtask ✅ 当前可用
grep '"event":"subtask_complete"' ~/.agent_go/task-*/execution.log \
  | jq 'select(.status=="failed")'
```

### 3.3 数据保留

| 数据 | 保留 | 清理方式 | 状态 |
|------|------|---------|------|
| meta.json / result.json | 永久 | `agent_go clean` | ✅ |
| execution.log | 永久 | `agent_go clean` | ✅ |
| PLAN.md / TASK.md / context.md | 永久 | `agent_go clean` | ✅ |
| session_stats.json | 30 天滚动 | 自动 | 📋 [Phase2] |

---

## 附录A：实施状态总览

| 层级 | 总数 | ✅ 已实现 | 📋 Phase1 | 📋 Phase2+ |
|------|------|----------|----------|-----------|
| E1 Task 属性 | 19 | 13 | 0 | 6 |
| E2 Plan 属性 | 12 | 5 | 6 | 1 |
| E3 Subtask 主属性 | 16 | 15 | 1 | 0 |
| E3 timing 子对象 | 5 | 0 | 5 | 0 |
| E3 change_stats | 6 | 0 | 6 | 0 |
| E3 merge_results | 3 | 0 | 3 | 0 |
| E3 verification_results | 4 | 0 | 4 | 0 |
| EV1 APICall | 10 | 4 | 5 | 1 |
| EV2 MergeOp | 4 | 0 | 4 | 0 |
| EV3 VerifyRun | 5 | 0 | 5 | 0 |
| **合计** | **84** | **37 (44%)** | **39 (46%)** | **8 (10%)** |

**当前可计算指标**: Q1, Q2, Q4, P3, P4, P6, U1, U5, U6, R2, R7 = **11/44 项 (25%)**
**Phase1 后可计算**: +Q3, Q5, Q6, Q7, Q8, P2, P5, C1, C2, C3, R3, R8 = **+12 项 (累计 23/44, 52%)**
**Phase2 后可计算**: +P1, P8, C4, C5, C6, C7, R1, R5, R6, U2, U3, U7 = **+12 项 (累计 35/44, 80%)**

---

*文档结束*
