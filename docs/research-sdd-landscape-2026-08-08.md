# 调研：主流编程 Agent / 通用 Agent 对 SDD 的支持程度——对 agent_go 的启发

> **类型**：调研分析（research），非设计文档
> **日期**：2026-08-08
> **用途**：作为后续概念设计/SDD 增强的输入材料。记录业界 SDD（Spec-Driven Development，规约驱动开发）现状、与 agent_go 现有能力对比、差距与启发。
> **关联**：[design/agent-go-input-spec.md](design/agent-go-input-spec.md)（agent_go Task Spec）；[prd.md](prd.md) Spec Gate §；[research-goal-loop-mechanism-2026-08-08.md](research-goal-loop-mechanism-2026-08-08.md)（前序调研）
>
> **产品口径说明（2026-08-08 更新）**：本文是方向性研究，不改变当前产品主线。当前先完成 M0-M1 的指标冻结和交付闭环；Spec 合规审查、Spec 偏差记录和循环状态埋点进入 M2，活文档、互操作和自改进能力进入 M4 后置决策。

---

## 一、调研背景

### 1.1 问题

SDD（规约驱动开发）正在成为 2026 年 AI 编程的主流范式——*"specify → plan → implement → validate"* 取代 *"vibe coding"*（凭感觉写）。agent_go 已有相当扎实的 SDD 实现（Task Spec 7 节 + 三层 Spec Gate，S11 里程碑，2026-08-01 落地）。本次调研回答：

1. Claude Code、Codex 等主流**编程 Agent** 对 SDD 的原生支持到什么程度？
2. OpenClaw、Hermes 等**通用 Agent 框架**在 SDD / 规划 / 自改进上有什么独特机制？
3. agent_go 在这个版图里处于什么位置？差距和启发在哪？

### 1.2 核心结论（先导）

> **agent_go 的 SDD 实现已是业界最完整的之一——Task Spec + 三层 Spec Gate（L1 硬门禁 / L1.5 AST 冲突 / L2 软警告设计）在 Claude Code / Codex / Spec Kit 中都没有对标。** 但业界有两个 agent_go 尚未覆盖的方向值得关注：① **"spec 即活文档"**（spec 随实现演进、双向同步）；② **"对抗式 spec 验证"**（多 agent 互查 spec 合规性）。

---

## 二、agent_go 现状基线（对比锚点）

基于代码级审查（spec.py / api.py / cli.py / executor.py），agent_go 当前 SDD 能力：

| SDD 能力 | agent_go 现状 | 实现状态 |
|----------|-------------|---------|
| 结构化输入 spec | Task Spec Markdown，7 节（目标/动机/范围/约束/验收/参考/风险） | ✅ `--spec` |
| Spec 模板生成器 | `agent_go spec template`（预填 repo 目录） | ✅ |
| Spec 独立校验 | `agent_go spec validate` | ✅ |
| **硬门禁（L1，确定性）** | 4 检查：必填节/长度下限/路径有效性/命令白名单 | ✅ 阻断执行 |
| **AST 冲突检测（L1.5）** | 零 LLM 成本，符号级 + 文件级冲突检测 | ✅ 符号级冲突阻断 |
| LLM 软警告（L2） | 范围完整性/约束一致性/验收可自动化/历史风险 | ⚠️ 设计完成，未实现 |
| Spec → Plan 注入 | §3/§4/§5/§7 → system prompt 硬约束；§1→task；§2/§6→user | ✅（展平为文本，LLM 遵守非强制） |
| 验证循环 | verify→fix→verify，max_retries，全量失败反馈，下游阻断 | ✅ |
| /goal 注入 | `--goal`/`--goal-hook`，默认关 | ✅ |
| 交互式 Scoping 辅助 | `agent_go scope` | ❌ P1 未实现 |
| Spec 质量基准测试 | bench v2 "spec detail gradient"（L0-L3） | ⚠️ 计划未跑 |

**关键特征**：agent_go 把 spec 当**准入契约**——执行前硬门禁，这是业界少有的"spec 有牙齿"设计。

---

## 三、主流编程 Agent 的 SDD 支持

### 3.1 Claude Code — 四阶段工作流 + Plan Mode + subagent review

🔗 [AugmentCode — Claude Code SDD Capabilities](https://www.augmentcode.com/guides/claude-code-spec-driven-development) | [Addy Osmani — How to Write a Good Spec](https://addyosmani.com/blog/good-spec/)

**机制**：
- **CLAUDE.md** 作为持久化真相源，4 层作用域（User/Project/Local/Managed）。但关键是：*"CLAUDE.md content is delivered as a user message rather than a system prompt"*——模型**概率性遵守，非确定性**。
- **四阶段**：Explore（Plan Mode Shift+Tab，只读）→ Plan（生成 PLAN.md）→ Implement（写代码）→ Commit（subagent 审查 diff vs PLAN.md）。
- **验证**：subagent review loop——*"every requirement is implemented, the listed edge cases have tests, and nothing outside the task's scope has changed"*。要求"show evidence rather than asserting success"。
- **确定性执行靠 Hooks**：因为 CLAUDE.md 只是建议性的，*保证*合规需要把规则移入 hooks（确定性的脚本）。

**关键局限**：
- 上下文耗尽：复杂会话填满 200k 窗口后，*compaction 触发时指令被完全忽略且无警告*。
- 维护负担：CLAUDE.md 超 200 行合规率下降。
- spec 合规无原生 drift detection（漂移检测）——需人工或 subagent 比对。

**对 agent_go 的启发**：
- ✅ agent_go 的 system prompt 硬约束注入 + L1 硬门禁**比 Claude Code 更强**——Claude Code 的 CLAUDE.md 只是"建议性 user message"，agent_go 是"阻断性硬门禁"。
- ⚠️ Claude Code 的 **subagent review（对抗式审查）** 是 agent_go 缺失的——agent_go 的 review --deep 是单模型审查，Claude Code 用独立 subagent 对比 spec vs diff。这呼应了 agent_go 的 Verifier-critic 方向（见 goal/loop 调研启发）。
- ⚠️ Claude Code 的 **context compaction 无警告丢失指令** 问题，agent_go 的 AgentLoop 也有（消息窗口粗暴截断留首+末30条）——需 re-anchor 检查点。

### 3.2 OpenAI Codex — AGENTS.md + PLANS.md + spec 模式

🔗 [OpenAI Cookbook — Using PLANS.md for multi-hour problem solving](https://developers.openai.com/cookbook/articles/codex_exec_plans) | [agents.md 开放格式](https://agents.md/) | [Plan/Spec Mode 讨论](https://github.com/openai/codex/discussions/7355)

**机制**：
- **AGENTS.md**（开放格式，6 万+ 项目用）：根级项目上下文（角色、约定、约束、何时用 plan/spec 文档的指针）。本质是"给 agent 看的 README"。
- **PLANS.md**：多步任务的轻量计划文档。工作流：更新 AGENTS.md 描述何时用 PLANS.md → 添加 PLANS.md → Codex 执行时引用。
- **Plan/Spec 双模式**（社区约定）：prompt 含 "plan" 关键词 → 轻量计划模式；含 "spec" → 正式 spec 模式。
- **codex-spec 工具**（社区）：把意图转为可执行 spec + plan，引导一致实现。

**关键特征**：
- Codex 的 spec 是**社区实践 + 约定**，非产品内建功能。AGENTS.md/PLANS.md 是 Markdown 文件，Codex 原生不解析、不校验、不门禁——全靠 prompt 引导。
- 与 Claude Code 类似，spec 合规是"概率性"的。

**对 agent_go 的启发**：
- ✅ agent_go 的 `--spec` + 解析 + 校验**远超 Codex 的 AGENTS.md 约定**——Codex 是"放个 Markdown 文件希望 agent 读"，agent_go 是"结构化解析 + 硬门禁阻断"。
- ⚠️ Codex 的 **PLANS.md 作为活文档**（agent 执行时引用、更新进度）值得借鉴——agent_go 的 Plan 确认后是不可变的（见 goal/loop 调研的"Plan 脆性"缺口）。
- ⚠️ agents.md **开放格式生态**（6 万+ 项目）是 agent_go 可考虑互操作的方向——agent_go 的 Task Spec 能否兼容/导出 AGENTS.md 格式？

### 3.3 Spec Kit（GitHub 官方开源工具包）— /specify → /plan → /tasks

🔗 [GitHub Blog — Spec-Driven Development with AI: Get Started with a New Open-Source Toolkit](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)

**机制**（最接近 agent_go 的对标）：
- **四阶段 slash 命令**：`/specify`（定义 what/why，AI 生成 spec 文档）→ `/plan`（提供技术参数，AI 生成实现计划）→ `/tasks`（拆解为小而隔离的可审查任务）→ `/implement`（agent 执行任务）。
- **产物分离**：Spec（只管用户体验+业务逻辑，不碰技术栈）+ Plan（技术 how，含约束/安全/架构）+ Tasks（"exactly what to work on"的颗粒）。
- **约束前置（Constraint Baking）**：*"Rather than relying on post-generation security checks, compliance and design rules are embedded directly into the /plan phase"*——约束在计划阶段就烤进去，而非事后检查。
- **隔离可测**：*"Each task should be something you can implement and test in isolation"*——任务粒度的可测性是设计目标。
- **Agent 无关**：兼容 Copilot / Claude Code / Gemini CLI。

**关键特征**：
- Spec Kit 的 spec/plan/tasks **三层分离**比 agent_go 的"Task Spec 单文档 → Plan"更细——agent_go 把 what/why/how 混在一个 Task Spec 里，Spec Kit 拆成三个独立产物。

**对 agent_go 的启发**：
- ⚠️ Spec Kit 的 **spec/plan/tasks 三层分离**是一个值得评估的方向——agent_go 当前 Task Spec §1 目标 + §5 验收 = spec 层；§3 范围 + §4 约束 = plan 层的输入。是否值得显式拆分？
- ✅ agent_go 的 **L1 硬门禁** 比 Spec Kit 强——Spec Kit 的校验是"phase boundary reflect and refine"（人工反思），agent_go 是确定性阻断。
- ✅ Spec Kit 的 **"constraint baking"** 与 agent_go 的"§3/§4 注入 system prompt 硬约束"理念一致——agent_go 已实践。
- ⚠️ Spec Kit 的 **agent 无关**（slash 命令 + Markdown 产物）是生态优势——agent_go 的 Task Spec 是自有格式，互操作性弱。

---

## 四、通用 Agent 框架的 SDD / 规划 / 自改进

### 4.1 Hermes Agent（NousResearch）— 自改进 skill + 三层记忆

🔗 [dev.to — Hermes Agent: The Self-Improving Agent Framework](https://dev.to/truongpx396/hermes-agent-the-self-improving-agent-framework-and-how-it-compares-to-openclaw-goclaw-22mc) | [GitHub — NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)

**机制**：
- **AIAgent loop**：`prompt → think → tool → obs → memory write → continue`，cache 友好的 prompt 布局。
- **自改进 skill（核心差异）**：skill 是 Markdown + YAML frontmatter，agent **自主**创建/编辑/fork/退役 skill（`skill_manage` 工具）。*"periodically prompts itself to reflect on whether the current trajectory should be captured as a reusable skill"*——运行时定期反思"这次经验该不该存成 skill"。
- **三层记忆**：Persistent Memory（append-only，在 cache 边界内）+ SessionDB（FTS5 全文搜索历史会话，search+LLM 摘要）+ 可插拔用户建模（Honcho/mem0/supermemory）。
- **自进化护栏**：用 DSPy/GEPA 对 artifact 做基准优化；skill 失败会自动退役。
- **开放标准**：skill 通过 `agentskills.io` 跨框架可移植。

**对 agent_go 的启发**：
- ⚠️ Hermes 的 **自改进 skill**是 agent_go skills 系统的进化方向——agent_go 的 skill 是静态文档（人写、注入为 prompt），Hermes 是 agent 自主创建/改进/退役。这呼应 goal/loop 调研的"循环状态即学习数据源"启发——agent_go 的 verify_state.json 失败模式可驱动 skill 自动生成。
- ⚠️ Hermes 的 **SessionDB（FTS5 全文搜索 + LLM 摘要召回）** 比 agent_go 的 verify_state.json（仅 resume 用）成熟——这是 KnowledgeStore（PRD H2-1）的一个具体实现参考。
- ✅ agent_go 的 **worktree 隔离 + 验证循环** 在代码工程场景比 Hermes 的通用 skill 更扎实——Hermes 是通用 agent，无代码级验证循环。

### 4.2 OpenClaw — 个人助手定位 + 多通道消息

🔗 [MindStudio — What Is Hermes Agent? The OpenClaw Alternative](https://www.mindstudio.ai/blog/what-is-hermes-agent-openclaw-alternative)

**机制**：
- 定位为**个人助手**（reliable multi-channel messaging + device presence），非自主工作流执行器。
- skill 是**静态、用户编写**的（registry），与 Hermes 的自改进形成对比。
- 工作区文件：AGENTS.md / SOUL.md / TOOLS.md——基础上下文，无结构化 spec 或门禁。
- 安全层较成熟：AES-256-GCM、SSRF 检测（GoClaw 分支）。

**对 agent_go 的启发**：
- OpenClaw 在 SDD 上**无可借鉴**——它是消息/设备助手，无代码 spec 概念。
- 但 OpenClaw 的 **SOUL.md（agent 人格/行为定义）** 概念有趣——agent_go 的 agent 类型系统（developer/architect/reviewer）是类似思路的代码级实现。

---

## 五、横向对比：SDD 能力矩阵

| SDD 能力 | agent_go | Claude Code | Codex | Spec Kit | Hermes |
|----------|----------|-------------|-------|----------|--------|
| **结构化 spec 输入** | ✅ 7 节 Task Spec | ⚠️ CLAUDE.md（建议性） | ⚠️ AGENTS.md（约定） | ✅ /specify 产物 | ❌ 无 |
| **spec 硬门禁（阻断）** | ✅ L1 四检查 + L1.5 AST | ❌ 无（概率遵守） | ❌ 无 | ❌ 无（人工 reflect） | ❌ 无 |
| **spec → plan 注入** | ✅ system prompt 硬约束 | ⚠️ PLAN.md（建议） | ⚠️ PLANS.md（引用） | ✅ /plan（constraint baking） | ❌ 无 |
| **plan 可变性** | ❌ 确认后不可变 | ⚠️ 可重生 | ⚠️ 可更新 | ✅ 需求变→重生 plan/tasks | ✅ 动态 |
| **验证循环** | ✅ verify→fix→verify + 下游阻断 | ⚠️ subagent review | ⚠️ 人工/CI | ⚠️ isolation test | ⚠️ 沙箱+工具审批 |
| **spec 合规审查** | ⚠️ L1.5 冲突检测（结构级） | ✅ subagent vs PLAN.md | ❌ | ⚠️ phase boundary | ❌ |
| **spec 即活文档** | ❌ 静态输入 | ⚠️ CLAUDE.md 可演进出 | ⚠️ PLANS.md 可更新 | ✅ 三层产物可重生 | ✅ skill 自改进 |
| **跨工具互操作** | ❌ 自有格式 | ⚠️ Markdown | ✅ agents.md 开放格式 | ✅ agent 无关 | ✅ agentskills.io |
| **自改进/学习** | ❌（verify_state 仅 resume） | ⚠️ auto-memory | ❌ | ❌ | ✅ skill 自创建/退役 |
| **对抗式验证** | ⚠️ review --deep 单模型 | ✅ subagent 独立审查 | ❌ | ❌ | ⚠️ peer subagent critique |

**定位判断**：
- agent_go 在**"spec 有牙齿"（硬门禁）和"验证循环"**上业界领先。
- agent_go 在**"spec 即活文档""自改进""跨工具互操作"**上落后。

---

## 六、四个启发（供后续设计参考）

> 以下为定性分析，不含具体实现方案。实际设计由后续 SDD 承接。

### 启发 1：spec 合规的"对抗式审查"——补上 L1.5 之后的语义层

**观察**：Claude Code 用独立 subagent 对比 spec vs diff（*"every requirement is implemented, nothing outside scope changed"*）。agent_go 的 L1.5 是 AST 结构级冲突检测，但**无语义级 spec 合规审查**——不检查"实现是否真的满足 §1 目标 / §5 验收"。

**启发**：agent_go 已有 review --deep（M7 审查阶段，独立模型逐子任务分析），但它审查的是"代码质量"，不是"spec 合规性"。可探索在 review 阶段增加 **spec-compliance 审查维度**——独立模型对照 Task Spec §1/§3/§5 检查实现是否偏离。这与 goal/loop 调研的 Verifier-critic 模式、L2 软警告（未实现）天然衔接。

### 启发 2：spec 即活文档——从"一次性输入"到"双向同步"

**观察**：Spec Kit 的三层产物（spec/plan/tasks）可随需求变化重生；Claude Code 的 CLAUDE.md 可被 agent 自主编辑演进出；Codex 的 PLANS.md 在执行中被引用更新。agent_go 的 Task Spec 是**一次性静态输入**——执行中不更新，实现偏差不会回流到 spec。

**启发**：agent_go 可探索"spec 双向同步"——执行中发现 spec 的 §3 范围遗漏或 §5 验收不可行时，不是静默绕过，而是**记录偏差并提示人工修订 spec**。这与 goal/loop 调研的"局部重规划"互补——重规划改 Plan，spec 偏差记录改 Spec。verify_state.json 可增加 `spec_deviation` 字段。

### 启发 3：自改进 skill——从静态文档到运行时进化

**观察**：Hermes 的核心差异是 agent 自主创建/编辑/退役 skill。agent_go 的 skill 是静态人写文档（注入为 prompt）。Hermes 的 *"reflect on whether the current trajectory should be captured as a reusable skill"* 是运行时学习。

**启发**：agent_go 的 verify_state.json（失败模式 + 有效策略）+ Hermes 思路 = **失败驱动的 skill 自动生成**。当某类失败模式反复出现（如"总是忘记处理 None 返回值"），系统可提示"是否生成一个 skill 提醒后续任务"。这呼应 goal/loop 调研的"循环状态即学习数据源"，且与 PRD H2-1 KnowledgeStore 同向。

### 启发 4：跨工具互操作——兼容 agents.md 开放生态

**观察**：agents.md 已是 6 万+ 项目用的开放格式；Spec Kit agent 无关；Hermes skill 走 agentskills.io。agent_go 的 Task Spec 是自有格式，与这个生态不互通。

**启发**：agent_go 可探索 **Task Spec ↔ AGENTS.md 双向兼容**——`agent_go spec export --format agents-md` 导出为 AGENTS.md 兼容格式，或 `--spec` 直接读 AGENTS.md 的 spec 节。这降低采用门槛（已有 AGENTS.md 的项目可零成本接入），也让 agent_go 融入开放生态而非自成孤岛。

---

## 七、与 agent_go 现有路线的关系

| 启发 | 对应 PRD/路线 | 关系 |
|------|-------------|------|
| 对抗式 spec 合规审查（启发 1） | review --deep + L2 软警告 | 给 review 增加 spec 合规维度；L2 的具体化 |
| spec 即活文档（启发 2） | 局部重规划（goal/loop 调研） | 互补——重规划改 Plan，偏差记录改 Spec |
| 自改进 skill（启发 3） | KnowledgeStore H2-1 + 自适应 H3 | 失败驱动 skill 生成，verify_state 作数据源 |
| 跨工具互操作（启发 4） | 无直接对应（新方向） | 降低采用门槛，融入 agents.md 生态 |

**核心判断**：agent_go 的 SDD 基础（Task Spec + 硬门禁）是业界最完整的之一，**不需要补基建，而是深化**——把"spec 有牙齿"从准入阶段延伸到执行中（活文档）和执行后（合规审查）。启发 1 和 2 是这条深化线的具体落点；启发 3 和 4 是长期能力（自改进、互操作），与 H2/H3 路线同向。

---

## 八、信息来源详述

### 来源 1：AugmentCode — Claude Code SDD Capabilities

🔗 [AugmentCode — Claude Code for Spec-Driven Development](https://www.augmentcode.com/guides/claude-code-spec-driven-development)

**核心论点**：Claude Code 的 SDD 是 *"specify requirements → generate a plan → implement against the plan → validate against the specification"* 四阶段。

**关键机制**：
- **CLAUDE.md 四层作用域**（User/Project/Local/Managed），但 *"delivered as a user message rather than a system prompt"*——概率遵守，非确定。
- **Plan Mode**（Shift+Tab）：只读探索，生成 PLAN.md。*"most useful specs are self-contained: they name the files and interfaces involved"*。
- **subagent review loop**：独立 subagent 审查 diff vs PLAN.md——*"every requirement is implemented, edge cases have tests, nothing outside scope changed"*。要求 *"show evidence rather than asserting success"*。
- **确定性靠 Hooks**：*保证* 合规需把规则移入 hooks（确定性脚本），因为 CLAUDE.md 只是建议。

**关键局限**：
- 上下文耗尽：compaction 触发时 *"instructions completely ignored without warning"*。
- CLAUDE.md >200 行合规率下降。

**对 agent_go 的启发**：agent_go 的硬约束 system prompt 注入 + L1 硬门禁 > Claude Code 的建议性 CLAUDE.md。但 Claude Code 的 subagent 独立审查（对抗式）是 agent_go 缺失的语义级 spec 合规检查。

---

### 来源 2：OpenAI Cookbook — Using PLANS.md for multi-hour problem solving

🔗 [OpenAI Cookbook — Codex Exec Plans](https://developers.openai.com/cookbook/articles/codex_exec_plans) | [agents.md](https://agents.md/) | [Plan/Spec Mode 讨论](https://github.com/openai/codex/discussions/7355)

**核心论点**：Codex 的 SDD 靠 **AGENTS.md + PLANS.md** 两份 Markdown 文档 + prompt 约定，非产品内建功能。

**关键机制**：
- **AGENTS.md**（6 万+ 项目用）：根级项目上下文，"给 agent 看的 README"。
- **PLANS.md**：多步任务计划文档。工作流：更新 AGENTS.md 描述何时用 PLANS.md → 添加 PLANS.md → 执行时引用。
- **Plan/Spec 双模式**：prompt 含 "plan" → 轻量计划；含 "spec" → 正式 spec 模式。
- **codex-spec**（社区工具）：意图 → 可执行 spec + plan。

**关键特征**：spec 合规全靠 prompt 引导，**原生不解析、不校验、不门禁**。

**对 agent_go 的启发**：agent_go 的 `--spec` 解析+校验+门禁远超 Codex 的约定。但 Codex 的 PLANS.md 作为**执行中可引用更新的活文档**值得借鉴（agent_go Plan 确认后不可变）。agents.md 的**开放格式生态**（6 万+项目）是互操作机会。

---

### 来源 3：GitHub Blog — Spec Kit 开源工具包

🔗 [GitHub Blog — Spec-Driven Development with AI: Spec Kit](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)

**核心论点**：Spec Kit 用 slash 命令（/specify → /plan → /tasks → /implement）强制四阶段 SDD，产物分离。

**关键机制**：
- **四阶段 slash 命令**：/specify（what/why）→ /plan（技术 how，constraint baking）→ /tasks（小而隔离的可审查任务）→ /implement。
- **三层产物分离**：Spec（只管 UX+业务）+ Plan（技术 how）+ Tasks（颗粒）。*"Each task should be something you can implement and test in isolation"*。
- **constraint baking**：*"rules are embedded directly into the /plan phase"*——约束前置而非事后检查。
- **agent 无关**：兼容 Copilot / Claude Code / Gemini CLI。

**对 agent_go 的启发**：Spec Kit 的 spec/plan/tasks 三层分离比 agent_go 的单 Task Spec 更细——值得评估是否显式拆分。Spec Kit 的 constraint baking 与 agent_go 的 §3/§4 注入理念一致（已实践）。Spec Kit 的 agent 无关 + slash 命令是生态优势。

---

### 来源 4：dev.to — Hermes Agent 自改进架构

🔗 [dev.to — Hermes Agent: The Self-Improving Agent Framework](https://dev.to/truongpx396/hermes-agent-the-self-improving-agent-framework-and-how-it-compares-to-openclaw-goclaw-22mc) | [GitHub — NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent)

**核心论点**：Hermes 是唯一主流的**自改进** agent 框架——运行时自主创建/编辑/退役 skill。

**关键机制**：
- **AIAgent loop**：`prompt → think → tool → obs → memory write → continue`，cache 友好。
- **自改进 skill**：skill = Markdown + YAML frontmatter，agent 用 `skill_manage` 工具自主创建/fork/退役。*"periodically prompts itself to reflect on whether the current trajectory should be captured as a reusable skill"*。
- **三层记忆**：Persistent Memory（append-only，cache 边界内）+ SessionDB（FTS5 全文搜索 + LLM 摘要召回）+ 可插拔用户建模（Honcho/mem0/supermemory）。
- **自进化护栏**：DSPy/GEPA 基准优化；skill 失败自动退役。开放标准 agentskills.io 跨框架可移植。

**对 agent_go 的启发**：Hermes 的自改进 skill 是 agent_go skills 系统的进化方向（静态人写 → 运行时自改进）。SessionDB（FTS5+摘要）是 KnowledgeStore 的实现参考。但 agent_go 在代码验证循环上远强于 Hermes（通用 agent 无代码级验证）。

---

### 来源 5：Addy Osmani — How to Write a Good Spec for AI Agents

🔗 [Addy Osmani — How to Write a Good Spec](https://addyosmani.com/blog/good-spec/)

**核心论点**：好 spec 的六要素 + 三层边界 + 模块化原则。*"Minimal does not necessarily mean short"*——该详细处详细，但聚焦窄。

**六要素**：Commands（完整可执行命令）/ Testing（框架+位置+覆盖率）/ Project Structure（显式目录）/ Code Style（具体示例非长文）/ Git Workflow（分支+commit+PR）/ Boundaries（三层约束）。

**三层边界**：✅ Always do（免审批）/ ⚠️ Ask first（高影响需审批，如改 DB schema）/ 🚫 Never do（硬禁止，如提交密钥）。

**原则**：模块化（divide and conquer，防"指令诅咒"——指令越多遵守率越降）/ 完整性（PRD 思维 + SRS 思维合一）/ 可测性（含自检+预期输入输出）/ 活文档（持续更新，版本控制锚定）。

**对 agent_go 的启发**：agent_go 的 Task Spec 7 节已覆盖多数要素。Addy 的**三层边界**（always/ask/never）比 agent_go 的 §3 范围（改/不改二分）更细——可考虑把 §3/§4 细化为三层。**模块化原则**印证 agent_go 的"子任务隔离"设计正确。

---

### 来源 6：BCMS / arXiv — SDD 理论定位

🔗 [BCMS — Spec-Driven Development: The Definitive 2026 Guide](https://www.thebcms.com/blog/spec-driven-development/) | [arXiv — From Code to Contract in the Age of AI](https://arxiv.org/html/2602.00180v1)

**核心论点**：SDD 在 2026 成为主流，是 TDD（测试驱动）和 vibe coding（凭感觉）之外的第三条路。学术视角：*"spec becomes the source of truth rather than the code"*——spec 取代代码成为真相源，invert（反转）了传统 spec-code 关系。

**对 agent_go 的启发**：印证 agent_go 的"spec 作为准入契约"方向正确。arXiv 的"spec as source of truth"支持启发 2（spec 即活文档）——如果 spec 是真相源，实现偏差应回流修订 spec，而非静默绕过。

---

## 附：agent_go 现状审查依据

| 文件 | 审查内容 |
|------|---------|
| `agent_go/spec.py` | Task Spec 解析 + L1 硬门禁 + L1.5 AST 冲突 + 模板生成 |
| `agent_go/api.py:172-354` | generate_plan，spec_context 注入 system prompt |
| `agent_go/cli.py:50-60,325-644,1828-1872` | --spec/--force 标志、_build_spec_context、cmd_spec |
| `agent_go/executor.py` | 验证循环（verify→fix→verify） |
| `docs/design/agent-go-input-spec.md` | Task Spec 7 节规范 |
| `docs/design/sdd-references-and-frameworks.md` | SDD 学术基础（arXiv:2603.24284 等） |
| `docs/prd.md` §4.1/§4.3 | Spec 输入、验证和合规审查产品规范 |
