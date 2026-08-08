# 调研：Agent Goal/Loop 机制设计——对 agent_go 的启发

> **类型**：调研分析（research），非设计文档
> **日期**：2026-08-08
> **用途**：作为后续概念设计/SDD 的输入材料。本文档不包含实现方案，仅记录现状分析、业界趋势、差距识别和方向性启发。
> **关联**：[prd.md](prd.md) 验证循环 §、H2/H3 演进路线；[design/verification-agent-goal-spec.md](design/verification-agent-goal-spec.md)（现有 goal/验证设计）
>
> **口径说明（2026-08-08 更新）**：本文中的 K1/K4/K8 数值为旧 Bench exploratory 基线，不再作为当前产品 KPI。当前优先级遵循 `roadmap.md` 的 M0-M3；无进展检测和循环状态埋点进入 M2，Reflexion、语义 Goal 和局部重规划属于后置实验能力。

---

## 一、调研背景与方法

### 1.1 问题

agent_go 的核心是 Plan → Decompose → Execute → Verify 流水线，其中"goal（目标）"和"loop（循环）"是两个贯穿始终的概念。本次调研旨在回答：

1. agent_go 现有的 goal/loop 机制处于业界什么位置？
2. 2026 年 agent 设计领域的 goal/loop 最佳实践是什么？
3. 差距在哪里？对 agent_go 的概念设计有什么启发？

### 1.2 方法

- **内部**：深度审查 agent_go 全部 goal/loop 相关代码（goal_injector.py、agent_loop.py、executor.py 验证循环、pipeline.py 波次循环、subtask.py watchdog）
- **外部**：检索 2026 年 agent 架构模式综述、Loop Engineering 学科定义、Reflexion/Verifier-critic 等具体模式

### 1.3 核心结论（先导）

> **agent_go 的 goal/loop 机制已有扎实的「骨架」，但缺关键的「神经」——它能"执行到验证通过"，但不能"从失败中学习"和"自适应调整"。** 业界正从 ReAct 裸循环演进到「Loop Engineering（循环工程）」，agent_go 处于中间位置：比裸 ReAct 强（有 Plan→Verify 外层），但离 2026 最佳实践还有三个结构性缺口。

---

## 二、agent_go 现状全景

### 2.1 五个循环

| 循环 | 位置 | 机制 | 现状评价 |
|------|------|------|---------|
| **Pipeline 波次循环** | `pipeline.py:_run_pipeline` | 拓扑分层 → 并发执行 → 级联阻断（上游 failed/blocked → 下游 blocked） | ✅ 业界领先（多数框架没有 DAG 编排） |
| **验证循环** | `executor.py:_verify_changes` | verify → fix → verify（max_retries，difficulty 分级 easy=2/medium=3/hard=5） | ✅ 扎实，但**无反思**——注入失败上下文重试，不分析失败根因 |
| **AgentLoop** | `agent_loop.py:run` | ReAct 工具调用（max_turns=20，消息窗口留首+末30条） | ⚠️ 基础完备，消息窗口管理粗放 |
| **/goal Stop Hook** | `goal_injector.py` | 验证命令 → /goal 条件 → Claude 内循环 + watchdog（max_turns/timeout） | ⚠️ **默认关闭**（goal.enabled=False），且 goal 条件是机械的 exit_code==0 |
| **Plan 迭代循环** | `cli.py` | 人工拒绝 → 重新生成 → diff 展示（max 5 次） | ⚠️ 纯人工，无自主重规划 |

### 2.2 Goal 机制现状

agent_go 的 goal 不是独立的语义目标，而是**从验证命令机械派生**的：

- `build_goal_condition()`（goal_injector.py:28-34）将验证命令用 ` && ` 连接，包装为 `"以下验证命令全部退出码为0: <cmds>"`
- 这本质是 `exit_code == 0` 的程序化检查，**无自然语言成功标准**
- 设计稿 `verification-agent-goal-spec.md:310-324` 曾规划更丰富的 `goal_condition`/`verification_mode`/`blocking` 子任务级 schema，但 §11.4 实施偏差记录（:908-920）显示**未实现**

### 2.3 关键代码事实

- **验证循环修复 prompt**（`_build_repair_prompt`, executor.py:484-557）：注入 `task_md + 失败标题 + 语义评估反馈 + 失败命令及输出 + 当前变更(diff) + 历史修复尝试`。这是"历史回避"（别犯同样的错），**不是根因抽象**。
- **Goal watchdog**（subtask.py:523-539）：goal_turn_count 超 MAX_GOAL_TURNS → kill；elapsed 超 GOAL_TIMEOUT → kill。kill_reason 传播到验证循环影响重试决策。
- **无反思代码**：grep `reflect|self_correct|meta_learn` 全代码库**零匹配**。
- **无重规划**：Plan 一旦确认就是固定的，验证循环失败只在子任务内重试，从不回到 Plan 阶段。
- **无自适应**：max_turns=20、max_retries=3 全是静态配置。planning.py:23 注释写着"V2 从 verify_state.json 历史学习"——但未实现。

---

## 三、业界趋势（2026）

### 3.1 从 ReAct 到 Loop Engineering

2026 年最显著的趋势是「Loop Engineering（循环工程）」被确立为一门独立学科。核心定义（[Data Science Dojo 2026 指南](https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/)）：

> **Agentic loop = Trigger + Verifiable Goal**。Agent 自主运行直到目标达成，无需持续人工提示。

关键论断：

> *"The difference between loop engineering and just running loops is that loop engineering includes the guardrails. These are not optional."*
> （循环工程和"只是跑循环"的区别在于——循环工程包含护栏，且护栏不是可选的。）

完整的循环 guardrails（护栏）清单：
1. **硬迭代上限**（Hard Iteration Cap）——agent_go ✅ 有（max_turns / max_retries）
2. **资源预算**（Resource Budgeting）——agent_go ✅ 有（L1/L2/L3 成本控制，业界领先）
3. **无进展检测**（No-Progress Detection）——agent_go ❌ 缺
4. **程序化目标检查**（Automated Goal Achievement）——agent_go ✅ 有（verification 命令）
5. **根因分析/反思**（Reflexion）——agent_go ❌ 缺
6. **重规划触发**（Strategic Reset / Inner-Outer Loop）——agent_go ❌ 缺

### 3.2 四个具体模式

调研识别出四个对 agent_go 最有价值的业界模式：

| 模式 | 来源 | 核心机制 | 实证效果 |
|------|------|---------|---------|
| **Reflexion（自我批评）** | [2026 架构分类](https://www.digitalapplied.com/blog/agent-architecture-patterns-taxonomy-2026) | ReAct + 每次迭代后显式 self-critique，批评存入记忆注入下次 | 数学/编码任务**减少重复错误 30-50%** |
| **Verifier-critic（验证驱动重规划）** | 同上 | 生成器 + 评审器，评审按 rubric 评估，失败触发重规划而非仅重执行 | 有效捕获幻觉/策略错误；风险是同模型共谋 |
| **No-Progress Detection（无进展检测）** | [Loop Engineering 指南](https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/) | 输出状态停滞跨多次迭代 → 自动终止 | 防止 token 浪费在"LLM 没实质改变代码"的空转 |
| **Strategic Reset（策略重置/内外环）** | 同上 | 步步执行反复停滞 → 外层循环放弃当前策略整体重来 | 防"执拗失败"（insistent failure） |

### 3.3 Plan-Execute 架构的头号失败模式

[2026 分类指南](https://www.digitalapplied.com/blog/agent-architecture-patterns-taxonomy-2026)明确指出 Plan-Execute 两阶段架构（agent_go 正是此架构）的头号失败模式：

> *"Plan brittleness when the world changes mid-execution"*（执行中途环境变化时的计划脆性）

对策是**添加 re-plan triggers when execution fails**（执行失败时添加重规划触发器）。agent_go 目前完全没有这个机制——Plan 确认后不可变。

### 3.4 /goal/Evaluator Model 的定位

业界将 Claude Code 的 `/goal` 这类 Evaluator Model 视为验证循环的**标准组件**（[Loop Engineering 指南](https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/)）：

> *Evaluator Models: tools like Claude Code's /goal command employ a separate, dedicated AI model at the end of each turn to objectively assess if the goal condition is satisfied.*

agent_go 实现了 `/goal` 机制但**默认关闭**（goal.enabled=False），意味着大多数运行没有这个内层加速。

---

## 四、三个结构性缺口

### 缺口 1：❌ 无「反思/自我纠正」循环

| 维度 | 现状 | 业界最佳 |
|------|------|---------|
| 失败处理 | 注入 stderr/diff → 重试**同一路径** | 生成根因分析 → **策略调整** → 重试 |
| 历史利用 | `历史修复尝试` 列表（历史回避） | Reflexion 批评存入记忆，抽象失败模式 |
| 效果 | N/A | 数学/编码减少重复错误 30-50% |

agent_go 的 `_build_repair_prompt` 注入了"历史修复尝试"，但这只是"别犯同样的错"的事实列表，不是"我为什么失败、应该换什么策略"的根因推理。

### 缺口 2：❌ 无「验证驱动重规划」

| 维度 | 现状 | 业界最佳 |
|------|------|---------|
| 失败响应 | 子任务内重试 max_retries 次 → 标记 failed → 下游级联 blocked | 判断"任务过大/方向错误" → 触发局部重规划（拆分/换路径） |
| Plan 可变性 | 确认后固定不可变 | 执行失败触发 re-plan |
| 失败模式 | Plan 脆性（plan brittleness） | Verifier-critic + 重规划触发器 |

这是 Plan-Execute 架构的头号失败模式，agent_go 完全暴露在这个风险下。

### 缺口 3：❌ 无「自适应循环深度」

| 维度 | 现状 | 业界最佳 |
|------|------|---------|
| max_retries | 静态（easy=2/medium=3/hard=5） | 基于历史边际通过率动态调整 |
| max_turns | 静态（20） | 基于任务复杂度/模型能力自适应 |
| difficulty 判定 | 静态阈值（planning.py 硬编码） | 基于历史首次通过率反馈调整 |

planning.py:23 注释已识别这个方向（"V2 从 verify_state.json 历史学习"），但未实现。

---

## 五、方向性启发（供后续设计参考）

> 以下为定性分析，不含具体实现方案。实际设计由后续 SDD 承接。

### 启发 1：补全循环 guardrails

agent_go 在 6 项 guardrails 中有 3 项（硬上限、资源预算、程序化目标），缺 3 项（无进展检测、根因分析、重规划触发）。可考虑将"guardrails 完备度"作为可观测性指标。

**可探索方向**：
- 无进展检测：retry 间 diff 哈希比对，连续无变化提前终止
- 根因分析：retry ≥ N 时插入轻量 LLM 生成失败分析，注入修复 prompt
- 重规划触发：连续失败信号（如 diff 大但验证全挂、错误模式重复）触发局部 Plan 拆分

### 启发 2：goal 从机械化到语义化

当前 goal 条件是 `exit_code == 0` 的机械派生。如果验证命令覆盖不全，/goal 会让 Claude 在错误方向空转——这是它默认关闭的合理性所在。

**可探索方向**：
- 引入语义 goal 条件（自然语言成功标准），让 evaluator 独立判断
- goal 质量提升后，可考虑默认开启 /goal 内层加速
- 语义 goal 可复用现有 evaluator 模块的能力（它已有"评估输出是否达标"的能力）

### 启发 3：Plan 从不可变到条件可变

Plan-Execute 架构的脆性在于 Plan 不可变。agent_go 可探索"条件可变"的 Plan——不是任意修改，而是在明确信号下触发受控的局部修订。

**可探索方向**：
- 局部重规划：单个子任务连续失败 → 判断是否任务过大 → 拆分为更细子任务
- 安全约束：重规划最多 1 次（防递归）；拆分子任务继承父任务预算上限
- 与 PRD H2-2 Branching（分支式工作流）共享"Plan 可修订"能力底座

### 启发 4：循环状态即学习数据源

业界实践强调把循环状态（失败模式、有效策略）沉淀为跨会话知识。agent_go 已有 verify_state.json，但仅用于 resume，不用于学习。

**可探索方向**：
- verify_state.json 数据结构前向兼容 KnowledgeStore（PRD H2-1）
- 预留 `failure_pattern` / `effective_strategy` 字段
- 历史数据驱动自适应参数（PRD H3-1 方向的具体化）

### 启发 5：内外循环分离

业界"Strategic Reset"模式区分内循环（步步执行）和外循环（策略重置）。agent_go 的验证循环是纯内循环——失败只重试，不升级到"换策略"。

**可探索方向**：
- 内循环（现有）：verify → fix → verify，max_retries 内重试
- 外循环（新增）：内循环耗尽 → 判断是否需策略重置（换路径/换模型/重规划）→ 是则升级

---

## 五·五章、建议目标与价值分析

> 第五章的 5 条启发是定性"方向"。本节把它们升级为**可衡量的建议目标**——每个目标锚定 agent_go 真实 KPI 基线（2026-08-06 bench 实测），明确"做到什么算成功"和"价值提升在哪里"。
>
> **历史基线数据来源**：旧版 `prd.md` KPI 表和 Bench 结果。该数据仅用于说明研究背景，不用于当前产品 KPI 判定。

### 建议 1：验证循环增加 Reflexion 批评层

**目标**：retry ≥ 2 时插入 LLM 失败分析，将"重复同一错误模式的 retry"占比从当前未知（需埋点）降至 **<15%**。

**影响 KPI**：
- **K8 首次验证通过率** 88.9% → 预期 **≥92%**（首次失败后的修复成功率提升，业界数据减重复错误 30-50%）
- **K1 任务成功率** 83.9% → 预期 **+2-4pp**（验证循环是 K1 的直接驱动因素）
- **K4 成本**：副作用是 retry 路径增加一次轻量 LLM 调用（~$0.002-0.005），但减少 retry 次数后净成本预期**下降**

**价值提升在哪**：
- 当前验证循环是"反应式"（注入 stderr → 重试同样方法）。Reflexion 升级为"反思式"（分析为什么失败 → 带策略调整重试），这是从"可靠执行器"到"会学习执行器"的关键跃迁。
- 业界实证：Reflexion 在编码任务**减少重复错误 30-50%**，延迟代价仅 +30%（且仅在 retry≥2 时触发，非每次）。
- 复用已有资产：agent_go 的 evaluator 模块已有"评估输出是否达标"的能力，failure_analysis 是它的 prompt 变体，**无需新模块**。

**成功判据**（可验收）：
- verify_state.json 记录每次 retry 的 failure_analysis
- retry≥2 的子任务中，"前后两次 diff 哈希相同"（无进展）的占比 <15%
- bench 复测 K8 ≥92%

---

### 建议 2：无进展检测——循环终止智能化

**目标**：连续 2 次 retry 的 diff 哈希相同（LLM 没实质改代码）时提前终止，而非跑满 max_retries。

**影响 KPI**：
- **K4 成本**：当前 hard 任务 max_retries=5，若第 3-5 次 retry 是无进展空转，每次浪费 $0.05-0.15。无进展检测可省 **hard 任务 ~20-40% retry 成本**。
- 不影响 K1/K8（无进展的 retry 本来就不会成功，提前终止只是止损）

**价值提升在哪**：
- 直接省钱：这是调研中**ROI 最高、实现最简单**的改进——只需在 retry 历史中比对 `git diff --stat` 的哈希。
- guardrail 补全：业界将"无进展检测"列为循环 6 项必备 guardrail 之一，agent_go 目前缺失。补上后 K6（可观测性）从 8/9 可达 9/9。
- 信号复用：`no_progress` 信号是建议 3（局部重规划）的触发条件——检测到无进展，才该考虑"是不是任务太大该拆分了"。

**成功判据**：
- retry 历史记录每次 diff_stat_hash
- 连续 2 次相同 → 提前终止，result 标记 `failure_reason: "no_progress"`
- bench 复测 hard 任务平均成本下降

---

### 建议 3：局部重规划触发器——打破 Plan 脆性

**目标**：子任务验证失败且信号匹配（无进展 / diff 过大但验证全挂 / 错误模式重复）时，触发局部 Plan 拆分，而非直接标记 failed。

**影响 KPI**：
- **K1 任务成功率** 83.9% → 预期 **+3-5pp**（当前 failed 任务中，相当部分是"任务粒度过大"导致，拆分后可通过）
- **K8**：间接正向（拆分后的子任务粒度更合理，首次通过率更高）

**价值提升在哪**：
- 解决 Plan-Execute 架构的头号失败模式：业界明确指出 *"plan brittleness when the world changes mid-execution"*。agent_go 当前完全暴露在此风险下——Plan 确认后不可变，连续失败只能标记 failed + 级联 blocked。
- H2-2 Branching 的前置能力：PRD H2-2 规划"分支式工作流"，但它依赖"Plan 可修订"这个底座。局部重规划是 Branching 的第一步——先学会"失败后拆分"，再学"主动多路径探索"。
- 信号已有：建议 2 的 `no_progress` + 现有的 retry 历史 + diff stat，组合即可判断"是否任务过大"。

**成功判据**：
- 失败子任务中，触发局部重规划且拆分后通过的比例可度量（埋点 `replan_triggered` / `replan_succeeded`）
- 重规划最多 1 次（防递归），拆分子任务继承父任务预算上限
- 单子任务级触发，不改变全局 Plan 结构

---

### 建议 4：goal 从机械化到语义化

**目标**：goal 条件从 `exit_code==0` 机械派生，升级为"自然语言成功标准 + evaluator 独立判断"，为 /goal 默认开启扫清质量障碍。

**影响 KPI**：
- **K3 耗时**：/goal 默认开启后，Claude 内循环加速，首次完成更快（Codex 实测 /goal 支撑 25h 长程不间断执行）
- **K8**：语义 goal 让 Claude 知道"什么算成功"，减少"代码改了但方向错"的无效 retry

**价值提升在哪**：
- 释放已有机制的潜能：agent_go 已实现 /goal + Stop Hook，但默认关闭。语义化 goal 是"开启它的前置条件"——当前机械 goal 若验证命令覆盖不全，会让 Claude 在错误方向空转。升级后可安全默认开启。
- 一致性：shell 验证（exit_code）+ 语义 goal（evaluator）形成双通道，与现有 evaluator 模块天然协同。
- 安全可控：语义 goal 仍以 shell 验证为硬门禁，evaluator 是 AND 叠加——不会因为 LLM 误判就放过。

**成功判据**：
- 子任务携带 `goal_description`（自然语言成功标准），由 Plan 阶段生成
- evaluator 可独立判断 goal_description 是否满足
- /goal 默认开启后，bench 复测 K8 不劣化

---

### 建议 5：verify_state.json 前向兼容 KnowledgeStore

**目标**：扩展 verify_state.json 数据结构，预留 `failure_pattern` / `effective_strategy` 字段，为 H2-1 KnowledgeStore 和 H3-1 参数自调优积累数据源。

**影响 KPI**：
- **K6 可观测性** 8/9 → 9/9（循环 guardrails 完备度的度量能力）
- 为 **K4 长期下降** 提供数据基础（H3-1 参数自调优需要历史边际通过率数据）

**价值提升在哪**：
- 零成本的前置投资：现在改 verify_state.json schema 加字段，不改变现有行为，但为 H2/H3 积累数据。等 KnowledgeStore 落地时已有 1-2 个季度的历史数据可用。
- 防"数据不可用"陷阱：如果不现在埋点，等 H3-1 启动时发现"没有历史 failure_pattern 数据"，要从头积累，延迟一个季度。
- 业界印证：*"State is stored in the file system... Every new session reads this current state to avoid repeating known errors."*——agent_go 已有这个机制（resume），只需扩展用途。

**成功判据**：
- verify_state.json 新增 `failure_pattern`（错误类型分类）/ `effective_strategy`（哪个 retry 尝试成功了）字段
- 字段写入但不影响现有 resume 逻辑（纯增量）
- 数据格式文档化，供后续 KnowledgeStore 消费

---

### 优先级与依赖关系

基于价值/成本比和依赖关系排序：

```
建议 2（无进展检测）─────────┐
   ↓ 提供信号                │
建议 1（Reflexion 批评层）───┤──→ 建议 5（数据埋点，贯穿全程）
   ↓ 提供 failure_analysis   │
建议 3（局部重规划）─────────┘
   
建议 4（语义 goal）── 独立，可与上述并行
```

| 优先级 | 建议 | 预估投入 | 依赖 | 预期 KPI 影响 |
|--------|------|---------|------|-------------|
| **P0** | 建议 5 数据埋点 | 1 天 | 无 | K6 9/9；为 H2/H3 积累数据 |
| **P0** | 建议 2 无进展检测 | 1-2 天 | 无 | K4 hard 任务 -20-40% retry 成本 |
| **P1** | 建议 1 Reflexion 批评层 | 2-3 天 | 建议 5（读 failure_pattern） | K8 ≥92%、K1 +2-4pp |
| **P1** | 建议 4 语义 goal | 2 天 | 无（可与建议 1 并行） | K3 改善、为 /goal 默认开扫障 |
| **P2** | 建议 3 局部重规划 | 3-4 天 | 建议 2（no_progress 信号） | K1 +3-5pp；H2-2 前置 |

**关键路径**：建议 5（埋点）→ 建议 2（无进展检测）→ 建议 1（Reflexion）→ 建议 3（重规划）。建议 4 可随时并行。建议 5 标为 P0 是因为它是"现在不改未来补票"的前置投资——越早埋点，H2/H3 启动时数据越充分。

### 价值总结

这 5 条建议目标的总价值，不是"5 个新功能"，而是**一条递进的能力链**：

```
当前：执行 → 验证 → 失败 → 反应式重试同样方法 → 跑满上限 → failed
                                                    
目标：执行 → 验证 → 失败 → [无进展检测止损] → [Reflexion 分析根因] → [策略调整重试]
                                    ↓ 仍失败
                            [局部重规划拆分] → 重新执行
                                    ↓ 全程
                            [verify_state 积累经验] → 跨会话学习
```

**从"反应式"到"反思式"再到"自适应"**——这正是 agent_go 从 H1（单任务可靠）跨越到 H2（跨上下文记忆）的核心能力建设。每一步都有明确的 KPI 锚点和业界实证支撑，不是凭空设计。

---

## 六、与 agent_go 现有路线的关系

本次调研的启发**不是新方向**，而是给 PRD 已规划的演进路线填上具体的第一步：

| 启发 | 对应 PRD 方向 | 关系 |
|------|-------------|------|
| Reflexion 批评层（启发 1） | 验证循环增强（P1 重点） | 给"验证循环"增加反思能力的具体落地 |
| 局部重规划（启发 3） | H2-2 Branching 分支式工作流 | Branching 的前置能力（Plan 可修订） |
| 自适应参数（启发 4） | H3-1 Harness 参数自动调优 | 细化到按任务类型/模型维度的自适应 |
| 语义 goal（启发 2） | /goal 默认开启 | 默认开启的质量前置条件 |
| 内外循环分离（启发 5） | PRD F-VERIFY-5/F-VERIFY-6 | 自评估的外循环形态 |

**核心判断**：agent_go 从"反应式"（失败→重试同样）到"反思式"（失败→分析→策略调整→重试）的升级，是从"可靠的执行器"进化为"会学习的执行器"的关键一步，与 H2/H3 路线完全吻合。

---

## 七、信息来源详述

> 以下逐一摘要每个来源的核心论点、关键数据和对 agent_go 的具体启发。

### 来源 1：Agentic Loops: From ReAct to Loop Engineering (2026 Guide)

🔗 [Data Science Dojo — Agentic Loops: From ReAct to Loop Engineering](https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/)

**核心论点**：Loop Engineering（循环工程）已确立为独立学科。其定义 agentic loop = trigger + verifiable goal，与普通自动化的区别在于 agent 内部有"目标是否达成"的评估步骤。

**关键数据/事实**：
- ReAct 在 ALFWorld 基准上比纯行动模式提升 **34%**，WebShop 提升 10%
- Codex CLI 的 `/goal` 命令实现过 **25 小时不间断执行、1300 万 token、3 万行代码** 的长程任务
- 单 agent 约消耗标准对话 **4 倍** token；多 agent 约 **15 倍**
- 反面案例：一个 agent 在 5 分钟内**重复执行一个坏的工具调用 400 次**——这正是无 guardrails 的后果

**六个 guardrails 清单**（本文最重要贡献）：
1. 硬迭代上限（iteration cap）
2. 资源/ token 预算
3. 无进展检测（no-progress detection）——输出停滞自动终止
4. 程序化目标检查（programmatic goal check）——用测试/编译而非 LLM 自评
5. Reflexion 自我批评——失败后生成批评存入记忆
6. 策略重置/内外环——内循环停滞时外循环放弃当前策略

**其他关键模式**：
- **Ralph Loop**：每轮迭代重置上下文窗口，状态持久化在代码库/TODO 文件中
- **State Persistence**：LangChain 的 `CLAUDE.md` 实践——累积知识文件，记录每次错误，确保未来会话不重复
- **内外循环**：Anthropic 多 agent 研究系统用编排层比单 agent **内部评测高 90.2%**；微软 Magentic-One 架构

**对 agent_go 的启发**：
- ✅ agent_go 有 guardrails 1/2/4（max_retries、L1-L3 成本控制、verification 命令）
- ❌ 缺 guardrails 3/5/6（无进展检测、Reflexion、策略重置）
- verify_state.json 已有状态持久化的雏形，但仅用于 resume，未用于"避免重复错误"的知识积累
- /goal 默认关闭与 Codex 的 /goal 25 小时长程能力形成对比——关键在于 goal 条件质量

---

### 来源 2：Agent Architecture Patterns: 2026 Taxonomy Guide

🔗 [Digital Applied — Agent Architecture Patterns: 2026 Taxonomy Guide](https://www.digitalapplied.com/blog/agent-architecture-patterns-taxonomy-2026)

**核心论点**：2026 年 agent 架构已形成清晰分类，四个模式各有明确适用场景和量化效果。

**四个模式详解**：

| 模式 | 机制 | 量化效果 | 失败模式 | 缓解 |
|------|------|---------|---------|------|
| **Reflexion（自我批评）** | ReAct + 每步后显式 self-critique，批评注入下次迭代上下文 | 延迟 +30%，但失败模式子集质量提升 10-30%，**减少重复错误 30-50%** | 过度纠正导致振荡；主观任务批评可靠性下降 | 限制 2-3 轮；主观领域配 rubric |
| **Plan-and-Execute（两阶段）** | planner 发出有序计划，executor（常为便宜模型）逐步执行 | 规划成本可跨多次运行摊销 | **plan brittleness**（计划脆性）；planner-executor 能力不匹配；过度优化"完成计划"而非"用户目标" | 添加 re-plan triggers；限制最大计划长度 |
| **Verifier-critic（验证评审）** | 生成器产出 → 评审器按 rubric 打分 → 生成器据反馈修订 | 验证子集推理成本翻倍，但削减关键错误 | **critic-generator 共谋**（同模型盲点相同）；边缘案例过度纠正 | 生成器和评审器用不同模型；限制修订轮数；低置信度转人工 |
| **Re-plan Triggers（重规划触发）** | 执行失败 → 中断执行图 → 控制流回到 planner 重新规划 | 防止长循环的 prefix-cache miss（可致成本飙升 5-10×） | 无触发器 = 动态环境全面失败 | ~30-40 步设 re-anchor 检查点；长循环 >50 步失去连贯性 |

**对 agent_go 的启发**：
- agent_go 属于 **Plan-and-Execute 模式**，直接暴露该模式的头号失败风险——plan brittleness。当前验证失败只在子任务内重试，无 re-plan trigger，是明确的改进方向
- agent_go 的验证循环可升级为 **Reflexion**：retry ≥ N 时插入失败分析步骤，预期减少重复错误 30-50%，代价是延迟 +30% 和额外 LLM 调用
- agent_go 的 review --deep（M7 审查阶段）已是 **Verifier-critic** 的雏形——独立模型评审。但需注意共谋风险：reviewer 应与 worker 用不同模型（PRD 模型分级策略已体现这一点）
- **re-anchor 检查点**：agent_go 的 AgentLoop 消息窗口（留首+末30条）是粗暴版 re-anchor，>50 步的连贯性损失风险存在

---

### 来源 3：The Agent Loop Decoded | Three Levels Every Agent Engineer Must Know

🔗 [Oracle Developers — The Agent Loop Decoded](https://blogs.oracle.com/developers/the-agent-loop-decoded-three-levels-every-agent-engineer-must-know)

**核心论点**：agent 循环有三个层次，理解层次划分是架构设计的基础。配套文 [What Is the AI Agent Loop?](https://blogs.oracle.com/developers/what-is-the-ai-agent-loop-the-core-architecture-behind-autonomous-ai-systems) 和 [Building a Memory-First Agent Harness](https://www.oracle.com/webfolder/technetwork/slackimages/devrel/slides-devcoach-040226.pdf) 补充了 Level 3 的细节。

**三层框架**：

| 层级 | 范围 | 内容 |
|------|------|------|
| **Level 1** | 单次调用 | assemble context（组装上下文）→ invoke model（调用模型）→ act（产出输出/工具调用） |
| **Level 2** | 循环（多次迭代） | 重复调用，状态在轮次间传递，工具调用结果反馈进来。简单形态：Think → Act → Observe → Repeat |
| **Level 3** | harness 即系统 | 编排、**记忆生命周期（五阶段）**、权限、错误处理、整体 agent 控制 |

**Level 3 的关键概念**：
- agent harness 是 agent loop 的**实现**，本身是一个完整系统
- "memory-first agent harness"——记忆优先的 harness 设计，定义完整的五阶段记忆生命周期
- Victor Dibia 的 [Agent Execution Loop](https://newsletter.victordibia.com/p/the-agent-execution-loop-how-to-build) 技术实现：Prepare Context → Call Model → Handle Response
- Steve Kinney 的 [Agent Loop Anatomy](https://stevekinney.com/writing/agent-loops) 强调循环内的 tool-permission 层

**对 agent_go 的启发**：
- agent_go 的架构天然映射到三层：AgentLoop = Level 1/2，pipeline + executor = Level 3 harness
- **Level 3 的"记忆生命周期"**是 agent_go 的薄弱点——当前无跨会话记忆（KnowledgeStore 是 PRD H2-1 方向）。Oracle 强调 harness 的记忆能力是 Level 3 成熟度的标志
- **tool-permission 层**：agent_go 的 Bash blocklist + 安全白名单（_is_safe_verification_command）已是对应实现，这是 Level 3 的必备组件

---

### 来源 4：ReAct vs Plan-and-Execute vs ReWOO vs Reflexion

🔗 [The AI Engineer (Substack) — ReAct vs Plan-and-Execute vs ReWOO vs Reflexion](https://theaiengineer.substack.com/p/the-4-single-agent-patterns)

**核心论点**：*"Every AI agent, at its core, is an LLM running in a loop. It receives a goal, decides..."*——所有 agent 本质是 LLM 在循环中运行，接收目标做决策。四种模式的差异在于循环结构。

**四种模式速览**：

| 模式 | 核心机制 | 优势 | 劣势 | 适用场景 |
|------|---------|------|------|---------|
| **ReAct** | 每步推理（reason per step），Thought→Action→Observation 循环 | 自适应、动态、对不确定/混乱任务可靠 | 慢且贵（多次 LLM 调用） | 探索性任务、信息持续变化 |
| **Plan-and-Execute** | 前置规划 + 便宜 executor 逐步执行 | 战略规划、executor 成本低 | 条件变化时适应性差 | 可预测的工作流 |
| **ReWOO** | 仅 2 次 LLM 调用（规划+求解） | 极致省 token | 静态计划、不适合探索 | 成本敏感、确定性强 |
| **Reflexion** | 带反思/批评的重试 | 自我纠正、跨重试改进 | 反思周期的额外开销 | 调试、试错型任务 |

**配套来源补充**：
- [Nutrient: ReWOO vs ReAct](https://www.nutrient.io/blog/rewoo-vs-react-choosing-right-agent-architecture/)：ReAct 可靠但贵；ReWOO 先规划后执行
- [SPR 对比表](https://spr.com/comparing-react-and-rewoo-two-frameworks-for-building-ai-agents-in-generative-ai/)：ReAct 随反馈演化计划；ReWOO 计划静态预定义
- [Agent Patterns 文档](https://agent-patterns.readthedocs.io/en/stable/patterns/rewoo.html)：ReAct 适合自适应工具使用/探索；ReWOO 不适合简单单步任务

**对 agent_go 的启发**：
- agent_go 是 **Plan-and-Execute + Reflexion（部分）** 的混合体——Plan 阶段前置规划，验证循环有重试但缺真正的 Reflexion 反思
- 关键洞见：agent_go 的"可靠性"优势正来自 Plan-and-Execute（vs 裸 ReAct 的不确定性），但代价是 plan brittleness
- ReWOO 的"2 次调用"思路对 agent_go 的 **Plan 缓存**（已有）有借鉴——对相似任务复用计划，省 planner token
- Reflexion 是 agent_go 验证循环最自然的升级路径，且适配"调试不熟悉代码库"这一 agent_go 核心场景

---

### 来源 5：Verified Multi-Agent Orchestration: A Plan-Execute... (arXiv)

🔗 [arXiv — Verified Multi-Agent Orchestration: A Plan-Execute...](https://arxiv.org/html/2603.11445v1)

**核心论点**：提出一个开放、模块化的多 agent 编排框架，核心创新是**显式的 verification-driven replanning loop（验证驱动重规划循环）**——协调策略（特别是验证驱动的重规划循环）在架构中是显式可插拔的。

**关键贡献**：
- 将"验证失败 → 重规划"这个环节从隐式（埋在各 agent 内部）提升为**显式的架构级一等公民**
- 模块化设计：协调策略可替换，不同任务可配不同验证-重规划策略
- 这是对 Plan-Execute 架构 plan brittleness 问题的学术回应

**对 agent_go 的启发**：
- agent_go 当前验证循环和 Plan 是**两个分离的、不互通的系统**——验证失败不会回流到 Plan。这篇论文的框架正是解决这个断裂
- "显式化协调策略"的思路：agent_go 可考虑把"验证失败后做什么"（重试？重规划？升级模型？）从硬编码逻辑（executor.py 的 if/else）提升为**可配置的策略**
- 这与 PRD H2-2 Branching（分支式工作流）和本文启发 3（局部重规划）学术上同源——都指向"Plan 可修订"

---

## 附：现状审查依据（agent_go 内部代码）

| 文件 | 行号 | 审查内容 |
|------|------|---------|
| `agent_go/goal_injector.py` | 全文(100行) | /goal 注入 + Stop Hook + 安全白名单 |
| `agent_go/agent_loop.py` | 148-314 | ReAct 循环结构、消息窗口、终止条件 |
| `agent_go/executor.py` | 484-557, 719-1197 | _build_repair_prompt、验证循环、goal 注入 |
| `agent_go/subtask.py` | 95-119, 523-561 | watchdog 配置、kill 逻辑、terminal re-check |
| `agent_go/pipeline.py` | 291-400 | 波次循环、级联阻断 |
| `agent_go/planning.py` | 23, 27-61 | 历史学习 TODO、decomposition 判定 |
| `docs/design/verification-agent-goal-spec.md` | 310-324, 908-920 | goal_condition schema 设计 vs 实施偏差 |
