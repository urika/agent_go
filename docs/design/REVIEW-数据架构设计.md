# 数据架构审查报告

> 版本: v3.0  
> 日期: 2026-05-27  
> 审查对象: [DATA-ARCHITECTURE-数据架构设计.md](DATA-ARCHITECTURE-数据架构设计.md) v2.2  
> 关联: [DESIGN-REVIEW-概念与机制问题.md](DESIGN-REVIEW-概念与机制问题.md) | [PRD-智能Agent角色与Skill分配.md](PRD-智能Agent角色与Skill分配.md)

---

## 一、审查概述

对 `DATA-ARCHITECTURE-数据架构设计.md`（下称"架构文档"）进行了三层审查：

1. **数据结构 vs 代码实际**：文档描述的字段/存储路径是否与代码产出一致
2. **ER 关系 vs 实际关联**：实体间关系是否反映真实的存储和引用机制
3. **建模范式适配**：关系型 ER 模型是否适合 agent_go 的实际存储模式

### 审查方法

- 读取全部 13 个源码文件
- 扫描 `~/.agent_go/task-*` 实际数据目录
- 解析真实 `meta.json`、`execution.log` 结构
- 对比架构文档中的每一个字段、关系、存储路径

---

## 二、问题清单

### 2.1 P0 — 严重（数据结构与实际不符）

#### P0-1: result.json 独立文件不存在

**文档描述**（多处）：

> - E3 Subtask: "存储: `<task_dir>/<sub_id>/result.json`"
> - E4 AgentRole: "result.json agent_type/agent_type_source"
> - E5 Skill: "result.json skills/skills_unresolved"
> - §3.3 数据保留: "meta.json / result.json"
> - EV2 MergeOp: "存储: result.json merge_results"

**实际代码**：

```
~/.agent_go/task-20260526-112220-871-23b3/
├── meta.json       ← 唯一数据文件
├── PLAN.md
├── SHARED_CONTEXT.md
├── execution.log
└── sub-1/
    └── work/       ← 只有 git worktree，无 result.json
```

- `executor.py` 将子任务结果直接 `append` 到 `meta["results"]` 数组
- `cli.py#L263` 中的 `result.json` 恢复逻辑说明此文件可能存在于早期版本，但当前版本不产出
- **所有子任务结果存储在 `meta.json.results[]`，不是独立文件**

**影响**：文档中所有引用 `result.json` 的描述、jq 查询、存储路径均需修正。

---

#### P0-2: Plan 不是独立实体

**文档描述**（E2 Plan）：

> "LLM 生成的结构化执行方案。存储: `execution.log` plan_complete event + `task_dir/PLAN.md`"

**实际代码**：

- Plan 的数据不存储为独立实体，而是直接嵌入 `meta.json.subtasks[]` 数组
- `cli.py#L219`: `(task_dir / "PLAN.md").write_text(plan_to_md(confirmed_plan))` — 仅是人类可读的 markdown 摘要
- `execution.log` 中 `plan_complete` 事件只有 `{event, iteration, step_count}` 三个字段
- 完整的 Plan 定义（每步的 title/description/files_hint/agent_type/skills/verification）全部在 `meta.json.subtasks[]`

**实际三层模型**：

```
文档描述:  Task ──→ Plan ──→ Subtask   (3 个独立实体)
实际结构:  Task ──→ (Plan+Subtask 合体)   (meta.json 包含 subtasks[])
```

**影响**：E2 Plan 应标注为"嵌入在 meta.json.subtasks[] 中，非独立存储"。

---

#### P0-3: execution.log 事件字段与实际不符

**文档描述**：

| 事件 | 文档声称字段 |
|------|-------------|
| `api_call` | provider, latency_ms, response_len, **prompt_tokens**, **completion_tokens**, **model** |
| `subtask_complete` | ... **duration_sec** ... |

**实际代码** (`api.py#L56-L59`):

```python
logger.info(json.dumps({
    "event": "api_call",
    "provider": provider,
    "latency_ms": int((t2 - t1) * 1000),
    "response_len": len(raw),
}))
```

实际 `api_call` 事件只有 4 个字段: `event`, `provider`, `latency_ms`, `response_len`。**无** `prompt_tokens`、`completion_tokens`、`model`。

**实际代码** (`executor.py`):

```python
logger.info(json.dumps({
    "event": "subtask_complete",
    "id": sub_id,
    "status": status,
    "sandbox_type": sandbox_type,
    "duration_sec": round(claude_time, 1),
    "verify_ok": verify_ok,
}))
```

实际 `subtask_complete` 使用 `duration_sec`（与文档一致），但文档 E3 实体表中标注为 `duration_sec` 而 results 中标注为 `duration_sec` — 这里一致。

**影响**：文档中标注 ✅ 的 `prompt_tokens`、`completion_tokens`、`model` 实际未实现（应为 📋）。

---

### 2.2 P1 — 中等（建模偏差）

#### P1-1: AgentRole / Skill 是配置引用，不是运行时 ER 实体

**文档描述**：将 AgentRole 和 Skill 画为 ER 图中的独立实体，通过 1:1 和 N:M 关系连接到 Subtask。

**实际机制**：

```python
# executor.py — agent_type 只是一个字符串
agent_type_name = subtask.get("agent_type", "developer")
agent = load_agent_type(agent_type_name, repo)
# ↑ 从 JSON 文件加载配置，无 FK，无级联，无事务

# executor.py — skills 也是字符串列表
skill_names = subtask.get("skills", [])
for sn in skill_names:
    sk = load_skill(sn, repo)  # ↑ 按 name 字符串查找文件
```

- **无外键约束**：subtask 中的 `agent_type: "reviewer"` 只是字符串匹配，引用的配置文件可以不存在
- **无级联**：删除一个 Agent 配置不影响已记录的历史数据
- **无运行时关联**：Skill 内容是 markdown 文本，被渲染后注入 TASK.md，然后消失

**应标注为**：配置引用（string match），而非 ER 实体关系。

---

#### P1-2: APICall FK 是隐式推断，非真实外键

**文档描述**：

> "task_id | string FK | ✅ | 关联任务（从 logger name 推断）"

**实际机制**：

- `execution.log` 是一个 JSON Lines 文件，每行一条日志
- `task_id` 并不直接记录在事件中，而是通过 `logging.FileHandler` 的文件路径间接关联
- 事件之间没有显式 FK — 只能通过"这个日志文件在哪个 task 目录下"来推断归属

**应标注为**：隐式关联（logger file path），非外键。

---

#### P1-3: MergeOp / VerifyRun 是值对象，非独立实体

**文档描述**：MergeOp 和 VerifyRun 画为独立实体，通过 1:N 关系连接到 Subtask。

**实际状态**：

- 这两个"实体"在代码中**尚未实现**（标注为 📋 [Phase1]）
- Phase1 计划是将它们作为 **嵌套子对象** 存入 `results[].merge_results` 和 `results[].verification_results`
- 它们永远不会独立存储 — 本质是值对象（Value Object），不是实体（Entity）

**应标注为**：值对象（嵌套子结构），非独立实体。

---

#### P1-4: Subtask 定义 vs 执行结果未区分

**文档描述**：E3 Subtask 混合了"定义"和"结果"两类属性。

**实际数据结构**：

```json
// meta.json 中有两个独立数组：
{
  "subtasks": [
    { "id": "sub-1", "title": "...", "agent_type": "architect", "skills": [...], ... }
  ],
  "results": [
    { "subtask_id": "sub-1", "status": "completed", "exit_code": 0, "duration_sec": 56.1, ... }
  ]
}
```

- `subtasks[]` 是 **StepDefinition**（Plan 阶段产出，定义要做什么）
- `results[]` 是 **StepExecution**（执行阶段产出，记录做了什么）
- 两者通过 `subtask_id` 字符串匹配关联（1:1）
- 文档把它们混在一个 E3 Subtask 实体里，模糊了边界

**建议**：分为 E3a StepDefinition（subtasks[]）和 E3b StepExecution（results[]），通过 subtask_id 链接。

---

### 2.3 P2 — 轻微（格式与完整性）

#### P2-1: Task ID 格式不一致

**文档描述**：

> "`task-YYYYMMDD-HHMMSS-mmm-xxxx`"

**实际数据**：

```
task-20260515-130936          ← 旧格式（6段）
task-20260526-110637-241-ca20 ← 新格式（有后缀）
```

文档只记录了新格式，未说明旧格式也存在。

---

#### P2-2: 事件清单不完整

**文档列出**：plan_generate, api_call, api_error, plan_complete, subtask_start, subtask_complete

**实际还有**：`plan_auto_confirmed`, `subtasks_auto_confirmed`, `subtask_headless_start`, `subtask_headless_complete`, `plan_decomposed`

---

#### P2-3: jq 查询示例不可执行

```bash
# 文档中的查询（需要 prompt_tokens 字段）
grep '"event":"api_call"' ~/.agent_go/task-*/execution.log \
  | jq -s 'map(.prompt_tokens+.completion_tokens)|add'
```

由于 `prompt_tokens` 和 `completion_tokens` 实际未实现（P0-3），此查询会返回 `null`。

---

#### P2-4: 缺少数据合约标注

文档混合了"当前已实现"和"PRD 计划"的数据字段，但部分标注不准确（P0-3 中 api_call 字段标为 ✅ 实为 📋）。建议对每个字段增加 `version` 标注，明确哪个版本引入。

---

## 三、建模范式分析：关系型 ER vs 文档型

### 3.1 核心问题

架构文档采用**关系型 ER 模型**描述数据，但 agent_go 的实际存储是**文档型**（单文件嵌套 JSON）。

这两种模型的本质区别：

| 维度 | 关系型 ER | 文档型（agent_go 实际） |
|------|----------|----------------------|
| 存储单元 | 每个实体一张表/文件 | 一个聚合根 = 一个文件 |
| 关联方式 | 外键 + JOIN | 数组索引 + 字符串匹配 |
| 一致性 | FK 约束 + 级联 | 代码逻辑保证 |
| 查询模式 | 灵活（任意维度 JOIN） | 按聚合根整体读取 |
| Schema 变更 | ALTER TABLE | 直接加字段 |

### 3.2 为什么 agent_go 是文档型

agent_go 的数据访问模式完全按任务边界聚合：

| CLI 命令 | 访问模式 | 适合的模型 |
|---------|---------|-----------|
| `show <task-id>` | 读一个任务的全部信息 | 文档型（读 1 个文件） |
| `resume <task-id>` | 恢复一个任务的执行状态 | 文档型（读 1 个文件） |
| `status` | 列出所有任务摘要 | 文档型（扫描目录头） |
| `list` | 列出任务 ID | 文档型（目录列表） |

**没有** "查所有任务的第 3 个子步骤" 这类跨任务查询需求。

**量化对比**：

```
关系型: cmd_show() → 读 task.json + plan.json + subtask-1.json + result-1.json + ... → 4+ 次 I/O
文档型: cmd_show() → 读 meta.json → 1 次 I/O（全部数据已就绪）
```

### 3.3 正确的文档型模型

```
┌─────────────────────────────────────────────────────┐
│  meta.json  (聚合根)                                  │
│                                                      │
│  Task 级字段:                                         │
│    task_id, task, repo, status, created,             │
│    tool_versions, skills, agent_type                 │
│                                                      │
│  Plan + Step 定义 (嵌入):                             │
│    subtasks: [                                        │
│      { id, title, description, files_hint,            │
│        agent_prompt, verification, depends_on,        │
│        agent_type, skills, risks }                    │
│    ]                                                  │
│                                                      │
│  执行结果 (嵌入):                                     │
│    results: [                                         │
│      { subtask_id, status, exit_code, summary,        │
│        worktree, sandbox_type, verify_ok,             │
│        duration_sec, agent_type_source,               │
│        skills_unresolved }                            │
│    ]                                                  │
│                                                      │
│  关联: results[i].subtask_id === subtasks[j].id       │
└─────────────────────────────────────────────────────┘

外部引用 (非 ER 关系):
  agent_type → ~/.agent_go/agents/<name>.json  (字符串匹配)
  skills[]   → ~/.agent_go/skills/<name>/SKILL.md  (字符串匹配)

隐式关联:
  execution.log → 通过文件路径归属 task 目录
```

**关键区别**：

| ER 模型中的"实体" | 文档型中的实际角色 |
|------------------|------------------|
| Task | 聚合根（文件本身） |
| Plan | 聚合根的嵌入属性（subtasks[] 数组） |
| Subtask（定义） | 嵌入数组的元素 |
| Subtask（结果） | 另一个嵌入数组的元素，通过 subtask_id 匹配 |
| AgentRole / Skill | 外部配置引用（字符串），非 ER 关系 |
| MergeOp / VerifyRun | 值对象（计划的嵌套子结构），非独立实体 |
| APICall | 日志事件（隐式关联），非 ER 实体 |

### 3.4 什么时候需要关系型

如果 agent_go 未来出现以下需求，应考虑引入 SQLite：

- **跨任务分析**："统计所有任务的子任务平均耗时" → 当前需扫描所有 meta.json
- **并发写入**：多个子任务同时写各自结果 → 当前需锁整个 meta.json
- **复杂关联**："所有使用 reviewer 但未注入 security-review 的子任务" → SQL 一条搞定

**当前阶段不需要**。文档型模型是正确选择 — 简单、直接、匹配访问模式。

---

## 四、修正建议

### 4.1 文档结构建议

| 修改 | 说明 |
|------|------|
| 将"第一部分：概念数据模型（ER）"改为"概念数据模型（文档型）" | 反映实际存储范式 |
| 删除 ER 图中的独立 Plan 实体 | Plan 嵌入 subtasks[] |
| 删除独立 result.json 存储路径 | 全部在 meta.json |
| AgentRole/Skill 标注为"配置引用" | 非运行时 ER 实体 |
| MergeOp/VerifyRun 标注为"值对象" | 非独立实体 |
| E3 拆分为 E3a StepDefinition + E3b StepExecution | 清晰区分定义与结果 |
| 修正 api_call 事件字段状态 | prompt_tokens/completion_tokens/model 应为 📋 |
| 补充缺失事件 | plan_auto_confirmed 等 |
| 添加 data contract version 标注 | 每个字段标注引入版本 |

### 4.2 正确的实体关系图

```
                    ┌──────────────────────┐
                    │     meta.json        │
                    │     (聚合根)          │
                    │                      │
                    │  Task 级字段          │
                    │  ├── task_id (PK)     │
                    │  ├── task, repo       │
                    │  ├── status, created  │
                    │  ├── tool_versions    │
                    │  ├── skills[] ────────┼──→ string ref → ~/.agent_go/skills/
                    │  └── agent_type ──────┼──→ string ref → ~/.agent_go/agents/
                    │                      │
                    │  Plan+Step 定义       │
                    │  └── subtasks[] ──┐   │
                    │      (嵌入数组)   │   │
                    │                   │   │
                    │  执行结果         │   │
                    │  └── results[] ───┼───┼─→ subtask_id match
                    │      (嵌入数组)   │   │
                    └──────────────────┼───┘
                                       │
                                       ▼
                              ┌────────────────┐
                              │  关联方式:       │
                              │  results[i]     │
                              │    .subtask_id  │
                              │  ===            │
                              │  subtasks[j].id │
                              └────────────────┘

  外部文件 (非 ER 关系):
    execution.log     → 隐式关联 (同目录)
    PLAN.md           → 人类可读摘要 (从 subtasks[] 生成)
    SHARED_CONTEXT.md → 运行时中间产物

  配置引用 (字符串匹配, 无 FK):
    agent_type → ~/.agent_go/agents/<name>.json
    skills[]   → ~/.agent_go/skills/<name>/SKILL.md
```

---

## 五、实际数据样例（供参考）

### 5.1 meta.json 真实结构

```json
{
  "task_id": "task-20260526-112220-871-23b3",
  "task": "在 agent_go 目录下创建 services.py 文件",
  "repo": "/Users/jinsongwang/workspace/agent_go",
  "created": "20260526-112220-871",
  "status": "completed",
  "reference_docs": [],
  "issue": "",
  "subtasks": [
    {
      "id": "sub-1",
      "title": "分析现有代码结构",
      "description": "...",
      "files_hint": "agent_go/config.py, agent_go/utils.py, ...",
      "agent_prompt": "请阅读以下文件...",
      "verification": "cat agent_go/config.py ...",
      "risks": ["..."],
      "depends_on": [],
      "skills": ["python-patterns"],
      "agent_type": "architect"
    }
  ],
  "results": [
    {
      "subtask_id": "sub-1",
      "status": "no_changes",
      "exit_code": 0,
      "summary": "无文件变更",
      "worktree": "/Users/jinsongwang/.agent_go/.../sub-1/work",
      "sandbox_type": "headless",
      "verify_ok": true,
      "duration_sec": 56.1
    }
  ],
  "tool_versions": {"claude": "..."},
  "skills": [],
  "agent_type": "developer"
}
```

### 5.2 execution.log 真实事件

```jsonl
{"event":"plan_generate","iteration":1,"has_supplement":false,"has_docs":false,"has_skills":true}
{"event":"api_call","provider":"anthropic","latency_ms":3150,"response_len":4520}
{"event":"plan_complete","iteration":1,"step_count":4}
{"event":"plan_auto_confirmed"}
{"event":"plan_decomposed","step_count":4}
{"event":"subtasks_auto_confirmed"}
{"event":"subtask_start","id":"sub-1","title":"...","depends_on":[],"headless":true}
{"event":"subtask_headless_start","id":"sub-1"}
{"event":"subtask_headless_complete","id":"sub-1","exit_code":0}
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless","duration_sec":56.1,"verify_ok":true}
```

---

## 六、总结

| 级别 | 数量 | 核心问题 |
|------|------|---------|
| P0 | 3 | result.json 不存在、Plan 非独立实体、事件字段标注错误 |
| P1 | 4 | AgentRole/Skill 是配置引用、APICall 隐式关联、MergeOp/VerifyRun 是值对象、定义 vs 结果未区分 |
| P2 | 4 | Task ID 格式、事件不全、jq 查询不可执行、缺版本标注 |

**根因**：架构文档用关系型 ER 模型描述了一个文档型存储系统，导致实体拆分过度、存储路径虚构、关系语义偏差。

**建议**：重构文档第一部分为"文档型数据模型"，以 meta.json 聚合根为中心，嵌入 subtasks[] 和 results[]，外部配置用字符串引用。保留维度分析模型（第二部分）作为分析层抽象 — 它不依赖存储范式。

---

---

# 附录：v2.0 架构文档审查（2026-05-26）

> 架构文档已从 v1.1（关系型 ER）重构为 v2.0（文档型数据模型）。
> 以下是对 v2.0 版本的审查结果。v1.0 审查中的 P0 问题已全部修正。

## V2 审查概述

v2.0 架构文档从关系型 ER 重构为文档型数据模型，方向完全正确。v1.0 中的 P0 问题（result.json 虚构、Plan 非独立实体、事件字段标注错误）已全部修正。本轮审查聚焦**字段细节与实际代码的一致性**。

### 审查方法

- 逐行读取 `executor.py`、`subtask.py`、`api.py`、`ui.py`、`cli.py` 中所有 `log_event()` 调用
- 对照架构文档 §2.1 示例 JSONL、§2.2 事件定义表中每个字段
- 对比 `results[]` 返回值 vs `execution.log` 事件字段

---

## V2 问题清单

### V2-P1（中等）— 4 项

#### V2-P1-1: `subtask_complete` 事件字段与代码不一致

**文档 §2.1 示例**（第 192 行）：
```json
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless","duration_sec":56.1,"verify_ok":true}
```

**文档 §2.2 事件定义表**（第 209 行）：

> ✅ id, status, sandbox_type, duration_sec, verify_ok

**实际代码**（[executor.py#L333-L337](../../agent_go/executor.py)）：
```python
log_event(logger, "subtask_complete", {
    "id": sub_id, "status": status, "sandbox_type": sandbox_type,
    "clone_sec": round(clone_time, 2), "claude_sec": round(claude_time, 2),
    "summary": summary, "verify_ok": verify_ok,
})
```

**差异**：

| 字段 | 文档 | 代码 | 说明 |
|------|------|------|------|
| `duration_sec` | ✅ | ❌ 不存在于事件中 | 代码用 `claude_sec` + `clone_sec` 两个独立字段 |
| `clone_sec` | ❌ | ✅ | 事件中记录 clone 耗时，文档遗漏 |
| `claude_sec` | ❌ | ✅ | 事件中记录 Claude 执行耗时，文档遗漏 |
| `summary` | ❌ | ✅ | 事件中记录变更摘要，文档遗漏 |

**注意**：`results[]` 返回值（[executor.py#L339-L343](../../agent_go/executor.py)）中确实有 `duration_sec`（取自 `claude_time`），但 `execution.log` 事件用的是 `clone_sec` + `claude_sec`。文档混用了两个字段来源。

**修正**：§2.2 事件定义表 `subtask_complete` 的字段应改为：
```
✅ id, status, sandbox_type, clone_sec, claude_sec, summary, verify_ok
```
§2.1 示例 JSONL 应改为：
```json
{"event":"subtask_complete","id":"sub-1","status":"completed","sandbox_type":"headless",
 "clone_sec":2.1,"claude_sec":95.3,"summary":"2 files changed, +45/-2","verify_ok":true}
```

---

#### V2-P1-2: `subtask_headless_complete` 事件字段严重遗漏

**文档 §2.1 示例**（第 191 行）：
```json
{"event":"subtask_headless_complete","id":"sub-1","exit_code":0}
```

**文档 §2.2 事件定义表**（第 208 行）：

> ✅ id, exit_code

**实际代码**（[subtask.py#L241-L246](../../agent_go/subtask.py)）：
```python
log_event(logger, "subtask_headless_complete", {
    "id": sub_id, "exit_code": final_rc,
    "interaction_detected": interaction,
    "attempts": attempt + 1,
    "output_lines": len(all_lines),
})
```

**遗漏字段**：

| 字段 | 类型 | 用途 |
|------|------|------|
| `interaction_detected` | bool | 是否检测到交互式提示（需要重试） |
| `attempts` | int | 实际尝试次数（1-based） |
| `output_lines` | int | Claude 输出总行数 |

`interaction_detected` 和 `attempts` 对判断 headless 重试质量至关重要，不应遗漏。

**修正**：§2.2 事件定义表 `subtask_headless_complete` 应改为：
```
✅ id, exit_code, interaction_detected, attempts, output_lines
```

---

#### V2-P1-3: 遗漏 `subtask_headless_retry` 事件

**代码中存在**（[subtask.py#L225](../../agent_go/subtask.py)）：
```python
log_event(logger, "subtask_headless_retry", {"id": sub_id, "attempt": attempt + 1})
```

文档 §2.2 事件定义表（第 198-209 行）共列出 11 个事件，**完全没有** `subtask_headless_retry`。

此事件在 headless 模式重试时触发，是排查"为什么某个子任务执行了多次"的关键日志。

**修正**：§2.2 事件定义表中在 `subtask_headless_start` 和 `subtask_headless_complete` 之间插入：

| 事件 | 时机 | 字段 |
|------|------|------|
| `subtask_headless_retry` | Claude -p 因交互退出后重试 | ✅ id, attempt |

---

#### V2-P1-4: `plan_decomposed` / `plan_auto_confirmed` / `subtasks_auto_confirmed` 事件字段与代码不一致

**文档 §2.1 示例**（第 188-189 行）：
```json
{"event":"plan_decomposed","step_count":4}
{"event":"subtasks_auto_confirmed"}
```

**文档 §2.2 事件定义表**（第 204-205 行）：

> `plan_decomposed`: ✅ step_count
> `plan_auto_confirmed`: ✅ —（无字段）
> `subtasks_auto_confirmed`: ✅ —（无字段）

**实际代码**：

| 事件 | 代码（文件:行） | 实际字段 |
|------|----------------|---------|
| `plan_decomposed` | [ui.py#L300](../../agent_go/ui.py) | `{"count": len(subtasks)}` — 字段名是 `count` 不是 `step_count` |
| `plan_auto_confirmed` | [ui.py#L134](../../agent_go/ui.py) | `{"iteration": iteration}` — 有字段，不是无字段 |
| `subtasks_auto_confirmed` | [ui.py#L345](../../agent_go/ui.py) | `{"count": len(subtasks)}` — 有字段，不是无字段 |

**修正**：

§2.2 事件定义表应改为：

| 事件 | 字段 |
|------|------|
| `plan_decomposed` | ✅ count (注意：不是 step_count) |
| `plan_auto_confirmed` | ✅ iteration |
| `subtasks_auto_confirmed` | ✅ count |

§2.1 示例 JSONL 应改为：
```json
{"event":"plan_decomposed","count":4}
{"event":"subtasks_auto_confirmed","count":4}
```

---

### V2-P2（轻微）— 2 项

#### V2-P2-1: `subtask_complete` 中 `status` 值枚举不完整

文档 §2.2 和 §2.3 查询示例中使用 `status=="failed"` 过滤，但未说明 `status` 字段的完整枚举值。

**实际代码**（[executor.py#L328-L331](../../agent_go/executor.py)）：
```python
if result.returncode == 0 and verify_ok:
    status = "no_changes" if summary == "无文件变更" else "completed"
else:
    status = "failed"
```

完整枚举：`completed` | `no_changes` | `failed`

**建议**：§2.2 事件定义表中 `subtask_complete` 的 `status` 字段增加枚举值说明。

---

#### V2-P2-2: 附录 A 统计数字待逐字段复核

附录 A 声称 "meta.json 顶层 14 个字段 ✅"，但实际 `cli.py#L230-L237` 初始化：

```python
{
    "task_id": task_id,       # 1
    "task": task,             # 2
    "repo": str(repo),        # 3
    "created": ts,            # 4
    "status": "running",      # 5
    "reference_docs": ...,    # 6
    "issue": issue_ref,       # 7
    "subtasks": confirmed,    # 8
    "results": [],            # 9
    "tool_versions": ...,     # 10
    "skills": ...,            # 11
    "agent_type": ...,        # 12
    "remote_url": ...,        # 13
}
```

共 13 个字段，不是 14 个。除非 `results: []` 空数组不计入顶层字段（仅计入子数组），则为 12 个。

**建议**：逐字段标注附录 A 的计数来源，确保可追溯。

---

## V2 总结

| 级别 | 数量 | 核心问题 |
|------|------|---------|
| P0 | 0 | v1.0 的 P0 已全部修正 ✅ |
| P1 | 4 | 事件字段与代码不一致（4 处）：subtask_complete、subtask_headless_complete、遗漏事件、字段名/字段数错误 |
| P2 | 2 | status 枚举不完整、附录统计待复核 |

### v1.0 → v2.0 修正验证

| v1.0 问题 | v2.0 状态 |
|-----------|----------|
| P0-1 result.json 不存在 | ✅ 已修正，改为 meta.json.results[] |
| P0-2 Plan 不是独立实体 | ✅ 已修正，文档标注为 subtasks[] 嵌入 |
| P0-3 事件字段标注错误 | ⚠️ 部分修正（api_call 已修正，但 subtask_complete 等仍有偏差 → V2-P1-1） |
| P1-1 AgentRole/Skill 是配置引用 | ✅ 已修正，§1.4 明确标注"字符串匹配，非 FK" |
| P1-2 APICall 隐式关联 | ✅ 已修正，§2.1 标注"通过文件路径隐式关联" |
| P1-3 MergeOp/VerifyRun 是值对象 | ✅ 已修正，§1.5 标注为"嵌套子结构" |
| P1-4 定义 vs 结果未区分 | ✅ 已修正，§1.3 明确区分 StepDefinition vs StepExecution |
| P2-1 Task ID 格式 | ✅ 已修正（文档中用新格式示例） |
| P2-2 事件清单不完整 | ⚠️ 部分修正（补了 plan_auto_confirmed 等，但遗漏 subtask_headless_retry → V2-P1-3） |
| P2-3 jq 查询不可执行 | ✅ 已修正，标注为 📋 需 Phase1 |
| P2-4 缺版本标注 | ✅ 已修正，全文版本标注体系完善 |

### 核心改进点

v2.0 架构文档的**范式描述**已完全正确：
- ✅ 明确声明文档型存储范式（§1.1）
- ✅ 聚合根 + 嵌入数组 + 字符串引用的模型准确
- ✅ StepDefinition/StepExecution 区分清晰
- ✅ 版本标注体系（✅/📋）实用且可追溯
- ✅ 维度分析模型与数据源映射明确

**剩余问题模式**：所有 V2 问题都集中在**事件字段的细节准确性**上 — 文档只写了部分字段，遗漏了代码中实际记录的完整字段集。建议逐一对照所有 `log_event()` 调用的实际参数，补齐所有事件的所有字段。

---

---

# 附录：v2.2 架构文档审查（2026-05-27）

> 架构文档更新至 v2.2。v2.0 审查中的 P1 问题（事件字段与代码不一致）已修正。
> 以下是对 v2.2 版本的审查结果。

## V3 审查概述

v2.2 已修正 v2.0 中 4 项 P1 问题（subtask_complete 字段、subtask_headless_complete 字段、补充 subtask_headless_retry 事件、plan_decomposed 字段名）。本轮审查发现**代码实现已超越文档标注** — 多个已实现功能仍被标记为计划阶段。

### 审查方法

- 逐行读取 `api.py`（181 行）、`executor.py`（391 行）、`ui.py`（413 行）中所有 `log_event()` 调用
- 读取 `metrics.py`（70 行）确认值对象实现
- 对照文档 §2.2 事件定义表和附录 A 的每个字段状态标注

---

## V3 问题清单

### V3-P1（中等）— 3 项

#### V3-P1-1: `api_call` 事件字段状态标注滞后

**文档 §2.2 事件定义表**（第 201 行）：

> 字段 (✅已实现): ✅ provider, latency_ms, response_len
> 字段 (📋计划): ✅ [v0.5] model, prompt_tokens, completion_tokens

**实际代码**（[api.py#L41-L46](../../agent_go/api.py)）：

```python
log_event(logger, "api_call", {
    "provider": provider, "model": model,
    "latency_ms": round(latency * 1000, 2), "response_len": len(content),
    "prompt_tokens": usage.get("input_tokens", 0),
    "completion_tokens": usage.get("output_tokens", 0),
})
```

代码**已产出全部 6 个字段**，`model`、`prompt_tokens`、`completion_tokens` 不应再标 `[v0.5]`，应移至 ✅ 已实现列。

**影响**：维度分析模型 §3.2 FACT-2 plan_generation 和 FACT-3 api_call 的指标可计算率被低估（文档暗示需要 Phase1 才有 tokens 数据，实际已有）。

---

#### V3-P1-2: `api_error` 事件标注为计划，实际已实现

**文档 §2.2 事件定义表**（第 202 行）：

> 字段 (✅已实现): —
> 字段 (📋计划): ✅ [v0.5] provider, status_code, error_message

**实际代码**（[api.py#L49-L51](../../agent_go/api.py)）：

```python
log_event(logger, "api_error", {
    "provider": provider, "status_code": e.code,
    "error_message": str(e)[:200],
})
```

`api_error` 事件**已完整实现**，不应在 ✅ 列标注为 "—"。应移至 ✅ 已实现列。

**影响**：文档读者误以为 api_error 不可用于监控和诊断。

---

#### V3-P1-3: 遗漏 3 个用户交互事件

**文档 §2.2 事件定义表**列出 12 个事件，但代码中还有 3 个用户交互事件未记录：

| 事件 | 代码位置 | 字段 |
|------|---------|------|
| `user_plan_choice` | [ui.py#L148](../../agent_go/ui.py) | choice, iteration, auto_confirm |
| `user_subtask_choice` | [ui.py#L359](../../agent_go/ui.py) | choice |
| `user_verify` | [ui.py#L408](../../agent_go/ui.py) | current, choice |

这 3 个事件记录了用户在交互模式下的决策，是**评估用户行为和自动化率**的关键数据源。

**影响**：
- 无法统计"用户手动确认 vs 自动确认"的比例
- §3.4 典型查询中无法回答"用户最常用的操作是什么"
- 附录 A 事件总数少计 3 个

---

### V3-P2（轻微）— 3 项

#### V3-P2-1: 附录 A 重复行

**文档第 328-330 行**：

```
**指标可计算率**: 当前 23/44 (52%) → Phase2 后 35/44 (80%)

**指标可计算率**: 当前 11/44 (25%) → Phase1 后 23/44 (52%) → Phase2 后 35/44 (80%)
```

存在两行 "指标可计算率"，数值不一致（一行说当前 52%，另一行说 25%）。应删除旧的一行。

---

#### V3-P2-2: 附录 A 事件总数需更新

文档第 323 行：

> execution.log events | 12 | 12 | — | —

实际代码中已有 15 个事件（12 + 3 个用户交互事件），且 api_call 和 api_error 的字段数也已变化：

| 修正项 | 旧值 | 新值 |
|--------|------|------|
| 事件总数 | 12 | 15 |
| api_call 扩展字段数 | 4 (标为 v0.5) | 3 (已实现，标为 ✅) |
| api_error | 未计 | 3 字段 (已实现) |
| plan_complete 扩展 | 2 (含 plan_duration_ms 标 v0.5) | plan_duration_ms 已实现 |

---

#### V3-P2-3: `plan_complete` 事件 `plan_duration_ms` 标注滞后

**文档 §2.2**（第 203 行）：

> 字段 (📋计划): ✅ [v0.5] plan_duration_ms

**实际代码**（[api.py#L146-L148](../../agent_go/api.py)）：

```python
plan_duration_ms = round((time.time() - plan_start) * 1000)
log_event(logger, "plan_complete", {"iteration": iteration, "step_count": len(plan.get("steps", [])),
                                     "plan_duration_ms": plan_duration_ms})
```

`plan_duration_ms` 已在代码中实现，应标为 ✅ 已实现，不应标 [v0.5]。

---

## V3 总结

| 级别 | 数量 | 核心问题 |
|------|------|---------|
| P0 | 0 | 无 |
| P1 | 3 | 代码已实现但文档仍标记为"计划"：api_call 6字段、api_error 事件、plan_duration_ms；遗漏 3 个用户交互事件 |
| P2 | 3 | 附录 A 重复行、事件总数偏少、plan_duration_ms 标注滞后 |

### v2.0 → v2.2 修正验证

| v2.0 问题 | v2.2 状态 |
|-----------|----------|
| V2-P1-1 subtask_complete 字段不一致 | ✅ 已修正（clone_sec, claude_sec, summary 已补入） |
| V2-P1-2 subtask_headless_complete 字段遗漏 | ✅ 已修正（interaction_detected, attempts, output_lines 已补入） |
| V2-P1-3 遗漏 subtask_headless_retry | ✅ 已修正（已补入事件表） |
| V2-P1-4 plan_decomposed 等字段名错误 | ✅ 已修正（count/iteration 已修正） |
| V2-P2-1 status 枚举不完整 | ✅ 已修正（§2.2 注中列出 completed/no_changes/failed） |
| V2-P2-2 附录统计 | ⚠️ 部分修正（新增附录 A 但有重复行和计数偏差） |

### 根因分析

v2.2 的所有问题源于同一个模式：**代码实现速度超过文档更新速度**。

`api.py` 在某次迭代中新增了 `model`/`prompt_tokens`/`completion_tokens`/`api_error`/`plan_duration_ms` 等字段，但数据架构文档的 §2.2 事件表和附录 A 未同步更新状态标注。

**建议操作**：
1. 将 §2.2 中所有 `[v0.5]` 标注改为 `✅`（api_call 6 字段、api_error 3 字段、plan_duration_ms）
2. 在 §2.2 事件表中补充 3 个用户交互事件
3. 删除附录 A 重复行，更新计数

---

*文档结束*
