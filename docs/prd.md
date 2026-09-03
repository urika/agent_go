# agent_go 产品需求文档

> 版本：v4.3
> 更新日期：2026-09-03
> 配套路线图：[roadmap.md](roadmap.md)
> 当前阶段：M0-M4、M4.5 已 `accepted`；谦逊层 H1-H4、Web 操作台全功能、决策辅助 M6.1-M6.5、看板编排 W1 已交付；bench 交付闭环基线 `delivery-20260820`（ADR=0.7045）已建立；阶段十三「多 Backend 架构」已提出（proposed）
> 北极星目标：**全自主交付（渐进自治）**——把人工介入从每个环节降到只剩「例外点」，而非追求人类完全不参与。
> Goal/Loop 调研输入：[archive/reference/research-goal-loop-mechanism-2026-08-08.md](archive/reference/research-goal-loop-mechanism-2026-08-08.md)
> 当前执行清单：[m0-task-list.md](m0-task-list.md)

## 1. 产品概述

### 1.1 产品定位

agent_go 是一个面向高频使用 Claude Code 及类似 Coding Agent 的工程师的异步开发任务编排器。当前以 Claude Code 为默认 worker backend，但架构上支持可插拔的多种 Agent Runtime。

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

### 1.1a 北极星：全自主交付（渐进自治）

agent_go 的长期目标是**全自主交付**，但定义为三根支柱同时成熟、且每升一级自治必须同步增加审计/回滚能力的渐进过程，而非「无审查自动 merge」的静态终点：

| 支柱 | 含义 | 衡量 |
|---|---|---|
| ① 工程闭环 | 产物可靠到达目标分支、可追溯、失败可分类可恢复 | Accepted Delivery Rate |
| ② 智能闭环 | 从失败中学习，不重复同类错误 | 首次验证通过率 / 复发率 / 重试成本 |
| ③ 人机信任 | 成本可控、可审计、可回滚、不被「虚假控制感」欺骗 | Cost per AD / Human Intervention Minutes |

可测量定义：

```text
Accepted Delivery Rate 逼近 1  ∧  Human Intervention Minutes → 0  ∧  Cost per AD 持续下降
```

三个「例外点」长期保留人工决策：**Plan 确认、merge 决策、失败审查**。其余环节逐步收敛为全自动。

关键边界（源自 SDD 学术综述 `design/sdd-references-and-frameworks.md`）：

- 当前 L2（Spec-First）→ 目标 L3（Spec-Anchored），**不以 L5（Spec-as-Source 全自动）为近期目标**。
- 同源审查是「回响」（P10）：`judge != candidate` 是铁律，全自主 merge 前必须有「不同源独立验证」兜底。
- 最高杠杆是 **Spec 质量**（P9），而非堆更多 Agent。

**产品叙事（谦逊地变强）**：agent_go 卖的不是「自动化」，是「可信的自动化」——每一次自动化升级都以「先证明交底可信」为前提。落地方向见 [谦逊层设计](design/humility-layer-design.md)（盲区交底 / 层间归因 / 知识生命周期 / 未覆盖视角 + 信任指标体系：审查后修改率 / 盲区命中率 / 复发可见率）。

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
- 可插拔 worker backend 接口（当前默认 Claude Code，支持 AgentLoop 直接 API 路径）。
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
- 为所有 backend 提供 100% 能力 parity；新 backend 按标准接口逐步评估和接入。
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

当前版本不允许执行失败后自动递归修改全局 Plan。执行前的 Plan 预检修复属于 F-PLAN-3；执行中的局部重规划已作为受控实验能力落地（2026-08-21，C3），必须：

- 最多触发一次。
- 继承父任务预算和权限。
- 记录 `replan_triggered`、`replan_succeeded`。
- 默认支持人工确认。
- 不能因重规划递归扩大任务图。

当前实现：无进展信号（`verify_revert` / `verify_divergence` / 失败模式重复）触发一次 Plan 拆分建议（`agent_go/replan.py`，LLM 拆分 + 确定性启发式兜底）；拆分步只注入修复 prompt，不创建新子任务节点；交互模式弹人工确认，headless/--yes 需显式 `verification.replan.auto_apply=true` 才自动执行（默认只记录建议等待人工处置）；执行前做 L2 预算预检（父任务预算已耗尽则不执行）；`replan_*` 字段入子任务 result 与 `log_event` 可审计。自动策略重置仍属于后续实验能力。

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

#### F-ROUTE-2 模型池化（✅ 已实现，2026-08-15）

- 模型单一实体三层：① `models.json` registry（模型固有：endpoint/key_ref/thinking 推理特性/JSON 输出遵从/TCO/quality_tags）② `router.roles` 角色绑定（planner/evaluator/worker/reviewer + 场景参数覆盖）③ 部署拓扑收敛代理侧（worker_backends deprecated）。
- 接入新模型 = `agent_go models add` + 角色绑定，**零代码改动**（声明式 thinking/JSON 自动适配）。
- 声明式能力：v4-pro/GLM thinking enabled、K3 thinking 始终开启、JSON strict/loose + response_format，均按 registry 声明自动处理。

#### F-ROUTE-3 难度自适应执行（✅ 已实现，2026-08-15）

- **e2e 端到端模式**：hard/架构级任务不拆分子任务（保留全局上下文），判定框架 L0 flag > L1 `min_difficulty` > L2 架构信号 > L3 默认拆分。
- **方案 B 生产配置**：planner=K3（coding 拆解）+ evaluator=GLM（JSON 评估稳定）+ worker 混合路由 + goal force——hard 17/18（94.4%）、medium/easy 100%、真实 dogfood 通过。
- 验证命令白名单支持 `pip install` 组合命令（`&&` 逐段校验）。

#### F-ROUTE-4 路由归因与策略可视（✅ 已实现，2026-08-15）

- R8 路由归因：代理响应头 route_target/actual_model/cost → metering is_local 纠正（force_fallback 回退不再误判本地）。
- R9 策略可视：配置中心展示代理路由策略（模型偏好/云端模型/阈值/Key 状态）。
- 模型选型数据依据：[model-selection-report.md](design/model-selection-report.md)（6 组合 × 6 hard 任务对比）。

#### F-DIAG-1 诊断数据面消费（✅ 已实现，2026-08-19）

llama-defender R13-R16 诊断数据面的消费侧落地（C1-C7，需求文档：[diag-dataplane-consumer-requirements-20260819.md](design/diag-dataplane-consumer-requirements-20260819.md)）：

- **会话级可观测（C1）**：worker/planner/evaluator 请求统一携带 `X-Claude-Code-Session-Id`（md5(task:sub)[:8] + 可读后缀），代理台账/档案按会话精确可取，不再被无头请求的按天合并污染。
- **诊断字段入计量（C2/C3）**：R13 响应头（`diag_request_id`/`prompt_processed_n`/`hit_ratio`/`epoch_count` 等）→ metering；`eval analyze_cost` 增路由分布（cloud>30% 告警）、按模型缓存命中率、注入计数值维度。
- **轮级看门狗（C4）**：子任务执行期间轮询代理 session ledger 检测重复轮——v1 检测+上报不杀进程，干预走既有 verify/retry 通道。
- **批次可复现（C5）**：bench 启动时快照代理 ctx_config/route_config → `{results}.proxy_context.json` sidecar，入 batch manifest（`eval batch-manifest --proxy-context`）。
- **失败处置与健康（C6/C7）**：`inspect` 输出失败子任务的 ledger/archive/metrics 取数提示；`review --deep` 附 sent_view 档案摘要；`config status` 健康检查增 ctx_config/backend_props（501 结构化降级显示）。
- **降级原则**：全部 fail-open——代理不可达/端点缺失/字段缺失一律跳过，绝不阻断主流程；契约由 `tools/check_llama_defender_contract.py` F 组固化（实测 20 PASS / 0 FAIL / 1 SKIP）。

#### F-ROUTE-5 多级降级链与评估仲裁（✅ 已实现，2026-08-16）

长尾可靠性（5bf4bec）：

- **evaluator 多级降级**：角色路由支持 `fallback`/`fallbacks` 多级 provider 链，空响应/非法 JSON/API 不可用自动依次降级并熔断；registry 模型属性自动补齐，fallback 只需写 model id。
- **低置信度仲裁**：evaluator 低置信度/解析不确定时自动调用 fallback evaluator 仲裁，优先采用更高置信度结果；高置信度结果不增加调用成本——防假阳性/假阴性污染验证门禁。
- **worker 升级链**：`worker_models_fallback_chain` 支持验证失败按 retry 顺序自动升级模型，兼容旧单值 fallback。
- 降级链三次 bench 样本归档验证（M5.1：兜底机制验证，非通过率手段）。

### 4.6 多 Backend 与 Agent Runtime

> 目标：把 agent_go 从“Claude Code 包装器”演进为“可插拔多 Agent Runtime 编排器”，在保持默认路径稳定的前提下，逐步引入开源/定制化 backend。

#### F-BACKEND-1 标准 Worker Backend 接口

系统应定义统一的 worker backend 接口，使不同 Agent Runtime 以相同契约接入子任务执行流程：

- 接收结构化任务描述（TASK.md / prompt）。
- 在指定 git worktree 中执行。
- 支持 headless / `--yes` 非交互模式。
- 返回完成状态、变更证据、成本/用量数据。
- 由 agent_go wrapper 统一完成 `git commit` / `git tag` / 验证循环（backend 可选择自己完成或交由 wrapper）。
- 失败时提供可解析的失败原因，支持 fallback 到默认 backend。
- MCP 工具注入对支持 MCP 的 backend 走同一注入层；不支持的 backend 显式降级（能力标签 `supports_mcp=false`），不得出现同一 MCP 配置在不同 backend 上静默行为不一致。

默认实现保持现有 Claude Code 路径不变；新增 backend 必须通过标准接口接入，不得作为硬编码特例分支。

#### F-BACKEND-2 AgentLoop 直接 API Backend

AgentLoop 作为“简单任务”直接 API backend，应从实验性开关升级为符合标准接口的正式 backend：

- 补齐 verification / scope / stuck 检测机制，与 Claude Code 路径的能力对齐。
- 扩展 ACI 工具集：至少支持 `Read`、`Write`、`Edit`、`Bash`、`Grep`、`Glob`、`view` 长文件导航。
- 引入 `explore` 只读模式，用于复杂子任务前的代码库扫描。
- 保持失败 fallback 到 Claude Code 的能力，直到 AgentLoop 独立通过 bench 验收。
- 仅在 bench 证明其 `Accepted Delivery Rate` 和 `Cost per AD` 不劣化于当前默认路径时，扩大默认启用范围。

#### F-BACKEND-3 开源 Backend 接入（Pi / OpenCode）

在标准 backend 接口稳定后，评估并接入满足以下最小契约的开源 Agent Runtime：

- 支持 headless 执行和指定工作目录。
- 能通过 CLI flag（如 `pi -p`、`--mode json/rpc`）或 SDK 接收任务描述。
- 工具集覆盖文件读写、shell、代码库探索。
- 能返回完成状态或结构化事件流。

当前已评估候选：

- **[Pi](https://github.com/earendil-works/pi)**：TypeScript/Bun 开源 agent harness，支持 `pi -p`、`--mode json`、`--mode rpc`、15+ providers、内置 `read/edit/write/bash/grep/find/ls`、无内置 MCP/权限系统。优先作为 Claude Code 的平替 backend 接入。
- **OpenCode**：开源 coding agent CLI，支持子 agent 和权限模型，可作为 Pi 之后的第二候选。

接入顺序：先 Pi（接口最完整），再 OpenCode；每项接入后必须经过 bench 对比，证明不劣化才扩大使用。

#### F-BACKEND-4 定制化 Agent 生态

通过标准 backend 接口和配置机制，支持用户/组织接入自定义 Agent Runtime：

- 配置式 backend 注册：`config.json` 中声明 backend 名称、命令模板、默认模型、能力标签。
- 按子任务特征路由 backend：简单任务 → AgentLoop；复杂任务 → Claude Code / Pi / 自定义 backend。
- backend 能力标签：如 `supports_verification`、`supports_mcp`、`interactive_only`、`headless`。
- 不强制要求所有 backend 支持 agent_go 全部功能；未支持的能力在路由时降级或 fallback。

### 4.7 CLI、JSON 和 MCP

系统应提供：

- CLI 交互模式。
- headless/`--yes` 模式。
- JSON 结构化输出。
- MCP Server：run/resume/inspect/review/list/cancel。
- MCP Resources：summary、plan、metering、log、review 等。
- MCP Prompts：失败诊断和恢复 SOP。
- MCP HTTP/SSE transport 和鉴权。

CLI、JSON 和 MCP 必须共享核心状态语义，不得出现同一任务在不同接口中显示不同结果。

### 4.8 问题跟踪与决策辅助

#### F-PROB-1 跨任务 Problem 实体（✅ 已实现，2026-08-16）

- 全局 `~/.agent_go/problems.jsonl` 跨任务累积 Problem 实体：三态 + 复发重开、半衰期（`stale_after_days` → dormant）、葬礼（`resolution_summary`）。
- `agent_go problems` CLI：列表（按出现次数降序）/ `--aggregate` 聚合分析 / `--only` 单个详情 / `--json` 机器可读。
- 定位：「越用越聪明」数据层——失败复发可关联历史 Problem（信任指标「复发可见率」的分子），同时是谦逊层 H3 与未来 KnowledgeStore（阶段 C4）的地基。

#### F-INSIGHT-1 决策辅助（✅ 已实现 M6.1-M6.5，2026-08-17~19）

证据驱动的策略建议层，三边界：① 目标由人定义；② 建议不直接执行；③ 证据强制绑定（设计：[decision-assistant-design.md](design/decision-assistant-design.md)，用户指南：[user-guide-decision-assistant.md](user-guide-decision-assistant.md)）。

- `agent_go eval insight --results <batch>`：证据物化（失败模式/成本/环境快照）+ 结构化建议（`problem`/`evidence_refs`/`cause_hypothesis`/`action`/`expected_impact`/`confidence`/`requires_approval`）。
- decision log：统一决策记录（change/evidence/goal/expected/actual），可审计可复盘。
- Web 🧠 洞察 tab：洞察报表 + 决策历史。
- `router recommend` 升级：规则初筛（确定性问题识别）+ LLM 精排（跨维权衡）。
- `insight --apply-suggestion N`：确认后自动应用（config 修改 + 备份 + 审计，可回滚）；无证据不入 --apply（强制校验）。
- 全自动决策（M6.6）明确不做——目标由人定义是产品红线。

### 4.9 谦逊层与信任体验（✅ 已实现，2026-08-16）

产品叙事：卖的不是「自动化」，是「可信的自动化」——每次自动化升级以「先证明交底可信」为前提（设计：[humility-layer-design.md](design/humility-layer-design.md)）。

- **交底报告（#48）**：交付时标注「没验证什么」（H1 盲区清单）与未覆盖视角（H4）。
- **层间归因（H2）**：失败可定位到「层」——修 spec / 修 plan / 调预算 / 换模型。
- **信任指标（#49）与信任体验（#50-52）**：审查后修改率 / 盲区命中率 / 复发可见率（见 §6.9）；Web 任务详情盲区卡片 + `inspect` 失败历史可见。
- 产品红线：信任指标是阶段 D（自治决策）的放行门，不达标不升级自动化。

### 4.10 看板与 Web 协作（✅ 已实现，2026-08-13~19）

- **Web 操作台全功能（R1-R17）**：只读观测（17 GET API）+ 写处置（run/resume/cancel/clean/review/merge/pr/confirm，token 鉴权 + web_audit.jsonl 审计）+ 配置中心（profile 切换/健康/编辑/diff）+ SSE。纯本地 Golden Path 端到端验收通过（[web-golden-path-acceptance-2026-08-13.md](archive/reference/web-golden-path-acceptance-2026-08-13.md)：10/10 步骤、$0.00、ACCEPTED_DELIVERY）。
- **协作扩展**：任务报告导出（md/html）、多用户角色（admin/viewer 双 token 权限分离）、任务级互斥锁、任务备注、规模化保护（并发上限 + 磁盘告警）。
- **看板任务管理**：`~/.agent_go/kanban.json` 单文件存储，5 阶段列（brainstorm→operations）× 3 类卡片（discussion/implementation/periodic），阶段流转 + history，task_ids 软链接执行任务（与 status.py 执行态正交，不动 meta.json）。
- **W1 任务分类器**：卡片 `automation` 字段（auto/manual/review）+ 分类规则（架构级关键词 / difficulty=hard → design 列云端+人工；明确 spec 模块 → implementation 列本地队列）+ dispatch 按列路由（automation=manual 强制人工确认计划）。本地模型定位「模块工厂」而非「系统架构师」（设计：[kanban-task-orchestration.md](design/kanban-task-orchestration.md)）。

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

### NFR-8 可维护性

架构 review（2026-09-03）识别的结构性约束：

- 单一模块行数应有上限意识：`executor.py`（~3.2K 行）、`web_server.py`（~4.7K 行）已超可维护阈值，新增功能优先落入既有拆分边界或新模块，不得继续向超大文件堆积。
- 模块变更必须遵守 doc-sync 规则（`docs/design/module-catalog.md` §模块变更规则）：公共接口变更同步 `docs/spec.md`，状态/数据契约/边界变更新增 ADR——防止文档与代码漂移。
- 并发执行共享同一 git 对象库，新增并发路径必须评估对象库写竞争（`gc.auto` 已禁用，pack 冲突仍是已知脆弱点）。
- 新接入的 worker backend 不得成为 `executor.py` 中的硬编码特例分支，必须走标准接口（见 F-BACKEND-1）。

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

### 6.9 信任指标（谦逊层，渐进自治放行门）

衡量「交底是否可信」，是阶段 D（自治决策）的放行依据。✅ 已实现（2026-08-16，`metrics.compute_trust_metrics` + Web/inspect 展示）：

```text
审查后修改率   = (rejected + changes_requested) / 有 review 决策的任务数
复发可见率     = 失败子任务中带 problem_id 的比例
盲区命中率     = 盲区标注项最终真出问题的比例（待跨任务历史积累）
```

| 指标 | 衡量什么 | 阶段 D 放行条件（方向） |
|---|---|---|
| 审查后修改率 | 交付的「初始可信度」 | 下降（用户动手改的越来越少） |
| 盲区命中率 | 盲区标注准确度（防「狼来了」） | 高（标注的盲区是真盲区） |
| 复发可见率 | 学习闭环覆盖率 | 上升（失败都能关联历史 Problem） |

## 7. 当前差距

> 状态说明（2026-08-20 刷新）：原 P0 工程闭环缺口已随阶段 A（A1-A3）、M4.5 与 M5 关闭；bench 交付闭环（`--with-delivery`）与 M4 goal 回溯（`compute_goal_adherence`）同日关闭，并产出首个有效 ADR 基线 `delivery-20260820`（ADR=0.7045、Cost per AD=$0.0171、gate 通过）。以下为面向「全自主交付」的剩余差距。

### P0（阻断渐进自治的剩余工程缺口）

- 旧口径 canonical 35 任务通过率 60%（方案 B 后 hard 子集 94.4%；新基线 delivery-20260820 为 decision 29 任务本地 27B 口径 pass_rate_diagnostic=0.75，与旧基线禁止直接混比），难任务成功率仍是硬任务集天花板。

### P1（智能闭环缺口）

- 局部重规划（执行中）已落地为受控实验能力（C3，2026-08-21）：「无进展 → 一次拆分建议」已自动触发，但 headless 下自动执行仍需显式 `auto_apply=true`；效果待 A/B 验证后才考虑默认自动执行。
- KnowledgeStore 未建：Problem / deviation / verify_state 数据已就位，A/B 实验未做（阶段 C4）。
- Issue 联动（原 M6，`--track-issues`）未启动：Problem 尚未与 GitHub issue 打通（deferred）。
- Reviewer 成本与质量收益未验证（阶段 D 灰度，review cost ≤ 主任务 20% 门禁）。
- B1 自动 merge 策略未拍板（现状 ff-only 保守提示 + 手动兜底）。

### P2（扩展与行为指标）

- IDE、CI、Office 和多 Runtime 扩展缺少真实用户需求证据。
- 缺少 PR 接受率、人工介入时间和用户重复使用率等行为指标。
- 稳定的 dogfood 任务集需持续扩充（M3 的 12 任务是首批）。
- 外部依赖：llama-defender `/api/metrics/history?session=` 已承诺未生效（404，Review L-6），契约脚本 F6 SKIP 待服务方补齐。
- eval_suite fixtures 双重跟踪的根治（ISSUE-36 已缓解，结构重构待 fixture 仓库补 remote）。

## 8. 版本计划

### M0：产品契约与指标冻结

状态：`accepted`（decision-20260812 基线：pass_rate_diagnostic=0.924、first_pass_rate=0.864、$/pass=$0.0193）。

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

状态：`accepted`（真实 PR 证据：urika/agent_go#38 MERGED、urika/vibe-astock#1 OPEN、urika/llama-defender#8 MERGED；192 项 M1 测试通过）。

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

状态：`accepted`（M2.1-M2.5 达成；deviation.py 数据层 + `agent_go deviation` + executor 失败集成；2134 测试通过）。

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

状态：`accepted`（12 任务 × 2 真实仓库，通过率 91.7%，总成本 $0.20；0 人工介入；归档基线 m3-dogfood-20260812）。

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

### M4：goal 回溯

状态：`accepted`（2026-08-20）。

目标：`completed` 不再只是「无 subtask 失败」的否定式判定，而是回看 goal/acceptance/overview 的合规度，避免「执行全过但漏了验收」的假交付。goal 合规度作为与 `status` 正交的维度记录（见 A1 决策），不改变 verification 决定 status 的语义。

实现：`planning.compute_goal_adherence`（确定性、零 LLM，四维度：契约证据执行覆盖 / 无验证静默通过 / 验收 ID 覆盖 / 交付要求达成）→ pipeline 收尾写 `meta.goal_adherence`（level/score/gaps/needs_human_review）；review 报告、`show`、replay、Web 详情四面可见；「执行全过但漏验收」标记 `needs_human_review=True` 建议人工补验收。13 项测试（tests/test_goal_adherence.py）。

### M5：问题跟踪（✅ implemented，2026-08-16）

全局 `~/.agent_go/problems.jsonl` 跨任务累积 Problem 实体（三态 + 复发重开 / 半衰期 / 葬礼）+ `agent_go problems` CLI。M5 地基同时支撑谦逊层 H3 与信任指标「复发可见率」。

### Issue 联动（原 M6，`deferred`）

`--track-issues` 显式开启（默认关，避免 issue 洪水，见 A6 决策）；Problem 状态机 + GitHub issue 联动。未启动。注：「M6」编号自 2026-08-17 起由决策辅助系列使用（见下），原 issue 联动改称 Issue 联动避免歧义。

### M6：决策辅助（✅ M6.1-M6.5 implemented，2026-08-17~19）

证据驱动的策略建议层：`eval insight`（证据绑定 + 结构化建议）→ decision log → Web 洞察 tab → 规则初筛 + LLM 精排 → `--apply-suggestion` 确认后应用（可回滚）。M6.6 全自动决策明确不做（目标由人定义是产品红线）。设计：[decision-assistant-design.md](design/decision-assistant-design.md)。

### M7+：智能闭环与自治

状态：`in-progress`（B5=b 已拍板 2026-08-16；B3 spec 冒烟已完成，弱正 ROI 留轻量；B1 未拍板）。

- **阶段 A 工程闭环**：✅ 文件所有权约束、函数级验收契约、未提交基线处理。
- **阶段 B spec 闭环**：✅ spec 持久化 + ID 链条/锚定门禁/快照/do-not-touch fail-close；5 任务冒烟 R2 追踪完整率 0→100%、R1 80%（唯一失败为本地 worker 能力，与 spec 无关）→ 弱正 ROI，保留轻量形态。
- **阶段 C 智能闭环**：✅ Reflexion 阈值化（retry≥2）+ verify_state 契约版本化；✅ 局部重规划（C3，2026-08-21，无进展触发一次拆分建议，默认人工确认）；待做：KnowledgeStore A/B（C4）。
- **阶段 D 自治决策**：Reviewer 灰度（review cost ≤ 20%）、自动 merge 策略（B1）、人只在例外点介入；放行门 = 信任指标（审查后修改率↓ / 盲区命中率高 / 复发可见率↑，见 §6.9）。
- **阶段 E Spec-as-Source**：仅安全/受限域试点 L5（不排期）。

逐项评估（原 M4 扩展能力决策，保留）：

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
- 自治度与审计同步：每升一级自治，必须同步增加审计/回滚能力，否则不做。
- 同源审查铁律：`judge != candidate`，全自主 merge 前必须有「不同源独立验证」兜底。
- 自治是渐进目标，不是静态终点：三个例外点（Plan 确认、merge 决策、失败审查）长期保留人工决策。
