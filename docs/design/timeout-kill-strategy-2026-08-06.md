# Timeout / Kill / 成本控制优化策略评估

> 日期：2026-08-06
> 状态：**设计阶段，未落地开发**
> 代码基线：当前 `feat/s10-bench-v2` 分支
> 关联文档：[bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md)（度量有效性诊断 + timeout 根因）、[k4-cost-recalibration.md](k4-cost-recalibration.md)（成本基线）、`docs/prd.md` §产品 KPI
> 目标对齐：PRD「预算限制下，任务顺利完成、高通过率、高效率」+ 原则 #5「复杂度判断在规划阶段收敛」

## 摘要

逐行对照源码盘点后，结论与初版策略假设**显著不同**：

> **当前产品已内置 progress-aware kill（`IDLE_TIMEOUT`）、按难度缩放的 retry_timeout、以及完整的三层 cost_control（L1/L2/L3）——它们全部代码就绪，仅被 `cost_control.enabled=False` 关闭。** v3 的「57% 超时」是 **bench 测量外壳**（`_run_with_grace`）造成的，不是产品运行时的成本控制；产品运行时本身**没有任务级墙钟 timeout**。

因此优化**不是"从零造预算杀手"**，而是：① 修测量（让 `kill_reason` 可分类、修正把已完成计为失败的 bug）——这是能安全开启 cost_control 的**前置闸门**；② 补三处真实缺口（per-task 预算输入、L3 优雅降级、规划期欠分解检测）；③ 在冻结基线上开启已就绪的 cost_control。净新增工作量小且聚焦。

---

## 一、先分清三个 kill 表面（这是 v3 被误诊的根因）

timeout/kill 在本系统里存在于**三个互不相同的表面**，v2/v3 分析里的混乱源自把它们当成同一个：

| 表面 | 位置 | 性质 | 是否进真实产品 |
|------|------|------|---------------|
| **A. bench 测量外壳** | `bench.py` `_run_with_grace` / `_dynamic_timeout` / `_collect_result` | **仅测量**：用墙钟包裹一次 bench run | ❌ 不在 `agent_go run` 路径 |
| **B. 产品运行时** | `subtask.py` / `executor.py` / `pipeline.py` | 真实执行时的 kill 机制 | ✅ 用户实际遭遇 |
| **C. 成本控制** | cost_control L1/L2/L3（横跨 B） | 预算驱动的熔断 | ✅ 但 `enabled=False` |

**关键事实**：v3 的 57% 超时 = **表面 A**（bench 外壳在 `hard_timeout` 杀进程），不是表面 B/C。`cli.py:1964` 注释自证——"bench subprocess timeout → agent_go 被 SIGKILL → meta.json 停在 plan 阶段"。产品运行时（表面 B）**没有任务级墙钟**，只有子任务级机制。这意味着：**把 v3 的通过率崩塌归因于"产品成本控制太紧"是错的**——当时产品成本控制多半根本没开。

---

## 二、当前实际策略盘点（ground truth，逐条对照源码）

### 表面 B：产品运行时 kill 机制

| 机制 | 位置 | 触发条件 | 默认值 | 评估 |
|------|------|---------|--------|------|
| **空闲 kill（progress-aware）** | `subtask.py:344` `IDLE_TIMEOUT` | 纯静默无事件 | **600s（10min）** | ✅ **已实现"无进展触发"**，不是墙钟 |
| 心跳日志 | `subtask.py:354` `HEARTBEAT` | 60s 无事件打日志 | 60s | 辅助可观测 |
| 单次硬超时 | `subtask.py:340` `hard_timeout` | 墙钟到点即 kill | = retry_timeout | 用于修复重试 |
| retry_timeout（难度缩放） | `executor.py:1025-1029` | 墙钟 | `min(base×mult, 900)`，mult={easy:1, med:1.5, **hard:2.5**} | ✅ **已按难度分档**（hard 2.5×，封顶 900s） |
| goal 看门狗 | `subtask.py:348-360` | 轮数/时长 | `MAX_GOAL_TURNS=20` / `GOAL_TIMEOUT=600s` | ✅ 双重保险 |
| SIGINT/SIGTERM 转发 | `pipeline.py:179-195` | 信号 | 存 meta + 转发子进程 | ✅ cooperative |

**结论：表面 B 已经是"progress-aware + 难度缩放"的设计**，与初版策略假设的"纯墙钟、按子任务数"不符——那个描述只对**表面 A（bench 外壳）**成立。

### 表面 C：cost_control 三层（全部就绪，全部关闭）

| 层 | 位置 | 机制 | 触发动作 | 配置 | 状态 |
|----|------|------|---------|------|------|
| **L1 单调用** | `subtask.py:153-162` | 给 claude 注入 `--max-budget-usd`（按难度） | claude 原生预算控制 | `per_subtask_budget_usd={easy:0.10,med:0.20,hard:0.50}` | 代码就绪 / enabled=False |
| **L2 子任务累计** | `executor.py:958-974` | 跨重试累计 cost ≥ `单次×subtask_multiplier` | **停止修复重试**（final fail） | `subtask_multiplier=2.5` | 代码就绪 / enabled=False |
| **L3 任务熔断** | `pipeline.py:275-300` | 任务累计 cost ≥ `max_budget_usd` | **剩余子任务标记 blocked** | `max_budget_usd=0.50`, `on_exceed=stop` | 代码就绪 / enabled=False |

三层都有 `write_censored_event` 审计落盘。**整套预算控制已实现，只差"开启 + 基线 + 度量可信"。**

---

## 三、优化策略评估（逐条对照代码）

把 [timeout 根因分析](bench-metric-validity-2026-08-06.md#四专题timeout-与通过率矛盾的根因与策略) 提出的 5 条策略，逐条对照上面的 ground truth：

| # | 策略 | 现状 | 净缺口 | 工作量 |
|---|------|------|--------|--------|
| 1 | 预算升为一等信号，timeout 降为安全网 | **L1/L2/L3 已实现预算控制**；表面 B 已是 progress-aware（timeout 本就是安全网） | (a) 仍 `enabled=False`；(b) 无 per-task 预算输入；(c) L3 靠 block 不靠降级 | 中 |
| 2 | kill 改"无进展"触发，非墙钟 | **`IDLE_TIMEOUT=600s` 已是无进展触发** | 纯静默判定偏粗（无法区分"深度思考"与"循环空转"），可加 token 速率/文件变更信号 | 小（增强） |
| 3 | 时间预算按难度分档 | **retry_timeout 已按难度（hard ×2.5）** | 仅 bench 外壳 `_dynamic_timeout` 按子任务数（**测量侧**需改，见策略 4 尾） | 小（bench 侧） |
| 4 | kill 分类打标（stuck/over_budget/cleanup_race）+ 修 `_collect_result` | **完全缺失**——kill 发生但结果不记原因；`_collect_result` 把 cleanup_race 计为失败 | 需新增 `kill_reason` 字段贯穿 B→A；修 `_collect_result` 的 aborted 分支 | **中（前置必做）** |
| 5 | 规划期欠分解检测 | **完全缺失**——planner 无 difficulty×子任务数守卫 | Plan 阶段加约束：高难度 + 低子任务数 → 再分解或升模型 | 中 |

**评估总判断**：
- 策略 2、3 的核心**早已存在**，无需重建，仅需小幅增强/迁移到 bench 侧。
- 策略 1 的预算控制**主体已存在**，缺口在"开启条件 + 输入 + 降级方式"。
- **真正的净新增工作是策略 4 和 5**，其中**策略 4 是一切的前置**——不修度量，就无法判断 cost_control 该不该开、开多紧，也无法验证开启后的效果。

---

## 四、净新增工作（gaps，聚焦）

按"是否阻塞其他工作"排序：

### G1. `kill_reason` 分类打标（阻塞项，最高优先级）
**问题**：当前 kill 发生在表面 B（idle/hard_timeout/L2/L3），但结果记录不区分原因。`_collect_result`（bench 表面 A）把所有 `timed_out` 一律计失败，于是 cleanup_race（已完成、收尾被杀）被误判——这是 [度量诊断](bench-metric-validity-2026-08-06.md) 里 65 条假失败、v3 通过率被腰斩的根因。

**设计要点**（不写代码，仅定 spec）：
- 在子任务/任务结果里加 `kill_reason ∈ {none, stuck, hard_timeout, over_budget_l2, over_budget_l3, cleanup_race, interrupted}`。
- `stuck` = `IDLE_TIMEOUT` 触发；`hard_timeout` = retry_timeout 触发；`over_budget_l2/l3` = cost_control 触发；`cleanup_race` = 子任务全 completed+verified 但进程被杀。
- `cleanup_race` 在 `_collect_result` 里**计为通过**（修正假失败）。
- metering/`write_censored_event` 已有 level 字段，可复用为 kill_reason 载体。

### G2. 修 `_collect_result` 的 aborted 分支（与 G1 同源）
**问题**：`all_passed` 在 aborted 分支前冻结，`binary_pass` 用旧值、`completed` 用新值，二者矛盾（详见度量诊断缺陷 1）。且 `all([])==True` 陷阱让"全失败"被判 binary_pass=True。

**设计要点**：`binary_pass` 移到 aborted 分支**之后**计算；`all_passed` 空集判 `False`；`pass_rate` 分母改用计划子任务数（meta.subtasks）而非 `len(results)`。

### G3. per-task 预算输入
**问题**：`max_budget_usd` 是全局 config，用户无法对单个任务设预算。与 PRD「预算限制下」诉求脱节——用户要的是"这次任务别超 $0.30"，不是全局均值。

**设计要点**：支持 `--budget` CLI 参数 / Task Spec 字段 → 注入为该任务的 L3 上限，覆盖 config 默认。结合 PRD 的 Spec 准入（S11-P0）天然落点。

### G4. L3 优雅降级（模型降档），而非硬 block
**问题**：L3 现在把剩余子任务标 `blocked`（硬停）。预算压力下更优解是**降级**（切便宜模型继续），保留部分产出，而非全弃。

**设计要点**：`on_exceed` 增加 `degrade` 选项 → 剩余子任务切 `worker_models` 下一档（如 hard→medium 模型）继续调度，并在结果里标 `degraded=True`。需配合 `worker_models_fallback` 已有的升级表（对称设计一个降级表）。

### G5. 规划期欠分解检测
**问题**：高难度任务被欠分解成 1-2 个长子任务 → 单子任务耗时长 → 撞 retry_timeout（即便 hard ×2.5）。根因在规划期，不在执行期。

**设计要点**：Plan 后加守卫——`difficulty ∈ {hard}` 且 `len(subtasks) < 阈值` → 触发再分解或强制升 worker 模型档。落地 PRD 原则 #5。

### G6. bench 侧 `_dynamic_timeout` 改按难度（测量侧）
**问题**：bench 外壳的 `_dynamic_timeout = max(YAML, 子任务数×150+120)` 按子任务数，但耗时由难度驱动——这是"控制变量指错方向"的实证（见度量诊断根因 A）。

**设计要点**：改为 `max(YAML, 难度基准×系数 + 缓冲)`，或与 retry_timeout 的难度倍数对齐。**仅影响测量，不影响产品**，但关系到 KPI 可信度。

---

## 五、落地路线（分阶段，本文止于设计）

> 严格遵循用户要求：**本节是路线设计，不含代码实现。**

**Phase 0（前置，必须最先）— 修测量**
- G1（kill_reason 打标）+ G2（`_collect_result` 修正）
- 完成后用新口径重算 v2/v3/v4，**确认 cost_control 开启前的真实通过率/成本基线**
- 这是 chicken-egg 的破局点：度量诊断说"开启 cost_control 前须立基线"，但立基线需要可信度量 → 必须先修度量

**Phase 1 — 开启已就绪的 cost_control**
- 在 Phase 0 冻结的基线上，小范围开启 L1/L2/L3（`enabled=True`）
- 引入 G3（per-task `--budget`），让用户能对单任务设约束
- 观察 kill_reason 分布，校准 `per_subtask_budget_usd` / `max_budget_usd` / `subtask_multiplier`

**Phase 2 — 补降级与规划守卫**
- G4（L3 `degrade` 降级路径）
- G5（规划期欠分解检测）
- G6（bench `_dynamic_timeout` 按难度，顺带修测量）

**Phase 3 — stuck 误杀规避（详见第八节）**
- `IDLE_TIMEOUT` 从"纯静默单维"升级为**多维活性**（claude 事件 ∨ worktree 文件变更 ∨ 进程树 CPU）+ **grace 复检门**，把"在干活却被当卡死"的误杀压到接近 0；并收窄 stuck-kill 的职责范围（让 budget + 轮数上限管常见情况）

---

## 六、风险与开放问题

| 风险/问题 | 说明 | 待决策 |
|-----------|------|--------|
| **chicken-egg：基线 vs 度量** | cost_control 开启前要基线，基线要可信度量，度量要 G1/G2 | Phase 0 必须先做，不可跳 |
| **L2/L3 阻断会污染通过率分母** | 即便度量修好，开启 cost_control 后 `blocked` 子任务仍会拉低 pass_rate——这是**预期的预算约束行为**，不是 bug | 需在 KPI 里把"预算熔断 blocked"与"能力失败"分开报（与 G1 的 kill_reason 配合） |
| **IDLE_TIMEOUT 单维误杀** | 纯静默判定在 claude 阻塞慢工具时误杀（`pytest`/build 跑 >600s 时 claude 不出事件）；反过来"卡循环但有零星输出"又漏判 | 第八节多维活性 + grace 复检门；收窄 stuck-kill 职责（budget + 轮数管常见情况） |
| **per-task 预算的 UX** | `--budget` 放 CLI 还是 Task Spec 字段？默认值取 config 还是按难度？ | 倾向 Spec 字段（S11 准入）+ CLI 覆盖 |
| **L3 降级 vs block 的策略选择** | 降级能保部分产出但可能引入质量不一致；block 干脆但浪费已花预算 | 建议 `on_exceed` 默认 `degrade`，`stop` 可选 |
| **bench 外壳与产品不一致** | 表面 A 按子任务数、表面 B 按难度，两套 timeout 逻辑分叉 | G6 统一到难度口径，避免测量与实际脱节 |

---

## 七、kill 语义规格：何时真正 kill 任务

"任务被 kill" 要分**两层**理解——"进程被终止"和"任务被判失败"是两件事，v2/v3 分析的混乱正源于把两者等同。本节给出可直接对照实现的判定规格。

### 第一层：进程级终止（物理 kill）

合法的进程终止只有五种触发，语义各不相同：

| 触发 | 位置 | 条件 | 性质 |
|------|------|------|------|
| **空闲 kill** | `subtask.py:344` `IDLE_TIMEOUT` | **600s 纯静默**（无 stdout/stderr 行） | progress-aware；**唯一约束首次执行的时限** |
| **重试墙钟** | `subtask.py:340` `hard_timeout` | 仅**修复重试**到 `retry_timeout`（hard ×2.5，封顶 900s） | **首次执行 hard_timeout=0，无墙钟** |
| **goal 看门狗** | `subtask.py:348` | goal 循环 >600s 或 >20 轮 | 双保险 |
| **外部信号** | `pipeline.py:188` | 用户 Ctrl-C / bench 外墙钟 → SIGTERM 转发 | 人为 / 测量 |
| **预算停止** | L2/L3 | 累计 cost 超阈 | **非 proc.kill，是停止后续工作**（L2 停重试 / L3 停调度）；L1 委托 claude 自身预算 |

> 关键事实：**首次执行没有墙钟上限**——一个持续缓慢产出的任务理论上可无限跑，只有"600s 彻底没动静"才会被杀。墙钟只约束修复重试。

### 第二层：任务级判失败（语义 kill——设计改的就是这层）

`kill_reason` 取值与判定（贯穿运行时 → 度量，由 G1 落地）：

| kill_reason | 算任务失败吗 | 理由 |
|-------------|-------------|------|
| `stuck_confirmed` | ✅ **是** | 多信号 + grace 复检确认无进展（见第八节） |
| `hard_timeout` | ✅ **是** | 修复重试墙钟到点且工作未完成 → 能力不足 |
| `cleanup_race` | ❌ **计为通过** | 全子任务 completed+verified，仅收尾被杀（修 G2） |
| `over_budget_l2` / `over_budget_l3` | ❌ **单独报预算结果** | 用户设定的预算约束生效，非能力问题 |
| `interrupted` | ❌ 中断，非失败 | 外部信号，可 resume |
| infra（cost=0 / API 故障） | ❌ infra 故障 | 基础设施，非模型/产品 |

### 一句话原则

> **一个正在产出（出 token / 改文件 / 跑工具）的任务，永远不该被墙钟杀。** 合法终止只有两种理由：**无进展**或**用户预算到限**；其中只有**无进展**计为任务失败，**预算到限**是约束按预期生效，**已完成但被杀**必须计为通过。

---

## 八、stuck 误杀规避：多维活性 + grace 复检门

第七节的 `stuck` 判定当前实现为"600s 纯静默即杀"（`subtask.py:208` 任意非空 stdout/stderr 行重置 `last_ts`，`subtask.py:343` 超时即 `proc.kill()`）。它的致命盲点：**claude 在等待工具返回时是静默的**——agent 调 `pytest`/build 跑 >600s，claude 阻塞等待、不出任何事件，于是被当卡死杀掉。**任务在干活却被误杀。**

### 根因：活性信号一维化

| claude 静默场景 | 在干活吗 | 当前判定 |
|----------------|---------|---------|
| 被慢工具阻塞（build/test/install >600s） | ✅ 在干活 | ❌ 误杀 |
| 等 API 响应（网络慢/限流） | ⚠️ infra | ❌ 误判 stuck |
| 真死锁/挂起 | ❌ 没干活 | ✅ 正确杀 |
| 生成 token 中 | ✅ 在干活 | ✅ 事件密集，安全 |

> 注意：G2（`cleanup_race` 计为通过）**救不了这个**——IDLE_TIMEOUT 误杀发生在工具执行中（未 commit），不满足 cleanup_race，仍是假失败。**修 kill 分类只解决"已完成被杀"的度量问题；"在干活被杀"的判定错误必须改 kill 逻辑本身。** 两条独立的修复线。

### 方案：多维活性 + grace 复检门（纯 stdlib 可实现）

活性信号从单维（claude 事件流）升级为**正交多维**，任一活跃即续命：

| 信号 | 实现方式 | 活跃时覆盖 | 失效场景 |
|------|---------|-----------|---------|
| **S1 claude 事件流**（现有） | stdout/stderr 出行 | 生成 token、工具派发/返回 | claude 阻塞等工具 |
| **S2 worktree 文件变更**（新增） | 主循环每 N 秒 `git status --porcelain` + mtime 快照 | build/测试写产物（claude 静默但磁盘在动） | 纯读、写到 worktree 外 |
| **S3 进程树 CPU 活性**（新增） | `ps -o pcpu` 遍历 `active_pids` 里 claude PID 的子孙 | CPU-bound 工作（编译、计算、下载解压） | 纯 I/O wait |

**正交性**：S1 死时（等工具）S2/S3 活；S2 死时（纯计算后批量写）S3 活。三者同时长时间死才可能是真卡死。接点都对得上——S2 用 `git status`（worktree 为 agent_go 拥有）；S3 用 `subprocess` 调 `ps` 遍历 `active_pids`（claude PID 已在 `subtask.py:192` 追踪）。

### grace 复检门（结构性避免误杀）

把"到点即 `proc.kill()`"改成**两阶段确认**——单次静默采样永不直接杀：

```
T=600s 静默触发 → 不杀，转"待裁定宽限态"
  ├─ 快照 S2（git status）+ S3（子孙 CPU 时间累计）
  ├─ 等 grace（如 120s）
  └─ 复检：
       · grace 内 S2 有变更 → 假警报，复位计时器，继续
       · grace 内 S3 有 CPU  → 假警报，复位计时器，继续
       · 两者皆死           → 确认 stuck，记 kill_reason=stuck_confirmed，kill
```

单次静默 → `stuck_suspected`（应几乎不再出现）；grace 复检通过 → `stuck_confirmed`。

### 收窄 stuck-kill 职责

产品运行时已有两个边界——**goal 轮数上限**（`MAX_GOAL_TURNS=20`）和**预算三层**。一个"卡住"的任务要么空转迭代（轮数挡）、要么烧钱（预算挡）。stuck-kill 唯一不可替代的场景是**"卡在单个工具调用上、既不迭代也不烧钱"**——窄场景。策略：让 budget + 轮数管常见情况，stuck-kill 退成带多信号门 + grace 复检的后盾。

### 不可约剩余（诚实边界）

纯外部 I/O 挂起（既不写 worktree、不耗 CPU、claude 不出事件，如工具内 hang 死的网络请求）观测上与真死锁不可分。这种情况**杀 + 留 checkpoint 供 resume 是正确处置**——它本就该被中断，不算误杀。本方案能把"误杀真在干的活"压到接近 0，剩余的是"观测不可分"的合理中断。

### 落地接点（实现时，本文不含代码）

1. **S2 worktree 活性**：`subtask.py:341` 主循环 `while proc.poll()` 内每轮加 `git status --porcelain` + mtime 快照，并列维护 `last_filechange_ts`。
2. **S3 进程树 CPU**：复用 `active_pids`，宽限期 `subprocess` 调 `ps` 遍历子孙，累计 CPU 增量。
3. **grace 复检门**：把 `if idle > IDLE_TIMEOUT: proc.kill()` 改为"进入 grace→复检 S2/S3→确认才杀"，并记 `kill_reason`。

---

## 九、与现有文档/代码的关系

- **[bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md)**：本文是其第四节 timeout 专题的**可执行延伸**——根因诊断 → 策略评估 → 落地路线。G1/G2 直接对应那里的"度量层缺陷 1 + 问题 4"。
- **[k4-cost-recalibration.md](k4-cost-recalibration.md)**：提供成本驱动因素（难度 4-5×、子任务数线性），是 G5/G6 难度口径的依据。
- **`config.py` cost_control 块**：三层配置已就绪，Phase 1 仅需 `enabled=True` + 校准值。
- **`prd.md` §产品 KPI**：K4（$/pass）目标 ≤$0.05 与实测差 7-14×，本文不直接修目标，但 G3（per-task 预算）把"够不着的全局目标"转为"可执行的单任务约束"。
