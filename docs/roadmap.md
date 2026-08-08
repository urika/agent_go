# agent_go Roadmap：可靠地产出可合并交付物

> 版本：v3.0
> 更新日期：2026-08-08
> 当前阶段：P0 产品契约与交付闭环收敛
> 产品主线：用户输入一次开发任务，agent_go 最终交付一个可审查、可合并的 PR。
> Goal/Loop 调研输入：[archive/reference/research-goal-loop-mechanism-2026-08-08.md](archive/reference/research-goal-loop-mechanism-2026-08-08.md)
> 当前执行清单：[m0-task-list.md](m0-task-list.md)

## 1. 产品目标

agent_go 当前不追求成为完整的 Agent 平台、项目管理系统或 IDE。当前唯一产品目标是：

```text
需求输入
  -> Plan
  -> 子任务执行
  -> 验证与修复
  -> 变更汇总
  -> 正确目标分支
  -> 可审查 PR
  -> 用户可合并
```

### 1.1 Accepted Delivery 定义

一次任务只有同时满足以下条件，才算 Accepted Delivery：

- 所有必要子任务完成，或明确标记为无需执行。
- 所有必要验证通过，且没有未处理的高风险告警。
- 代码变更已提交到明确的 delivery branch。
- delivery branch 与目标 base branch、PR head/base 关系正确。
- 用户可以通过 PR 或显式 merge 命令取得完整变更。
- `meta.json` 持久化 `commit_hash`、`target_branch`、`delivery_branch` 和 `pr_url`（如已创建）。
- 失败任务保留可审查现场，并提供 inspect/review/resume 指引。

### 1.2 产品不承诺

当前阶段不承诺以下能力：

- 自动替代需求管理、排期和人员协作系统。
- 自动决定所有复杂任务的最佳架构方案。
- 通过 KnowledgeStore 或自进化机制保证成功率必然提升。
- 通过单一模型 benchmark 结果保证生产质量。
- 同时维护 Office、IDE、CI、多个 Agent Runtime 等所有产品表面。

### 1.3 现有能力底座

以下能力已经存在于代码库，可作为 M0-M3 的实现基础，但不自动代表 Accepted Delivery：

- Plan -> Decompose -> Execute 主流程。
- Git worktree 隔离、DAG wave 调度和上游 artifact 传递。
- 验证、修复重试、blocked 阻断和失败 worktree 保留。
- 结构化 metering、模型路由、成本控制和恢复命令。
- `review`、MCP Server、MCP Client、产物导出和 CLI JSON 输出。

后续验收关注这些能力是否共同形成可靠交付，而不是继续单独增加模块数量。

### 1.4 SDD 能力分层

SDD 能力按产品关键路径分三层建设，不把所有治理能力一次性前置：

| 能力 | 当前基础 | 固定落地阶段 | 验收证据 |
|---|---|---|---|
| 规范可追踪 | Task Spec、Plan、Subtask、Verification 已有 | M1.4 基础追踪；M3 真实验证；后置持久化/L2 | requirement/acceptance criterion 到 Plan、测试和 PR 的追踪矩阵 |
| 架构可审查 | 架构文档、architect agent、合规字段已有 | M1.4 基础架构审查；M3 验证价值 | Architecture Decision、Review decision、architecture compliance report |
| 交付可验证 | Accepted Delivery 判定已有 | M1.1-M1.3 | delivery branch、commit 汇总、PR head/base、mergeability |
| 偏差可反馈 | failure class、retry、review、recover 已有 | M2.1-M2.4；M3 评估 | spec/architecture deviation、failure pattern、effective strategy、人工介入时间 |

M1.4 只建设最小可追踪和可审查闭环，不建设完整 KnowledgeStore、活文档或自动架构决策；这些能力必须经过 M3 真实任务验证后再决定。

## 2. Roadmap 管理规则

### 2.1 统一状态

路线图只使用以下状态，不再使用“代码完成”直接代表产品完成：

| 状态 | 含义 |
|---|---|
| `proposed` | 已提出，尚未确认价值 |
| `designed` | 方案已确定，尚未实现 |
| `implemented` | 代码已实现 |
| `tested` | 自动化测试覆盖完成 |
| `dogfooded` | 已在真实任务中使用 |
| `measured` | 指标数据已采集并可复现 |
| `accepted` | 满足产品验收门禁 |
| `deferred` | 暂缓，不进入当前关键路径 |

只有 `accepted` 才能从当前路线图移入“已完成”。

### 2.2 变更门禁

任何新增功能必须回答：

- 它是否直接提高 Accepted Delivery Rate？
- 它是否直接降低 Cost per Accepted Delivery？
- 它是否直接降低人工介入时间或交付失败率？
- 是否有真实用户场景和可执行验收？
- 是否会增加主交付链路的复杂度和故障面？

如果以上问题无法回答，该功能进入 `proposed/deferred`，不进入当前实施计划。

## 3. 指标契约

旧版 bench v1/v2/v3/v4 数据存在采集器漂移、timeout 误判、基础设施失败混入和 `$/pass` 分母偏差。旧数据只能作为 exploratory 数据，不用于季度达标判定。

### 3.1 唯一产品指标

#### Accepted Delivery Rate

```text
Accepted Delivery Rate
= Accepted Delivery 数 / 有效任务数
```

任务级依赖链中，部分子任务完成但无法形成可交付 PR，只计为失败或未完成，不计为部分成功。

#### Cost per Accepted Delivery

```text
Cost per Accepted Delivery
= 有效成本总额 / Accepted Delivery 数
```

成本必须区分：

- `model_cost`
- `verification_cost`
- `review_cost`
- `infrastructure_failure`
- `budget_abort`

#### First-pass Rate

```text
First-pass Rate
= 首次执行即完成验证的完整任务数 / 有效任务数
```

#### Time to Accepted Delivery

从任务启动到产生可审查交付分支或 PR 的总时间，而不是只统计某个 Claude 子进程的耗时。

#### Human Intervention Minutes

用户在 Plan 修改、失败审查、手动合并和恢复操作上实际花费的时间。

### 3.2 故障分类

所有任务必须使用稳定的 failure class：

```text
model_failure
verification_failure
timeout
budget_abort
infrastructure_failure
delivery_failure
user_cancelled
system_error
```

基础设施失败不得伪装成模型失败；预算中止不得伪装成能力失败；交付失败必须独立计数。

### 3.3 Metric Freeze Gate

在进入真实 KPI 考核前，必须冻结：

- 固定任务集和版本。
- 固定采集器和 schema 版本。
- 固定模型、配置、timeout 和 retry 策略。
- 固定失败分类和分母规则。
- 每条记录包含 `source_batch`、`schema_version` 和运行配置摘要。
- Bench 案例按 `smoke`、`core`、`decision`、`stress` 分 suite 管理；canonical 案例保留，日常运行按 suite 精简。
- 旧数据与新基线禁止直接混比。

## 4. 当前阶段：M0 产品契约与指标冻结

目标：让团队能够可信回答“什么算成功、多少钱、多久、为什么失败”。

### M0.1 产品契约

交付物：

- Accepted Delivery 状态定义。
- delivery branch、target branch、PR head/base 规则。
- `completed`、`completed_unverified`、`verification_failed`、`delivery_failed`、`blocked`、`cancelled` 状态语义。
- 失败恢复和人工介入路径。

验收：

- 文档、CLI、MCP 响应和 `meta.json` 使用同一套状态语义。
- 不存在“Claude 退出码为 0 就等于交付成功”的隐含判断。

### M0.2 指标冻结

交付物：

- 新 bench schema。
- 任务级成功和成本公式。
- failure class 分类。
- Metric Freeze Gate 检查命令和报告。

验收：

- 同一批数据重复计算结果一致。
- 能区分模型失败、基础设施失败、timeout、预算中止和交付失败。
- 输出 Accepted Delivery Rate 和 Cost per Accepted Delivery。

状态：`implemented/tested`（M0-1 至 M0-12 已实现；正式固定 baseline 尚未生成，因此暂不标记为产品 `accepted`）。

## 5. 阶段一：M1 交付闭环

目标：解决“代码做出来但没有可靠到达用户目标分支”的最高优先级问题。

### M1.1 交付分支模型

交付物：

- 每个 task 明确记录 `base_commit`、`base_branch`、`delivery_branch`。
- 所有成功子任务的 commit hash 可追溯。
- 多子任务结果汇总到一个明确的 delivery branch。
- 上游 artifact merge 和最终交付 merge 语义分离。

验收：

- 单子任务真实 Git 仓库端到端通过。
- 多子任务依赖链真实 Git 仓库端到端通过。
- 非 `main` 默认分支（如 `master`、`develop`）可以正确执行。
- 不依赖提交时间窗口判断 worker 是否产生了 commit。

### M1.2 PR 交付

交付物：

- `cmd_pr` 使用明确的 `head` 和 `base`，禁止把当前工作目录 HEAD 误推到 `main`。
- `pr_url`、head branch、base branch 写入任务结果。
- PR 创建失败归类为 `delivery_failure`，不能报告为 completed。
- 提供显式 `agent_go merge` 或等价的人工交付命令。

验收：

- 生成的 PR head/base 正确。
- PR 包含全部已接受子任务变更。
- 交付失败时可以从 delivery branch 重试，不需要重新执行 Claude。

### M1.3 交付状态与恢复

交付物：

- commit、verification、delivery 三种状态分离。
- `recover` 和 `resume` 使用 task lock、base commit 和 commit hash。
- 已提交但未验证的任务进入 `committed_unverified`，不得直接进入下游。

验收：

- SIGTERM、SIGKILL、PR 创建失败、merge 冲突等场景均可区分。
- recover 不会破坏运行中的 task。
- resume 不会重复提交或混入旧 worktree 改动。

### M1.4 SDD 最小治理闭环

目标：在交付闭环中建立最小的“规范可追踪、架构可审查”能力，避免 SDD 只停留在输入格式和 Prompt 注入层。

交付物：

- 为 Spec requirement 和 acceptance criterion 分配稳定 ID。
- Plan step、subtask、verification 和 delivery record 支持引用 requirement ID。
- 执行前生成最小 Architecture Decision，记录边界、依赖方向和关键约束。
- Architecture Review 产生 `approved`、`rejected` 或 `changes_requested` 决策。
- 生成任务级 `traceability_matrix` 和 `architecture_compliance` 摘要。
- 未通过的架构审查不得进入执行，除非用户明确覆盖并留下审计记录。

验收：

- 一个真实任务可以从 requirement 追踪到 Plan、测试和 PR。
- 架构审查结果持久化到任务产物，并在 CLI、MCP 和报告中可见。
- 缺少 requirement/acceptance criterion 映射的任务被标记为追踪不完整，而不是静默通过。
- 该能力不自动替代人工做复杂架构决策。

## 6. 阶段二：M2 核心可靠性

目标：在交付闭环成立后，降低失败和人工恢复成本。

### M2.1 验证与失败阻断

交付物：

- shell、lint/type/test、semantic evaluator 的职责边界。
- 验证失败上下文和 repair retry 记录。
- 上游失败时下游明确 blocked。
- 无进展检测，避免重复 retry 消耗预算。
- 循环状态埋点：`diff_stat_hash`、`failure_pattern`、`effective_strategy`、`no_progress`。
- 有界 Reflexion：仅在 retry 达到阈值后分析根因，不改变默认成功语义。

验收：

- 注入验证失败后，系统能自动修复或明确阻断。
- 重试次数、成本、失败原因可查询。
- 连续无进展不会无限消耗 token。
- 循环状态可供后续 KnowledgeStore 消费，但不会在 M2 自动修改历史知识。

### M2.2 Goal/Loop 受控增强

调研结论表明，当前系统已有硬迭代上限、资源预算和程序化验证，但缺少无进展检测、根因分析和策略升级。M2 只实现低风险、可观测的部分：

- retry 间记录 diff/stat 哈希。
- 连续两次无实质变化时提前终止，标记 `no_progress`。
- retry 达到阈值后，可调用独立 evaluator 生成 `failure_analysis`。
- Reflexion 结果只用于下一次 repair prompt，不直接改变任务状态。
- 每次额外分析必须受 token、次数和任务预算约束。
- `/goal` 继续默认关闭，直到语义 goal 通过独立实验验证。

验收：

- 无进展任务不会跑满全部 retry 上限。
- `failure_analysis` 和 `effective_strategy` 写入 `verify_state.json`。
- Reflexion 失败或超时会降级为普通 repair，不阻塞主流程。
- 额外 Reflexion 成本可单独计量。

### M2.3 成本与进程边界

交付物：

- 单次调用、子任务、任务级预算的统一语义。
- 并发启动前 reservation。
- metering 不可用时 fail-safe。
- Claude 及其子进程整体回收。

验收：

- 并发任务不会在预算检查竞态下无限超支。
- `cost_censored` 不重复计费。
- timeout、budget abort 和 infrastructure failure 可区分。

### M2.4 人工审查与恢复体验

交付物：

- 失败摘要、保留 worktree、review、resume 指引统一。
- `inspect -> review -> resume -> delivery` 形成闭环。
- CLI 和 MCP 返回同样的核心状态和修复建议。

验收：

- 用户无需阅读完整日志即可判断下一步动作。
- 失败任务的人工恢复时间可以测量。

### M2.5 Spec/Architecture 偏差反馈

目标：把失败从一次性错误升级为可定位、可修复、可复用的偏差记录，但不在 M2 自动修改全局知识或 Spec。

交付物：

- `spec_deviation`：需求、范围或验收标准与实现的偏差。
- `architecture_deviation`：模块边界、依赖方向或架构约束偏差。
- `acceptance_gap`：未满足的验收标准及其验证证据。
- 偏差根因分类：Spec 不完整、Plan 误解、拆解错误、实现错误、验证不足、交付汇总错误。
- 偏差修复状态、人工决策和是否需要回写 Spec 的记录。
- 偏差数据与 `failure_pattern`、`effective_strategy` 关联。

验收：

- 每个未通过的真实任务都能区分执行失败、Spec 偏差、架构偏差和交付失败。
- 偏差记录能进入下一次 repair prompt，但不会未经批准修改全局 Plan 或知识库。
- 偏差的人工处理时间和重复发生率可统计。

## 7. 阶段三：M3 真实任务验证

目标：验证产品主线，而不是继续用单元测试数量替代产品证据。

### M3.1 Dogfood 任务集

使用 10-20 个真实工程任务，覆盖：

- 新增功能
- bug 修复
- 跨文件重构
- 测试补充
- 依赖或配置迁移
- 一个明确的失败恢复场景

每个任务必须记录：

- 是否产生 Accepted Delivery
- 是否需要人工改 Plan
- 是否需要人工修复代码
- 总耗时
- 总成本
- 重试次数
- 人工介入分钟数
- 失败分类
- requirement/acceptance criterion 追踪完整性
- architecture review 结果和偏差数量
- spec/architecture deviation 及其修复状态

### M3.2 产品验收门禁

M3 不预先承诺绝对 KPI，先建立可信基线。至少需要：

- 100% 任务有完整结果记录。
- 100% 任务有明确最终状态。
- 0 个交付成功但找不到目标分支或 PR 的任务。
- infrastructure failure 与 model failure 分开统计。
- 所有失败任务都有可执行恢复路径。
- 关键 requirement 都能追踪到测试和最终交付物。
- 架构审查结果与最终变更一致，或有明确的人工覆盖记录。

完成 M3 后，基于真实数据设定下一阶段目标，而不是继续沿用未经验证的 `$0.05` 或 `K1 ≥97%` 目标。

## 8. 扩展能力决策门

以下能力暂不排入固定实施日期，只在 M3 完成后按实验结果决定。

### KnowledgeStore

先做 A/B 实验：无历史经验 vs 注入历史验证命令和失败模式。只有在 Accepted Delivery Rate 提升、成本不劣化且错误知识可淘汰时，才进入产品化。

M2 产生的 `failure_pattern`、`effective_strategy` 和 `no_progress` 只是候选数据，不代表已经建立 KnowledgeStore。

### Spec 闭环

M1.4 先提供最小 requirement/acceptance criterion 追踪和架构审查；M3 再通过 5 个以上真实任务观察填写成本、Plan 编辑次数、追踪完整率和交付成功率，决定是否建设 Spec 持久化、双向同步和 L2 语义审查。

### Reviewer 灰度

只对高风险任务开启，必须证明人工审查时间下降或 Accepted Delivery Rate 提升，且 review cost 不超过主任务成本的 20%。

### Branching Workflow

仅在 hard 任务存在可识别的策略分叉、且主路径失败有明确替代方案时启用。分支预算必须独立计量。

局部重规划是 Branching 的前置能力，但最多允许一次，且必须继承父任务预算，不允许递归扩张任务图。

### 语义 Goal

当前 `/goal` 主要由验证命令的 `exit_code == 0` 机械派生。只有在自然语言 `goal_description`、独立 evaluator 和 shell 验证形成 AND 关系，并通过真实任务验证后，才考虑开启默认 goal。

### 局部重规划与策略重置

当出现无进展、错误模式重复或变更规模异常但验证持续失败时，可以提出一次局部重规划建议。默认先请求人工确认，不自动改变全局 Plan；自动策略重置属于后续实验能力。

### MCP/Office/IDE/CI 扩展

只有存在真实用户场景、端到端验收和独立成功率指标时进入 roadmap。功能接入不等于产品成功，必须能证明对 Accepted Delivery 或人工成本有贡献。

### H3 自进化

在 KnowledgeStore、失败分类和指标冻结之前不启动。没有可信历史数据，自进化只会放大测量错误和错误经验。

## 9. 暂缓清单

在 M0-M3 通过前，以下事项不进入关键路径：

- 多 Runtime Worker
- IDE 插件
- 完整项目管理和 issue 生命周期
- 自动 Skill 蒸馏
- 自动编排拓扑自演化
- 多方案探索模式
- 大规模 Office 能力扩展
- 以 benchmark 排名为目标的模型扩展

已有功能的安全修复、正确性修复和必要测试不受此限制。

## 10. 关键风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 交付分支语义错误 | 用户拿不到可合并代码 | M1 真实 Git 端到端门禁 |
| KPI 口径再次漂移 | 错误模型和成本决策 | Metric Freeze Gate + schema version |
| 成本控制误杀 | 用户不再信任自动执行 | L1/L2/L3 分层，预算基线校准后启用 |
| 自动修复无进展 | token 和时间失控 | diff/验证结果无进展检测 |
| 扩展能力分散资源 | 核心交付链路延期 | 新功能必须通过产品价值评审 |
| 历史知识污染 | 后续 Plan 质量下降 | 来源、置信度、过期和人工回滚机制 |

## 11. 当前决策

当前唯一关键路径为：

```text
M0 产品契约与指标冻结
  -> M1 交付闭环
  -> M2 核心可靠性
  -> M3 真实任务验证
  -> 扩展能力逐项决策
```

在 M3 结束前，不对“年度 K1 ≥97%”“$/pass ≤$0.03”等绝对目标做硬承诺；先建立可信 Accepted Delivery 基线，再根据真实数据制定下一版目标。
