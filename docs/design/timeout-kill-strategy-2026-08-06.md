# Timeout / Kill / 成本控制优化策略评估

> 日期：2026-08-06
> 状态：**G1/G2 已落地（2026-08-07，S12-P0 度量修复）；G3/G4/G8 已落地（2026-08-07，S12-P1 per-task 预算 + 降级 + kill_reason 感知）；G5/G6 已落地（2026-08-07，S12-P2 欠分解检测 + 按难度 timeout）；S12-P3 多维活性 + grace 复检门已落地（2026-08-07，subtask.py S2 worktree 文件活性 + S3 进程树 CPU 活性 + STUCK_GRACE_SEC=120 复检，慢工具不再被误杀）；G7 待后续 Phase**
> 代码基线：`feat/s12-metric-fix` 分支
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
- **持久化时机（review 修订）**：当前 S12-P0 实现是 `_collect_result` **事后推断**（方案 B：从 timed_out + per_subtask + cost 反推），对 SIGKILL 已鲁棒。但第十节 runtime failure_class **驱动重试决策**时需 kill_reason 在 kill 之前可得——采用方案 A（kill 决策点先写 `kill_state`/metering 事件 → SIGTERM 给 grace 落盘 → 仍不退才 SIGKILL）+ 方案 B 反推兜底。**SIGKILL 事件可能丢失，fallback 反推是 mandatory。**

### G2. 修 `_collect_result` 的 aborted 分支（与 G1 同源）
**问题**：`all_passed` 在 aborted 分支前冻结，`binary_pass` 用旧值、`completed` 用新值，二者矛盾（详见度量诊断缺陷 1）。且 `all([])==True` 陷阱让"全失败"被判 binary_pass=True。

**设计要点**：`binary_pass` 移到 aborted 分支**之后**计算；`all_passed` 空集判 `False`；`pass_rate` 分母改用计划子任务数（meta.subtasks）而非 `len(results)`。

### G3. per-task 预算输入
**问题**：`max_budget_usd` 是全局 config，用户无法对单个任务设预算。与 PRD「预算限制下」诉求脱节——用户要的是"这次任务别超 $0.30"，不是全局均值。

**设计要点**（review 修订）：
- 支持 `--budget` CLI 参数 / Task Spec 字段 → 注入为该任务的 L3 上限，覆盖 config 默认。结合 S11 Spec 准入天然落点。
- **默认值动态化**：不用全局固定值（如 0.50），而用 `Σ(per_subtask_budget_usd[difficulty] × subtask_multiplier × len(subtasks))` 动态计算——否则 hard 任务 10 子任务与 easy 任务 2 子任务共用同一 L3 上限，导致 hard 过早熔断。
- **budget_mode 三态**（Spec 字段）：`strict` = 超预算 block；`degrade` = 触发 G4 降级；`ignore` = 关 L3（仅 L1/L2 生效）。把 G3/G4 串成一个连贯的预算策略。

### G4. L3 优雅降级（模型降档），而非硬 block
**问题**：L3 现在把剩余子任务标 `blocked`（硬停）。预算压力下更优解是**降级**（切便宜模型继续），保留部分产出，而非全弃。

**设计要点**：`on_exceed` 增加 `degrade` 选项 → 剩余子任务切 `worker_models` 下一档（如 hard→medium 模型）继续调度，并在结果里标 `degraded=True`。需配合 `worker_models_fallback` 已有的升级表（对称设计一个降级表）。

**降级质量门（review 修订）**：降级模型可能 verify 必败（如 hard 任务 Opus→Sonnet 断崖），此时 degrade 只是"延长死亡时间"而非保产出。需加安全阀：
- 降级后子任务 `max_retries` 降为 **1**（不无限烧降级模型的钱）；
- 降级后**连续 N 个子任务 verify 失败 → 自动回退 `stop`**（不再降级烧钱）；
- 结果标 `degraded=True`，让最终验收人知道"这部分是便宜模型做的，需重点 review"。

### G5. 规划期欠分解检测
**问题**：高难度任务被欠分解成 1-2 个长子任务 → 单子任务耗时长 → 撞 retry_timeout（即便 hard ×2.5）。根因在规划期，不在执行期。

**设计要点**：Plan 后加守卫——`difficulty ∈ {hard}` 且 `len(subtasks) < 阈值` → 触发再分解或强制升 worker 模型档。落地 PRD 原则 #5。

**阈值分阶段（review 修订）**：硬编码阈值会误杀"确需少量子任务的 hard 任务"（如给现有模块加单点功能）。分两版：**V1** 阈值 = `difficulty_base_subtasks[hard]=3`（硬编码，快速落地）；**V2** 从 `verify_state.json` 历史重试率学习——"hard 任务中子任务数 ≤N 的 retry 率是否显著高于 >N 的"，数据驱动定阈值。

### G6. bench 侧 `_dynamic_timeout` 改按难度（测量侧）
**问题**：bench 外壳的 `_dynamic_timeout = max(YAML, 子任务数×150+120)` 按子任务数，但耗时由难度驱动——这是"控制变量指错方向"的实证（见度量诊断根因 A）。

**设计要点**（review 强调）：改为 `max(YAML, 难度基准 × mult + 缓冲)`，**mult 直接复用 retry_timeout 的难度倍数表 `{easy:1, med:1.5, hard:2.5}`**——保持测量侧与执行侧口径一致，避免"bench 按子任务数、产品按难度"两套逻辑分叉制造噪声。**仅影响测量，不影响产品**，但关系到 KPI 可信度。

### G7. infra/API 指数退避重试（验证循环）—— 最高 ROI 小改动
**问题**：claude 因 API 故障 / cost=0 退出时，验证循环**立即重试 ×3**——若 API 持续宕机，3 次重试几秒内锤同一个不可用端点，浪费且无益。infra 故障多为瞬时，应"等"而非"锤"。

**设计要点**（review 修订：按状态码差异化，非统一退避）：对 cost=0 / API-error 类 claude 退出，**按 HTTP 状态码 / error subtype 拆分退避表**：

| 信号 | 策略 |
|------|------|
| **429 rate-limit** | 尊重 `Retry-After` 头，无则短退避 |
| **529 / 503 overload** | 指数退避，**短基线**（10s / 20s / 40s） |
| **网络超时 / DNS** | 指数退避（30s / 60s / 120s） |
| **401 / 403 鉴权失败** | **零退避，立即停 + 告警**（见第十一节 #3 陷阱） |

cost≈0 故退避几乎免费；直接服务 PRD 及格线"周五 run、关机走人、周一 merge"——否则一次凌晨 API 抖动废掉整夜无人值守任务。退避期间做健康探测（第十节），恢复即重试，不死等定时器。

### G8. 验证循环 kill_reason 感知（不重试预算熔断）
**问题**：当前验证循环不区分 kill_reason——一个因 L2 预算熔断而停的子任务，仍可能被验证失败触发重试，**花更多钱在已超预算的任务上**，违背预算约束本身。

**设计要点**：retry 前检查 kill_reason / cost_control 熔断标记——`over_budget` 类直接判 Failed、不进重试；`cleanup_race` 不重试（已成功）；`infra` 走 G7 退避。与 G1（kill_reason 贯穿）配合。

**状态机联动（review 修订）**：`kill_reason` 是子任务生命周期事件的 **mandatory 字段**——验证循环状态机：`subtask 结束 → 写 kill_reason → 验证循环读取决策`：`over_budget`/`cleanup_race` 短路（失败/成功，不 verify）；`infra` 走 G7 退避；`stuck`/`hard_timeout` 正常 verify 但 max_retries 可能已耗尽。**验证循环不启动 verify，除非 kill_reason 已解析**（G1 方案 A 的写入 + 方案 B 的反推二选一可得）。

---

## 五、落地路线（分阶段，本文止于设计）

> 严格遵循用户要求：**本节是路线设计，不含代码实现。**

**Phase 0（前置，必须最先）— 修测量 + stuck 快速补丁**
- G1（kill_reason 打标）+ G2（`_collect_result` 修正）
- **+ S2 快速补丁**（review 修订）：仅加 worktree 文件变更检测（实现简单、低风险），先把最明显的"等 pytest/build"误杀压下来——stuck 误杀独立于 cost_control，是产品运行时体验痛点，不必等 Phase 3 全多维方案
- 完成后用新口径重算 v2/v3/v4，**确认 cost_control 开启前的真实通过率/成本基线**
- 这是 chicken-egg 的破局点：度量诊断说"开启 cost_control 前须立基线"，但立基线需要可信度量 → 必须先修度量

**Phase 1 — 开启已就绪的 cost_control + 无人值守鲁棒性**
- 在 Phase 0 冻结的基线上，小范围开启 L1/L2/L3（`enabled=True`）
- 引入 G3（per-task `--budget` + 动态默认 + budget_mode），让用户能对单任务设约束
- **G7（infra/API 差异化退避）前置到此**（review 修订）：它是"开启 cost_control 后无人值守"的命门——cost_control 开了若没有 infra 退避，一次 API 抖动仍废掉整夜。实现独立、ROI 极高，不该等 Phase 4
- G8（验证循环 kill_reason 感知，依赖 Phase 0 的 G1，顺带落地）
- 观察 kill_reason 分布，校准 `per_subtask_budget_usd` / `max_budget_usd` / `subtask_multiplier`

**Phase 2 — 补降级与规划守卫**
- G4（L3 `degrade` 降级路径 + 降级质量门）
- G5（规划期欠分解检测，V1 硬编码阈值）
- G6（bench `_dynamic_timeout` 复用 retry_timeout 难度倍数，顺带修测量）

**Phase 3 — stuck 误杀规避（详见第八节）**
- `IDLE_TIMEOUT` 从"纯静默单维"升级为**多维活性**（claude 事件 ∨ worktree 文件变更 ∨ 进程树 CPU）+ **grace 复检门**（Phase 0 的 S2 快速补丁已先行，此处补全 S3 进程树 CPU + grace 复检门），把"在干活却被当卡死"的误杀压到接近 0

**Phase 4 — 自适应重试（长期）**
- metering + kill_reason + 重试结果 → "失败类 × 策略 → 历史成功率"学习，按命中率路由（KnowledgeStore / H3-2 经验分类）；从规则版（前十节）升级为经验版

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
| **`git status` 大 monorepo 性能（review）** | 第八节 S2 用 `git status --porcelain` 每 N 秒扫 worktree，大型 monorepo（10万+文件）可能 1-3s/次，反噬主循环 | 限定 `git status --porcelain <关心子目录>`，或用 `inotify`/`fsevents` 监听；spec 标注"性能敏感场景需优化" |
| **`ps` 遍历子孙跨平台（review）** | 第八节 S3 用 `ps` 遍历 `active_pids` 子孙——macOS 与 Linux 的 `ps`/`--ppid` 语法不通用，"纯 stdlib 可实现"过乐观 | 提供平台抽象层 `get_process_tree_cpu()`，明确支持矩阵（macOS/Linux），或用 `pgrep -P` |
| **cleanup_race 写入竞态（review）** | 第七节定义需"全子任务 completed+verify_ok"，若 SIGKILL 落在二者写入之间会漏判 | 实际 `meta.json` 原子写使二者在同一 snapshot 一致，竞态窗口基本不存在；防御性可放宽为"全 completed-or-verified 且无 failed" |
| **`--auto-resume` 子任务级 checkpoint（review）** | 任务级 `meta.json` 难支持并发/细粒度恢复，resume 可能重跑已完成子任务 | 现有 per-subtask `verify_state.json` + commit 边界已提供子任务级 checkpoint；`--auto-resume` 应基于它而非新造 |

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

## 九、重试策略：何时该、何时不该

> 触发问题：任务 Killed 或异常退出后，是否有必要自动重试？结论——**多数场景已被覆盖或不该重试，只有 infra 退避 + kill_reason 感知两处净增益。**

### 先厘清"死亡"的三个层次（"自动重试"指哪层）

| 层次 | 触发 | 当前是否自动重试 |
|------|------|----------------|
| **claude 子进程被杀**（IDLE 600s / retry_timeout / goal 看门狗） | `subtask.py` kill 机制 | **是**——被杀的 claude 不短路失败，走验证；验证失败则验证循环重跑（≤`max_retries`，`worker_models_fallback` 升级模型） |
| **子任务最终失败**（`max_retries` 用尽 / L2 熔断） | executor 判 Failed | 否（正确——边际递减） |
| **agent_go 整进程异常退出**（SIGKILL / crash / 断电） | OS / OOM | 否——靠 `recover` + `resume`（手动） |

**关键事实**：第一层已经自动重试了。多数人问"要不要重试"时默认指后两层，但 claude 级 kill 其实已被验证循环兜住——这是评估的起点，也意味着"加泛化的 kill 后重试"是多余的。

### 按 `kill_reason` 逐场景评估

| kill_reason | 当前处理 | 重试价值 | 重试成本 | 建议 |
|-------------|---------|---------|---------|------|
| **cleanup_race**（超时但全完成已验证） | 计 Completed | ❌ 0（已成功） | — | **不重试** |
| **over_budget_l2/l3**（预算到限） | 停止 / blocked | ❌ 负（重试=在超预算任务上花更多钱，违背预算约束本身） | 高 | **坚决不重试** |
| **infra**（cost=0 / API 故障） | 验证失败 → 立即重试 ×3 | ✅ 高（多为瞬时，且 cost≈0 重试几乎免费） | 低 | **应重试，但加退避**（G7） |
| **stuck_or_hardtimeout**（真卡死） | 验证失败 → 重试 + 升级模型 | 🟡 低-中（看是否瞬时） | 高 | **不盲目重试**；升级/降级已有，靠预防（G5） |
| **interrupted**（外部信号 / SIGKILL 整进程） | recover + resume | N/A（进程已死，无法进程内重试） | — | **跨进程续跑**（见下） |

### 核心判断：重试只在"改变了什么"时才有价值

> **用相同输入（同 prompt、同模型）重试一个卡住的任务，几乎必然以同样方式卡住。** 这是 `stuck` 类重试收益低的根本原因——不是"再试一次"，是"什么都没变"。

因此**有价值的重试必须改变至少一个变量**：
- 换模型（`worker_models_fallback` **已有**——retry 时升级）
- 缩范围 / 换分解（规划期欠分解检测——G5 待做）
- 退避等待（infra 瞬时故障——G7 当前缺失）

当前对 `stuck` 的重试已有"换模型"，但**没有"换分解"和"退避"**——前者靠预防，后者是 G7。

### 成本约束下，任何自动重试必须满足四条件

| 条件 | 现状 |
|------|------|
| **有界**（max_retries + L2 预算封顶 `per_subtask_budget×2.5`） | ✅ 已有 |
| **kill_reason 感知**（不重试 over_budget / cleanup_race） | ❌ 需补（G8） |
| **升级而非重复**（retry 必变模型/范围） | 🟡 部分（模型有，范围无） |
| **边际停止**（retry 预期增量成本 > 成功价值×剩余预算时停） | ❌ 没有（当前固定 max_retries，不看性价比） |

### 异常退出（SIGKILL / crash）单独说

整进程被杀**无法在进程内自动重试**（进程已死）。当前机制正确：`meta.json` 原子写 + SIGTERM handler 尽力保存 → `recover` 从 worktree 重判状态 → `resume` 续跑未完成子任务；**commit 是唯一完成边界**保证 resume 不重跑已完工部分。要"自动"恢复只有两条路：

- **外部守护**（systemd / GitHub Actions restart-on-failure / launchd）：工业标准，agent_go 不该自管进程生命周期。文档化此模式即可。
- **`--auto-resume` 启动选项**：agent_go 启动时检测 stale interrupted 任务则自动续跑。是**产品功能**，改变"一次 run = 一次进程"心智模型，且与并发/交互模式有交互——值得做但需单独设计，非"加重试"。

### 结论

**要不要自动重试？分场景：**

- ✅ **明确该做（唯一有净收益的新增）**：**infra/API 指数退避**（G7）——把"立即重试 ×3"改成对 cost=0/API-error 的 claude 退出做指数退避。高 ROI，服务无人值守鲁棒性。
- ✅ **配套必做**：**验证循环 kill_reason 感知**（G8）——retry 前过滤 `over_budget`（不重试）与 `cleanup_race`（已成功），避免烧钱。
- ❌ **明确不做**：泛化的"kill 后自动重试"（claude 级已有）、over_budget 重试、cleanup_race 重试、`max_retries` 后结构性 failed 重试、整进程 SIGKILL 的进程内重试。
- 🟡 **不靠重试，靠别的**：`stuck` → 预防（G5）+ 升级/降级（已有）+ 多维活性 grace 门（S12-P3，少误杀就少需重试）；异常退出 → `recover`+`resume`（已有）+ 可选 `--auto-resume`（产品决策）。

**一句话**：当前系统对 claude 级 kill 已有有界、升级式自动重试，**不需要再加泛化的"kill 后自动重试"**；真正值得做的是 **G7（infra 退避）+ G8（kill_reason 感知重试门）** 两件具体事，其余场景要么已覆盖、要么重试弊大于利。

### 场景适配：无人值守过夜（PRD 及格线）

> PRD 及格线："周五 4 点 run、关机走人、周一信心满满 merge"。无人值守 = 没人处理瞬时故障、没人批准花费、没人重启进程。当前重试机制对**逻辑失败**有利，对**瞬时基础设施故障**不利——后者破坏力在夜间被放大。

**有利（夜间逻辑失败兜底）**：验证循环重试（无需人在场）+ `worker_models_fallback` 重试升模型 + `block_on_failure` 级联隔离 + `recover`/`resume` 防崩溃丢工。

**不利（夜间命脉缺口）**：

| 缺口 | 夜间后果 |
|------|---------|
| **infra 无退避（G7）** | 凌晨 API 抖动 5min → 立即重试 ×3 全锤同一不可用端点 → 子任务 Failed。本会 2min 自愈的瞬时故障废掉一个子任务 |
| **cost_control 默认关** | 无人批准花费却无预算护栏；hard 任务重试循环可能夜间超预期烧钱 |
| **无 `--auto-resume`** | 进程崩溃 / 机器睡眠 → 停 `interrupted`，第二天来是半成品、需手动 `resume` |

**重试会反过来伤场景吗**：不会——`max_retries=3`（难度封顶）已把单子任务重试封到 ~4× 单次成本，`block_on_failure` 阻止级联烧钱。会伤的是"无界重试"（不存在）和"默认无预算护栏"（cost_control 缺口，非重试本身的错）。

**验收侧（第二天看结果）反而变好**：S12-P0 的 `kill_reason` 让早上能一眼分清 `cleanup_race`（其实成功，不用管）/ `infra`（瞬时，值得 `resume`）/ `stuck`+`over_budget`（真问题，需人决策）；配合 `notify_event` 推送 + `review --task` 聚合 diff，验收闭环是通的。**问题不在"看不清结果"，而在"结果不够常是完工"。**

**让"下班提交、上班验收"可靠的优先级**：

1. **G7（infra 指数退避）**——必需品，非锦上添花。直接解决"一次夜间抖动废一个子任务"，是该场景最高 ROI 改动。
2. **无人值守默认开 L3 预算护栏**（或 `--unattended` 隐含开 cost_control）——给"走人"加硬预算保险。
3. **`--auto-resume`**（产品决策）——崩溃/睡眠后自动续跑，让"来上班看到的是完工"。

---

## 十、失败判定与差异化处理

> 触发问题：能否对不同失败执行不同重试策略？结论——能，且应把今天"均匀重试"升级为"分类-施策"。本节给判定信号、policy 表、日志缺口与环境恢复机制。

### 判定信号（现成为主，缺两条）

判定不靠新传感器，多数字段已在采。逐类判定逻辑：

| failure_class | 判定逻辑（真实字段） | 信号源 | 现成？ |
|---------------|---------------------|--------|--------|
| **infra/API** | `cost_usd≈0` 且（`final_rc≠0` 或 token 极低）→ 早夭没花钱 | metering + subtask 结果 | ✅ |
| **verify 逻辑失败** | `final_rc==0`（claude 成功）但 verification_results 有非零 exit + 有 diff | verify_results | ✅ |
| **stuck/timeout** | 命中 `headless_hard_timeout` / goal_timeout / IDLE 杀 | log_event + 空闲监控 | 🟡 部分 |
| **over_budget** | `write_censored_event(level=L2/L3)` | metering 审计 | ✅ |
| **cleanup_race** | timed_out 且 per_subtask 全 completed+verified | S12-P0 | ✅ |
| **结构性重复失败** | verify_state.json 里同一 failed_cmd 连挂 ≥2 次 | verify_retry 事件 | 🟡 有数据、没聚合成特征 |
| **Plan 欠分解** | `difficulty=hard` 且 `len(subtasks)<阈值` 且反复失败 | plan + 重试史 | 🟡 需聚合 |

两条最强锚点：**`cost≈0` 是 infra 的强信号**（花了钱基本排除 infra）；**"同一 failed_cmd 连挂两次"是结构性的强信号**（第三次盲目重试几乎必败）。

### 日志暴露的信息够不够？

够做第一版（infra / verify / budget / cleanup_race 四类稳），但**两个缺口**卡 stuck vs infra 的精细分：

- ❌ **IDLE_TIMEOUT 杀进程无结构化事件**——只 `logger.error` 一行文本，不在 `log_event` 流里（`hard_timeout` 有事件、IDLE 没有）。**修复：补 `log_event(logger, "idle_timeout", {...})`。**
- ❌ **claude `result.subtype` 没解析**——stream-json 的 result 事件带 subtype（success/error/max_tokens…）和具体错（529/503/rate-limit），代码只 `logger.info` 一行、没进结构化字段。**修复：把 subtype + 错误类型抽进 metering/结果**，才能区分"瞬时过载（退避）/ 硬错误（停）/ 鉴权失败（报警）"。

补这两条后，infra 可细分到三档，退避策略更对症。

### failure_class → 差异化策略（policy 表）

| failure_class | 策略 | 现状 |
|---------------|------|------|
| **infra/API** | 退避重试（30/60/120s，同档或降档模型） | ❌ G7（缺，立即重试×3） |
| **verify 逻辑失败** | 上下文增强重试（注入 stderr+diff，升模型） | ✅ 验证循环 |
| **stuck/timeout 无进展** | 变变量重试：升模型 + 缩范围 + 加时限；不盲目重复 | 🟡 升模型有，缩范围无 |
| **over_budget** | 不重试 / 降级（切便宜模型继续） | 🟡 停止有，降级缺（G4/G8） |
| **Plan 欠分解** | re-plan（回规划期再分解），非重试 | ❌ G5 |
| **cleanup_race** | 不重试（已成功） | ✅ S12-P0 |
| **整进程崩溃** | resume（非重试）：recover + 续跑 | ✅ 已有 |

这张表把 G4/G5/G7/G8 **串成统一的"分类-施策"机制**，而非四个孤立补丁。

### 环境恢复能否通知系统？

**现状：不能自动**（pull-based，`resume` 手动）。但**入站通道已就位**，缺的是触发器：`agent_go resume` 命令、MCP `resume_task` 工具、SSE 生命周期事件流都在；notify 仅出站。

- **在飞任务**（进程活着、子任务在退避）：把 G7 退避升级为**健康探测式**——退避期间探 API 健康（1-token ping），探测一成功就立刻重试。env 恢复自动触发，**零外部依赖**。这是最优解。
- **进程已死**（SIGKILL/崩溃）：在飞探测帮不了——走**外部 watcher**（systemd timer / cron / launchd）周期探健康 + 扫 `interrupted` meta，恢复后调 `resume`/MCP `resume_task`。通道有、watcher 无（属 `--auto-resume` 产品决策）。

### 边界：灰度有界升级 + 边际停止

判别主轴：**瞬时/会自愈 + 修法机械** → 自理；**结构性/需判断** → 人。灰度地带（如 transient-timeout vs structural-stuck 难分）走**有界升级**：自理一次（变变量：升模型/缩范围）→ 仍败 → 判结构性 → 停 + 选择题给人。**绝不在灰度地带无限重试**（烧钱）。配边际停止：retry 预期增量成本 > 成功价值×剩余预算 → 停（当前缺，应随 policy 表一起加）。

---

## 十一、失败场景全景：自理 vs 人为

> 把判定落到真实跑任务会遇到的全景。系统能自行处理的是"瞬时 infra + 机械修法"；必须人为的是"输入缺陷 + 环境凭据 + 能力上限 + 质量判断"。

### 真实失败场景全表

| # | 场景 | 原因类 | 恢复依赖 | 自理？ | 机制 |
|---|------|-------|---------|-------|------|
| 1 | API 529/503 过载、429 限流 | infra | 等几秒~几分钟 | ✅ | 退避 G7 |
| 2 | 网络抖动 / DNS 失败 | infra | 网络恢复 | ✅ | 退避 G7 |
| 3 | **API 401/403（key 过期/吊销，常凌晨轮换）** | infra | 人换 key | ❌ | 检测+告警，别锤 |
| 4 | 磁盘满 / OOM / fd 耗尽 | infra | 清理或降载 | 🟡 | 清缓存/降并发，否则人 |
| 5 | **pytest/node 缺失、版本不对** | infra(环境) | 人装依赖 | ❌ | 检测并精确报缺什么 |
| 6 | claude CLI 缺失/版本不兼容 | infra(工具) | 人安装 | ❌ | 启动门禁 |
| 7 | 机器睡眠 / 断电 / 重启 | infra(外部) | 唤醒后续跑 | 🟡 | resume/watcher |
| 8 | 模型输出畸形/空 | 模型 | 重试 | ✅ | verify-retry |
| 9 | **模型幻觉不存在的 API** | 模型(结构) | 换思路 | ❌ | 检测重复失败→停转人 |
| 10 | 模型循环（重复同一 tool call） | 模型(结构) | 打破循环 | 🟡 | 检测重复→re-plan/停 |
| 11 | 上下文溢出（单子任务太大） | 计划(结构) | 再分解 | ✅ | re-plan G5 |
| 12 | 模型拒绝（安全/内容） | 模型(结构) | 改任务 | ❌ | — |
| 13 | **任务描述歧义/欠约束** | 计划(输入) | 人澄清 | ❌ | **#1 人为项**，spec 准入 |
| 14 | Plan 欠分解（hard 任务 1-2 子任务）/ 过分解 | 计划(结构) | 再分解 | ✅ | re-plan G5 |
| 15 | 子任务依赖环 / 不可满足依赖 | 计划(结构) | 重排 | ✅ | pipeline 已检测→re-plan |
| 16 | 多子任务同文件/同符号冲突 | 计划(结构) | 重分解 | 🟡 | L1.5 检测→re-plan/人 |
| 17 | **验证失败：真实 bug（测试挂）** | 代码 | 修代码 | ✅ | **verify-retry（最常见自理项）** |
| 18 | **验证命令本身错（spec 给的测试命令不对）** | 代码(输入) | 人改 spec | ❌ | 检测"verify cmd 自身报错"→停 |
| 19 | 编译/构建错 | 代码 | 修代码 | ✅ | verify-retry |
| 20 | max_retries 用尽仍失败 | 代码(结构) | 人审查 | ❌ | 保留 worktree→inspect |
| 21 | 上游 merge 冲突 | 代码 | 解冲突 | 🟡 | 重试，不解则人 |
| 22 | L2/L3 预算到限 | 预算 | 降级或停 | ✅ | degrade G4 |
| 23 | **假阳性（验证过但代码错）** | 质量 | 人判断 | ❌ | evaluator 抓部分；review 把关 |
| 24 | **方向错（跑通但解错问题）** | 质量(输入) | 人判断 | ❌ | spec/方向问题 |
| 25 | 回归（改坏既有功能） | 质量 | 修 | 🟡 | tests_broken 抓→retry，持续则人 |

### 能自行处理（Tier 1）—— 五种自动动作

| 自动动作 | 覆盖场景 | 机制 |
|---------|---------|------|
| 退避重试 | #1 #2 #8 | G7 |
| 上下文增强重试 + 升模型 | #17 #19 | verify-retry + worker_models_fallback（已有） |
| re-plan 再分解 | #11 #14 #15 | G5 |
| 降级继续 | #22 | G4 |
| resume 续跑 | #7 | recover+resume（已有） |

共性：失败原因**外部会自愈**（infra）或**修法是确定性机械操作**（重试/再分解/降级），系统不需做判断。

### 必须人为（Tier 3）—— 四类

| 人为类 | 场景 | 为什么非人不可 |
|--------|------|---------------|
| **输入缺陷** | #13 任务歧义、#18 verify 命令错、#24 方向错 | 系统不知你"到底要什么"——spec/需求层，模型再强也猜不准 |
| **环境/凭据** | #3 key 过期、#5 缺 pytest、#6 缺 claude | 需系统外装东西/换密钥，系统无权限 |
| **结构性能力上限** | #9 幻觉、#10 死循环、#12 拒绝、#20 重试用尽 | 模型/任务到天花板，继续是烧钱 |
| **质量判断** | #23 假阳性、#25 持续回归 | "对不对"的最终裁判权在人 |

**产品原则**（PRD #4）：推给人时**做选择题、不做问答题**——带 `kill_reason`/`failure_class` 上下文 + 备选项（如 "[inspect] [强制重试换 Opus] [跳过] [终止]"），而非裸 "failed"。

### 判别原则

**瞬时/机械 → 自理；结构性/判断 → 人。** 灰度地带走有界升级（自理一次→仍败→结构性→停+选择题），绝不无限重试。

### 两个高频人为项 + 一个陷阱

- **#13 任务歧义 / #18 spec 错**：执行层救不了，应在**入口（Spec 准入 + scope 澄清，S11）**前置拦截，而非等执行失败。
- **#3 key 过期**：夜里最常见"看似 infra、实则人为"陷阱。failure_class 应把 infra **细分"瞬时过载 vs 鉴权失败"**——401/403 立即停止重试 + 告警，而非当 529 退避锤到天亮。这正是第十节"解析 result subtype"的实际价值。

**一句话**：系统能自行处理**瞬时 infra + 机械修法**（五件套）；必须人为的是**输入缺陷 + 环境凭据 + 能力上限 + 质量判断**。产品层的功课：把前者做扎实让"走人"成立，把后者"选择题化"让人回来时一眼能决策。

---

## 十二、与现有文档/代码的关系

- **[bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md)**：本文是其第四节 timeout 专题的**可执行延伸**——根因诊断 → 策略评估 → 落地路线。G1/G2 直接对应那里的"度量层缺陷 1 + 问题 4"。
- **[k4-cost-recalibration.md](k4-cost-recalibration.md)**：提供成本驱动因素（难度 4-5×、子任务数线性），是 G5/G6 难度口径的依据。
- **`config.py` cost_control 块**：三层配置已就绪，Phase 1 仅需 `enabled=True` + 校准值。
- **`prd.md` §产品 KPI**：K4（$/pass）目标 ≤$0.05 与实测差 7-14×，本文不直接修目标，但 G3（per-task 预算）把"够不着的全局目标"转为"可执行的单任务约束"。
