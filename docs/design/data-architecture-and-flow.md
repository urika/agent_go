# agent_go 数据架构与流向

> 状态：设计草案（2026-07-25）
> 视角：从数据驱动过程优化的角度，刻画整个系统中的数据架构、流向和反馈闭环。
> 这是与「功能架构」正交的「数据架构」视图，两部分互补。
>
> **当前口径说明（2026-08-08）**：本文中的 K4/K8 数值目标属于旧版规划示例。当前产品以 Accepted Delivery、Cost per Accepted Delivery 和真实任务验证为准；Bench 按 suite 分层运行。

---

## 1. 数据全景

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            INPUT LAYER                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Task     │  │ Config   │  │ Skills   │  │ Docs     │  │ Knowledge│ │
│  │ 文本描述  │  │ JSON     │  │ YAML+MD  │  │ Markdown  │  │ JSON     │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
│       │              │             │             │              │       │
├───────┴──────────────┴─────────────┴─────────────┴──────────────┴──────┤
│                           PROCESS LAYER                                │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  Plan Engine                                                   │     │
│  │  输入: task + config + skills + docs + knowledge (待建)        │     │
│  │  输出: Plan JSON (steps + dependencies + verification)         │     │
│  └─────────────────────┬────────────────────────────────────────┘      │
│                        │                                               │
│  ┌─────────────────────▼────────────────────────────────────────┐      │
│  │  Pipeline Scheduler (DAG Wave + ThreadPool + Worktree + Claude) │   │
│  │  输出(单次运行):                                                  │   │
│  │    ├── meta.json        — 结果/subtask/timing/change_stats      │   │
│  │    ├── metering.jsonl   — 每 API 调用的 role/tokens/cost/...   │   │
│  │    ├── events.jsonl     — 生命周期事件（待建）                    │   │
│  │    └── review.json      — 审查结论                               │   │
│  └─────────────────────┬────────────────────────────────────────┘      │
│                        │                                               │
│  ┌─────────────────────▼────────────────────────────────────────┐      │
│  │  Knowledge Store（待建）                                       │     │
│  │  输入: meta.json + metering.jsonl (跨 N 次运行聚合)             │     │
│  │  输出: patterns / failure-signals / verified-cmds              │     │
│  │  消费: 下一轮 Plan Engine → Prompt 注入历史经验                  │     │
│  └──────────────────────────────────────────────────────────────┘      │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│                          OUTPUT LAYER                                  │
│                                                                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐  │
│  │ meta.json│  │metering  │  │ events   │  │ knowledge│  │ review │  │
│  │ 单任务    │  │ .jsonl   │  │ .jsonl   │  │ /        │  │ .json  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬───┘  │
│       │              │             │              │             │      │
├───────┴──────────────┴─────────────┴──────────────┴─────────────┴──────┤
│                        CONSUMPTION LAYER                               │
│                                                                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐│
│  │ eval     │  │ CI Gate  │  │ Plan     │  │ Grafana  │  │ API      ││
│  │ 质量/成本 │  │$/pass rate│  │ 注入经验   │  │ 看板     │  │ query    ││
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘│
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 数据实体详细定义

### 2.1 输入层

| 实体 | 格式 | 位置 | 来源 | 变更频率 |
|------|------|------|------|---------|
| Task 描述 | 纯文本 | CLI 参数 / Python API 参数 | 用户输入 | 每次运行 |
| Config | JSON | `~/.agent_go/config.json` | 用户配置 + DEFAULT_CONFIG | 低频（手动修改） |
| Skills | YAML + Markdown | `~/.agent_go/skills/<name>/SKILL.md` | 用户/社区编写 | 低频 |
| Reference Docs | Markdown / 文本 | `--docs` 参数指定路径 | 用户挂载 | 每次运行 |
| **Knowledge (待建)** | JSON | `~/.agent_go/knowledge/<repo-hash>/` | 跨运行自动提取 | 每次运行后增量更新 |

### 2.2 过程数据（运行时）

| 实体 | 生成时机 | 作用域 | 关键字段 |
|------|---------|--------|---------|
| Plan JSON | Plan Engine 执行后 | 单次运行（暂存） | `steps[]`, `dependencies`, `shared_resources`, `overview` |
| Subtasks | `plan_to_subtasks()` 后 | 单次运行 | `id`, `title`, `description`, `agent_type`, `skills`, `depends_on`, `files_hint`, `verification` |
| verify_state.json | 每次验证后 | 单子任务 | `retry_count`, `last_output`, `last_stderr`, `last_diff` |
| metering_path env | 子进程启动时 | 单子任务 | `AGENT_GO_METERING_PATH` 环境变量指向 metering.jsonl |

### 2.3 输出持久层

#### meta.json（单次运行最终结果）

```json
{
  "schema_version": "2.0",
  "task_id": "task-1721800000",
  "status": "completed",
  "task": "任务描述",
  "repo": "/path/to/repo",
  "base_branch": "main",
  "created": "2026-07-25T10:00:00",
  "pass_rate": 0.92,
  "cost_usd": 0.15,
  "duration_sec": 240,
  "subtasks": [{}],
  "results": [{}],
  "review": { "decision": "approved", "reviewed_at": "..." },
  "metrics": {
    "k1_subtask_pass_rate": 0.92,
    "k2_first_pass_rate": 0.80,
    "k3_cost_per_subtask": 0.03
  }
}
```

**作用**：单次运行的完整档案。所有下游分析的基础数据源。

#### metering.jsonl（每 API 调用一条记录）

```jsonl
{"role":"planner","virtual_model":"agentgo-planner","actual_provider":"anthropic","actual_model":"claude-sonnet-4-20250514","prompt_tokens":1500,"completion_tokens":400,"cost_usd":0.0105,"latency_ms":3200,"result":"success","fallback_reason":"","task_id":"task-xxx","subtask_id":""}
{"role":"worker","virtual_model":"agentgo-worker","actual_provider":"deepseek","actual_model":"deepseek-chat","prompt_tokens":800,"completion_tokens":200,"cost_usd":0.0004,"latency_ms":1800,"result":"success","fallback_reason":"","task_id":"task-xxx","subtask_id":"sub-1","difficulty":"easy"}
{"role":"worker","virtual_model":"agentgo-worker","actual_provider":"anthropic","actual_model":"claude-sonnet-4","prompt_tokens":2500,"completion_tokens":1200,"cost_usd":0.0255,"latency_ms":8500,"result":"fallback","fallback_reason":"primary_unavailable","task_id":"task-xxx","subtask_id":"sub-3","difficulty":"hard","policy_violation":"planner_fallback_configured"}
```

**作用**：北极星指标（$/pass rate）的唯一可信数据源。每行一个 API 调用，精确到角色/模型/成本/延迟。

#### events.jsonl（待建）

```jsonl
{"type":"plan.generated","ts":"2026-07-25T10:00:05","task_id":"task-xxx","payload":{"step_count":5}}
{"type":"subtask.started","ts":"2026-07-25T10:01:00","task_id":"task-xxx","subtask_id":"sub-1","payload":{}}
{"type":"subtask.completed","ts":"2026-07-25T10:05:00","task_id":"task-xxx","subtask_id":"sub-1","payload":{"status":"completed","duration_sec":240,"verify_ok":true}}
{"type":"pipeline.completed","ts":"2026-07-25T10:20:00","task_id":"task-xxx","payload":{"status":"completed","pass_rate":0.80,"cost_usd":0.15}}
```

**作用**：全生命周期事件溯源。供外部系统（CI/IDE/Webhook）实时订阅，不依赖轮询。

#### review.json

```json
{
  "task_id": "task-xxx",
  "reviewed_at": "2026-07-26T09:00:00",
  "decision": "approved",
  "summary": "审查通过"
}
```

**作用**：审查结论持久化。Quality Dashboard 读取后展示在 PR 描述中。

### 2.4 知识存储层（待建）

#### patterns.json

```json
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
      "content": "DB 迁移类任务应拆为 3 步：schema→数据→回滚",
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

**作用**：跨运行知识沉淀。每次运行成功后增量更新，下次 Plan 时注入 Prompt。

---

## 3. 数据流详解

### 3.1 流 A：单次运行数据流（当前已完整）

```
CLI 输入 ──→ Plan Engine ──→ Pipeline ──→ Claude ──→ Verify ──→ Commit ──→ Review
   │            │               │           │          │           │          │
   ▼            ▼               ▼           ▼          ▼           ▼          ▼
task.txt    plan.json       meta.json   metering    verify_     git diff    review
                             (进行中)    .jsonl      state       --numstat  .json
                                                    .json
```

**生命周期**：秒级（单次 run 的 1-30 分钟内完成）

### 3.2 流 B：跨运行聚合流（当前部分存在）

```
meta.json(N) ──→ analyze_quality() ──→ Q1-Q10 指标
metering.jsonl(N) ──→ analyze_cost() ──→ $/pass rate / by_model / by_role / policy_violations
meta.json(N) ──→ analyze_reliability() ──→ 任务完成率 / 阻断率 / 重试成功率

                         ↓

CI Gate: gate_cost(baseline) → passed/fail → exit 0/1
```

**生命周期**：分钟级（run 完成后触发 eval）

### 3.3 流 C：知识反馈闭环流（待建设）

```
流 B 的输出 ──→ KnowledgeStore.extract() ──→ patterns.json
                                                    │
                    Agent 链回环                       │
                    ┌─────────────────────────────────┘
                    ▼
              Plan Engine Prompt 追加:
              "本项目历史经验 (N次运行):
               - 已验证命令: pytest tests/ -x -q (成功率92%)
               - 高频失败: 超时 → 建议 --timeout=30
               - 项目特征: Python + pytest + poetry"
                    │
                    ▼
              下次 run 的 Plan 质量更高
              首次通过率提升 → 流 A 质量提升
                    │
                    ▼
              KnowledgeStore 再次增量更新
              → 复利效应
```

**生命周期**：跨任务（小时级增长，持续累积）

---

## 4. 三种优化反馈环

### 4.1 内环：单次运行内优化（秒级）

```
验证失败
  │
  ▼
收集: stdout / stderr / git diff --stat
  │
  ▼
RepairAgent: 注入完整失败上下文 → Claude 修复
  │
  ▼
再验证 ──→ 通过 → 继续
  │
  └──→ 失败 + retry < max_retries → 再修复
  │
  └──→ 失败 + retry ≥ max_retries → blocked 阻断下游
```

**数据驱动点**：`verify_state.json` 记录每次重试的输出和 diff

**当前验证目标**：减少无进展 retry，记录 failure_pattern/effective_strategy，并验证其对 Accepted Delivery 的影响。

### 4.2 中环：运行 → 门禁优化（分钟级）

```
Pipeline 完成
  │
  ▼
meta.json + metering.jsonl
  │
  ▼
eval gate(baseline=0.05)
  │
  ├── pass (exit 0) → CI 继续
  │
  └── fail (exit 1) → 阻断发布 + 通知团队
                        │
                        ▼
                   工程师调整路由/配置
                        │
                        ▼
                   下次运行成本下降
```

**数据驱动点**：`Cost per Accepted Delivery` 对比冻结基线；旧 `$/pass rate` 仅作同 suite 内诊断。

**当前优化目标**：成本不失控且不损害 Accepted Delivery；绝对成本目标待 M3 真实任务验证后重新制定。

### 4.3 长环：跨运行知识优化（跨任务）

```
第 1 次运行
  ├── verify_ok 命令: "pytest tests/ -x -q"
  └── failure: "DB migration 冲突"
         │
         ▼  KnowledgeStore
第 2 次运行
  ├── Plan prompt 注入: "已验证: pytest, DB migration 建议 3 步"
  └── 首次通过率: 60% → 75%
         │
         ▼  KnowledgeStore
第 10 次运行
  ├── 知识库: 8 个模式 + 3 个失败信号
  ├── 验证命令不再随机生成
  ├── 项目特有模式自动传承
  └── 首次通过率: 75% → 90%
```

**数据驱动点**：`meta.json` 的 `verify_ok` / `failure_reason` / `change_stats` 跨任务聚合

**优化目标**：K8 首次通过率 ≥90% + K3 任务耗时缩短

---

## 5. 数据流向图（完整版）

```
                         ┌─────────────┐
                         │   用户输入    │
                         │ (task/docs)  │
                         └──────┬──────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────┐
│                  Plan Engine                          │
│                                                       │
│  Prompt = task + config + skills + docs               │
│          + knowledge hints (待建)                     │
│                                                       │
│  Output: Plan JSON (steps/deps/verification)         │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│             Pipeline Scheduler                        │
│                                                       │
│  Wave 1: ── sub-1 ── sub-2 (并发)                     │
│  Wave 2: ── sub-3 (依赖 sub-1)                        │
│  Wave 3: ── sub-4 (依赖 sub-2)                        │
│                                                       │
│  每子任务:                                            │
│    worktree → merge 上游 → claude -p → verify         │
│    → commit + tag                                     │
│                                                       │
│  输出: meta.json (逐结果追加)                          │
│        metering.jsonl (每 API 一行)                   │
└──────────────┬──────────────────────────┬────────────┘
               │                          │
               ▼                          ▼
        ┌──────────┐            ┌─────────────────┐
        │ meta.json│            │ metering.jsonl   │
        │ (N条)    │            │ (M行)            │
        └────┬─────┘            └───────┬─────────┘
             │                          │
             ▼                          ▼
     ┌─────────────────────────────────────────┐
     │           eval 聚合层                    │
     │                                         │
     │ quality: Q1-Q10 + score                │
     │ cost:    total/by_model/per-role        │
     │          + $/pass + policy_violations   │
     │ perf:    P1-P6 + score                 │
     │ reliability: rates + sandbox + retry   │
     │ ux:      agent/skill/doc distribution  │
     └──────────────────┬──────────────────────┘
                        │
                  ┌─────┴─────┐
                  │           │
                  ▼           ▼
           ┌──────────┐  ┌──────────┐
           │ CI Gate   │  │ eval CLI │
           │ exit 0/1  │  │ 人类查看  │
           └──────────┘  └──────────┘
                        │
                        ▼ (待建)
              ┌─────────────────────┐
              │  KnowledgeStore     │
              │  _extract_patterns  │
              │   → patterns.json   │
              │   → failure-        │
              │     signals.json    │
              │   → verified-cmds   │
              │     .json           │
              └─────────┬───────────┘
                        │ 注入下一轮
                        ▼
              ┌─────────────────────┐
              │    Plan Engine      │ ← 回环
              │  (Prompt 追加知识)   │
              └─────────────────────┘
```

---

## 6. 数据治理

| 维度 | 策略 |
|------|------|
| **隔离性** | 按 repo hash 隔离：`~/.agent_go/knowledge/<repo-hash>/` |
| **格式** | JSON（人类可读 + 机器可解析） |
| **保留** | metering.jsonl → 全部保留（成本审计）<br>meta.json → 全部保留（质量追溯）<br>knowledge/ → 增量更新（模式持续演进） |
| **安全** | metering 不包含 API Key<br>knowledge 仅含项目模式，不含凭据<br>所有数据本地存储，不自动上传 |
| **降级** | KnowledgeStore JSON 解析失败 → 跳过注入，回退无知识模式<br>events.jsonl 写入失败 → 不阻塞 pipeline |
| **版本** | 所有 JSON 文件含 `schema_version` 字段 |

---

## 7. 与功能架构的关系

```
功能架构（是什么）              数据架构（怎么流动）
┌──────────────┐              ┌──────────────────────────┐
│ cmd_run()    │              │ task + config → Plan JSON │
│ generate_plan│              │ → Prompt (含知识注入)      │
│ _run_pipeline│              │ → meta + metering        │
│ run_subtask  │              │ → verify_state           │
│ cmd_eval     │  ← 正交 →    │ → eval 聚合               │
│ cmd_review   │              │ → review.json            │
│ notify_event │              │ → events.jsonl           │
│ KnowledgeStore│             │ → patterns.json          │
└──────────────┘              └──────────────────────────┘
```

**功能架构**回答"系统有哪些模块、各模块做什么"
**数据架构**回答"系统有哪些数据、数据如何生成、流向哪里、如何形成闭环"

两者互补，共同构成系统的完整描述。

---

## 8. 数据量预测与存储架构决策

### 8.1 单次运行数据量实测

| 数据文件 | 大小 | 说明 |
|---------|------|------|
| `meta.json` | ~5 KB | 5 子任务，含 results/change_stats/timing |
| `metering.jsonl` | ~10 KB | 50 行 × 200 B（planner + 5 worker × 每次重试均有记录） |
| `review.json` | ~0.5 KB | — |
| `execution.log` | ~5 KB | INFO 日志 |
| `events.jsonl` | ~3 KB | 待建，~20 事件 |
| Worktree git objects | ~100 KB | 共享对象库，非独立文件 |
| **单次总计** | **~25 KB** | 不含 git objects |

### 8.2 增长模型

| 用户类型 | 日均 runs | 月存储 | 年存储 | 1 年后面临评估扫描数 |
|---------|----------|--------|--------|--------------------|
| 个人开发者 | 5 runs | 3.7 MB | 45 MB | ~1,800 个目录 |
| 小团队（5 人） | 25 runs | 18 MB | 225 MB | ~9,000 个目录 |
| 中团队（20 人） | 100 runs | 75 MB | 900 MB | ~36,000 个目录 |
| CI 集成（每 PR+每日） | 200 runs | 150 MB | 1.8 GB | ~73,000 个目录 |
| 重载组织（100 人+CI） | 500 runs | 375 MB | 4.5 GB | ~180,000 个目录 |

### 8.3 操作退化曲线

```
操作                              无索引扫描       5K 目录   50K 目录  200K 目录
─────                             ───────────      ───────   ───────   ────────
agent_go list                     glob("task-*")    8ms      20ms      50ms
agent_go show <id>                单文件 read       0.3ms    0.3ms     0.3ms
agent_go eval cost                扫所有 metering   ~3s      ~30s      ~2min
agent_go eval quality             扫所有 meta       ~2s      ~20s      ~80s
agent_go status --watch           扫所有 meta       ~2s      ~20s      ~80s
KnowledgeStore._extract_patterns  扫所有 meta       ~3s      ~30s      ~2min
query_project_trend(repo, 30d)    扫所有+按repo过滤  ~5s      ~50s      ~5min
```

核心瓶颈：`_scan_task_dirs()` = `glob("task-*")` 全量扫描，评估 7 处、planning 1 处、CLI 3 处、TUI 1 处。每次全量 O(n) 无索引。

### 8.4 存储架构升级阈值

```
阶段         时间点       目录数    数据量   存储架构
─────        ──────      ──────    ──────   ────────
Q3 2026      <1 月        500      12 MB    纯文件系统 ✅
Q4 2026      3-6 月      5,000    125 MB   纯文件系统 ✅
2027 H1      6-12 月     20,000   500 MB   纯文件系统 ⚠️ eval 5-10s，建议轻量优化
2027 H2      12-18 月    50,000   1.2 GB   纯文件系统 🟡 仍可工作，建议目录分片
2028+        24+ 月     200,000   5 GB     纯文件系统 🟢 可工作但 >1min，建议 CSV 索引
```

### 8.5 决策结论

> **结论：2 年内不需要升级存储架构。** 纯文件系统 + JSON 方案在 50K 目录、~1GB 数据以内性能完全可接受。

**不升级的理由**：
1. 当前单次 `glob("task-*")` + 全量读 JSON 在 5K 目录下 < 3s，仍然可用
2. 引入数据库（SQLite 或更重）违背"零外部依赖"的设计原则
3. 2 年内没有组合查询需求——当前查询模式只有"按目录扫描"和"按 task_id 读单文件"

**渐进式优化路径（如未来需要）：**

| Phase | 时间 | 方案 | 改动量 | 延后拐点至 |
|-------|------|------|--------|-----------|
| P0 | 12 月后 | 目录分片 `~/.agent_go/tasks/2026/Q3/task-xxx/` | ~2h | 200K 目录 |
| P1 | 18 月后 | CSV 摘要索引（每次 pipeline 完成追加一行） | ~0.5d | 500K 目录 |
| P2 | 24 月后 | SQLite 可选聚合缓存（JSON 仍是 source of truth） | ~2d | 无限 |

**触发升级的信号**：
- `eval cost` 单次执行 > 30s（~50K 目录）
- `glob("task-*")` 返回 > 1s（~200K 目录）
- 磁盘使用 > 10 GB（~400K 目录）
- 需要按时间/模型/cost 组合查询（产品需求驱动）

---

## 9. 建设优先级

| 数据流 | 状态 | 优先级 | 价值 |
|--------|------|--------|------|
| 流 A 单次运行数据 | ✅ 已完成 | — | 基础 |
| 流 B 跨运行聚合 | ✅ 已完成（`eval` 系列） | — | 基础 |
| 流 B CI Gate | ✅ 已完成（`gate_cost`） | — | 基础 |
| 流 C 知识提取 | ⏳ 待建（KnowledgeStore） | P2 | 复利效应 |
| 流 C 事件总线 | ⏳ 待建（events.jsonl + Webhook） | P1 | 实时集成 |
| 跨项目趋势 | ⏳ 待建（`query_project_trend`） | P1 | 管理视角 |
| 外部集成查询 | ⏳ 待建（Python API 结构化返回） | P0 | 基础设施化入口 |

## 10. 当前数据契约（As-Built / M0）

### 10.1 运行事实

`meta.json` 是单任务生命周期事实，核心字段包括：

- `task_id`、`repo`、`task`、`status`。
- `base_commit`、`base_branch`、`delivery_branch`、`target_branch`。
- `subtasks[]`、`results[]`。
- `recovered_at`、`schema_version`。
- `accepted_delivery`、`failure_class`（M0 计算契约）。

`results[]` 是子任务事实，核心字段包括：

- `subtask_id`、`status`、`verify_ok`、`retry_count`。
- `commit_hash`、`branch`、`worktree`。
- `failure_reason`、`kill_reason`、`failure_class`。
- `verification_results`、`verification_state`。

### 10.2 计量事实

`metering.jsonl` 每行代表一次 API/模型/验证计量事件，至少区分：

- `role`、`actual_provider`、`actual_model`。
- `task_id`、`sub_id`、`difficulty`。
- `prompt_tokens`、`completion_tokens`、`cost_usd`、`latency_ms`。
- `result`、`fallback_reason`、`event`。
- `cost_censored` 不作为新的实际成本重复累计。

### 10.3 评估事实

Bench record 必须携带：

- `bench_schema_version`。
- `task_id`、`task_version`、`suite`、`source_batch`、`repeat`。
- `model`、`planner_model`、`judge_model`、`difficulty`。
- `failure_class`、`accepted_delivery`、`pr_created`。
- `spec_compliance`、`architecture_compliance`。

旧 `pass_rate` 和 `$ / pass` 只能用于同 suite、同 source batch 的诊断，不替代产品级 Accepted Delivery 指标。

### 10.4 数据所有权

| 数据 | 写入者 | 消费者 | 事实类型 |
|---|---|---|---|
| `meta.json` | pipeline/recover | CLI/Web/MCP/eval | 任务状态事实 |
| `result.json` | executor/pipeline | inspect/review/eval | 子任务事实 |
| `metering.jsonl` | config/api/executor | eval/metric report | 成本计量事实 |
| `verify_state.json` | executor | resume/未来 KnowledgeStore | 验证过程事实 |
| Bench JSONL | bench | models/report | 评估事实 |
| `review.json` | review | CLI/Web/PR | 人工/模型审查事实 |
