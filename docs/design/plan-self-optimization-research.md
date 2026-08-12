# Plan 级反馈与受控重规划调研及设计决策

> 状态：调研与产品 Review 完成；Plan preflight repair 已落地，plan_feedback/局部重规划待验证（2026-08-12）
> 关联：[roadmap.md](../roadmap.md) §8 局部重规划与策略重置 · [workflow-vs-subagent-review.md](workflow-vs-subagent-review.md)
> 触发背景：decision-20260812 基线 6 个任务因验证命令不合规失败，根因在 plan 阶段——验证命令由 planner 生成但违反安全白名单。评估「执行反馈是否应回流到 plan 生成，形成自动重规划双向循环」。

## 1. 问题边界

当前 agent_go 有三层循环，但缺**执行结果→plan 生成的横向回流**：

```
L0 plan 交互循环   confirm_plan S/D/R（人工触发重生成，max_plan_iterations=5）
L1 执行循环        subtask 验证失败 → 修复重试（executor，max_retries=5）
L2 防呆循环        G8 短路 / 回退检测 / 打地鼠检测（防无效重试烧钱）
```

M2.2 的 `failure_analysis`/`effective_strategy` 已写入 verify_state.json，但**没有消费方**——plan 生成看不到执行端的失败模式，同类 plan 错误反复出现。

用户问题：是否需要「plan 自动重规划执行」的双向循环（系统根据执行反馈自动改 plan 并重跑），借鉴主流 agent 设计。

### 1.1 两个正交维度（先厘清概念边界）

调研文献中的「优化演进路径」与 agent_go 的产品开发是两个**不同维度**，不能混为一谈：

**维度 A —— harness 自优化杠杆的深度**（Lilian Weng 的路径，能力纵深）：

```
prompt → structured context → workflow → harness code → optimizer code
```

这描述的是「系统为了让被驱动的模型（worker）表现更好，把优化手段从改提示词，深化到改上下文结构、改工作流编排、改 harness 代码，直到改优化器本身」的**能力杠杆层级**。它衡量的是「自优化的深度」，与软件开发里程碑无关。

**维度 B —— agent_go 软件开发的演进**（roadmap M0→M3，产品功能阶段）：

```
M0 契约与指标 → M1 交付闭环 → M2 核心可靠性 → M3 真实任务验证
```

这是 agent_go 作为产品的**功能里程碑**：先立契约和可信指标，再打通交付，再提升可靠性，再用真实任务验证。它衡量的是「产品能力覆盖」，与自优化杠杆深度无关。

**两个维度正交**：维度 A 回答「agent_go 用哪一层杠杆优化 worker 行为」，维度 B 回答「agent_go 的功能开发进展到哪」。一个深（维度 A 到 workflow/harness code）不意味着产品阶段落后，反之亦然。

**agent_go 在维度 A 的现状**：优化手段**已分布在多个杠杆层级**（非单一阶段）：

| 维度 A 层级 | agent_go 现有机制 |
|------------|-------------------|
| prompt | 拆分三原则、验证命令生成规范（api.py） |
| structured context | TASK.md / TASK_BASE.md 共享基座、架构上下文注入、skill 注入 |
| workflow | 拓扑波次、G8 短路、回退/打地鼠检测、成本 L2/L3 |
| harness code | 白名单门禁、沙箱、plan_quality 校验（G6/G7/G8） |
| optimizer code | 无（plan_feedback / 自动重规划即此层，待落地） |

**为什么这个区分对本决策重要**：判断「是否做自动重规划」只取决于维度 A——agent_go 是否应该在 optimizer code 这一杠杆层投入。它**不**取决于维度 B 的产品阶段，也不意味着「prompt 阶段没走完就不能做深」。反之，维度 A 的深浅也不改变维度 B 的 M2/M3 验收。

（本节为对初稿逻辑偏差的修正：初稿把维度 A 的「演进路径」误当作 agent_go 的开发路径，断言「agent_go 正处于 prompt→structured context 阶段」，混淆了两个正交维度。特此澄清。）


## 2. 主流 agent 设计调研

### 2.1 工程框架

| 框架 | plan→执行循环 | 双向闭环 | 关键机制 |
|------|--------------|----------|----------|
| **LangGraph plan-and-execute** | planner→worker 节点循环 | 半（critic 节点可反馈） | graph 节点化，planner 生成计划、worker 执行、critic 评审 |
| **OpenHands (ACI)** | plan→act→reflect→revise 循环 | 全（reflect 后 revise plan） | Agent-Computer Interface，反思驱动计划修订 |
| **Claude Code plan mode** | plan 先于执行 | 否（plan 批准后只执行） | Plan/Explore 隐藏 subagent，用户批准为边界 |
| **SWE-agent** | harness 单任务循环 | 否（命令级循环） | 纯执行 harness，无 plan 层 |
| **Devin** | plan→execute→observe→re-plan | 全（DAG 上持续 re-plan） | 长 horizon 任务持续重规划，observe 反馈驱动 |

**规律**：工程框架要么没有 plan 层（SWE-agent），要么 plan 批准即冻结（Claude Code），要么做**持续 re-plan**（Devin/OpenHands）。agent_go 目前是「Claude Code 式」——plan 批准后执行端只跑 subtask，plan 不更新。

### 2.2 学术范式

| 范式 | 循环结构 | 借鉴点 | 风险 |
|------|----------|--------|------|
| **Reflexion** | act→observe→reflect→refine plan→act | verbal reinforcement，反思文字注入下一轮 | 需要 eval 信号 |
| **Self-Refine** | generate→feedback→refine（同模型） | 同模型自我反馈 | 弱 evaluator 时退化 |
| **ToT (Tree of Thoughts)** | 推理作为搜索 | 多候选+回溯 | 成本爆炸 |
| **STOP (Self-Taught Optimizer)** | 优化 improver 本身 | 递归自改进 | **弱模型上退化**（GPT-3.5/Mixtral 效果下降） |
| **ADAS / AFlow** | meta-agent 搜索 workflow | 代码作为通用语言 | 搜索空间大 |
| **Self-Harness** | weakness mining→proposal→validation | 三阶段 propose-evaluate-accept | 依赖 eval 可测性 |
| **Meta-Harness** | harness 优化 harness | 分层优化 | 复杂度高 |

### 2.3 Lilian Weng《Harness Engineering》(2026-07) 关键洞察

这篇是本次调研中关于 harness 自改进的重要综述之一，为 agent_go 的决策提供参考：

1. **Harness 优化演进路径**：prompt → structured context → workflow → harness code → optimizer code。这是 **harness 自优化杠杆的深度演进**——描述「系统为了让基座模型表现更好，把优化从提示词一路深化到工作流、到 harness 代码、再到优化器本身」的能力纵深。
2. **AHE 三可观测性支柱**：组件/经验/决策可观测性——每个 failure pattern 要能映射到具体组件，编辑需「证据+预测」。agent_go 的 verify_state.json + metering.jsonl 已有「经验可观测性」基础。
3. **reward hacking 风险**：自优化循环「优化什么信号就被 hack」。若 plan 自动重规划优化「通过率」，系统会学会生成更简单的 plan（把 hard 拆成 easy 或只做表面改动）而不是真正完成任务。
4. **weak evaluator 问题**：自改进循环只在 eval 可测时有效。agent_go 的语义评估（evaluator）本身有假阳性率，用它驱动 plan 重规划会放大误差。
5. **weak model 上递归优化退化**：STOP 在 GPT-3.5/Mixtral 上效果下降。agent_go 的生产 planner 是 deepseek-v4-pro，属中等模型，递归自优化收益不确定。
6. **diversity collapse**：自优化会让策略收敛到单一模式，失去探索多样性。
7. **negative results 保存**：失败的 harness 改动要记录，防止重复尝试。
8. **human 上移到 oversight 层**：自优化不是替代人工，而是把人工从「执行细节」提升到「方向监督」。

### 2.4 逐框架深度分析

#### LangGraph plan-and-execute

**机制**：以有向图建模 agent 流程。典型 plan-and-execute 模式由三个节点组成——`planner` 节点接收任务并生成执行计划；`worker` 节点从计划中取出一个步骤执行（调用 LLM 或工具）；`critic` 节点评审执行结果并决定下一步。图可以带条件边（conditional edges），worker 完成后根据 critic 判定决定「回退到 planner 重计划」还是「继续下一步」还是「终止」。

**循环结构**：`planner → worker → critic → (planner | next step | done)`。这里的闭环是**任务内循环**——critic 发现当前步骤失败时，把失败信息返回 planner 重新生成该步骤的计划，而不是整个任务重来。

**对 agent_go 的启示**：agent_go 的拓扑波次调度本质上是静态 DAG（depends_on 预先固定），没有运行时 critic 反馈边。LangGraph 的模式提示：可以让「上游 subtask 失败」成为下游重计划的触发器——但 agent_go 已有 blocked 传播（上游失败→下游 blocked），缺的是「blocked 后是否重新规划下游」的决策。

**借鉴度：中。** 它的 critic 反馈是可选的 graph 边，agent_go 可以只在「上游失败且下游可重写」时插入一条重规划边，而非全图动态化。

#### OpenHands (ACI, Agent-Computer Interface)

**机制抽象**：OpenHands 的 Agent-Computer Interface 让 Agent 通过受限接口与「计算机」交互，接口可暴露 Bash 命令执行、文件操作、浏览器等能力。本文将其 agent 行为抽象为 `plan → act → reflect → revise`：

- **plan**：agent 基于任务目标制定步骤
- **act**：通过 ACI 执行动作（bash/file/browser）
- **reflect**：观察执行结果，判断是否达成目标
- **revise**：若未达成，修订计划继续

**关键点**：在 OpenHands 的相关平台和研究描述中，观察结果与反思可以影响后续行为。本文借鉴的是「反思驱动计划修订」这一模式，不把该抽象等同于某个固定的内部实现细节。

**对 agent_go 的启示**：agent_go 的 L1 修复循环（验证失败→注入失败上下文→重试）本质就是 ACI 的 act→reflect→revise，但作用范围是**单 subtask 内部**。OpenHands 把同样的循环扩展到整个任务层面（revise 可以重写整体计划）。agent_go 若扩展，应把「subtask 失败」从「仅修复该 subtask」升级为「可选地修订该 subtask 及其依赖」。

**借鉴度：中高。** ACI 的接口约束与 agent_go 的白名单门禁同构（都是「agent 只能通过受限接口行动」），但 ACI 的 revise 是全计划级，agent_go 当前只做 subtask 级。

#### Claude Code plan mode

**机制**：Claude Code 提供 `--plan` 模式——agent 先探索代码库、生成计划并等待**用户批准**，批准后进入执行。plan 与执行是两阶段，plan 一旦批准即冻结（不因执行失败自动重计划）。Plan/Explore 是隐藏 subagent，负责只读探索，不产生代码改动。

**循环结构**：`explore → plan → user_approve → execute`。没有执行后的自动回环——执行失败由用户在会话中人工决定「修改 plan」或「继续」。

**对 agent_go 的启示**：agent_go 的 confirm_plan / confirm_subtasks 就是 Claude Code 的 plan mode（人工批准为边界）。这意味着 agent_go 当前的「plan 冻结」设计是有主流背书的。**执行失败后是否需要自动重计划，在 Claude Code 的设计里是「留给用户」的**，不是自动的。

**借鉴度：高（作为「不做」的参照）。** 它证明了「plan 批准即冻结 + 人工纠偏」是可行且被广泛采用的设计，agent_go 不需要为了「更智能」而放弃这个简单可靠的边界。

#### SWE-agent

**机制**：SWE-agent 是单任务 harness——把 agent 限制在一个精心设计的命令行接口内（受限 bash + 文件编辑器），一次解决一个 GitHub issue。循环是纯命令级的：`observe current state → run command → observe output → repeat`。**没有 plan 层**——不预先生成多步计划，而是每步根据当前状态决策。

**对 agent_go 的启示**：SWE-agent 证明「无 plan 层 + 强接口约束」也能达到 SOTA（SWE-bench 高分）。它的优势是简单、成本低、无计划规划开销；劣势是没有全局视野，无法保证跨文件一致性。

**借鉴度：低（agent_go 已有 plan 层）。** 但它验证了 agent_go 的白名单门禁方向——受限接口是可靠性关键，而非「放开权限让模型自由发挥」。

#### Devin

**公开能力层面的描述**：Devin 面向长 horizon 软件工程任务，公开产品材料强调计划、执行、测试和交付协作。本文将其作为「持续 observe 后 re-plan」的产品范式进行对照；其完整内部 DAG 调度实现并未作为本调研的可验证事实。

**循环结构（抽象模型）**：`plan → execute → observe → re-plan → execute ...`。这是**任务级的持续重规划**——计划不是一次性产物，而是随执行进展持续演化的状态。

**对 agent_go 的启示**：agent_go 的拓扑波次 + delivery branch 是「静态 DAG 的一次性执行」。Devin 的「动态 DAG」需要：执行中可增删 subtask、失败可重新规划下游、plan 产物可演进。这是最大的工程差距。

**借鉴度：高（作为「远期目标」）。** Devin 式的动态 re-plan 可作为产品形态参考，但不应把公开产品描述当作内部技术实现的直接证据。对 agent_go 而言，其工程复杂度和成本风险都最高（每轮 re-plan 都要重新规划 + 可能重跑已完成步骤）。

#### 学术范式深度分析

**Reflexion**（Shinn et al.）：核心是「言语强化」——agent 在每轮 act→observe 后，生成一段「反思」文字（这次为什么失败、下次应该怎么做），把反思作为下一轮的额外上下文。反思**不改变模型权重**，只改变 prompt。论文报告了其在所测试的推理、编码和决策任务上的改进。**关键约束：需要可测的 eval 信号**（测试通过/失败）来触发反思，不能直接外推为所有任务都有效。

**Self-Refine**（Madaan et al., arxiv 2303.17651）：同模型生成初稿→自我反馈→精修，迭代 K 轮。与 Reflexion 不同，Self-Refine 的反馈是「针对当前输出的具体修改建议」，而非「对失败的反省」。**关键弱点：反馈质量受限于模型自身能力**——弱模型给不出好的修改建议（与 STOP 的发现一致）。

**ToT（Tree of Thoughts，Yao et al.）**：把推理问题建模为树搜索——每一步生成多个候选思考，评估每个候选的价值，用 BFS/DFS 回溯探索。**在规划类问题（Game of 24、创意写作、迷你填字）上显著提升**，但每步多候选的成本是线性的 K 倍。**对 agent_go 的启示：多候选 plan 生成 + 评估器选优是「更强的 plan 层」，但成本是当前单 plan 的 K 倍。**

**STOP（Self-Taught Optimizer）**：让 LLM 编写一个「改进器」（improver）来优化基座模型的提示/上下文，然后用改进后的提示再生成新的改进器，递归迭代。**决定性发现：在 GPT-3.5、Mixtral 等中等模型上，STOP 的递归优化反而导致性能下降**（性能低于单次优化的基线）——基础模型能力是递归自改进的天花板。**对 agent_go 的启示：不要用中等模型做「优化 planner 的 planner」这类深层递归。**

**Self-Harness**：三阶段循环——`weakness mining`（从失败案例中挖掘系统弱点）→ `bounded harness proposal`（提出有界的 harness 改动建议）→ `validation`（在新案例上验证改动是否真的修复）。propose-evaluate-accept 的接受门槛是「验证通过」。

**Meta-Harness**：harness 优化 harness——把 harness 本身当作被优化对象，用 LLM 生成新的 harness 逻辑。复杂度最高，风险最高。

**ADAS / AFlow**：用 meta-agent 在搜索空间中寻找更优的 agentic workflow（ADAS 把 workflow 表达为代码，AFlow 用 MCTS 搜索 workflow 图）。**对 agent_go 的启示：这类方法把「workflow 设计」本身自动化，但搜索空间巨大、成本不可控，属于研究前沿而非工程实践。**

### 2.5 综合提炼：双向循环的设计空间

把所有调研合并到一个统一的设计空间：

```
                   单向（反馈注入，不重跑）          双向（重规划并重跑）
               ┌──────────────────────────┬──────────────────────────┐
   人工触发     │ agent_go L0 confirm_plan  │ Claude Code 用户改 plan   │
               │ (S/D/R 人工重生成)         │                          │
   规则触发     │ 本次提议: plan_feedback    │ 上游失败→下游重规划       │
               │ (被拒命令→下次注入)        │ (受控, 单步)              │
   模型触发     │ M2.2 Reflexion 注入修复    │ Devin 动态 DAG re-plan    │
               │ (retry 内反思)            │ (持续, 全量)              │
               └──────────────────────────┴──────────────────────────┘
```

- **左下象限（规则触发·单向）**：本次提议的 plan_feedback，成本最低、风险最低。
- **右下象限（模型触发·双向）**：Devin 式持续 re-plan，能力最强、风险最高。
- **右上象限（规则触发·双向）**：上游失败→下游重规划的「受控单步重规划」，是 Devin 的轻量版，处于「现在可做」与「远期目标」之间。

**关键结论**：主流实现没有一上来就做右下象限。都是从左下/左上起步，用「反馈注入」积累经验，再逐步向「自动重规划」演进。agent_go 当前在左上（人工重生成），下一步应落左下（规则反馈注入），而非直接跳右下（Devin 式）。


### 2.6 决策借鉴的提炼

**「持续 re-plan 双向循环」是能力最强的范式（Devin/OpenHands），但也是风险最高的。** 主流实现的共同护栏：

1. **re-plan 需要明确触发信号**（Devin 的 observe 失败、Reflexion 的 eval 失败），不是每步都 re-plan。
2. **re-plan 成本受控**（Reflexion 的有界反思、Self-Harness 的 bounded proposal）。
3. **eval 信号必须可信**（自改进只在 eval 可测时有效）。
4. **递归自优化在弱模型上退化**——不要追求 harness 优化 harness 的深层递归。

## 3. agent_go 现状、产品目标与分析过程

| 能力 | 现状 | 对应借鉴 |
|------|------|----------|
| plan 生成 | planner 单次生成 + 人工 S/D/R 重生成 | Claude Code 式（plan 批准即冻结） |
| 执行反馈 | verify_state.json 持久化 failure_analysis/effective_strategy | 有「经验可观测性」，无消费方 |
| 失败模式映射 | deviation.py 记录 failure_pattern=no_progress | 部分 AHE（决策可观测性雏形） |
| 语义评估 | evaluator 独立模型（deepseek-v4-pro） | 存在 weak evaluator 风险 |
| 白名单门禁 | 验证命令 4 阶段校验（default-deny） | 组件可观测性（被拒原因明确） |
| plan 质量校验 | validate_plan_quality（G6/G7/G8 + blocking） | 有，但执行后不回灌 |

**核心缺口**：执行端产生的「验证命令不合规」「文件越界」「覆盖不全」等**确定性失败模式**，没有结构化回流到 plan 生成。这些失败不需要 LLM 重规划——它们是**规则可判定的生成质量问题**，可以用确定性反馈修正。

### 3.1 产品目标与非目标

Plan 自优化不是产品终点，而是服务于产品交付目标的控制能力。评估任何自动重规划能力时，优先级应如下：

1. **提高 Accepted Delivery Rate**：最终交付必须满足需求、验证、commit、delivery branch 和 PR/merge 条件；不能只提高某个中间通过率。
2. **降低人工介入成本**：减少用户手工修改 Plan、手工定位失败原因和手工恢复任务的时间。
3. **降低 Cost per Accepted Delivery**：重规划产生的额外 Planner、Worker、验证和评估成本必须低于它带来的交付收益。
4. **保持可解释、可恢复、可审计**：每次 Plan 修订必须记录触发原因、版本差异、影响的 Subtask、预算和最终结果。
5. **保持需求和架构约束**：自动重规划不能通过删除 requirement、缩小验收范围或绕开安全门禁来制造表面成功。

以下不是当前阶段的目标：

- 让 agent_go 自动修改自身 harness 或自动优化 Planner 模型。
- 为了提高 `pass_rate` 而降低任务范围、删减验收标准或改变指标口径。
- 立即实现 Devin 式的无限动态 DAG。
- 把历史参考值 `$0.05` 或 `K1 >=97%` 当作未经 M3 验证的硬 KPI。roadmap 明确要求先用真实任务建立可信基线。

### 3.2 分析过程

本次产品评估按以下链路进行，而不是从「主流 Agent 有 re-plan」直接推导「agent_go 必须实现 re-plan」：

1. **先定义产品问题**：当前 Plan 批准后基本冻结，执行反馈不能回到 Plan；这可能导致确定性 Plan 错误在不同任务中重复出现。
2. **再核对真实数据**：检查 decision-20260812 的失败记录、`failure_class`、`kill_reason`、`verification_results` 和安全门禁审计，而不是只看模型通过率。
3. **区分失败根因**：把验证命令不合规、基础设施故障、超时、实现能力失败、需求偏差和交付失败分开；只有其中一部分属于 Plan 问题。
4. **区分解决层级**：判断问题应由 Plan 预检、执行重试、局部重规划、跨任务反馈还是人工介入解决，避免用一个大循环处理所有失败。
5. **比较主流设计**：对照 LangGraph、OpenHands、Claude Code、SWE-agent、Devin 以及 Reflexion/Self-Refine 等范式，观察它们的触发条件、循环范围和成本护栏，而不是只借鉴「有循环」这一表面形式。
6. **评估副作用**：检查 reward hacking、weak evaluator、重复重试、DAG 状态污染、需求覆盖丢失、交付分支不一致和成本失控风险。
7. **建立可证伪验收**：为每个阶段定义 Accepted Delivery、人工介入、成本、Plan 修订和失败复发率指标，用 A/B 或配对真实任务验证收益。

### 3.3 真实问题与方案覆盖度

decision-20260812 有 35 条记录、10 条失败。验证命令被拒是一类明确的 Plan 生成问题，但它不能代表全部失败，也不能证明持续自动重规划已经必要：

| 问题类型 | 典型现象 | 适合的第一解决层 | 当前方案覆盖 |
|---|---|---|---|
| Plan 确定性错误 | 验证命令不在白名单、文件范围冲突、依赖环 | 执行前 Plan 预检和一次修复 | 当前仅部分具备，需补 Plan preflight repair |
| 同类错误跨任务复发 | 不同任务反复生成相同不合规命令 | `plan_feedback` 跨任务反馈 | 设计中，尚未落地 |
| 上游失败导致下游 blocked | 下游原计划无法继续 | 一次局部重规划 | 尚未落地 |
| 模型实现失败 | 验证失败、语义不通过、修复不收敛 | Subtask repair / Reflexion / 人工恢复 | 不应直接交给 Plan feedback |
| 基础设施失败 | API、环境、进程、依赖不可用 | 超时、降级、恢复和基础设施治理 | 不属于 Plan 自优化核心范围 |
| 交付失败 | PR、mergeability、merge 冲突 | delivery branch / PR / merge 恢复 | 已由 M1 覆盖 |

因此，Plan 自优化的合理目标不是「解决全部失败」，而是减少**可由 Plan 预防或解释的失败**，同时不改变其他失败类别的归属和恢复路径。

### 3.4 目标能力的分阶段定义

推荐把「Plan 自优化」拆为四个产品层级：

| 层级 | 定义 | 是否立即做 |
|---|---|---|
| Plan preflight repair | 执行前发现确定性 Plan 错误，自动修订一次并重新校验 | **立即做** |
| Plan feedback | 把历史确定性失败以有界规则反馈注入下一次 Plan | 可与 preflight 并行，小范围验证 |
| 局部一次性重规划 | 当前 Subtask 失败后，仅重规划未执行的下游步骤 | M3 后灰度 |
| 持续动态重规划 | 执行中不断重写全局 Plan/DAG 并重跑 | 暂缓 |

其中，**Plan preflight repair** 与 `plan_feedback` 不能混为一谈：前者可以修复当前任务，后者主要防止未来任务复发。当前设计文档原先把二者都称为「双向循环」的组成部分，产品上应分别验收。

### 3.5 可行性和验收指标

Plan preflight repair 的技术可行性高，因为它可以复用现有 `validate_plan_quality`、`_is_safe_verification_command`、Plan 版本快照和人工确认机制；不需要先解决动态 DAG 和执行现场迁移。

局部一次性重规划的技术可行性中高，但必须解决已完成 commit、下游 worktree、依赖关系、预算继承、traceability 和 delivery branch 的状态一致性。

持续动态重规划虽然技术上可实现，但当前产品证据不足。它需要同时证明评估信号可信、重规划收益大于成本，并且不会降低 Accepted Delivery 的正确性。

建议用以下指标验收，而不是只看是否发生了 re-plan：

| 指标 | 目标含义 |
|---|---|
| Plan preflight 阻断率 | 发现问题后不让不合规 Plan 进入 Worker |
| 自动修复成功率 | 修订后 Plan 通过全部确定性门禁的比例 |
| 同任务恢复率 | Plan 修复后最终完成/交付的比例 |
| Accepted Delivery Rate | 产品最终交付是否真的改善 |
| Cost per Accepted Delivery | 额外 Planner/Worker 成本是否值得 |
| Human Intervention Minutes | 用户是否少花时间改 Plan 和恢复任务 |
| Plan 修订次数 | 是否出现循环和振荡 |
| requirement/acceptance 覆盖变化 | 自动修订是否偷删目标 |
| 同类失败复发率 | `plan_feedback` 是否真正减少重复错误 |

建议的首轮实验门槛：自动 Plan 修复最多 1 次；修订前后 requirement、acceptance 和安全约束不得减少；不合规验证命令不得进入执行；额外 Plan 成本控制在任务总成本的 10% 至 15% 以内；在至少 10-20 个配对真实任务上比较基线与实验组，且 Accepted Delivery 不下降。

## 4. 设计决策

### 4.1 结论：先做 Plan 预检修复，再做有界反馈；暂不做持续双向重规划

基于调研（weak evaluator 风险 + weak model 递归退化 + reward hacking + 成本不可控），**反对**直接实现 Devin 式持续 re-plan 双向循环。

**支持**实现两层低风险能力：

1. **同任务的 Plan preflight repair**：执行前发现确定性错误，自动修订一次并重新校验；这是当前任务的 Plan 修复能力。
2. **跨任务的 plan_feedback**：把历史确定性失败以规则摘要注入下次 Plan；这是未来任务的防复发能力。

二者都不等于 Devin 式持续双向重规划。持续重规划仍暂缓。

```
当前任务：generate_plan
    ↓ Plan preflight
    ├─ 通过 → confirm_plan → execute
    └─ 阻断 → 结构化修复反馈 → Planner 修订一次 → 再校验

未来任务：执行失败（确定性信号）
    ↓ 结构化捕获
  plan_feedback（跨任务聚合）
    ↓ 注入
  下次 generate_plan supplement
```

### 4.2 反馈范围：只反馈「规则可判定的生成质量问题」

| 可反馈（确定性） | 不可反馈（需 eval/LLM） |
|------------------|--------------------------|
| 验证命令被白名单拒绝（含原因） | 验证真失败（模型能力） |
| 验证命令与 files 不匹配 | 语义评估不通过 |
| 子任务文件越界/覆盖他步文件 | 需求覆盖不足的深层判断 |
| 欠分解/过度分解（G6/G7/G8 已检出） | 架构决策错误 |

可反馈项全部是 **planner 本可避免的确定性错误**，且反馈内容为「该命令为何被拒 + 正确写法建议」（如 `python -c` 多行 → `python -m <模块>`），由规则生成而非 LLM 总结——避免 weak evaluator 放大。

### 4.3 有界性（避免无界循环）

1. **一次反馈，不循环**：plan_feedback 只在 plan 生成时注入一次（作为 supplement），不做「plan→执行→plan」连续自优化。连续自优化（Meta-Harness 式）留待 M3 真实任务验证后。
2. **大小有界**：feedback 注入有字符上限，成本计入 plan 阶段（plan token 预算内），不新增无限消耗。
3. **阈值门控**：同一失败模式出现 ≥N 次（如 2 次）才生成反馈，避免单次噪声触发。
4. **时间窗口**：反馈基于最近 N 个任务的聚合，不无限累积。

### 4.4 与已有能力的关系

- **不是替代 M2.2 Reflexion**：Reflexion 是执行端「retry 内反思修复」；plan_feedback 是计划端「跨任务学习」。两者互补。
- **不是替代 human confirm**：confirm_plan 仍是最终边界，plan_feedback 只是让 planner 首次生成更准，减少人工纠偏次数。
- **不改变 G8 语义**：G8 短路保留（执行端不重试），plan_feedback 在计划端防复发。

### 4.5 P0 机制设计（Plan preflight repair + plan_feedback）

**同任务预检修复**：

```text
generate_plan
    ↓
validate_plan_quality + verification command gate
    ├─ 通过 → confirm_plan → plan_to_subtasks → execute
    └─ 阻断 → 生成确定性修复反馈
                    ↓
                Planner 修订 v2（最多一次）
                    ↓
                再次校验
                    ├─ 通过 → 进入 confirm/execute
                    └─ 仍失败 → 阻断并请求人工处理
```

预检修复只允许修改触发阻断的问题，不得删除 requirement、acceptance criterion、验证责任或安全约束。修订前后必须保存 Plan diff、`replan_reason`、`replan_scope` 和 `changed_subtask_ids`。这是解决当前任务验证命令不合规的主要机制。

**数据流**：

```
executor._log_rejected_command(cmd, reason)
    │ 规则生成建议（非 LLM）
    ├─ python -c 多行/装饰器  → "python -c 仅限单行；多行逻辑改用 python -m <模块> 或测试框架"
    ├─ bash/sh -c 包裹        → "不要用 bash/sh 包裹；直接写白名单内的命令"
    ├─ 自然语言前缀            → "命令前不要加自然语言描述，直接写命令"
    └─ 未知命令               → "使用白名单内的命令前缀：<列出相近前缀>"
    ↓ 写入
  ~/.agent_go/plan_feedback.jsonl  (每行 {ts, task_id, repo_scope, rejected_cmd, reason, suggestion, bucket})
    ↓ 阈值门控（同 bucket ≥2 次）
  generate_plan(supplement=plan_feedback_summary)
```

**bucket 分类**（规则可判定的失败模式，对齐 §4.2）：

| bucket | 匹配规则 | suggestion |
|--------|----------|------------|
| `python_c_multiline` | 命令含 `python -c` 且被拒因含换行/语法错误 | 建议 `python -m <模块>` |
| `bash_wrap` | 以 `bash -c`/`sh -c` 开头 | 禁止包裹，直接写白名单命令 |
| `nl_prefix` | 被拒因 `未知命令: <首词>` 且首词非白名单 | 去掉自然语言前缀 |
| `dotdot_path` | 含 `..` 路径穿越 | 用仓库内相对路径 |
| `redirection` | 含 `>`/`<` 重定向 | 验证命令不支持重定向 |

**注入方式**：作为 `generate_plan` 的 `supplement` 参数（已有该参数，用于「用户补充」）。与用户 supplement 合并，但加 `===== 计划反馈 =====` 分隔，标明「历史任务中以下验证命令被拒绝，请避免生成同类命令」。

反馈数据必须按仓库或配置作用域隔离，不能默认把一个项目的经验注入所有项目；原始命令需要脱敏、截断并过滤疑似密钥，Planner 只接收结构化原因和建议，不直接接收未经清洗的任意历史文本。

**有界性落实**：
- 每条反馈 ≤200 字符，最多注入 5 条（超出截断）。
- 阈值：同 bucket 在最近 5 个任务中出现 ≥2 次才激活（避免单次噪声）。
- 一次性注入：feedback 只影响本次 plan 生成，不参与执行后回写（无连续循环）。
- 默认关闭或灰度开启，保留 negative result 和版本回滚能力；必须经过 A/B 验证后才扩大作用域。

## 5. 落地建议（分阶段）

**阶段一（P0，已落地）**：执行前 Plan preflight repair。
- 直接阻断当前任务的验证命令不合规、文件范围冲突、依赖环和覆盖不足。
- 最多自动修订一次；仍不通过则阻断并请求人工确认，不进入 Worker。
- 复用现有 `validate_plan_quality`、安全命令解析器、Plan 版本快照和人工确认机制。

**阶段二（P0/P1，低风险灰度）**：被拒验证命令 → 结构化 plan_feedback（正确写法由规则生成）→ 注入下一次 `generate_plan`。
- 主要解决 decision-20260812 暴露的 4 类未来复发问题（`python -c` 多行/装饰器、bash 包裹、自然语言前缀、命令白名单覆盖）。
- 必须增加仓库作用域、脱敏、阈值、feature flag 和 A/B 评估。

**阶段三（P1，M3 后）**：局部一次性重规划。
- 仅针对失败 Subtask 和未执行的下游步骤，不重写全局 Plan，不重跑已接受 commit。
- 默认人工确认；继承原任务预算和 requirement/acceptance 约束。
- 在 10-20 个配对真实任务上验证后，才考虑默认自动执行。

**阶段四（暂缓）**：Devin 式持续 re-plan 双向循环 / Meta-Harness 式 harness 自优化。风险高（weak evaluator + reward hacking），需 M3 真实数据支撑后决策。

## 6. Review 结论与建议

### 6.1 Review 结论

本次 Review 结论为：**方向成立，但原文不宜直接作为“持续自动重规划”的实施规格**。文档可以作为调研和产品决策依据，实施前还必须把 Plan preflight repair 的状态、预算、回滚和验收协议单独冻结。

| Review 项 | 发现 | 处理结论 |
|---|---|---|
| 范围定义 | 标题和正文容易把 plan_feedback、同任务 Plan 修复、持续 re-plan 混为一类 | 已拆成四个产品层级，并将标题改为“反馈与受控重规划” |
| 问题覆盖 | 被拒验证命令只是 Plan 缺陷的一类，不能代表 timeout、模型失败和基础设施失败 | 已增加失败类型与解决层级矩阵 |
| 当前任务救援 | 跨任务 `plan_feedback` 不能修复已经生成的当前 Plan | 已增加 Plan preflight repair，并明确两者差异 |
| 产品目标 | 只讨论 Plan 是否变好，未充分绑定 Accepted Delivery、成本和人工时间 | 已增加目标、非目标、指标和实验门槛 |
| 证据强度 | 35 条决策基线可支持问题假设，但不能证明持续 re-plan 必然提升成功率；`97%` 仍是历史参考值 | 已将其降级为待验证假设，不作为当前硬 KPI |
| 安全与治理 | 全局 feedback 可能跨仓库污染，也可能把原始命令中的敏感内容带入 Planner | 已增加仓库作用域、脱敏、截断、feature flag 和回滚要求 |
| 外部资料 | 部分主流 Agent 的内部实现不可完全验证，不能把产品宣传或抽象模型当作内部架构事实 | 已对 OpenHands 和 Devin 的表述增加限定 |
| 文档结构 | 原框架小节层级不一致，影响阅读和目录导航 | 已修正标题层级 |

### 6.2 继续扩展前必须补齐的协议

在继续实现 plan_feedback 或执行中的局部重规划前，至少需要冻结以下内容：

1. **Plan 修订状态**：`plan_v1 → preflight_repair → plan_v2 → validated / blocked`，不能覆盖原 Plan。
2. **修订范围**：只允许修复确定性阻断问题；requirement、acceptance、架构约束和目标分支不可被自动删除或放宽。
3. **预算边界**：Plan 修复调用计入任务预算，最多一次自动修订；失败后转人工确认或明确阻断。
4. **状态一致性**：Plan 修复发生在 Worker 启动前，不得污染已执行 Subtask、commit、delivery branch 或 metering 记录。
5. **实验设计**：基线组不启用自动修复，实验组启用一次修复；记录 Plan 修复成功率、Accepted Delivery、成本和人工介入时间。
6. **回滚条件**：Accepted Delivery 下降、requirement 覆盖下降、额外成本超过阈值或出现循环时，自动关闭 feature flag。

### 6.3 产品建议

- **立即实施**：Plan preflight repair，这是当前问题最直接、最可控的解决方案。
- **低风险灰度**：plan_feedback 只作为未来任务的防复发机制，默认关闭或限定仓库范围。
- **M3 后评估**：基于真实任务数据决定是否启用一次局部重规划。
- **继续暂缓**：全局动态 DAG、无限重规划、Meta-Harness 和自动优化 optimizer code。

## 7. 参考

### 7.1 综述

- Lilian Weng, *Harness Engineering for Self-Improvement*, 2026-07-04 — https://lilianweng.github.io/posts/2026-07-04-harness/

### 7.2 工程框架

- LangGraph *Plan-and-Execute* (LangChain Blog) — https://www.langchain.com/blog/planning-agents
- LangGraph *plan-and-execute* 官方 notebook — https://github.com/langchain-ai/langgraph/blob/main/examples/plan-and-execute/plan-and-execute.ipynb
- OpenHands *ACI (Agent-Computer Interface)* — https://github.com/OpenHands/openhands-aci
- OpenHands 主论文：Wang et al., *OpenHands: An Open Platform for AI Software Developers as Generalist Agents*, arXiv 2407.16741 — https://arxiv.org/abs/2407.16741
- Claude Code 官方文档（plan mode） — https://docs.anthropic.com/en/docs/claude-code
- Devin (Cognition AI) — https://devin.ai/
- SWE-agent *Agent-Computer Interface* — https://swe-agent.com/0.7/background/aci/

### 7.3 学术范式

- Reflexion: Shinn et al., *Reflexion: Language Agents with Verbal Reinforcement Learning*, NeurIPS 2023, arXiv 2303.11366 — https://arxiv.org/abs/2303.11366
- Self-Refine: Madaan et al., *Self-Refine: Iterative Refinement with Self-Feedback*, NeurIPS 2023, arXiv 2303.17651 — https://arxiv.org/abs/2303.17651
- ToT: Yao et al., *Tree of Thoughts: Deliberate Problem Solving with Large Language Models*, NeurIPS 2023, arXiv 2305.10601 — https://arxiv.org/abs/2305.10601
- STOP: Zelikman et al., *Self-Taught Optimizer (STOP): Recursively Self-Improving Code Generation*, arXiv 2310.02304 — https://arxiv.org/abs/2310.02304
- Self-Harness: Zhang et al., *Self-Harness: Harnesses That Improve Themselves*, arXiv 2606.09498 — https://arxiv.org/abs/2606.09498
- Meta-Harness: *Meta-Harness: End-to-End Optimization of Model Harnesses*, arXiv 2603.28052 — https://arxiv.org/abs/2603.28052
- ADAS: Hu et al., *Automated Design of Agentic Systems*, ICLR 2025, arXiv 2408.08435 — https://arxiv.org/abs/2408.08435
- AFlow: Zhang et al., *AFlow: Automating Agentic Workflow Generation*, arXiv 2410.10762 — https://arxiv.org/abs/2410.10762

### 7.4 本地实证数据

- decision-20260812 决策基线（35 任务，10 失败：6 infra / 2 timeout / 2 verification_failure） — `eval_suite/baselines/decision-20260812/`
- 验证命令被拒审计（真实被拒命令溯源） — `~/.agent_go/verification_audit.jsonl`
