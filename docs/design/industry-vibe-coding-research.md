# 业界 Vibe Coding / Agentic Engineering 调研：架构分解问题如何被解决

> **版本**：v1.0
>
> **目的**：调研业界在「AI 编码 Agent 的架构分解、模块边界识别、接口契约传递」问题上的实践和方法，对照 agent_go 当前能力，识别可借鉴的方向。
>
> **日期**：2026-08-01

---

## 一、核心发现：这个问题业界也正在解决，远未成熟

**直接结论：agent_go 面临的 Planner 架构分解问题，是整个行业正在集体攻坚的问题。没有一个工具「已经解决了」——但有一套正在形成的共识方法论。**

三个关键信号：

1. **Andrej Karpathy 2025年2月提出「Vibe Coding」，2026年亲自否定了它**——用「Agentic Engineering」替代：人掌握架构、测试和审查，Agent 执行代码生成。Karpathy 的原话：「你编排 Agent 写代码，你拥有架构、测试和审查。」

2. **arXiv 2026年4月论文「Architecture Without Architects」**——标题本身就是诊断：AI 编码 Agent 在做隐式架构决策，而这些决策几乎从不被人作为架构来审查。论文发现：prompt 措辞不同，同一个 FAQ chatbot 能生成 141 行/2 文件 vs 827 行/6 文件——**架构决策被藏在 prompt 的措辞差异里**。

3. **数据证实了无结构化的代价**：AI 生成代码中 45% 存在安全漏洞、代码重复增加 48%、重构活动下降 60%——这些是在无结构化的 vibe coding 阶段观测到的数据。

---

## 二、业界共识：四层方法论

六个独立来源（Red Hat、SitePoint、ACM、arXiv、开源框架）收敛到同一套方法论：

### 层 1：Spec-First — 先写规格，再写代码

**这是最强的共识信号。每个来源都独立得出了这个结论。**

| 来源 | 方法 | 关键实践 |
|------|------|---------|
| **Red Hat**（2026.3） | 「四支柱」：Vibes → Specs → Skills → Agents | Spec 是精确的、权威的指令。用模块化 Markdown 文件，分离「是什么」和「怎么做」 |
| **Sprint 框架** | `specs.md` → Orchestrator → Architect → Implementers → Testers | 两份「第二大脑」文件：`.claude/project-goals.md`（业务愿景）+ `.claude/project-map.md`（技术架构） |
| **Addy Osmani**（Google） | 「15 分钟瀑布模型」 | 先写 spec.md（需求、架构、测试策略），然后 Agent 执行。spec 是 Agent 工作的契约 |
| **APS**（Anvil Plan Spec） | 四层规格层级 | Index（为什么）→ Modules（模块边界）→ Work Items（执行授权）→ Action Plans（协调） |

**与 agent_go 的对照**：agent_go 的 `--spec` + Task Spec 7 章节 = 业界 Spec-First 共识的落地。**agent_go 在这个维度上不落后——它正好在共识方向上。**

### 层 2：Architecture Gate — 生成代码之前，先审查架构

**这是「Vibe Architecting」论文的核心建议：Agent 必须先输出架构设计，人批准后再生成代码。** 中文 AI 开发社区的一份 2026 工作流指南报告：强制 Agent 先输出完整架构文档（目录结构、数据库 schema、API 列表、依赖版本），**人批准后再写代码——后期重构减少 70%。**

| 实践 | 说明 |
|------|------|
| **Pre-Generation Architecture Review** | Agent 先输出架构方案 → 人审查 → 批准后生成代码 |
| **Risk-Tiered Gates** | LOW（UI/样式/测试）→ Agent 自主；MEDIUM（新依赖/DB 迁移）→ Agent 先问；HIGH（认证/支付/数据模型变更/删除）→ 必须人批准 |
| **Three-Layer Governance** | Constraints（AGENTS.md 规则）→ Conformance（生成后检查）→ Knowledge（经验反馈） |

**与 agent_go 的对照**：agent_go 已有 Plan 确认环节（Y/S/D/E/R/N）。**差距在于**：当前 Plan 确认只展示步骤分解，不展示架构影响分析（模块边界变更、接口契约变更、风险等级）。如果 Plan 确认环节加入架构影响分析，就等于落地了 Architecture Gate。

### 层 3：Interface Contract — Agent 之间通过结构化契约通信

**多个框架独立发现了同一个模式**：

| 来源 | 名称 | 核心机制 |
|------|------|---------|
| **SitePoint** | 「Model Handshake」 | Agent N 的输出 schema = Agent N+1 的输入 schema。四阶段流水线（Analyst → Architect → Implementer → Reviewer），用 JSON Schema 做 agent 间通信协议。关键字段：`scope`, `affected_files`, `interface_contracts`, `constraints`, `prior_decisions` |
| **Agent Chronos 2.0** | 「Tree Decomposition」 | PRD → 递归分解为树节点。每个节点有「显式子契约」：父节点只能通过子节点的接口组合实现。如果父节点需要「绕过子节点用隐藏逻辑」，说明分解错了 |
| **AI Chain（ACM）** | 「Function Signature Worker」 | 每个 worker 定义输入/输出/前置条件/后置条件，「类似于软件工程中的接口规范」 |
| **agent-teams-cc** | 「Goal-backward verification」 | 验证者不检查「任务是否完成」，而是检查「目标是否达成」：Exists（文件存在）→ Substantive（非空壳代码）→ Wired（被 import 和使用）。**80% 的空壳代码在前两层通过但在第三层暴露** |

**与 agent_go 的对照**：agent_go 的 git worktree + tag merge 是产物传递的机制层实现——上游产出通过 git 而非 prompt 传递。**这个设计恰好避免了业界正在批评的「通过 prompt 传递大量上下文导致信息丢失」的问题。** agent_go 在这个维度上超前于多数框架。

**差距在于**：agent_go 缺乏接口契约的**语义验证**——上游合入下游 worktree 的代码，是否真的符合设计文档中定义的接口契约？当前只能通过 shell exit code 验证「代码能跑」，不能验证「接口符合约定」。

### 层 4：Prompt-as-Contract — Prompt 是执行契约，不是建议

| 来源 | 核心主张 |
|------|---------|
| **AI Agent Engineering Book** | Prompt Contract 九要素：Objective, Inputs, Constraints, Tool Contract, Approval Gate, Forbidden Actions, Refusal Conditions, Completion Criteria, Output Schema。**分离这些类别，防止 Agent 自行推断优先级和 trade-off** |
| **Faceted Prompting（TAKT）** | 将 prompt 分解为五个独立文件：Persona（谁）、Policy（规则/禁止）、Instruction（步骤）、Knowledge（参考上下文）、Output Contract（输出结构）。每个 workflow step 组合不同的 facet |
| **Implementation Prompts（CodeMag）** | 好的执行 prompt 定义：切片边界、正确的 Agent 角色、工作顺序、所需产物、可观测的验收标准、如何验证和演示结果 |

**与 agent_go 的对照**：agent_go 的 Plan step `agent_prompt` 字段的当前质量方差很大——有时是详细的约束指令，有时只是 step title 的改写。**P0 REQ-3（架构约束传递到 agent_prompt）正是 Prompt-as-Contract 的落地。**

---

## 三、agent_go 与业界的位置对照

| 维度 | 业界共识 | agent_go 当前 | 差距 |
|------|---------|-------------|------|
| **Spec-First** | spec.md → Agent 执行 | ✅ `--spec` + Task Spec 7 章节 | 在共识方向上。需落地实施 |
| **Architecture Gate** | Agent 先输出架构方案 → 人批准 | ⚠️ Plan 确认环节存在但不展示架构影响 | 需在 Plan 确认中加入架构影响分析 |
| **模块边界分解** | 按模块边界切分，而非按技术层 | ❌ 当前按技术层分解（模型→API→测试） | P0 REQ-1 |
| **接口契约** | Agent 间通过结构化 schema 通信 | ✅ git worktree + tag merge 机制层优于业界 | 缺接口契约语义验证（P2 REQ-7） |
| **Prompt-as-Contract** | Prompt 是执行契约，九要素分离 | ⚠️ agent_prompt 字段存在但质量方差大 | P0 REQ-3 |
| **接口先行** | 接口骨架先于实现，下游并行 | ❌ 依赖仅表达「等 A 完成」 | P0 REQ-2 |
| **架构知识传递** | 通过 CLAUDE.md / AGENTS.md / project-map.md | ✅ Skill 系统 + CLAUDE.md + `--docs` | 设计文档→Planner 的结构化提取待改进 |
| **验证层次** | Exists → Substantive → Wired | ⚠️ shell exit code + semantic 评估 | 缺 Wired 层验证（代码是否被真正集成使用） |

---

## 四、业界尚未解决的问题（agent_go 的机会）

以下问题是整个行业都在探索、尚无共识方案的：

### 1. 跨 Agent 架构一致性自动验证

没有任何工具能自动验证「上游 Agent 的产出是否符合架构设计文档中定义的接口契约」。Model Handshake 靠 JSON Schema 做语法验证，但语义验证仍然靠人。**agent_go 的 P2 REQ-7 如果做出来，是差异化能力。**

### 2. 架构决策的追溯链

论文「Architecture Without Architects」指出：AI Agent 做的架构决策具有「规模、速度、不透明」三个特征——没有 ADR（Architecture Decision Record）、没有设计文档、没有记录推理过程。**agent_go 的 P2 REQ-8（设计文档与代码双向追溯）如果做出来，是差异化能力。**

### 3. 分解质量的自动评估

Agent Chronos 提出了「组合验证」：如果父节点能干净地通过组合子节点的接口实现，分解就是正确的。但这个验证目前是手动的。**agent_go 的 bench 框架 + cross_judge 可以在 bench 层面做分解质量评估——不需要等业界方案。**

### 4. 知识反馈闭环

Red Hat 的四支柱框架提到了 Knowledge 层但未给出具体实现。APS 提到了经验反馈但停留在概念阶段。**agent_go 的 KnowledgeStore（H2-1）+ bench 数据反馈闭环 = 业界领先的实践。**

---

## 五、可借鉴的具体实践

### 5.1 立即可借鉴（不改 agent_go 架构）

| 实践 | 来源 | 借鉴方式 |
|------|------|---------|
| **Prompt-as-Contract 九要素** | AI Agent Engineering Book | 用于标准化 Task Spec 的 §4（约束）和 §5（验收标准）——确保每个 Spec 都包含 Objective/Constraints/Completion Criteria |
| **Risk-Tiered Gates** | agent-guardrails-template | 用于 Spec Gate L2 软警告——高风险变更（认证/支付/数据模型）强制人确认 |
| **三层验证（Exists/Substantive/Wired）** | agent-teams-cc | 用于 semantic evaluator 增强——不只检查「代码能跑」，还要检查「代码被真正集成使用」 |
| **Faceted Prompting** | TAKT | Task Spec 7 章节已经接近 facet 模式——§1 目标=Objective, §4 约束=Policy, §5 验收标准=Completion Criteria |

### 5.2 中期可借鉴（需 agent_go 改动）

| 实践 | 来源 | 借鉴方式 |
|------|------|---------|
| **Pre-Generation Architecture Review** | ABP Studio, APS | agent_go Plan 确认环节增加「架构影响分析」卡片 |
| **Model Handshake** | SitePoint | agent_go 的 dependencies 增加「接口依赖」类型——下游只需要上游的接口骨架即可并行 |
| **第二大脑文件** | Sprint（project-goals.md + project-map.md） | 对应 agent_go 的 CLAUDE.md + architecture.md ——两份自动维护的项目知识文件 |

### 5.3 不需要借鉴的（agent_go 已经更好）

| 实践 | 来源 | 为什么 agent_go 不需要 |
|------|------|----------------------|
| Vector DB 做代码检索 | 多个来源 | agent_go 用 git worktree 隔离——Worker 只看到相关文件，不需要从千文件中检索 |
| 多 Agent 聊天协商 | agent-teams-cc | agent_go 用 tag merge 做产物传递——比 Agent 间聊天更可靠、更可复现 |
| PRD → 代码树自动分解 | Agent Chronos | agent_go 的 Plan → Decompose → Execute 已经做到了，且通过 `--spec` 接受人工指导 |

---

## 六、对 agent_go 路线图的建议

### 立即做（S11-P0 期间）

1. **将 Prompt-as-Contract 九要素映射到 Task Spec 7 章节**——确保 Spec 模板覆盖 Objective/Inputs/Constraints/Completion Criteria/Output Schema。（~0.5d，改模板文案）
2. **Plan 确认环节增加架构影响摘要**——不只是展示步骤列表，而是展示「模块边界变更」「接口契约变更」「风险等级」。（~1d，改 ui.py）

### S10 bench v2 后做

3. **将三层验证（Exists/Substantive/Wired）纳入 semantic evaluator**——用 bench 数据验证「代码是否被真正集成使用」。（~2d）
4. **agent_go 的两份「第二大脑」文件自动维护**——CLAUDE.md（项目规则）+ architecture.md（技术架构），由 agent_go 每次执行后自动更新。（~2d，依赖 KnowledgeStore）

### 远期探索

5. **架构决策追溯链**（P2 REQ-8）——每次 agent_go 执行的架构影响自动记录，形成可追溯的 ADR
6. **模块边界变更的自动影响分析**——当 Spec 涉及跨模块变更时，自动分析受影响的下游模块

---

## 七、关键结论

1. **agent_go 面临的 Planner 问题不是 agent_go 独有的——是整个行业的前沿问题。**「Architecture Without Architects」这篇 2026 年 4 月的论文说明学术界刚刚开始系统研究这个问题。

2. **agent_go 在三个维度上超前于业界**：git worktree 产物传递（vs prompt 传递）、Spec-First 结构化输入、bench 驱动的能力评估。保持这些优势。

3. **agent_go 在三个维度上落后于业界共识**：模块边界分解（P0 REQ-1）、接口先行（P0 REQ-2）、架构约束传递到 agent_prompt（P0 REQ-3）。这三个都是 prompt engineering 可解决的，不需要架构改动。

4. **最大的机会**：架构一致性自动验证 + 架构决策追溯链。业界没有任何工具做到这两点。如果 agent_go 在 P2 做到了，是真正的差异化。

---

*数据来源：*
- Karpathy (2025→2026): Vibe Coding → Agentic Engineering
- arXiv: "Architecture Without Architects: How AI Coding Agents Shape Software Architecture" (2604.04990, 2026.4)
- Red Hat: "Vibes, specs, skills, and agents: The four pillars of AI coding" (2026.3)
- SitePoint: "The Model Handshake: Chaining AI Agents for Complex Refactors"
- AI Agent Engineering Book: Prompt-as-Contract 九要素
- TAKT: Faceted Prompting
- Agent Chronos 2.0: Tree Decomposition
- APS (Anvil Plan Spec): 四层规格层级
- Sprint: spec-driven state machine
- agent-teams-cc: Goal-backward verification
- agent-guardrails-template: Risk-Tiered Gates

---

## 附录 A：第二轮深度搜索补充发现（2026-08-01）

### A.1 SDD（Specification-Driven Development）三足鼎立

搜索确认，Spec-Driven Development 已经从概念进入工程实践，形成了三个主要的开源工具/方法论：

| 工具 | 来源 | Stars | 核心机制 |
|------|------|-------|---------|
| **Spec-Kit** | GitHub 官方 | — | 结构化 slash commands：`/speckit.constitution`（项目原则）→ `/speckit.specify`（规格）→ `/speckit.plan`（计划）→ `/speckit.implement`（实现）→ `/speckit.verify`（验证）。五阶段流水线 |
| **OpenSpec** | Fission-AI 社区 | — | `openspec/changes/<name>/` 目录结构：`proposal.md`（提案）→ `specs/`（规格）→ `design.md`（设计）→ `tasks.md`（任务）。**先写全部规划制品，再写一行代码** |
| **BMAD-METHOD** | Brian (bmadcode) | 19.7k+ | 角色分工框架：Blueprint（蓝图）→ Architect（架构）→ Developer（开发）。核心前提：「不要把 AI 当万能助手，而是当成一组有分工的角色」 |

**三者共同点**：Spec → Plan → Tasks → Implement 的流水线。**在写任何代码之前，先写完规格、设计、任务拆分。** 这与 agent_go 的 Plan → Decompose → Execute 是同一个方向。

**与 agent_go 的关键差异**：Spec-Kit/OpenSpec/BMAD 的 Spec 和 Plan 是**同一个 Agent 在交互式对话中完成的**（人+AI 协作），而 agent_go 将 Spec 写作（Phase 2，人在 Claude Code 中完成）和 Plan 生成（Phase 3，agent_go headless）**分离为不同阶段**。agent_go 的设计更强调「Spec 是人写的，Plan 是 agent_go 生成的」——这更符合 Anthropic 2026 年 Agentic Engineering 的「人拥有架构」原则。

### A.2 BMAD-METHOD：角色分工的工程化

BMAD 是目前社区最完整的 AI 开发流程框架。关键设计：

```
BMAD 角色分工：
  ├─ Orchestrator（编排者）—— 协调各角色，管理流程
  ├─ Business Analyst（业务分析）—— 需求分析
  ├─ Architect（架构师）—— 架构设计
  ├─ Developer（开发者）—— 代码实现
  ├─ Code Reviewer（代码审查）—— 审查代码
  └─ Tester（测试者）—— 测试验证

BMAD 核心约束：
  - "Dev Agents Must Be Lean"（开发 Agent 必须精简）
    → 最小化上下文依赖
  - "Every phase produces artifacts"（每个阶段产生产出物）
    → PRD → Architecture → Tasks → Code → Tests
  - "Gates between phases"（阶段间有门禁）
    → 架构批准后才能编码
```

**与 agent_go 的对照**：BMAD 的 Orchestrator = agent_go 的 Pipeline 调度器；BMAD 的 Architect = agent_go 的 Planner + 工程师写的技术方案；BMAD 的 Developer = agent_go 的 Worker。**agent_go 缺少的是 BMAD 的「阶段间门禁」机制——当前 Plan 确认只是确认步骤列表，不是确认「架构影响」。**

### A.3 OpenSpec 的目录结构：Spec 作为代码的一部分

OpenSpec 的设计最具工程感——Spec 不是外部文档，而是代码仓库的一部分：

```
openspec/changes/add-dark-mode/
  ├── proposal.md     ← 提案（为什么做）
  ├── specs/
  │   └── ui/
  │       └── spec.md ← UI 模块规格
  ├── design.md       ← 设计文档
  └── tasks.md        ← 任务列表
```

OpenSpec 的一个核心命令：`/opsx:propose add-dark-mode` —— 一步创建完整的规划目录结构。

**与 agent_go 的对照**：agent_go 的 `docs/tasks/` 目录结构 + Task Spec 7 章节与 OpenSpec 的 changes 目录结构**高度相似**。差异在于：OpenSpec 的 `design.md` 是架构设计文档（agent_go 对应 `docs/design/`），OpenSpec 的 `tasks.md` 是任务列表（agent_go 对应 Task Spec + Plan steps）。**agent_go 可以借鉴 OpenSpec 的 `/opsx:propose` 模式——`agent_go scope` 一步创建完整的 Spec 目录结构。**

### A.4 Plan-and-Execute 模式：RePlan 机制

Plan-and-Execute 是 2025-2026 年 AI Agent 设计模式的核心范式之一，用于复杂任务的拆解与执行。

```
Plan-and-Execute 架构：
  1. 规划阶段 (Plan)
     ├─ Agent 分析用户任务
     ├─ 拆解为有序子步骤（含依赖关系）
     └─ 生成 Plan
  2. 执行阶段 (Execute)
     ├─ 逐步执行子步骤
     └─ 收集每步结果
  3. 重规划 (RePlan) ← 这是 agent_go 缺少的
     ├─ 检测到步骤失败 → 分析失败原因
     ├─ 不是简单重试当前步骤
     └─ 重新生成剩余步骤的计划（考虑已完成步骤的结果）
  4. 整合阶段
     └─ 汇总所有步骤结果
```

**agent_go 的当前行为**：验证失败 → RepairAgent 注入失败上下文 → 修复同一步骤 → 重试（max 3 次）。这是 **Retry**，不是 **RePlan**。

**Plan-and-Execute 的 RePlan**：失败后不只是修复当前步骤——是**重新规划剩余步骤**。比如：Step 2 失败的原因是「Step 1 的实现方式导致 Step 2 无法按原计划执行」→ RePlan 不只是修 Step 1，而是重新规划 Step 2-5 的方案。

**对 agent_go 的启示**：当前的「修复重试」机制在简单错误上有效，但在架构级错误上无效——如果 Plan 本身的分解有问题（比如按技术层分解而非模块边界），RepairAgent 修不了。**需要在验证循环中增加「是否应该 RePlan 而非 Retry」的判断。**

### A.5 Harness Engineering：不是模型的问题，是环境的问题

一篇知乎文章（2026 年 7 月）提出了 Harness Engineering 的概念，核心论点与 agent_go 的设计原则高度共鸣：

> "AI 编程 Agent 当前最普遍的失效模式不是推理错误，而是环境问题——多会话之间状态不衔接，任务边界不清晰，完成标准没有外部锚点。同一个 Claude Code，在精心设计的环境里能连续完成数十步骤的复杂重构，在另一个项目里却在第三步就开始偏轨。**模型没变，变的是工作环境。**"

文章建议的关键实践：
1. **任务必须切到可独立验证的粒度**（不能「改整个模块」，要「改这个函数+验证这个测试」）
2. **每个任务开始前重置环境到已知状态**（不能依赖上一个任务的副作用）
3. **完成标准必须在任务外定义**（不能靠 Agent 自己判断「我做好了」）

**agent_go 在这三个实践上的状态**：实践 1 = Plan 的 steps（⚠️ 粒度不够细）；实践 2 = git worktree 隔离（✅ 已完美实现）；实践 3 = 验证命令（✅ shell exit code）+ semantic 评估（⚠️ 待增强）。

### A.6 中文社区的关键信号

几个值得注意的中文社区动态：

1. **SDD + 多 Agent 协同实践**（腾讯新闻，2026.1）：提出「AI 原生的规范驱动开发方法论」，核心是**三份规范固化开发意图**：业务规范（做什么）、技术规范（怎么做）、验证规范（怎么算做完）。打通「SDD → Agent Harness 工程 → 主流 AI IDE」全链路。

2. **Spec Coding：AI 开发新范式**（知乎，2025.10）：提出「Write Once, Run Everywhere」——写一遍 Spec，在多个模型/工具上达到相近效果。关键观察：**Spec 写得越详细，模型升级后的效果迁移越好。**

3. **AI 编程三剑客对比**（掘金，2026.2）：Spec-Kit vs OpenSpec vs Superpowers 的深度对比。结论：**OpenSpec 的目录结构设计最适合与现有代码仓库集成**；Spec-Kit 的 slash command 体系最易上手；Superpowers 的跨平台方法论最灵活。

### A.7 Claude Code 生态的演变信号

1. **Subagents 作为核心原语**：Claude Code 从 v2.1 开始将 Subagent 作为一等公民。Explore/Plan/General-purpose 三种内置类型 + 自定义 Subagent（Markdown 文件定义）。**Subagent = 用于执行单个任务的隔离 Claude 实例，有自己的上下文窗口、系统 prompt、工具列表、权限。** 这与 agent_go 的 Worker 在 worktree 中隔离执行是**同一个设计理念的不同实现**。

2. **Agent Teams（实验性）**：v2.1.32 引入。每个 teammate 是独立 Claude 实例，通过 `SendMessage` 通信。**Token 极其昂贵**（仅推荐 Opus 4.7+）。

3. **Ultracode / Dynamic Workflows**：Opus 4.8 引入。一个编排 session 启动数百个并行 subagent。**用于宽并行工作（重构跨文件、测试矩阵），不用于窄串行任务。**

**关键判断**：Claude Code 的 subagent 生态和 agent_go 的 Worker 隔离执行是**解决同一问题的两种路径**。Claude Code subagent 更轻量（一条命令启动）、更灵活（自定义 system prompt + tools），但**不提供 agent_go 的结构化 Plan → Decompose → Verify 流水线和 bench 评估能力**。两者的关系不是竞争，而是互补——agent_go 可以作为 Claude Code subagent 的上层编排器。

### A.8 第二轮搜索的关键结论

1. **SDD 已经是一致共识**：Spec-Kit / OpenSpec / BMAD 三足鼎立，核心理念完全一致——先写 Spec，再写代码。agent_go 的 Task Spec 设计在这个趋势中是正确方向。

2. **BMAD 的角色分工框架**是当前最完整的 AI 开发流程参考。agent_go 可以借鉴其「阶段间门禁」设计——Plan 确认不只是确认步骤，而是确认「架构影响」。

3. **Plan-and-Execute 的 RePlan 机制**是 agent_go 当前验证循环的关键缺失。当前的 RepairAgent 是 Retry，不是 RePlan。架构级错误需要 RePlan。

4. **Harness Engineering** 正在成为一个独立学科——关注 Agent 的工作环境而非模型能力。agent_go 的 git worktree 隔离是这个学科的最佳实践之一。

5. **Claude Code subagent 生态与 agent_go 互补**——subagent 是执行单元，agent_go 是编排层。agent_go 可以不做 subagent 的事（执行），继续做 subagent 不擅长的事（结构化 Plan → 验证 → bench 评估）。
