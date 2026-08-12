# agent_go 产品需求文档

> 版本：v3.0
> 更新日期：2026-08-08
> 配套路线图：[roadmap.md](roadmap.md)
> 当前阶段：M0 产品契约与指标冻结
> Goal/Loop 调研输入：[archive/reference/research-goal-loop-mechanism-2026-08-08.md](archive/reference/research-goal-loop-mechanism-2026-08-08.md)
> 当前执行清单：[m0-task-list.md](m0-task-list.md)

## 1. 产品概述

### 1.1 产品定位

agent_go 是一个面向高频使用 Claude Code 的工程师的异步开发任务编排器。

它将一次开发任务组织为：

```text
需求输入 -> Plan -> 子任务 -> 隔离执行 -> 验证/修复 -> 变更汇总 -> 可合并 PR
```

agent_go 的核心差异不是提供新的模型，而是提供跨模型可复用的：

- 任务规划和拆解。
- Git worktree 上下文隔离。
- DAG 子任务协调。
- 自动验证和失败阻断。
- 成本、状态和恢复控制。
- 可审查的最终交付。

### 1.2 核心用户

首要用户是：

- 每周多次使用 Claude Code 或类似 Coding Agent 的工程师。
- 需要委派多步骤开发任务，但不希望持续盯住终端的工程师。
- 需要在 CI、脚本或 MCP Host 中调用开发任务编排能力的用户。

当前不以项目经理、非技术用户或大型组织流程管理者为首要用户。

### 1.3 核心产品承诺

> 用户输入一次开发任务，agent_go 最终交付一个可审查、可合并的 PR；如果无法交付，必须明确说明原因并提供可执行的恢复路径。

产品不承诺每次任务都成功，但承诺：

- 成功与失败状态可信。
- 失败原因可定位。
- 变更不会丢失或混入其他任务。
- 用户可以继续恢复、审查或重新交付。

## 2. 成功定义：Accepted Delivery

一次任务只有满足以下条件，才算 Accepted Delivery：

- 所有必要子任务完成，或明确标记为无需执行。
- 所有必要验证通过。
- 没有未处理的高风险告警。
- 代码变更已提交到明确的 delivery branch。
- delivery branch、target branch、PR head/base 关系正确。
- PR 或显式 merge 命令可以取得完整变更。
- `meta.json` 持久化 `commit_hash`、`target_branch`、`delivery_branch` 和 `pr_url`（如已创建）。
- 失败任务保留可审查现场并提供 inspect/review/resume 指引。

以下情况不算 Accepted Delivery：

- Claude 进程退出码为 0，但代码未提交。
- 子任务完成，但最终 PR 缺少部分变更。
- 代码只存在于孤立 worktree，用户无法取得。
- 任务部分完成但下游被阻断。
- PR 创建失败但任务仍被标记 completed。

## 3. 产品范围

### 3.1 当前核心范围

- Plan 生成和确认。
- 子任务拆解和依赖调度。
- worktree 隔离执行。
- Claude Code headless/interactive 执行。
- 验证命令、失败修复和重试。
- 上游 artifact 传递。
- commit、delivery branch 和 PR 交付。
- 失败阻断、状态恢复和人工审查。
- 成本控制、模型路由和结构化计量。
- CLI、JSON 和 MCP 调用接口。

### 3.2 当前不在承诺范围

- 需求管理、排期、人员分工和项目管理。
- 自动替代产品经理或架构师的决策。
- 完整 IDE、Desktop、Mobile 或 Web 开发环境。
- 自动保证复杂任务的最佳架构方案。
- 通过单一 benchmark 排名保证生产质量。
- 同时维护所有模型、Coding Runtime 和 Office 产品表面。
- 在没有真实场景和指标证据时扩展新集成。

## 4. 功能需求

### 4.1 任务规划与拆解

#### F-PLAN-1 Plan 生成

系统应根据任务描述、仓库结构、配置、Skills 和可选 Spec 生成结构化 Plan。

Plan 至少包含：

- 任务概览。
- 执行步骤。
- 子任务描述。
- 依赖关系。
- 文件范围提示。
- 验证方式。
- agent type、difficulty 和 skills。

#### F-PLAN-2 Plan 确认

用户应能在执行前：

- 接受 Plan。
- 跳过步骤。
- 编辑步骤。
- 重新生成 Plan。
- 查看 Plan 版本差异。

Headless 模式必须保留确定性准入检查，不能因为 `--yes` 静默跳过硬门禁。

#### F-PLAN-3 Plan 预检与一次性修复

Plan 在用户确认和 Worker 启动前，必须经过确定性预检，至少覆盖：

- 验证命令安全白名单。
- requirement/acceptance criterion 覆盖。
- 文件范围、禁止修改范围和子任务之间的冲突。
- 依赖环和不可验证的上游步骤。

对于可由确定性规则判断的 Plan 缺陷，系统可以自动向 Planner 注入结构化修复反馈并重新生成一次 Plan。该机制必须满足：

- 默认最多自动修订一次。
- 修订前后保存 Plan 版本和 diff。
- 不得删除或放宽 requirement、acceptance criterion、架构约束、验证责任或目标分支。
- 修订后必须重新执行完整预检；仍不通过则阻断执行或请求人工确认。
- 修订调用计入任务预算，并记录 `plan_repair_count`、`plan_repair_attempted` 和 `plan_repair_history`。

Plan 预检修复发生在执行前，不改变 G8 验证拒绝短路语义，也不等同于执行中的局部或全局自动重规划。

#### F-PLAN-4 DAG 调度

系统应根据依赖关系按 wave 调度子任务，并满足：

- 依赖未完成时不得启动下游。
- 上游失败时下游明确 blocked。
- 并发任务之间不共享可变执行状态。
- 循环依赖必须明确报告。

#### F-PLAN-5 Goal Contract 与 Goal Policy

详细设计见：[Goal 机制设计](design/goal-mechanism-design.md)。

系统应为有明确目标的任务生成结构化 Goal Contract，至少包含：

- `goal_description`。
- acceptance criteria。
- completion evidence（verification 命令和必要的语义证据）。
- 适用约束和是否要求 delivery。

Goal Contract 默认存在，但不代表 Goal Loop 默认开启。系统应根据 Plan 的难度、可验证性、是否 headless、预算和风险生成 `goal_recommendation`，并通过 Goal Policy 解析最终执行策略：

- `off`：不启用持续 Goal，保留普通执行和验证。
- `auto`：根据确定性策略自动选择。
- `force`：用户明确要求持续自主执行。
- `hook`：启用 Goal continuation 和确定性 Stop Hook。

决策优先级为：

```text
用户覆盖 > 配置策略 > 系统确定性策略 > Planner recommendation > 默认策略
```

Goal Policy 必须记录 `goal_mode`、`goal_backend`、决策原因和风险码。Goal 不能绕过 Plan Preflight、verification、commit、pipeline、delivery 或 Accepted Delivery 判定。

Goal 机制约束：

- 默认不对所有任务强制开启 Goal Loop。
- 必须有安全且可执行的 completion evidence 才能进入 `auto/force/hook`。
- 必须有 max turns、timeout 和 budget 上限。
- Goal evaluator 的完成结果不能单独标记 Task/Subtask completed。
- Goal 状态至少支持 `ACTIVE`、`COMPLETED`、`BLOCKED`、`PAUSED`、`CANCELLED`、`TIMED_OUT`、`BUDGET_EXCEEDED`。

Goal Contract、Goal Policy 和 Goal Evidence 应在 meta、status、review、replay 和 metering 中可查询。

### 4.2 隔离执行与变更管理

#### F-EXEC-1 Worktree 隔离

每个子任务应在独立 worktree 和 branch 中执行，不能污染主工作区或其他任务。

#### F-EXEC-2 Artifact 传递

下游子任务可以读取已完成上游的提交结果，但必须通过明确的 commit/tag/merge 关系传递。

#### F-EXEC-3 Commit 完成边界

系统必须区分：

- 代码已修改但未提交。
- 已提交但未验证。
- 已提交且验证通过。
- 已进入 delivery branch。
- 已生成 PR。

commit、verification 和 delivery 不得被压缩为单一状态。

#### F-EXEC-4 正确交付

系统必须记录并验证：

- `base_commit`。
- `base_branch`。
- `delivery_branch`。
- 子任务 commit hash。
- PR head branch。
- PR base branch。

不得使用提交时间窗口猜测 worker 是否产生了 commit。

### 4.3 验证与修复

#### F-VERIFY-1 确定性验证

支持安全白名单内的测试、lint、type check 和其他验证命令，并记录：

- 命令。
- 退出码。
- stdout/stderr 摘要。
- 执行耗时。
- attempt 和 retry 信息。

#### F-VERIFY-2 自动修复

验证失败时，系统可将失败上下文注入 Repair Agent，按配置重试，并在每次重试后重新运行完整必要验证。

#### F-VERIFY-3 失败阻断

达到重试上限后：

- 当前子任务标记 failed。
- 依赖它的下游标记 blocked。
- 失败 worktree 按策略保留。
- 结果中写入 failure class 和恢复指引。

#### F-VERIFY-4 无进展控制

系统应识别连续重试没有产生代码、验证或状态进展的情况，避免无效 token 消耗。

#### F-VERIFY-5 循环状态与受控反思

系统应为每次验证重试记录可审计的循环状态：

- `diff_stat_hash`。
- `failure_pattern`。
- `effective_strategy`。
- `no_progress`。
- `failure_analysis`（如启用 Reflexion）。

当 retry 达到配置阈值后，可以调用独立 evaluator 生成失败根因分析。分析结果只用于后续 repair prompt，不能绕过 shell 验证、直接修改任务状态或无限增加预算。

#### F-VERIFY-6 受控策略升级

当前版本不允许执行失败后自动递归修改全局 Plan。执行前的 Plan 预检修复属于 F-PLAN-3；执行中的局部重规划仍是后续实验能力，必须：

- 最多触发一次。
- 继承父任务预算和权限。
- 记录 `replan_triggered`、`replan_succeeded`。
- 默认支持人工确认。
- 不能因重规划递归扩大任务图。

### 4.4 恢复、审查与交付

#### F-RECOVER-1 中断恢复

系统应处理 SIGTERM、SIGINT、SIGKILL、进程异常和外部取消，并尽可能保留：

- 已完成子任务。
- 已提交 commit。
- 验证状态。
- metering 记录。
- worktree 现场。

#### F-RECOVER-2 并发保护

同一 task 的 run、resume 和 recover 不能并发修改 worktree 或 meta.json。

#### F-REVIEW-1 结果审查

用户应能查看：

- 聚合 diff。
- 按文件分组的变更。
- 子任务状态和失败原因。
- 验证摘要。
- 成本和模型信息。
- 独立 Reviewer 的深度分析（可选）。

#### F-DELIVERY-1 PR 交付

系统应支持：

- 创建正确 head/base 的 PR。
- 保存 PR URL 和分支信息。
- PR 创建失败后独立重试交付。
- 必要时通过显式 merge 命令完成交付。

### 4.5 成本、模型与计量

#### F-COST-1 分层成本控制

系统支持：

- L1：单次调用预算。
- L2：子任务累计预算。
- L3：任务级预算。
- strict、degrade、ignore 三种预算模式。
- 并发启动前 budget reservation。

成本控制不能把预算中止伪装成模型能力失败。

#### F-COST-2 计量

每次模型调用应记录：

- task id。
- subtask id。
- role。
- virtual model。
- actual provider/model。
- prompt/completion tokens。
- cost。
- latency。
- fallback/degrade 原因。
- failure class。

metering 不可用时，strict/degrade 模式必须 fail-safe，不能将成本当作零继续执行。

#### F-ROUTE-1 模型路由

支持按以下维度路由：

- planner/worker/reviewer 角色。
- easy/medium/hard 难度。
- 任务类型。
- provider 和 backend。
- fallback/degrade 策略。

模型选择不能只依据厂商 benchmark，必须有 agent_go 自有任务数据支持。

### 4.6 CLI、JSON 和 MCP

系统应提供：

- CLI 交互模式。
- headless/`--yes` 模式。
- JSON 结构化输出。
- MCP Server：run/resume/inspect/review/list/cancel。
- MCP Resources：summary、plan、metering、log、review 等。
- MCP Prompts：失败诊断和恢复 SOP。
- MCP HTTP/SSE transport 和鉴权。

CLI、JSON 和 MCP 必须共享核心状态语义，不得出现同一任务在不同接口中显示不同结果。

## 5. 非功能需求

### NFR-1 正确性

- commit/tag 失败不得报告成功。
- PR head/base 必须正确。
- 部分完成不得计为完整交付。
- 未验证代码不得进入下游。
- 非 `main` 默认分支必须支持。

### NFR-2 可靠性

- 中断后可恢复。
- recover 不得破坏运行中任务。
- resume 不得重复提交。
- 子进程树必须整体清理。
- 异常路径必须释放 task lock、heartbeat、MCP、worktree 和 Git 临时状态。

### NFR-3 安全

- 验证命令使用白名单。
- MCP 工具使用 allowlist 和 tool filter。
- HTTP MCP 默认本地绑定。
- 远程访问必须支持 token 鉴权。
- 执行 Agent 不得自行绕过关键验证。
- Reviewer 尽量与 Worker 使用不同模型来源。

### NFR-4 成本

- L1 防单次失控。
- L2 防重试循环失控。
- L3 防任务级失控。
- 预算 reservation 防并发竞态超支。
- `cost_censored` 不得重复计费。
- 成本和 failure class 可审计。

### NFR-5 性能

- 简单任务具备可接受的端到端完成时间。
- 并发不应显著降低成功率和稳定性。
- 长时间有进展的任务不得被静默误杀。
- 使用 Time to Accepted Delivery 衡量真实交付耗时。

### NFR-6 可观测性

用户必须能够回答：

- 任务当前阶段。
- 已完成、失败和 blocked 的子任务。
- 失败原因和下一步操作。
- 总成本、模型和重试次数。
- commit、delivery branch 和 PR 位置。

### NFR-7 可测试性

必须使用真实临时 Git 仓库覆盖：

- 单子任务交付。
- 多子任务依赖。
- 非 `main` 分支。
- commit/tag/merge/PR 失败。
- SIGTERM/SIGKILL。
- recover/resume 竞态。
- 进程树清理。
- metering 不可用。
- 并发预算竞态。

## 6. 产品指标

旧 Bench v1/v2/v3/v4 数据存在采集器漂移、timeout 误判、基础设施失败混入和 `$/pass` 分母偏差，不能直接作为当前 KPI 基线。

### 6.1 Accepted Delivery Rate

```text
Accepted Delivery Rate
= Accepted Delivery 数 / 有效任务数
```

有效任务排除 `budget_abort`、`infrastructure_failure`、`user_cancelled`、
`system_error` 以及显式 `valid_task=false` 的记录；model、verification、timeout
和 delivery failure 保留在分母中。

部分子任务完成但无法形成可交付 PR，不计为部分成功。

### 6.2 Cost per Accepted Delivery

```text
Cost per Accepted Delivery
= 有效成本总额 / Accepted Delivery 数
```

有效成本需要区分 model、verification、review、infrastructure failure 和 budget abort。

### 6.3 First-pass Rate

```text
First-pass Rate
= 首次执行即完成验证的完整任务数 / 有效任务数
```

### 6.4 Time to Accepted Delivery

从任务启动到可审查 delivery branch 或 PR 的总时间。

### 6.5 Human Intervention Minutes

记录用户修改 Plan、审查失败、手动合并和恢复任务消耗的时间。

### 6.6 其他辅助指标

```text
Timeout Rate = timeout 任务数 / 有效任务数
Retry Rate = total_retries > 0 的任务数 / 有效任务数
Delivery Failure Rate = delivery_failure 任务数 / 有效任务数
```

`Time to Accepted Delivery` 使用 Accepted Delivery 任务的平均
`elapsed_sec`；没有观测值时返回 `null`。

### 6.7 Failure Class

统一使用：

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

### 6.8 Metric Freeze Gate

在 KPI 正式考核前必须固定：

- 任务集和版本。
- 采集器和 schema 版本。
- 模型、配置、timeout、retry 策略。
- 失败分类和分母规则。
- `source_batch`、`schema_version` 和运行配置摘要。
- Bench 案例按 `smoke`、`core`、`decision`、`stress` 分 suite 管理；canonical 案例保留，日常运行按 suite 精简。

## 7. 当前差距

### P0

- 交付 branch、目标 branch 和 PR head/base 需要统一并完成真实 Git 验收。
- PR 创建失败必须独立标记为 `delivery_failed`。
- CLI、MCP、meta 和 recover 的状态语义需要统一。
- 可信指标基线尚未冻结。
- 任务级 Accepted Delivery 和 Cost per Accepted Delivery 尚未成为正式门禁。

### P1

- 无进展检测和局部重规划仍不完整。
- Goal Contract/Policy 已完成设计，当前 Goal Loop 仍默认关闭；Claude/Kimi provider adapter、auto 策略和默认开启条件需要真实任务 A/B 验证。
- Reflexion 失败分析和循环状态埋点尚未形成稳定产品契约。
- recover/resume、checkpoint、进程树清理需要更多故障注入测试。
- MCP 工具成功率和产物完整率尚未完整聚合。
- Spec 的真实使用率和收益尚未验证。
- KnowledgeStore 的收益尚未通过 A/B 实验验证。
- Reviewer 成本和质量收益尚未验证。

### P2

- IDE、CI、Office 和多 Runtime 扩展缺少明确的真实用户需求证据。
- 缺少 PR 接受率、人工介入时间和用户重复使用率等行为指标。
- 缺少稳定的用户 dogfood 任务集。

## 8. 版本计划

### M0：产品契约与指标冻结

目标：明确什么算成功、多少钱、多久、为什么失败。

交付物：

- Accepted Delivery 定义。
- delivery branch、target branch、PR head/base 规则。
- 统一状态语义。
- 任务级成功和成本公式。
- failure class。
- 固定 bench schema、任务集和采集器。

验收：

- 文档、CLI、MCP、meta 使用相同状态语义。
- 同一批数据重复计算结果一致。
- 模型、验证、timeout、预算、基础设施和交付失败可区分。

### M1：交付闭环

目标：用户能够拿到完整、正确、可合并的 PR。

交付物：

- 记录 `base_commit`、`base_branch`、`delivery_branch`。
- 多子任务汇总到统一 delivery branch。
- 修复 PR head/base 逻辑。
- 保存 commit hash 和 PR URL。
- 增加显式 merge 或交付命令。
- 交付失败独立重试。

验收：

- 单子任务真实 Git 端到端通过。
- 多子任务依赖链真实 Git 端到端通过。
- `main`、`master`、`develop` 等默认分支均可工作。
- PR 完整包含任务变更。
- 交付失败后不需要重新运行 Agent 即可重试交付。

### M2：核心可靠性

目标：降低失败率、恢复成本和无效 token 消耗。

交付物：

- commit、verification、delivery 三层状态分离。
- task lock 和 recover/resume 保护。
- 验证失败阻断。
- 无进展检测。
- `diff_stat_hash`、`failure_pattern`、`effective_strategy` 和 `no_progress` 埋点。
- 有界 Reflexion 失败分析。
- L1/L2/L3 成本边界。
- 进程组整体清理。
- 统一失败摘要和恢复指引。

验收：

- SIGTERM/SIGKILL 可恢复。
- 不重复提交、不污染其他任务。
- 不出现无限 retry。
- 无进展任务不会跑满全部 retry 上限。
- Reflexion 失败或超时会降级为普通 repair，不阻塞主流程。
- 成本控制竞态可控。
- 用户无需阅读完整日志即可知道下一步动作。

### M3：真实任务验证

目标：用真实产品证据替代单元测试数量。

使用 10-20 个真实工程任务，覆盖新增功能、Bug 修复、跨文件重构、测试补充、迁移和恢复场景。

记录：

- Accepted Delivery Rate。
- Cost per Accepted Delivery。
- First-pass Rate。
- Time to Accepted Delivery。
- Human Intervention Minutes。
- retry 次数。
- failure class。
- PR 创建和合并结果。

基础门禁：

- 100% 任务有完整结果和最终状态。
- 0 个任务成功但找不到目标分支或 PR。
- infrastructure failure 与 model failure 分开统计。
- 所有失败任务都有可执行恢复路径。

M3 完成后，基于真实数据设定下一阶段目标，不继续沿用未经验证的 `$0.05` 或 `K1 >= 97%` 硬目标。

### M4：扩展能力决策

M3 通过后，再逐项评估：

- KnowledgeStore：先做 A/B 实验。
- Spec：先验证用户使用意愿和交付收益。
- Reviewer：证明降低人工审查时间或提升交付成功率。
- Branching：仅针对存在真实策略分叉的 hard 任务。
- 语义 Goal：自然语言 goal、独立 evaluator 和 shell 验证必须形成 AND 关系，并证明默认开启不降低 Accepted Delivery Rate。
- 局部重规划：最多一次、继承父预算，默认人工确认；通过实验后才考虑自动策略重置。
- MCP/Office/IDE/CI：需要真实场景、端到端验收和独立指标。
- H3 自进化：必须建立可信历史数据后再启动。

## 9. 产品决策原则

- 先交付，再扩展。
- 先冻结指标，再讨论达标。
- 任务级交付优先于子任务级完成率。
- 真实用户任务优先于 benchmark 排名。
- 失败必须可解释、可恢复、可审计。
- 新功能必须证明不损害 Accepted Delivery。
- 代码实现、测试通过、真实使用和产品验收必须分开记录。
