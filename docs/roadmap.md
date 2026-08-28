# agent_go Roadmap：可靠地产出可合并交付物

> 版本：v4.3
> 更新日期：2026-08-21
> 当前阶段：M0-M4、M4.5（模型池化，hard 94.4%）已 `accepted`；阶段八（诊断数据面）、谦逊层 H1-H4、Web 操作台全功能、决策辅助 M6.1-M6.5、看板编排 W1-W4 已交付；阶段 C C1/C2/C3 + C4 KnowledgeStore A/B smoke 与葬礼回写链路已落地；bench 交付闭环基线 `delivery-20260820`（ADR=0.7045）已建立
> 产品主线：用户输入一次开发任务，agent_go 最终交付一个可审查、可合并的 PR。
> 北极星目标：**全自主交付（渐进自治）**——把人工介入从每个环节降到只剩「例外点」，而非追求人类完全不参与。
> Goal/Loop 调研输入：[archive/reference/research-goal-loop-mechanism-2026-08-08.md](archive/reference/research-goal-loop-mechanism-2026-08-08.md)
> 当前执行清单：[task-list-2026-08-20.md](task-list-2026-08-20.md)（历史：[m0-task-list.md](m0-task-list.md)）

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

Bench 在进入正式 decision baseline 前，遵循 [ADR-009 Bench 收敛决策](design/adr/ADR-009-bench-convergence.md) 和 [Bench 收敛计划](archive/design/bench-convergence-plan.md)：先完成数据/状态清理、Plan/Verifier 收敛和 Golden Tasks，再扩大任务矩阵。

### 1.5 全自主交付目标（北极星）

「全自主交付」不是单一维度的静态终点，而是三根支柱同时成熟、且**每升一级自治必须同步增加审计/回滚能力**的渐进过程：

| 支柱 | 含义 | 衡量 |
|---|---|---|
| ① 工程闭环 | 产物可靠到达目标分支、可追溯、失败可分类可恢复 | Accepted Delivery Rate / Delivery Failure Rate |
| ② 智能闭环 | 从失败中学习，不重复同类错误 | 首次验证通过率 / 复发率 / 重试成本 |
| ③ 人机信任 | 成本可控、可审计、可回滚、不被「虚假控制感」欺骗 | Cost per AD / Human Intervention Minutes / audit 覆盖 |

**关键边界**（源自 SDD 学术综述 `sdd-references-and-frameworks.md`）：

- 当前定位 L2（Spec-First）→ 目标 L3（Spec-Anchored），**不以 L5（Spec-as-Source 全自动）为近期目标**。
- 同源审查是「回响」（P10）：全自主 merge 前必须有「不同源独立验证」兜底，`judge != candidate` 是铁律。
- 提高自治度的最高杠杆是 **Spec 质量**（P9：恢复完整 spec 即恢复单 Agent 89% 上限），而非堆更多 Agent。

「全自主交付」的可测量定义：

```text
Accepted Delivery Rate 逼近 1  ∧  Human Intervention Minutes → 0  ∧  Cost per AD 持续下降
```

且每个可自动化的环节（Plan 确认、merge 决策、失败审查除外，这三者是「例外点」）都应在无人工介入下闭环。

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

状态：`accepted`（M0-1 至 M0-12 已实现；正式固定 baseline 已生成：`decision-20260812`，35 任务 canonical，`pass_rate_diagnostic=0.924`、`first_pass_rate=0.864`、`$/pass=$0.0193`，`eval gate` 通过，据此标记为产品 `accepted`）。

## 5. 阶段一：M1 交付闭环

目标：解决“代码做出来但没有可靠到达用户目标分支”的最高优先级问题。

状态：`accepted`（M1.1-M1.4 交付物与验收全部达成，2026-08-12 完成正式验收。真实交付证据：urika/agent_go PR#38 MERGED、urika/vibe-astock PR#1 OPEN、urika/llama-defender PR#8 MERGED，均 head=delivery branch、base=main；多子任务依赖链 + 非 main 默认分支端到端验证通过；192 项 M1 相关测试通过。唯一 ⚠️ 项为架构审查硬门禁，属刻意保留的 fail-open 设计。指标口径：bench harness 不创建真实 PR，formal baseline（decision-20260812）`accepted_delivery_count=0` 为设计预期，`accepted_delivery_rate` 有效值由上述真实 PR 提供证据，两项口径并存成立）。

### M1.1 交付分支模型

交付物：

- 每个 task 明确记录 `base_commit`、`base_branch`、`delivery_branch`。✅（`cli.py:763` meta 初始化 + `_record_git_meta`；真实任务 meta.json 均含三项）
- 所有成功子任务的 commit hash 可追溯。✅（`results[].commit_hash` 逐子任务记录，`cmd_inspect`/`replay` 可查）
- 多子任务结果汇总到一个明确的 delivery branch。✅（`delivery.py:create_delivery_branch` 聚合全部已接受子任务 commit）
- 上游 artifact merge 和最终交付 merge 语义分离。✅（子任务内 `git merge` upstream tag 属 artifact 传递；`cmd_merge`/`create_delivery_branch` 属交付 merge）

验收：

- 单子任务真实 Git 仓库端到端通过。✅（urika/agent_go PR#38 MERGED，2026-08-09；urika/vibe-astock PR#1 OPEN，head=delivery branch）
- 多子任务依赖链真实 Git 仓库端到端通过。✅（task-20260809-084815：3 subtask / 2 依赖链 → ACCEPTED_DELIVERY）
- 非 `main` 默认分支（如 `master`、`develop`）可以正确执行。✅（task-20260809-113310：base_branch=`fix/dogfooding-issues`，2 subtask 全部 completed + delivery branch 生成）
- 不依赖提交时间窗口判断 worker 是否产生了 commit。✅（以 `commit_hash` 存在性为准，见 `recover.py` "commit 是完成边界"）

### M1.2 PR 交付

交付物：

- `cmd_pr` 使用明确的 `head` 和 `base`，禁止把当前工作目录 HEAD 误推到 `main`。✅（`cli.py:1897` head=delivery_branch、base=target_branch，无 delivery_branch 则拒绝）
- `pr_url`、head branch、base branch 写入任务结果。✅（`meta.pr_url/pr_head/pr_base`，成功与 PR 复用路径均持久化）
- PR 创建失败归类为 `delivery_failure`，不能报告为 completed。✅（`cli.py:1980` `delivery_failed=True`、`accepted_delivery=False`、status=DELIVERY_FAILED）
- 提供显式 `agent_go merge` 或等价的人工交付命令。✅（`cmd_merge`，含 mergeability 预检 + PR-open 阻断 + 已合并 PR commit 同步）

验收：

- 生成的 PR head/base 正确。✅（urika/vibe-astock#1 head=`agent_go/.../delivery` base=main；urika/llama-defender#8 MERGED head/base 一致）
- PR 包含全部已接受子任务变更。✅（`create_delivery_branch` 聚合全部已完成子任务 commit 后创建 PR；`test_create_delivery_branch_aggregates_commits`）
- 交付失败时可以从 delivery branch 重试，不需要重新执行 Claude。✅（`cmd_pr` 从保留的 delivery branch 直接重新推送/创建 PR，不重跑 worker；`test_cmd_pr_gh_real_failure_marks_delivery_failed`）

### M1.3 交付状态与恢复

交付物：

- commit、verification、delivery 三种状态分离。✅（subtask `status`=completed/failed/no_changes（commit+verify）；delivery 独立为 `delivery_failed/accepted_delivery/status=ACCEPTED_DELIVERY/DELIVERY_FAILED`）
- `recover` 和 `resume` 使用 task lock、base commit 和 commit hash。✅（`recover.py:238` / `pipeline.py:311` 共享 `.task.lock`（fcntl LOCK_EX|LOCK_NB）；以 commit hash 与 verify 日志判定）
- 已提交但未验证的任务进入 `committed_unverified`，不得直接进入下游。✅（`recover.py:214` 有 commit + verify 未知 → committed_unverified；`resume` 接力重验证后下游 wave 才可执行）

验收：

- SIGTERM、SIGKILL、PR 创建失败、merge 冲突等场景均可区分。✅（recover 按 commit/verify 状态分类；PR 失败→DELIVERY_FAILED；merge 冲突→`check_mergeability` 报告冲突文件）
- recover 不会破坏运行中的 task。✅（`.task.lock` 非阻塞独占锁，BlockingIOError 即拒绝并发，与 pipeline/resume 互斥）
- resume 不会重复提交或混入旧 worktree 改动。✅（recover 无条件 reset orphan 变更、不替用户 commit；`test_resets_*`/`test_no_commits_no_orphan_no_changes` 等 56 项恢复测试覆盖）

### M1.4 SDD 最小治理闭环

目标：在交付闭环中建立最小的"规范可追踪、架构可审查"能力，避免 SDD 只停留在输入格式和 Prompt 注入层。

交付物：

- 为 Spec requirement 和 acceptance criterion 分配稳定 ID。✅（`governance.extract_spec_requirements`，REQ-xxx/AC-xxx + 变体归一化 + 编号条款兜底）
- Plan step、subtask、verification 和 delivery record 支持引用 requirement ID。✅（plan `requirement_ids`/`acceptance_criteria_ids` 透传 + `validate_plan_quality` 覆盖率检查）
- 执行前生成最小 Architecture Decision，记录边界、依赖方向和关键约束。✅（`governance.architecture_review`，LLM 审查，fail-open）
- Architecture Review 产生 `approved`、`rejected` 或 `changes_requested` 决策。✅（决策持久化到 `meta.architecture_review`）
- 生成任务级 `traceability_matrix` 和 `architecture_compliance` 摘要。✅（`governance.build_traceability_matrix`）
- 未通过的架构审查不得进入执行，除非用户明确覆盖并留下审计记录。⚠️（决策已生成并持久化；`architecture_review.enabled` 默认 false，未作为硬门禁）

验收：

- 一个真实任务可以从 requirement 追踪到 Plan、测试和 PR。✅（`agent_go governance <task-id>`）
- 架构审查结果持久化到任务产物，并在 CLI、MCP 和报告中可见。✅（`governance` CLI + `governance_task` MCP tool）
- 缺少 requirement/acceptance criterion 映射的任务被标记为追踪不完整，而不是静默通过。✅（`assess_traceability` status=incomplete）
- 该能力不自动替代人工做复杂架构决策。✅（默认关闭，fail-open）

## 6. 阶段二：M2 核心可靠性

目标：在交付闭环成立后，降低失败和人工恢复成本。

状态：`accepted`（M2.1-M2.5 交付物与验收达成，2026-08-12 完成正式验收。M2.5 偏差反馈为本轮补齐实现（deviation.py + `agent_go deviation` CLI + executor 集成），M2.2 failure_analysis/effective_strategy 持久化补全到 verify_state.json；Plan preflight repair 作为 M2.1 的执行前确定性修订能力补齐。全量 2134 测试通过。CI yaml 依赖阻塞已修复。两处 ⚠️ 属刻意保守：有界 Reflexion 每次 retry 触发（非阈值后）、`/goal` 保持默认关闭）。

### M2.1 验证与失败阻断

交付物：

- shell、lint/type/test、semantic evaluator 的职责边界。✅（executor 验证循环：shell 命令确定性检查 + evaluator.py LLM 语义评估分层，失败上下文注入 `_build_repair_prompt` executor.py:640-703）
- Plan preflight repair。✅（`cli._preflight_repair_plan` 在 Worker 启动前检查确定性 Plan 缺陷；最多自动修订一次，重新校验失败则阻断；记录 `plan_repair_count`/`plan_repair_history`）
- 验证失败上下文和 repair retry 记录。✅（`results[].verification_results` 记录 exit_code/stdout/stderr tail/retry_count；metering 记录 retry 成本）
- 上游失败时下游明确 blocked。✅（pipeline.py:442-448 依赖链级联：upstream failed/blocked → downstream blocked）
- 无进展检测，避免重复 retry 消耗预算。
  - ✅ 已落地（2026-08-11）：回退/振荡检测 `diff_stat_hash` + `verification.revert_threshold`（默认 2），同一累积状态出现 ≥ 阈值即终止并标记 `verify_revert`；打地鼠检测 `diverge_similarity_threshold`。见 [workflow-vs-subagent-review.md](design/workflow-vs-subagent-review.md)。
- 循环状态埋点：`diff_stat_hash`、`failure_pattern`、`effective_strategy`、`no_progress`。✅（diff_stat_hash 随 retry 记录；`verify_revert`/diverge 终止信号映射为 failure_pattern=no_progress*，写入 deviation.jsonl（deviation.py）；`effective_strategy` 由 readonly_review 写入 verify_state.json，M2.2 补全）
- 有界 Reflexion：仅在 retry 达到阈值后分析根因，不改变默认成功语义。✅（2026-08-16 补齐：Reflexion 阈值化 retry≥2 触发（faebd0b，B5=b 决策落地）+ verify_state 稳定契约版本化（1d00870）；readonly_review 独立模型黑盒分析，结果仅注入 repair prompt 不改变任务状态）

验收：

- 注入验证失败后，系统能自动修复或明确阻断。✅（修复重试 + blocked 阻断 + G8 拒绝短路；`test_verification_failure_marks_failed`/`test_revert_detection_terminates_early`）
- 重试次数、成本、失败原因可查询。✅（`results[].retry_count/total_cost_usd/failure_reason`；metering.jsonl）
- 连续无进展不会无限消耗 token。✅（revert_threshold=2 提前终止 + diverge 检测 + L2 子任务成本上限）
- 循环状态可供后续 KnowledgeStore 消费，但不会在 M2 自动修改历史知识。✅（verify_state.json + deviation.jsonl 持久化，只读消费）

### M2.2 Goal/Loop 受控增强

调研结论表明，当前系统已有硬迭代上限、资源预算和程序化验证，但缺少无进展检测、根因分析和策略升级。M2 只实现低风险、可观测的部分：

- retry 间记录 diff/stat 哈希。✅（`_diff_stat_hash` executor.py:987 + `_diff_stat_hashes` 累积）
- 连续两次无实质变化时提前终止，标记 `no_progress`。✅（revert_threshold=2 检测同态终止，kill_reason=verify_revert → failure_pattern=no_progress，deviation.py 映射）
- retry 达到阈值后，可调用独立 evaluator 生成 `failure_analysis`。✅（readonly_review 独立模型生成 root_cause；M2.2 补全后写入 verify_state.json 的 failure_analysis 字段）
- Reflexion 结果只用于下一次 repair prompt，不直接改变任务状态。✅（`_build_repair_prompt` 注入 readonly_review，status 判定不受影响）
- 每次额外分析必须受 token、次数和任务预算约束。✅（readonly_review timeout_ms/max_tokens 配置；metering role=reviewer 单独计费，受 L2/L3 成本控制约束）
- `/goal` 继续默认关闭，直到语义 goal 通过独立实验验证。✅（config.goal.enabled 默认 false，--goal 显式开启）

验收：

- 无进展任务不会跑满全部 retry 上限。✅（revert/diverge 检测提前终止；`test_revert_detection_terminates_early`/`test_divergence_early_terminates`）
- `failure_analysis` 和 `effective_strategy` 写入 `verify_state.json`。✅（`_persist_verify_state` 新增参数，readonly_review 结果持久化；`test_verify_state_persists_failure_analysis`）
- Reflexion 失败或超时会降级为普通 repair，不阻塞主流程。✅（`_safe_optional_call` fail-open，executor.py:1626-1642）
- 额外 Reflexion 成本可单独计量。✅（metering role=reviewer 事件，`test_review_enabled_parses_response` 断言）

### M2.3 成本与进程边界

交付物：

- 单次调用、子任务、任务级预算的统一语义。✅（L1 `claude --max-budget-usd` per difficulty subtask.py:288；L2 子任务累计 `_meter_cost_for_sub` executor.py；L3 任务级 `max_budget_usd`/动态默认预算 pipeline.py:474-529）
- 并发启动前 reservation。✅（`_subtask_budget_reservation` pipeline.py:156，波前预检）
- metering 不可用时 fail-safe。✅（pipeline.py:495 metering_unavailable → infrastructure_failure，不误判模型）
- Claude 及其子进程整体回收。✅（`_terminate_process_group` SIGTERM→SIGKILL subtask.py:332-342）

验收：

- 并发任务不会在预算检查竞态下无限超支。✅（reservation + 波前原子检查；`--budget-mode` strict/degrade/ignore）
- `cost_censored` 不重复计费。✅（pipeline.py:112-113 控制审计事件不计入累计消费）
- timeout、budget abort 和 infrastructure failure 可区分。✅（failure.py KILL_REASON_CLASS 映射 + classify_failure 优先级）

### M2.4 人工审查与恢复体验

交付物：

- 失败摘要、保留 worktree、review、resume 指引统一。✅（`cmd_inspect` 列保留 worktree + `cmd_review` 汇总 + resume 指引，cli.py:1091/1250）
- `inspect -> review -> resume -> delivery` 形成闭环。✅（inspect 现场查看 → review 批准 → resume 重跑失败子任务 → pr/merge 交付）
- CLI 和 MCP 返回同样的核心状态和修复建议。✅（MCP tools run/resume/inspect/review/governance + diagnose_failure 与 CLI 共享 `task_status`/meta 读取）

验收：

- 用户无需阅读完整日志即可判断下一步动作。✅（review CLI 输出批准/拒绝/建议 + inspect 状态摘要；cli.py:1503 给出明确下一步命令）
- 失败任务的人工恢复时间可以测量。✅（meta 记录 created/finished 时间戳 + elapsed；`agent_go status` 汇总）

### M2.5 Spec/Architecture 偏差反馈

目标：把失败从一次性错误升级为可定位、可修复、可复用的偏差记录，但不在 M2 自动修改全局知识或 Spec。

交付物：

- `spec_deviation`：需求、范围或验收标准与实现的偏差。✅（deviation.py `DeviationEvent.deviation_type`，支持 spec_deviation 类型）
- `architecture_deviation`：模块边界、依赖方向或架构约束偏差。✅（scope_violation 检测 → architecture_deviation，executor L1 范围合规）
- `acceptance_gap`：未满足的验收标准及其验证证据。✅（semantic_fail/failed_cmds → acceptance_gap + evidence）
- 偏差根因分类：Spec 不完整、Plan 误解、拆解错误、实现错误、验证不足、交付汇总错误。✅（`ROOT_CAUSE_CATEGORIES` 6 类 + `classify_deviation` 确定性启发式）
- 偏差修复状态、人工决策和是否需要回写 Spec 的记录。✅（`human_decision`/`spec_rewrite_required`/`requires_approval` 字段）
- 偏差数据与 `failure_pattern`、`effective_strategy` 关联。✅（kill_reason→failure_pattern=no_progress 映射；effective_strategy 从 readonly_review 传入）

验收：

- 每个未通过的真实任务都能区分执行失败、Spec 偏差、架构偏差和交付失败。✅（classify_deviation：infra→非能力偏差 requires_approval=False；scope→architecture_deviation；semantic/失败命令→acceptance_gap；delivery_failure 单列）
- 偏差记录能进入下一次 repair prompt，但不会未经批准修改全局 Plan 或知识库。✅（deviation 记录只持久化供查询，修复仍走验证循环，无自动回写）
- 偏差的人工处理时间和重复发生率可统计。✅（`aggregate_deviations` 输出 resolved/require_approval/spec_rewrite_pending + by_root_cause/by_failure_class 分布，`agent_go deviation` CLI 查询）

状态：`accepted`（M2.5 于 2026-08-12 补齐实现，见阶段二状态行；deviation.py 数据层 + `agent_go deviation` CLI + executor 失败集成）。

## 7. 阶段三：M3 真实任务验证

目标：验证产品主线，而不是继续用单元测试数量替代产品证据。

状态：`accepted`（M3.1-M3.2 于 2026-08-12 完成真实仓库 dogfood 验证。12 个任务 × 2 真实仓库（vibe-astock / llama-defender）× 6 类场景（新增功能/bug 修复/跨文件重构/测试补充/依赖配置迁移/失败恢复），通过率 11/12（91.7%），总成本 $0.20（$0.017/任务）。evaluator diff 累积基座缺陷在此阶段发现并修复（真实仓库多 commit 历史导致 `root..HEAD` 失效 → 改用 `_base_commit`）。归档基线 `m3-dogfood-20260812`）。

### M3.1 Dogfood 任务集

使用 10-20 个真实工程任务，覆盖：

- 新增功能。✅（ld-add-message-char-estimator / ld-add-text-similarity-batch，均通过）
- bug 修复。✅（va-fix-is-weekend-invalid-date / va-fix-validate-trade-date-robustness，均通过）
- 跨文件重构。✅（ld-refactor-scrub-ansi-shared 通过；va-refactor-tx-symbol-dedup 失败——agent 产出正确但验证命令跑全量 tests/ 触发既有失败，属验证命令设计经验）
- 测试补充。✅（ld-test-session-tier-boundaries / ld-test-detect-content-type / va-test-strip-model-noise，均通过）
- 依赖或配置迁移。✅（ld-config-migrate-sieve-defaults / va-config-add-cache-ttl，均通过）
- 一个明确的失败恢复场景。✅（ld-recover-pipe-verification：验证命令门禁合规要求触发失败恢复闭环，通过）

每个任务必须记录：

- 是否产生 Accepted Delivery。✅（results.jsonl `accepted_delivery` 字段；fixture 模式无真实 PR 但有 delivery_branch）
- 是否需要人工改 Plan。✅（0/12 需人工改 Plan，Plan 一次性生成）
- 是否需要人工修复代码。✅（0/12 需人工修复，2 个重试后自动通过）
- 总耗时。✅（`elapsed_sec`，12 任务 41.4 分钟）
- 总成本。✅（`total_cost_usd`，$0.2038）
- 重试次数。✅（`total_retries`，3 个任务触发重试）
- 人工介入分钟数。✅（0——全自动化，无人工介入）
- 失败分类。✅（9 success / 2 timeout / 1 verification_failure，0 infrastructure）
- requirement/acceptance criterion 追踪完整性。✅（`agent_go governance <task-id>` 可查 traceability_matrix）
- architecture review 结果和偏差数量。✅（默认 fail-open；deviation.jsonl 记录 1 条 timeout 偏差，正确标记为非能力偏差）
- spec/architecture deviation 及其修复状态。✅（`agent_go deviation` 聚合查询；1 条 deviation requires_approval=False）
- Goal Contract、最终 `goal_mode`、Goal backend、Goal turns、Goal stop reason 和 Goal evidence。⚠️（`/goal` 默认关闭，M3 未启用语义 goal——属刻意保守，见 M2.2）
- 是否触发 Goal、是否发生 Goal timeout/budget stop、Goal 额外成本和人工介入时间。⚠️（同上，Goal 未启用，N/A）

### M3.2 产品验收门禁

M3 不预先承诺绝对 KPI，先建立可信基线。至少需要：

- 100% 任务有完整结果记录。✅（12/12 完整 jsonl 记录）
- 100% 任务有明确最终状态。✅（全部有 status + failure_class）
- 0 个交付成功但找不到目标分支或 PR 的任务。✅（fixture 模式 delivery_branch 全部生成；真实 PR 证据见 M1: urika/llama-defender#8 MERGED）
- infrastructure failure 与 model failure 分开统计。✅（0 infrastructure / 2 timeout / 1 verification_failure / 9 success，分开统计）
- 所有失败任务都有可执行恢复路径。✅（失败任务 worktree 保留，`agent_go inspect` + `resume` 可恢复；va-refactor 的失败因验证命令设计，修正验证命令即可恢复）
- 关键 requirement 都能追踪到测试和最终交付物。✅（governance traceability_matrix + verification_results）
- 架构审查结果与最终变更一致，或有明确的人工覆盖记录。✅（architecture_review 默认 fail-open，deviation.jsonl 记录偏差供审查）

完成 M3 后，基于真实数据设定下一阶段目标，而不是继续沿用未经验证的 `$0.05` 或 `K1 ≥97%` 目标。✅（M3 实测 $/任务 $0.017，$0.05 目标已达成；真实仓库通过率 91.7%）

## 7.5 阶段四：M4 goal 回溯

目标：`completed` 不再只是「无 subtask 失败」的否定式判定，而是回看 goal/acceptance/overview 的合规度，避免「执行都过但漏了验收」的假交付。

状态：`accepted`（2026-08-20 收口）。对应 `business-architecture.md` 缺口 2（goal 回溯断裂）。实现：`planning.compute_goal_adherence`（确定性、零 LLM）在 pipeline 收尾写入 `meta.goal_adherence`；13 项测试（tests/test_goal_adherence.py）。

### M4.1 交付物

- `completed` 判定回看 goal/acceptance/overview，缺失验收不得静默通过。✅（四个维度：契约证据执行覆盖 / 无验证静默通过子任务 / 验收 ID 覆盖 / 交付要求达成）
- goal 合规度作为与 `status` 正交的维度记录（不改变 verification 决定 status 的语义，见 A1 决策）。✅（`meta.goal_adherence`：level=full/partial/low/unknown + score + gaps + detail；失败任务不打 needs_human_review）
- 合规度低的任务在 review/replay/status 中可见，供人判断是否需人工补验收。✅（review 报告 🎯 节 + `show` ⚠️ 提示 + replay 摘要 + Web 详情透传；`needs_human_review=True` 时明示「建议人工补验收」）

### M4.2 验收

- 一个「执行全过但漏了验收标准」的任务被标记为合规度不足，而非 completed。✅（TestSilentAcceptanceMiss 6 用例：证据未执行/被拒绝/未通过/静默通过/AC 未覆盖/交付未达成）
- goal 合规度可查询、可追溯。✅（meta 持久化 + review/show/replay/web 四面可见，gaps 含 type+detail 可审计）

## 7.6 阶段五：M5 问题跟踪与 Issue 联动

目标：把失败从「单任务内」升级为「跨任务可聚合」的一等公民（Problem 实体），支撑根因分析和复发率统计。

| M | 内容 | 状态 |
|---|------|------|
| **M5 问题跟踪** | 全局 `~/.agent_go/problems.jsonl`，跨任务累积 Problem 实体：三态 + 复发重开、半衰期（stale_after_days→dormant）、葬礼（resolution_summary）；`agent_go problems` CLI（列表/聚合/详情/JSON） | ✅ `implemented`（2026-08-16：d0335ff 数据层 + d7150a3 CLI 收尾；同时支撑谦逊层 H3 与信任指标「复发可见率」） |
| **Issue 联动**（原 M6） | `--track-issues` 显式开启（默认关，避免 issue 洪水，见 A6 决策）；Problem 状态机 + GitHub issue 联动 | `deferred`（未启动；「M6」编号自 2026-08-17 起由决策辅助系列使用，见 §7.12，故改称 Issue 联动避免歧义） |

## 7.7 阶段六：智能闭环与自治（决策门后）

目标：从「反应式」升级到「反思式」，再逐步收敛人工介入点，向全自主交付（渐进自治）演进。

### 决策门（对应 `business-architecture.md` B 类决策）

| 决策 | 问题 | 结论 |
|------|------|---------|
| B2 定位 | 交付工具 vs bench 工具 | ✅ 交付工具（M1 证据） |
| B5 循环智能层级 | 反应式 → 反思式？ | ✅ 已拍板 B5=b（2026-08-16）：先做最小止血 + 埋点，Reflexion 阈值化已落地（faebd0b + 1d00870） |
| B4 问题跟踪定位 | issue 状态机 vs 聚合 vs Reflexion 记忆源 | ✅ 聚合 + Reflexion 记忆源：M5 Problem 实体已落地（承载复发/解法），GitHub issue 联动 deferred |
| B1 merge 策略 | 分叉时 ff-only 失败 vs 自动 merge commit | 未拍板：ff-only 保守提示 + 手动命令兜底（现状沿用） |
| B3 spec ROI | 0 次使用还做 4 天闭环吗 | ✅ 已冒烟（2026-08-15，5 任务）：R2 追踪完整率 0→100%（4/4 有效交付）、R1 80%（唯一失败为本地 worker 能力，与 spec 无关）→ **弱正 ROI，spec 闭环保留轻量形态** |

### 能力清单（按自治度递进，每级设产品价值门禁）

**阶段 A — 补齐工程闭环（最高优先）**
- A1 文件所有权约束：同一核心文件只允许一个 subtask 负责（规划期 + L1 门禁强制）。✅ 已实现（88d0c5a，`_is_core_file` + `core_file_shared_ownership` blocking）。⚠️ 2026-08-24 降级为 warning（ISSUE-44：串行 merge 机制下分层共享合法，blocking 误杀合法计划，三臂 bench 10 run 0 执行即 BLOCKED）；并行无序共享仍 blocking（`file_overlap_without_dependency`）。
- A2 函数级验收契约：subtask 验收绑定函数/行为级条件（`_extract_verification_commands` 扩展）。✅ 已实现（f6e2cb0，`classify_verification_scope` 五级锚定 + suite 级弱锚定告警）。
- A3 未提交基线处理：启动检测 dirty worktree，`--baseline` 显式提交或强提示。✅ 已实现（`get_dirty_files`/`commit_baseline`；`--allow-dirty`/`--baseline`；headless 默认 fail-safe 中止；meta 记录 `baseline_dirty`/`baseline_action`）。
- A4 M4 goal 回溯（见 §7.5，✅ accepted 2026-08-20）。

**阶段 B — spec 闭环验证（✅ 冒烟完成，弱正 ROI 留轻量）**
- B1 spec 持久化 + 闭环实施。✅ 已实现（d5cc175：ID 链条/锚定门禁/spec 快照/后段注入/traceability 自动触发/do-not-touch fail-close；b01c644 冒烟实证修复：预算头寸口径 + AC 硬映射兜底 + e2e 路径）。
- B2 AST 冲突检测器（P9，97% 精度零 LLM 成本）加进 Spec Gate。未启动（轻量形态下优先级下调）。
- B3 5+ 真实任务冒烟。✅ 已完成（2026-08-15，结论见 B3 决策行）。

**阶段 C — 智能闭环（B5=b 已拍板）**
- C1 数据埋点补全（verify_state schema 前向兼容 KnowledgeStore）。✅ 已实现（1d00870，verify_state 稳定契约版本化 + reflexion 来源标记）。
- C2 Reflexion 批评层：改「retry≥2 触发」，受 token/次数/预算约束。✅ 已实现（faebd0b）。
- C3 局部重规划：失败触发一次 Plan 拆分建议，默认人工确认。✅ 已实现（2026-08-21，无进展信号 verify_revert/divergence/失败模式重复触发一次拆分修复；契约遵守 F-VERIFY-6：最多一次、继承父预算（L2 预检）、replan_triggered/replan_succeeded 入 result+log_event、交互模式人工确认/headless 需 verification.replan.auto_apply=true、拆分步只注入修复 prompt 不扩大任务图；tests/test_replan.py 17 用例）。
- C4 KnowledgeStore A/B：历史经验注入 vs 无，仅 ADR↑ + 成本不劣化 + 可淘汰才产品化。🔨 实现+smoke 链路验证完成（2026-08-21）：`knowledge.py` 三源提取（Problem/deviation/verify_state）注入 repair prompt，`--with-knowledge` 注入臂 + `knowledge_arm` 臂标记 + `knowledge_injected` 埋点 + 可淘汰（suppressed_ids/dormant 排除）；smoke 7×2×2 臂注入链路真实生效，但参与度仅 2/28（problems.jsonl 全 opened 无 resolution_summary），两臂指标差异为噪声级。✅ 同日补葬礼回写链路（`record_resolution`：重试后成功自动回写「失败模式+解法」，problem_resolution_written 埋点），知识库从此能攒「解法」级经验。全量 decision A/B 待知识库积累后重约。

**阶段 D — 自治决策（谨慎）**
- D1 Reviewer 灰度（高风险任务，review cost ≤ 主任务 20% 门禁）。
- D2 自动 merge 策略落地（B1 决策后）。
- D3 目标态：人只在「例外点」介入（Plan 确认 + merge 决策 + 失败审查），其余全自动。
- **放行门（#49 信任指标）**：审查后修改率下降 + 盲区命中率高 + 复发可见率上升——交底可信，才允许自动化升级。D-0 现状报告（2026-08-21，[trust-metrics-baseline](../docs/design/trust-metrics-baseline-2026-08-21.md)）：~~不可判定~~ → 三指标已有两路自动读数：交付后返工率 1.6%（2/128，行动项 1 ✅）、盲区命中率 0/15（行动项 2 ✅，`agent_go trust`）；复发可见率真实样本仍不足（problem 录制上线后 n=5），剩余前置 = 攒 ≥30 真实任务窗口。**D-1 放行评估（2026-08-28，[trust-metrics-eval-d1](../docs/design/trust-metrics-eval-d1-2026-08-28.md)）：不放行**——返工率 3.8% 达标（成熟期线），复发可见率方向对但 n=1，审查后修改率 n=3 < 10 不可判定，盲区命中率 0/37 低于 50% 下限（口径失灵，阻塞项 A1）；行动项 A1 口径修复 + A2 攒 ≥10 review 决策 + A3 攒失败样本。

**阶段 E — Spec-as-Source 探索（不排期，试点）**
- 仅在安全/受限域（OpenAPI→stub 类）试点 L5，验证「再生测试」质量门禁。

### 关键路径

```text
阶段 A（工程闭环）✅ ──┬─ A4 goal 回溯（M4，收尾中）
                      └─ A5 问题跟踪（M5 ✅；Issue 联动 deferred）
阶段 B（spec 闭环）✅ 冒烟弱正 ROI → 阶段 C（智能闭环：C1/C2 ✅ → C3 局部重规划 / C4 KnowledgeStore A/B）
                               → 阶段 D（自治，依赖 C + B1 + 信任指标放行门）
                               → 阶段 E（仅试点）
```

## 7.8 阶段七：模型与执行能力（M4.5 里程碑，已完成 2026-08-15）

目标：从「单一模型链」升级为「**模型池化 + 难度自适应执行**」——hard 任务通过率从 0/6 到 94.4%。

### 背景（实验证据驱动）

hard（功能系统级）任务在 Plan→拆分→worker 局部执行的流程下全部失败（本地 35B 0/6）。对照实验证明根因是**拆分丢失全局上下文**，而非模型能力——端到端单模型执行同一任务成功。由此建立「拆分 vs 端到端」判定框架并落地模型池化。

### 已交付

| 能力 | 说明 | 证据 |
|------|------|------|
| **e2e 端到端模式** | hard/架构级任务不拆分子任务，保留全局上下文（worktree 隔离 + 验证循环 + goal + metering 全保留）；判定框架：L0 `--e2e`/`--split` flag > L1 `min_difficulty` > L2 架构级特征信号 > L3 默认拆分 | hard 通过率 0/6 → 33%（首个跃迁） |
| **模型实体三层设计**（P1-P3） | ① `models.json` registry（endpoint/key_ref/thinking/JSON 遵从/TCO/quality_tags，`agent_go models list/add`）② `router.roles` 角色绑定（planner/evaluator/worker/reviewer，thinking 场景覆盖）③ 部署拓扑收敛代理侧（worker_backends deprecated） | 接入新模型零代码（GLM/K3/v4-pro 声明式适配） |
| **方案 B 生产配置** | planner=K3（coding 拆解强）+ evaluator=GLM（JSON 评估稳定，规避 K3 纯 thinking 缺陷）+ worker 混合路由 + goal force | hard 17/18（94.4%）、medium/easy 6/6（100%）、真实 dogfood 通过 |
| **R8 路由归因** | 代理响应头携带 route_target/actual_model/cost，metering is_local 纠正（force_fallback 回退不再误判本地） | 成本/归因可信，选型决策依据 |
| **验证命令白名单扩展** | pip/pip3 install 支持（`&&` 组合命令逐段校验） | db-performance 通过 |
| **看板** | 5 阶段 × 3 类型卡片任务管理（web-only，SSE + 审计） | 冒烟验证通过 |
| **R9 策略可视** | 配置中心展示代理路由策略（模型偏好/云端模型/阈值） | 联合测试通过 |

### 通过率演进（6 个 canonical hard 任务，同口径）

```text
本地 35B 拆分           0/6  (0%)
e2e + v4-flash         2/6  (33%)   ← e2e 端到端模式
e2e + v4-pro           3/6  (50%)
e2e + GLM              5/6  (83%)
e2e + K3               3/6  (50%)
e2e + K3 planner + GLM evaluator   17/18 (94.4%，3 次重跑)  ← 方案 B
```

架构改进贡献排序：e2e 端到端（0→33%）> planner/evaluator 上强模型（33→83%）> 角色互补（83→94.4%）> 验证白名单扩展。

### 验收标准（全部达成）

1. 接入新模型 = `agent_go models add` + `router.roles` 绑定，**零代码改动**（thinking/JSON/TCO 声明式）
2. 全难度可用：hard 94.4% + medium/easy 100%
3. 真实仓库功能任务端到端 DELIVERY_READY + merge 交付
4. 成本/归因可信（R8 修正后 metering）
5. 模型选型有数据依据（[model-selection-report.md](design/model-selection-report.md)：6 组合对比）

### 与 §7.7 阶段六的关系

本阶段是阶段六「智能闭环」的**执行能力底座**：模型池化 + 难度自适应 + 归因可信后，阶段 C（Reflexion/局部重规划）和阶段 D（自治决策）的「换模型/归因复盘」才有可靠基础。

## 7.9 阶段八：诊断数据面消费与契约治理（已完成 2026-08-19）

目标：llama-defender R13-R16 诊断数据面（会话台账/轮级指标/请求档案/ctx_config/backend props）的消费侧落地——让「代理侧可见」变成「agent_go 计量、评测、处置可用」；同步完成三项目架构 review 的 agent_go 侧整改。

### 已交付

| 能力 | 说明 | 证据 |
|------|------|------|
| **C1 会话头注入** | worker/planner/evaluator 统一携带 `X-Claude-Code-Session-Id`（md5(task:sub)[:8]+可读后缀），台账按会话精确归因 | 实测 `/api/sessions` 见 `key_source=header` |
| **C2 诊断入计量** | R13 四头（diag_request_id/prompt_processed_n/hit_ratio/epoch_count）入 metering；子任务结束追加 `worker_diag` 事件 | metering.jsonl 实测含 session_key |
| **C3 评测诊断维度** | `eval analyze_cost` 增 route_distribution（cloud>30% 告警）/hit_ratio_by_model/injection_counts | test_eval 增补 5 用例 |
| **C4 轮级看门狗** | 执行期间轮询 session ledger 检测重复轮（v1 检测+上报不杀进程，干预走 verify/retry） | tests/test_diag_watchdog.py（6 用例） |
| **C5 批次可复现** | bench 快照 ctx_config/route_config → proxy_context sidecar 入 batch manifest | `eval batch-manifest --proxy-context` |
| **C6 失败处置提速** | `inspect` 输出 ledger/archive/metrics 取数提示；`review --deep` 附 sent_view 档案摘要 | — |
| **C7 健康检查扩展** | `config status` 增 ctx_config/backend_props（501 结构化降级显示） | 活代理实测正确 |
| **契约测试 F 组** | `tools/check_llama_defender_contract.py` 增 6 用例固化 R13-R16 消费面 | 20 PASS / 0 FAIL / 1 SKIP（F6=服务方 known-issue L-6） |
| **Review 整改（A-1/A-2/A-3）** | `worker_backends` 收敛单值 `worker_base_url`；真实模型探测切 `/api/status` JSON（HTML 降级兜底）；`diag.CONTRACT_API_VERSION="2"` 契约版本标注 | ISSUE-41~43；全量 2598 测试通过 |

设计原则：诊断消费**全部 fail-open**（代理不可达/端点缺失/字段缺失一律跳过，绝不阻断主流程）；header 构造/截断口径以代理侧契约文档（api_version="2"）为唯一权威。

文档：[diag-dataplane-consumer-requirements-20260819.md](design/diag-dataplane-consumer-requirements-20260819.md)（需求+实施记录）、[three-project-architecture-review-20260819.md](design/three-project-architecture-review-20260819.md)（三项目 review，P1 项 swe-eval S-1/S-2 同日落地）。

## 7.10 阶段九：谦逊层与信任指标（已完成 2026-08-16）

目标：产品叙事从「自动化」升级为「可信的自动化」——每次自动化升级以「先证明交底可信」为前提（设计：[humility-layer-design.md](design/humility-layer-design.md)）。

### 已交付

| 能力 | 说明 | 证据 |
|------|------|------|
| **H1 交付盲区清单 + H4 未覆盖视角** | 交底报告标注「没验证什么」与未采用的备选视角 | 705e372 |
| **H2 层间归因** | 失败可定位到「层」（修 spec / 修 plan / 调预算 / 换模型） | 8fb9182 |
| **H3 Problem 实体数据层** | 半衰期 + 葬礼，M5 地基（见 §7.6） | d0335ff |
| **#48 交底报告 + #49 信任指标 + #50 信任体验** | `metrics.compute_trust_metrics`：审查后修改率 / 盲区命中率 / 复发可见率 | 8339cab |
| **#51/#52 谦逊层 UI** | Web 任务详情盲区卡片 + `inspect` 失败历史 | 98a2d59 |

信任指标是阶段 D（自治决策）的放行门：审查后修改率下降 + 盲区命中率高 + 复发可见率上升——交底可信，才允许自动化升级。

## 7.11 阶段十：Web 操作台全功能与协作扩展（已完成 2026-08-13 ~ 08-18）

目标：把 CLI 能力完整搬到 Web 操作台（观测 + 处置 + 配置中心），并向多用户协作扩展。

### 已交付

- **Web 操作台全功能（R1-R17）**：观测 + 处置（run/resume/cancel/clean/review/merge/pr/confirm）+ 配置中心（local⇄cloud）+ SSE。纯本地 Golden Path 端到端验收通过（[web-golden-path-acceptance-2026-08-13.md](archive/reference/web-golden-path-acceptance-2026-08-13.md)：10/10 步骤、纯本地 $0.00、终态 ACCEPTED_DELIVERY、审计链完整）。
- **协作扩展**：任务报告导出（md/html，a390192）、多用户角色（admin/viewer 双 token，a74bb1c）、任务级互斥锁 + 报告 Web 化（cccc467）、任务备注（86d409b）、规模化保护（并发上限 + 磁盘告警，62cda1e）。
- **可靠性 P0**：多级模型降级链（evaluator fallback/fallbacks + worker_models_fallback_chain）+ 低置信度 evaluator 仲裁（5bf4bec，防假阳性/假阴性污染验证门禁）。
- **发布准备**：QUICKSTART 5 分钟上手 + 打包验证（e38bbb2）；CI 首次全绿（ruff 64 / mypy 156 预存错误清零，04e547f/4bde81d）。
- **生态沉淀**：模型评测方法论（1e384a0，M5.4——评测流程/可信度/教训）。

注：本阶段协作扩展在提交记录中使用「M5.x」编号（M5.1 降级链样本 / M5.2 协作 / M5.3 规模化 / M5.4 生态），与 §7.6「M5 问题跟踪」是两条并行线，编号沿用历史、不再迁移。

## 7.12 阶段十一：决策辅助 M6.1-M6.5（已完成 2026-08-17 ~ 08-19）

目标：证据驱动的策略建议层，**不升级为自动决策执行层**（设计：[decision-assistant-design.md](design/decision-assistant-design.md)；用户指南：[user-guide-decision-assistant.md](user-guide-decision-assistant.md)）。

三边界：① 目标由人定义（LLM 不改目标）；② 建议不直接执行（requires_approval，确认后走 --apply）；③ 证据强制绑定（强制 `--results` manifest 校验，无证据拒答，输出必带 `evidence_refs`）。

| M | 能力 | 状态 |
|---|------|------|
| M6.1 | `eval insight` MVP：证据物化（失败模式/成本/环境快照，evidence.py）+ 结构化建议 | ✅ 4f743c4 |
| M6.2 | decision log：统一决策记录（change/evidence/goal/expected/actual），可审计可复盘 | ✅ 2af73c1 |
| M6.3 | Web 🧠 洞察 tab：洞察报表 + 决策历史展示 | ✅ fd4e218 |
| M6.4 | 规则初筛 + LLM 精排：`router recommend` 升级为确定性问题识别 + LLM 跨维权衡 | ✅ 6c68a09 |
| M6.5 | `insight --apply-suggestion N`：确认后自动应用（config 修改 + 备份 + 审计，可回滚） | ✅ 77ed831 |
| M6.6 | 全自动决策 | **明确不做**（目标由人定义是产品红线） |

注：本系列沿用「M6」编号，与 §7.6 原「M6 issue 联动」不同——后者已改称 Issue 联动（deferred）。

## 7.13 阶段十二：看板驱动的智能任务编排（W1-W4 已交付，2026-08-19 ~ 08-21）

目标：基于看板把任务按「可自动化程度」分流到成本-能力最优路径——本地模型是「模块工厂」而非「系统架构师」（设计：[kanban-task-orchestration.md](design/kanban-task-orchestration.md)、[kanban-board.md](design/kanban-board.md)）。

### 已交付

- **看板 MVP + 二期**：5 阶段列 × 3 类卡片（5bd2022）；归档视图 / 跨进程锁 / 状态快照缓存 / 派发幂等（6849065）；派发原子化 `dispatch_card` 单锁 link+move + 输入卫生（fd30a73）。
- **W1 任务分类器**：卡片 `automation` 字段（auto/manual/review）+ 分类规则（架构级关键词 / difficulty=hard → design 列云端+人工；明确 spec 模块 → implementation 列本地队列）+ dispatch 按列路由（automation=manual 强制 confirm_mode=web 人工确认计划）；PoC 验证通过（7807742）。
- **W2 派发与回流闭环**：dispatch 异步化——立即返回 + 后台执行 + 回调关联（7dc03c9）；任务完成自动流转看板卡片（7bb20fe）；完成/失败通知接入 on_exit → notify_event（a9558c7）。
- **W3 计划确认与审批**：design 列计划确认——确认通过后才流转 implementation（b41af99）；blocked 通知带现场链接 + 失败回流逻辑修复（53e1fdb）；operations 列审批——approve 终确认 / reject 回退重做（9be4f36）。
- **W4 自学习与成本自适应**：分类器自学习——分类准确率统计与可视化（ab5472c）；成本-质量自适应——本地队列 vs 云端 $/pass 权衡分析（6a5300d）；自动降级建议——失败卡片一键 insight 分析生成修复建议（7b78441）。
- **验收测试**：看板工作流端到端验收——manual/auto 判定 + dispatch 流转 + 失败降级通知带现场链接（72a4708）。

### 后续（未排期）

- 本地后台队列批量执行（implementation 列零边际成本异步跑批）。

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

Goal 分为 Goal Contract、Goal Recommendation、Goal Policy 和 Goal Evidence 四层。Goal Contract 默认生成，用于 Plan、verification、治理和交付追踪；Goal Loop 不对所有任务默认开启。

分阶段落地：

1. **Goal Contract 标准化**：从 Task/Spec/acceptance/verification 生成 `goal_contract`，写入 Plan/Subtask/meta，并在 status/review/replay 中可见。
2. **Goal Policy Auto**：增加 `auto|off|force|hook` 策略；Planner 只提供 recommendation，用户覆盖优先，系统确定性策略负责最终解析。
3. **Provider Adapter**：统一 Claude CLI 原生 Goal、Claude Agent SDK、Kimi Code Goal 和 agent_go internal watchdog 的状态、预算、轮次和退出原因。
4. **真实任务 A/B**：用 10-20 个长任务比较 Goal off 与 Goal auto/force，只有 Accepted Delivery 不下降、成本可接受、人工介入下降、false-success 接近 0 时，才扩大默认范围。

当前 `--goal` 保留为显式 force 兼容入口，`--no-goal` 保留为 off 覆盖入口，Goal 默认仍关闭；Goal 不得绕过 Plan Preflight、verification、commit、pipeline、delivery 或 Accepted Delivery 判定。

**A/B 实验结论（2026-08-12，3 任务 × 2 模式小样本）**：6/6 全部 DELIVERY_READY，两臂成功率无差异；force 模式平均成本高 ~30%（仅在实际触发多轮继续时）；goal_turns 计量正确（5/0/8）。小样本不支持默认启用，`goal_policy.policy` 保持 `off`。详见 [goal-ab-experiment-2026-08-12.md](archive/design/goal-ab-experiment-2026-08-12.md)。

### 局部重规划与策略重置

执行前的 Plan preflight repair 已作为 M2 可靠性能力落地：只修复确定性 Plan 缺陷，最多自动修订一次，不能删除需求或放宽验收约束。执行中的局部重规划仍属于后续实验能力：当出现无进展、错误模式重复或变更规模异常但验证持续失败时，可以提出一次局部重规划建议，默认先请求人工确认，不自动改变全局 Plan；自动策略重置属于后续实验能力。

### MCP/Office/IDE/CI 扩展

只有存在真实用户场景、端到端验收和独立成功率指标时进入 roadmap。功能接入不等于产品成功，必须能证明对 Accepted Delivery 或人工成本有贡献。

### 本地模型生命周期管理

弱模型 worker 实验（goal_ab，2026-08-12）已证明本地模型（经 llama-defender 代理）可作为生产 worker 后端，但启停/切换/监控依赖人工操作 manage.sh。将本地模型纳入 agent_go 管理，分阶段落地（设计：[local-model-management-design.md](design/local-model-management-design.md)）：

1. **P0 只读管理面**：`agent_go model status/list/current/diagnose`；分级诊断（proxy/backend/模型漂移）。
2. **P1 启停与切换**：`model start/stop/switch`，切换四步原子序列（switch→stop-backend→reload→start-backend）+ 失败回滚 + 活跃任务并发保护。
3. **P2 服务保活 + Pipeline 集成**：诊断→修复阶梯（reload→start-backend→restart，逐级幂等升级）；pre-flight readiness；`auto_start`/`auto_repair`；**Plan 前模型感知快照注入 planner**（本地不可达时按云端路由，fail-open）；执行中不打断在途任务。不可达且修复失败时明确归因 `infrastructure_failure`。
4. **P3 监控**：status 面板 + web 页面展示后端健康与 ttft 指标。

默认 `local_model_manager.enabled=false`，不改变现有 difficulty→worker_models→worker_backends 路由语义。

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

当前关键路径为：

```text
M0 产品契约与指标冻结  ✅ accepted
  -> M1 交付闭环        ✅ accepted
  -> M2 核心可靠性      ✅ accepted
  -> M3 真实任务验证    ✅ accepted
  -> M4.5 模型池化      ✅ accepted（hard 94.4%、方案 B）
  -> 阶段八 诊断数据面   ✅ 完成（2026-08-19）
  -> 阶段九 谦逊层       ✅ 完成（信任指标放行门已立）
  -> 阶段十 Web 全功能   ✅ 完成（Golden Path 验收 + 协作扩展）
  -> 阶段十一 决策辅助   ✅ M6.1-M6.5（建议层，M6.6 明确不做）
  -> 阶段十二 看板编排   ✅ W1-W4（分类器/派发回流/计划确认审批/自学习与成本自适应）
  -> M4 goal 回溯       ✅ accepted（2026-08-20，compute_goal_adherence）
  -> M5 问题跟踪        ✅ implemented；Issue 联动（原 M6）deferred
  -> 阶段 B spec 闭环   ✅ 冒烟弱正 ROI，留轻量
  -> 阶段 C 智能闭环    C1/C2/C3 ✅ + C4 KnowledgeStore A/B smoke ✅（葬礼回写链路已闭环）
  -> 阶段 D 自治决策    → 信任指标放行门达标 + B1 决策后
  -> 阶段 E Spec-as-Source  仅试点
```

下一阶段三件事（按优先级）：

1. **bench 交付闭环自动验证**：✅ 完成（2026-08-20，`eval bench --with-delivery` 本地交付 merge 闭合判定 + 首个有效 ADR 基线 `delivery-20260820`，见下方基线行）。**M4 goal 回溯**：✅ accepted（同日，`compute_goal_adherence` 正交合规度，「执行全过但漏验收」显式标记）。
2. **阶段 C 续项**：C3 局部重规划 ✅（2026-08-21，无进展触发一次拆分建议，默认人工确认，F-VERIFY-6 契约全守）→ C4 KnowledgeStore A/B（Problem/deviation/verify_state 数据已就位，在 delivery-20260820 基线上做两臂对比）。
3. **阶段 D 放行评估**：信任指标（审查后修改率 / 盲区命中率 / 复发可见率）跨任务积累达标后，启动 Reviewer 灰度与 B1 自动 merge 决策。

在可信 Accepted Delivery 基线建立前，不对「年度 K1 ≥97%」「$/pass ≤$0.03」等绝对目标做硬承诺。当前实测基线：真实仓库通过率 91.7%（11/12）、$/任务 $0.017；**首个有效 ADR 基线 `delivery-20260820`**（2026-08-20，`--with-delivery` 本地交付闭环）：ADR=0.7045（31/44 valid）、Cost per AD=$0.0171、pass_rate_diagnostic=0.75、first_pass_rate=0.727、timeout_rate=9.1%、delivery_failure=0、human_intervention=0、eval gate 通过（$/pass=$0.0156）。口径：decision suite 29 任务 × repeat 2、worker 经本地代理（Qwen3.8-27B），与 decision-20260812 云端基线禁止直接混比。
